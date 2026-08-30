# pyright: reportPrivateUsage=false
"""集成测试:成品守卫(_guard_rendered_export)接入 _translate_images 的端到端行为。

fake 的 export_project 按预设序列返回导出字节,验证:
- 可疑渲染(透明底+文字)触发告警并重试导出一次,重试恢复时发送重试结果;
- 两次都可疑时发送评分较优的一次且不中断翻译;
- 正常成品不触发守卫(只导出一次);
- guard_blank_retry=false 完全跳过守卫;
- 源图基准缺失时 guard_max_white_absolute=0.0 可强制触发(调试路径)。
"""

from __future__ import annotations

import io
import os
from collections.abc import Sequence
from pathlib import Path

import pytest
from PIL import Image

import main as main_module
from conftest import MakePlugin
from koharu_client import (
    AppConfig,
    KoharuApiError,
    MetaInfo,
    OperationInfo,
    ProjectInfo,
)


def _png_bytes(png: "Image.Image") -> bytes:
    buffer = io.BytesIO()
    png.save(buffer, format="PNG")
    return buffer.getvalue()


def _transparent_text_image() -> Image.Image:
    """透明底 + 黑色文字笔画:缺背景层渲染的典型形态(透明占比 ≈ 0.99)。"""
    image = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    for x in range(5, 55, 2):
        image.putpixel((x, 30), (0, 0, 0, 255))
        image.putpixel((x, 31), (0, 0, 0, 255))
    return image


def _half_transparent_image() -> Image.Image:
    """半透明页(透明占比 0.5,仍超 5% 硬信号阈值,但比全透明页"更好")。"""
    image = Image.new("RGBA", (60, 60), (255, 255, 255, 0))
    for y in range(30, 60):
        for x in range(60):
            image.putpixel((x, y), (255, 255, 255, 255))
    return image


def _opaque_page_image() -> Image.Image:
    """不透明漫画页:白底 80% + 深色内容 20%(白占比 0.8,与测试源图一致)。"""
    image = Image.new("RGB", (60, 60), (255, 255, 255))
    for index in range(720):  # 60*60*0.2
        image.putpixel((index % 60, index // 60), (30, 30, 30))
    return image


def _write_source(path: Path) -> None:
    """写入源图文件(与 _opaque_page_image 内容一致,基准白占比 0.8)。"""
    _opaque_page_image().save(path, format="PNG")


def _write_broken_source(path: Path) -> None:
    """写入 PIL 无法解析的"图片"文件(触发源图基准缺失的兜底路径)。"""
    path.write_bytes(b"not a real image")


class FakeKoharuClient:
    """KoharuClient 最小替身:export_project 按预设序列返回,并记录调用次数。"""

    exports: list[tuple[bytes, str]] = []
    export_calls: int = 0

    def __init__(self, base_url: str, *, timeout: float, connect_timeout: float) -> None:
        self.base_url = base_url

    async def __aenter__(self) -> "FakeKoharuClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        pass

    async def wait_until_ready(
        self, *, timeout_seconds: float = 60.0, interval_seconds: float = 1.0
    ) -> MetaInfo:
        return {"name": "koharu-fake", "version": "0"}

    async def close_project_if_any(self) -> bool:
        return True

    async def create_project(self, name: str) -> ProjectInfo:
        return {"name": "proj-1"}

    async def create_pages(
        self, image_paths: Sequence[str | os.PathLike[str]]
    ) -> ProjectInfo:
        return {"name": "proj-1", "pages": [{"id": "page-1"}]}

    async def get_config(self) -> AppConfig:
        return {
            "pipeline": {
                "detection": {"model": "koharu-layout-rfdetr-seg-2xl"},
                "ocr": {"model": "baberu-ocr"},
                "translation": {
                    "model": {"provider": "deepseek", "vision": False},
                    "target_language": "zh-CN",
                },
                "inpainting": {"model": "lama"},
            },
            "providers": {},
            "typesetting": {"font_families": ["CCWildWords", "Adobe 黑体 Std"]},
        }

    async def patch_config(self, patch: dict[str, object]) -> dict[str, object]:
        return {}

    async def set_provider_secret(self, provider_id: str, secret: str) -> None:
        return None

    async def get_pipeline_steps_from_config(self) -> list[str]:
        return ["full"]

    async def start_pipeline(self, steps: list[str]) -> str:
        return "op-1"

    async def wait_operation(
        self,
        operation_id: str,
        *,
        timeout_seconds: float = 900.0,
        interval_seconds: float = 2.0,
    ) -> OperationInfo:
        return {"id": operation_id, "status": "finished"}

    async def export_project(
        self, export_format: str = "rendered", *, pages: list[str] | None = None
    ) -> tuple[bytes, str]:
        FakeKoharuClient.export_calls += 1
        if not FakeKoharuClient.exports:
            raise AssertionError("export sequence exhausted")
        return FakeKoharuClient.exports.pop(0)

    async def close_project(self) -> None:
        pass

    async def delete_project(self, project_id: str) -> None:
        pass


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    exports: list[tuple[bytes, str]],
) -> None:
    FakeKoharuClient.exports = list(exports)
    FakeKoharuClient.export_calls = 0
    monkeypatch.setattr(main_module, "KoharuClient", FakeKoharuClient)


def _saved_bytes(output_paths: list[str]) -> bytes:
    assert len(output_paths) == 1
    return Path(output_paths[0]).read_bytes()


async def test_guard_retries_once_and_recovers(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """首次导出可疑 → 告警 + 重试一次 → 重试恢复时发送重试结果。"""
    bad = _png_bytes(_transparent_text_image())
    good = _png_bytes(_opaque_page_image())
    _install_fake_client(monkeypatch, [(bad, "image/png"), (good, "image/png")])
    plugin = make_plugin()

    source = tmp_path / "page-1.png"
    _write_source(source)

    with caplog.at_level("INFO", logger="astrbot"):
        outputs = await plugin._translate_images([str(source)], "zh-CN")

    assert FakeKoharuClient.export_calls == 2
    assert _saved_bytes(outputs) == good  # 发送的是重试(正常)导出
    assert "export guard: suspicious render" in caplog.text
    assert "export guard: retry recovered" in caplog.text


async def test_guard_no_false_positive_single_export(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """正常成品:只导出一次,不触发守卫。"""
    good = _png_bytes(_opaque_page_image())
    _install_fake_client(monkeypatch, [(good, "image/png")])
    plugin = make_plugin()

    source = tmp_path / "page-1.png"
    _write_source(source)

    outputs = await plugin._translate_images([str(source)], "zh-CN")

    assert FakeKoharuClient.export_calls == 1
    assert _saved_bytes(outputs) == good


async def test_guard_both_suspicious_sends_better_without_interruption(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """两次导出都可疑:不中断翻译,发送评分较优(透明占比更低)的一次并记录 error。"""
    worse = _png_bytes(_transparent_text_image())  # 透明 ≈ 0.99
    better = _png_bytes(_half_transparent_image())  # 透明 = 0.5
    _install_fake_client(monkeypatch, [(worse, "image/png"), (better, "image/png")])
    plugin = make_plugin()

    source = tmp_path / "page-1.png"
    _write_source(source)

    with caplog.at_level("ERROR", logger="astrbot"):
        outputs = await plugin._translate_images([str(source)], "zh-CN")

    assert FakeKoharuClient.export_calls == 2
    assert _saved_bytes(outputs) == better  # 较优的一次
    assert "export guard: retry still suspicious" in caplog.text


async def test_guard_disabled_skips_check(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """guard_blank_retry=false:完全跳过守卫,可疑导出也只调用一次、原样发送。"""
    bad = _png_bytes(_transparent_text_image())
    _install_fake_client(monkeypatch, [(bad, "image/png")])
    plugin = make_plugin(overrides={"guard_blank_retry": False})

    source = tmp_path / "page-1.png"
    _write_source(source)

    outputs = await plugin._translate_images([str(source)], "zh-CN")

    assert FakeKoharuClient.export_calls == 1
    assert _saved_bytes(outputs) == bad


async def test_guard_absolute_threshold_zero_force_triggers(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """源图基准缺失(无法解析) + guard_max_white_absolute=0:强制触发守卫路径。

    两次导出白占比都 > 0 → 都判可疑 → 重试一次后发送较优者,翻译不中断。
    """
    page = _png_bytes(_opaque_page_image())
    _install_fake_client(monkeypatch, [(page, "image/png"), (page, "image/png")])
    plugin = make_plugin(overrides={"guard_max_white_absolute": 0.0})

    source = tmp_path / "page-1.png"
    _write_broken_source(source)

    outputs = await plugin._translate_images([str(source)], "zh-CN")

    assert FakeKoharuClient.export_calls == 2
    assert _saved_bytes(outputs) == page

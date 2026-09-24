# pyright: reportPrivateUsage=false
"""集成测试:_translate_images 的翻译并发锁(translation concurrency is fixed at 1)。

产品代码声称翻译并发固定为 1:两个并发的 _translate_images 调用必须被
插件实例上的 asyncio.Lock 串行化,且首个调用失败后锁必须被释放。本文件
把 main.KoharuClient 替换为带门闩的本地 fake,用 asyncio.Event(而非
sleep 计时)精确同步并发时序,并用 fake 的类级计数器证明同一时刻最多
只有一个翻译处于 KoharuClient 上下文内。
"""

from __future__ import annotations

import asyncio
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


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    """用 PIL 生成真实 PNG 字节(export_project 的假响应载荷)。"""
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    """向 path 写入真实 PNG 文件(作为翻译输入图片,会被插件真实复制)。"""
    Image.new("RGB", (8, 8), color).save(path, format="PNG")


class FakeKoharuClient:
    """KoharuClient 最小替身:async 上下文管理器 + _translate_images 所需方法。

    类级 active/max_active 统计同一时刻处于客户端上下文内的翻译数;
    gate 挡住首个进入的调用并在等待前置位 entered,供测试精确同步;
    fail_start_pipeline 为 True 时 start_pipeline 抛 KoharuApiError。
    """

    active: int = 0
    max_active: int = 0
    gate: asyncio.Event | None = None
    entered: asyncio.Event | None = None
    fail_start_pipeline: bool = False
    page_batches: list[list[tuple[str, bool]]] = []

    def __init__(self, base_url: str, *, timeout: float, connect_timeout: float) -> None:
        self.base_url = base_url

    async def __aenter__(self) -> "FakeKoharuClient":
        FakeKoharuClient.active += 1
        FakeKoharuClient.max_active = max(
            FakeKoharuClient.max_active, FakeKoharuClient.active
        )
        gate = FakeKoharuClient.gate
        if gate is not None and not gate.is_set() and FakeKoharuClient.active == 1:
            assert FakeKoharuClient.entered is not None
            FakeKoharuClient.entered.set()
            await gate.wait()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        FakeKoharuClient.active -= 1

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
        FakeKoharuClient.page_batches.append(
            [(str(Path(path)), Path(path).exists()) for path in image_paths]
        )
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
        # 镜像真实 KoharuClient 的实现:0.66 配置存在即返回 ["full"]。
        pipeline = (await self.get_config()).get("pipeline")
        if pipeline is None:
            return []
        return ["full"]

    async def start_pipeline(self, steps: list[str]) -> str:
        if FakeKoharuClient.fail_start_pipeline:
            raise KoharuApiError("fake pipeline failure")
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
        return _png_bytes((0, 255, 0)), "image/png"

    async def close_project(self) -> None:
        pass

    async def delete_project(self, project_id: str) -> None:
        pass


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate: asyncio.Event,
    entered: asyncio.Event,
    fail_start_pipeline: bool,
) -> None:
    """重置 fake 类级状态并替换 main.KoharuClient(每个测试独立配置)。"""
    FakeKoharuClient.active = 0
    FakeKoharuClient.max_active = 0
    FakeKoharuClient.gate = gate
    FakeKoharuClient.entered = entered
    FakeKoharuClient.fail_start_pipeline = fail_start_pipeline
    FakeKoharuClient.page_batches = []
    monkeypatch.setattr(main_module, "KoharuClient", FakeKoharuClient)


async def test_concurrent_translate_calls_are_serialized(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """两个并发 _translate_images 被串行化:第二个等第一个完全结束后才进入客户端。"""
    gate = asyncio.Event()
    entered = asyncio.Event()
    _install_fake_client(
        monkeypatch, gate=gate, entered=entered, fail_start_pipeline=False
    )
    plugin = make_plugin()

    first_page = tmp_path / "page-1.png"
    second_page = tmp_path / "page-2.png"
    _write_png(first_page, (255, 0, 0))
    _write_png(second_page, (0, 0, 255))

    task_a = asyncio.create_task(
        plugin._translate_images([str(first_page)], "Simplified Chinese")
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    task_b = asyncio.create_task(
        plugin._translate_images([str(second_page)], "Simplified Chinese")
    )
    await asyncio.sleep(0.05)  # 让 task B 运行到等待锁的挂起点

    # 首个调用被门闩挡住并持锁,第二个调用必须等待,不能进入客户端。
    assert not task_b.done()
    assert FakeKoharuClient.max_active == 1

    gate.set()
    outputs_a = await task_a
    outputs_b = await task_b

    assert FakeKoharuClient.max_active == 1
    assert FakeKoharuClient.active == 0
    assert len(outputs_a) == 1 and Path(outputs_a[0]).is_file()
    assert len(outputs_b) == 1 and Path(outputs_b[0]).is_file()
    assert outputs_a != outputs_b
    # 两次请求都真实上传了缓存副本(真实 PNG 文件,名称零填充为 001.png)。
    assert len(FakeKoharuClient.page_batches) == 2
    for batch in FakeKoharuClient.page_batches:
        assert len(batch) == 1
        assert batch[0][1] is True
        assert Path(batch[0][0]).name == "001.png"


async def test_lock_released_after_failure(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """首个调用失败(KoharuApiError)后锁被释放,后续调用可正常执行。"""
    gate = asyncio.Event()
    entered = asyncio.Event()
    _install_fake_client(
        monkeypatch, gate=gate, entered=entered, fail_start_pipeline=True
    )
    plugin = make_plugin()

    page = tmp_path / "page.png"
    _write_png(page, (255, 0, 0))

    task_a = asyncio.create_task(
        plugin._translate_images([str(page)], "Simplified Chinese")
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    gate.set()

    with pytest.raises(KoharuApiError):
        await task_a
    assert FakeKoharuClient.max_active == 1
    assert FakeKoharuClient.active == 0

    # 失败后锁已释放:同一插件实例上的下一次翻译成功完成。
    FakeKoharuClient.fail_start_pipeline = False
    outputs = await plugin._translate_images([str(page)], "Simplified Chinese")

    assert FakeKoharuClient.max_active == 1
    assert len(outputs) == 1 and Path(outputs[0]).is_file()
    assert len(FakeKoharuClient.page_batches) == 2

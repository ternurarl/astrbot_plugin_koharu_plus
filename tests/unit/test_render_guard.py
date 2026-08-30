# pyright: reportPrivateUsage=false
"""成品守卫(export guard)模块级函数单元测试。

覆盖:_render_quality_stats / _source_white_ratio / _export_images_from_content
/ _render_guard_issues / _render_guard_score。判定语义:透明占比 > 5% 为硬信号,
白占比超过源图基准 +0.30(基准缺失时超过绝对阈值)为软信号。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image as PILImage

import main


def _transparent_text_png(width: int = 100, height: int = 100) -> bytes:
    """透明底 + 少量黑色文字笔画(模拟渲染缺背景层的导出,透明占比 ≈ 0.99)。"""
    buffer = io.BytesIO()
    image = PILImage.new("RGBA", (width, height), (0, 0, 0, 0))
    for x in range(10, 90, 2):
        image.putpixel((x, height // 2), (0, 0, 0, 255))
        image.putpixel((x, height // 2 + 1), (0, 0, 0, 255))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _opaque_page_png(dark_count: int = 2000, size: int = 100) -> bytes:
    """不透明漫画页:白底 + dark_count 个深色像素,白占比 = 1 - dark_count / size²。"""
    buffer = io.BytesIO()
    image = PILImage.new("RGB", (size, size), (255, 255, 255))
    for index in range(min(dark_count, size * size)):
        image.putpixel((index % size, index // size), (30, 30, 30))
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _load_png(data: bytes) -> PILImage.Image:
    return PILImage.open(io.BytesIO(data))


# --- _render_quality_stats --------------------------------------------------------


def test_render_quality_stats_transparent_text_is_mostly_transparent() -> None:
    transparent, white = main._render_quality_stats(_load_png(_transparent_text_png()))
    assert transparent > 0.05  # 硬信号:缺背景层呈大面积透明
    assert white > 0.9  # 铺白后只剩文字笔画,几乎全白


def test_render_quality_stats_normal_page_no_transparency() -> None:
    transparent, white = main._render_quality_stats(_load_png(_opaque_page_png()))
    assert transparent == 0.0
    assert 0.5 < white < 0.9


def test_render_quality_stats_pure_white_page() -> None:
    data = _opaque_page_png(dark_count=0)
    transparent, white = main._render_quality_stats(_load_png(data))
    assert transparent == 0.0
    assert white == 1.0


def test_render_quality_stats_rgb_image_never_transparent() -> None:
    # JPEG 类无 alpha 输入:透明占比恒为 0,只能靠白占比信号判定。
    buffer = io.BytesIO()
    PILImage.new("RGB", (8, 8), (200, 200, 200)).save(buffer, format="JPEG")
    transparent, white = main._render_quality_stats(_load_png(buffer.getvalue()))
    assert transparent == 0.0
    assert white == 0.0  # L=200 不在 241..255 区间,不算白像素


# --- _source_white_ratio ----------------------------------------------------------


def test_source_white_ratio_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "source.png"
    path.write_bytes(_opaque_page_png(dark_count=2500))
    ratio = main._source_white_ratio(path)
    assert ratio is not None
    assert 0.7 < ratio < 0.8


def test_source_white_ratio_unreadable_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    assert main._source_white_ratio(path) is None


# --- _export_images_from_content --------------------------------------------------


def test_export_images_from_content_single_image() -> None:
    images = main._export_images_from_content(_opaque_page_png(), "image/png")
    assert len(images) == 1
    assert images[0].size == (100, 100)


def test_export_images_from_content_zip_keeps_page_order() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("01-page-a.png", _opaque_page_png(dark_count=1000))
        archive.writestr("02-page-b.png", _opaque_page_png(dark_count=5000))
        archive.writestr("notes.txt", "ignored")
    content = buffer.getvalue()
    images = main._export_images_from_content(content, "application/zip")
    assert len(images) == 2


# --- _render_guard_issues ---------------------------------------------------------


def test_guard_issues_transparent_export_is_suspicious() -> None:
    suspicious, stats = main._render_guard_issues(
        _transparent_text_png(), "image/png", [1.0], 0.9
    )
    assert suspicious is True
    assert len(stats) == 1
    assert stats[0][0] > 0.05


def test_guard_issues_normal_export_not_suspicious() -> None:
    # 源图基准可用:白占比贴近基准,不触发软信号,也不误报。
    suspicious, stats = main._render_guard_issues(
        _opaque_page_png(dark_count=2000), "image/png", [0.8], 0.9
    )
    assert suspicious is False
    assert stats[0][1] == 0.8


def test_guard_issues_white_over_baseline_is_suspicious() -> None:
    # 源图基准 0.5,渲染全白 1.0:超过 +0.30 余量,无透明也判异常。
    suspicious, _ = main._render_guard_issues(
        _opaque_page_png(dark_count=0), "image/png", [0.5], 0.9
    )
    assert suspicious is True


def test_guard_issues_white_page_with_white_source_not_suspicious() -> None:
    # 纯白页面 + 纯白源图:基准免疫,不误报(绝对阈值兜底仅基准缺失时生效)。
    suspicious, _ = main._render_guard_issues(
        _opaque_page_png(dark_count=0), "image/png", [1.0], 0.9
    )
    assert suspicious is False


def test_guard_issues_absolute_threshold_fallback_when_baseline_missing() -> None:
    # 基准缺失(源图无法解析):走绝对阈值兜底。
    suspicious, _ = main._render_guard_issues(
        _opaque_page_png(dark_count=0), "image/png", [], 0.9
    )
    assert suspicious is True
    # 白占比未超绝对阈值的正常页不触发。
    suspicious, _ = main._render_guard_issues(
        _opaque_page_png(dark_count=2000), "image/png", [], 0.9
    )
    assert suspicious is False


def test_guard_issues_absolute_threshold_zero_force_triggers() -> None:
    # guard_max_white_absolute=0.0:任何白占比 > 0 的页都触发(调试强制路径)。
    suspicious, _ = main._render_guard_issues(
        _opaque_page_png(dark_count=2000), "image/png", [], 0.0
    )
    assert suspicious is True


def test_guard_issues_unreadable_export_not_suspicious() -> None:
    suspicious, stats = main._render_guard_issues(
        b"not an image at all", "image/png", [1.0], 0.9
    )
    assert suspicious is False
    assert stats == []


# --- _render_guard_score ----------------------------------------------------------


def test_guard_score_prefers_export_closer_to_baseline() -> None:
    bad_stats = [(0.99, 0.99)]  # 缺背景:透明 + 铺白后几乎全白
    good_stats = [(0.0, 0.8)]  # 正常页
    assert main._render_guard_score(good_stats, [0.8], 0.9) < main._render_guard_score(
        bad_stats, [0.8], 0.9
    )


def test_guard_score_zero_for_clean_export() -> None:
    assert main._render_guard_score([(0.0, 0.8)], [0.8], 0.9) == 0.0

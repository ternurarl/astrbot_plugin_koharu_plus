# pyright: reportPrivateUsage=false
"""集成测试:上传缓存命名(_cache_ordered_upload_images)的顺序语义。

koharu 服务端导入时按上传文件名的字典序排序,因此缓存文件名必须零填充,
使字典序等于任务内原始顺序。覆盖:
- 18 张图(≥10 必现乱序的典型场景)字典序排序后仍保持任务顺序;
- 批次数超过 999 时命名宽度动态加宽,排序仍保持任务顺序;
- 白占比基准与缓存路径按下标对齐。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from conftest import MakePlugin


def _write_png(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (2, 2), color).save(path, format="PNG")


def _make_sources(tmp_path: Path, count: int) -> list[str]:
    sources: list[str] = []
    for index in range(1, count + 1):
        source = tmp_path / f"src-{index:05d}.png"
        _write_png(source, (index % 256, 0, 0))
        sources.append(str(source))
    return sources


def test_upload_cache_names_keep_task_order_under_lexicographic_sort(
    make_plugin: MakePlugin, tmp_path: Path
) -> None:
    plugin = make_plugin()
    sources = _make_sources(tmp_path, 18)

    cached_paths, cache_dir, white_ratios = plugin._cache_ordered_upload_images(sources)

    names = [Path(path).name for path in cached_paths]
    assert names == [f"{index:03d}.png" for index in range(1, 19)]
    # 服务端按文件名字典序排序;排序结果必须与任务顺序一致。
    assert sorted(cached_paths) == cached_paths
    assert sorted(path.name for path in cache_dir.iterdir()) == names
    assert len(white_ratios) == len(cached_paths)


def test_upload_cache_name_width_covers_more_than_999_images(
    make_plugin: MakePlugin, tmp_path: Path
) -> None:
    plugin = make_plugin()
    sources = _make_sources(tmp_path, 1001)

    cached_paths, _, _ = plugin._cache_ordered_upload_images(sources)

    names = [Path(path).name for path in cached_paths]
    assert names[0] == "0001.png"
    assert names[-1] == "1001.png"
    assert sorted(cached_paths) == cached_paths


def test_upload_cache_white_ratios_align_with_cached_paths(
    make_plugin: MakePlugin, tmp_path: Path
) -> None:
    plugin = make_plugin()
    # 首张为可解析 PNG,第二张为坏文件(白占比基准为 None,守卫走绝对阈值兜底)。
    good = tmp_path / "good.png"
    _write_png(good, (255, 255, 255))
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    cached_paths, _, white_ratios = plugin._cache_ordered_upload_images(
        [str(good), str(broken)]
    )

    assert [Path(path).name for path in cached_paths] == ["001.png", "002.png"]
    assert white_ratios[0] is not None
    assert white_ratios[1] is None

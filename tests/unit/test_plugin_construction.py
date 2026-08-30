# pyright: reportPrivateUsage=false
"""插件真实构造路径(__init__ + initialize)单元测试。

其余测试均通过 __new__ 绕过 __init__,构造器回归(Star.__init__ 交互、
数据目录解析、队列信号量初始化)不可见。本文件直接构造真实插件实例并
调用 initialize(),验证默认配置回退、显式配置生效与 queue_depth 校验行为。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context

import main as main_module
from main import DEFAULT_CONFIG, KoharuMangaTranslatorPlugin


class _FakeContext:
    """Star.__init__ 所需的最小上下文表面。

    SDK 4.14.6 中 Star.__init__ 仅调用 StarTools.initialize(context)
    (保存类级引用)并赋值 self.context = context,不读取 context 的任何
    成员,因此空壳对象即可满足构造路径;边界处用 cast 转成 Context。
    """


def _fake_context() -> Context:
    """SDK 边界 cast:_FakeContext → Context(与 _fakes.as_context 同模式)。"""
    return cast(Context, _FakeContext())


def _config(**overrides: object) -> AstrBotConfig:
    """SDK 边界 cast:外部 dict → AstrBotConfig(AstrBotConfig 是 dict 子类)。

    产品 __init__ 内部按同样的边界模式再 cast 成 PluginConfig
    (cast(PluginConfig, config or {}))——本测试真实走通这条路径。
    """
    return cast(AstrBotConfig, dict(overrides))


async def test_constructor_defaults_fallback_to_default_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认构造:配置回退 DEFAULT_CONFIG,信号量 = queue_depth + 1 = 4,数据目录按 SDK 路径解析。"""
    monkeypatch.setattr(main_module, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = KoharuMangaTranslatorPlugin(_fake_context(), None)

    assert plugin._str_conf("target_language") == DEFAULT_CONFIG["target_language"]
    assert plugin._int_conf("queue_depth") == 3
    assert getattr(plugin, "_queue_semaphore")._value == 4
    assert plugin._data_dir == tmp_path / "plugin_data" / "astrbot_plugin_koharu_plus"
    assert not plugin._data_dir.exists()

    await plugin.initialize()
    assert plugin._data_dir.is_dir()


async def test_constructor_honors_explicit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式配置生效,证明无硬编码 host/port:自定义 API 地址与队列深度被采用。"""
    monkeypatch.setattr(main_module, "get_astrbot_data_path", lambda: str(tmp_path))
    config = _config(
        koharu_api_base_url="http://127.0.0.1:4000/api/v1",
        queue_depth=5,
    )
    plugin = KoharuMangaTranslatorPlugin(_fake_context(), config)

    assert plugin._str_conf("koharu_api_base_url") == "http://127.0.0.1:4000/api/v1"
    assert plugin._int_conf("queue_depth") == 5
    assert getattr(plugin, "_queue_semaphore")._value == 6


def test_constructor_rejects_negative_queue_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前行为:queue_depth + 1 < 0(即 queue_depth <= -2)时构造抛 ValueError。

    __init__ 用 asyncio.Semaphore(queue_depth + 1) 初始化信号量,标准库对
    负初始值直接抛 ValueError("Semaphore initial value must be >= 0")。
    注意 queue_depth = -1 得到 Semaphore(0)(零许可,合法),不报错;
    本测试将该行为固化为文档:插件侧不做配置前置校验,依赖标准库报错。
    """
    monkeypatch.setattr(main_module, "get_astrbot_data_path", lambda: str(tmp_path))
    with pytest.raises(ValueError):
        KoharuMangaTranslatorPlugin(_fake_context(), _config(queue_depth=-2))

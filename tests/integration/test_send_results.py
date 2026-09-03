"""集成测试:_send_forward_result / _send_one_by_one 的截断与防御。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import astrbot.api.message_components as Comp
import pytest

from _fakes import (
    FakeBot,
    FakeCtx,
    FakeEvent,
    FakeInst,
    as_context,
    as_event,
    image_file_uri,
    send_forward_result,
    send_one_by_one,
)
from conftest import MakePlugin
from main import ForwardNode, QuotedBatch
from onebot_client import OneBotClient, OneBotSendError
import main


def _make_outputs(tmp_path: Path, names: list[str]) -> list[str]:
    """在 tmp_path 下创建真实输出文件并返回路径列表(供 Nodes.to_dict 读取)。"""
    outputs: list[str] = []
    for name in names:
        output_path = tmp_path / name
        output_path.write_bytes(b"fake-image-bytes")
        outputs.append(str(output_path))
    return outputs


async def test_forward_max_send_truncates_from_end(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """转发:max_send_images 为总预算,超出部分从尾部节点丢弃。"""
    plugin = make_plugin(overrides={"max_send_images": 2})
    outputs = _make_outputs(tmp_path, ["o1.png", "o2.png", "o3.png", "o4.png"])
    batch = QuotedBatch(
        image_paths=["/tmp/a.png", "/tmp/b.png", "/tmp/c.png", "/tmp/d.png"],
        forward_nodes=[
            ForwardNode(uin="1", name="a", image_indices=[0, 1]),
            ForwardNode(uin="2", name="b", image_indices=[2, 3]),
        ],
    )
    event = FakeEvent([])
    await send_forward_result(plugin, event, batch, outputs)

    assert len(event.sent_chains) == 1
    chain = event.sent_chains[0]
    assert len(chain) == 1
    nodes_comp = chain[0]
    assert isinstance(nodes_comp, Comp.Nodes)
    # 预算 2 全部被第一个节点消耗,第二个节点(尾部)被丢弃
    assert [node.uin for node in nodes_comp.nodes] == ["1"]
    assert len(nodes_comp.nodes[0].content) == 2
    assert [
        comp.file for comp in nodes_comp.nodes[0].content if isinstance(comp, Comp.Image)
    ] == [
        image_file_uri(outputs[0]),
        image_file_uri(outputs[1]),
    ]


async def test_forward_max_send_truncates_mid_node(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """转发:预算在节点中间耗尽 → 该节点保留前 N 张,后续节点全部丢弃。"""
    plugin = make_plugin(overrides={"max_send_images": 3})
    outputs = _make_outputs(tmp_path, ["o1.png", "o2.png", "o3.png", "o4.png", "o5.png"])
    batch = QuotedBatch(
        image_paths=["/tmp/a.png", "/tmp/b.png", "/tmp/c.png", "/tmp/d.png", "/tmp/e.png"],
        forward_nodes=[
            ForwardNode(uin="1", name="a", image_indices=[0, 1, 2, 3]),
            ForwardNode(uin="2", name="b", image_indices=[4]),
        ],
    )
    event = FakeEvent([])
    await send_forward_result(plugin, event, batch, outputs)

    nodes_comp = event.sent_chains[0][0]
    assert isinstance(nodes_comp, Comp.Nodes)
    assert [node.uin for node in nodes_comp.nodes] == ["1"]
    assert len(nodes_comp.nodes[0].content) == 3


async def test_forward_index_out_of_range_skipped_keeps_valid(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """转发:节点图片下标越界被防御跳过,有效图仍保留并发送。"""
    plugin = make_plugin()  # max_send_images=0 → 预算 = 输出数
    outputs = _make_outputs(tmp_path, ["o1.png"])
    batch = QuotedBatch(
        image_paths=["/tmp/a.png"],
        forward_nodes=[ForwardNode(uin="1", name="a", image_indices=[0, 99])],
    )
    event = FakeEvent([])
    await send_forward_result(plugin, event, batch, outputs)

    assert len(event.sent_chains) == 1
    nodes_comp = event.sent_chains[0][0]
    assert isinstance(nodes_comp, Comp.Nodes)
    assert len(nodes_comp.nodes) == 1
    assert len(nodes_comp.nodes[0].content) == 1
    first_image = nodes_comp.nodes[0].content[0]
    assert isinstance(first_image, Comp.Image)
    assert first_image.file == image_file_uri(outputs[0])


async def test_forward_all_nodes_empty_no_send(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """转发:所有节点图片下标越界(节点全空)→ 不发送任何消息。"""
    plugin = make_plugin()
    outputs = _make_outputs(tmp_path, ["o1.png", "o2.png"])
    batch = QuotedBatch(
        image_paths=["/tmp/a.png", "/tmp/b.png"],
        forward_nodes=[
            ForwardNode(uin="1", name="a", image_indices=[5, 6]),
            ForwardNode(uin="2", name="b", image_indices=[7]),
        ],
    )
    event = FakeEvent([])
    await send_forward_result(plugin, event, batch, outputs)

    assert event.sent_chains == []


async def test_forward_no_outputs_no_send(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """转发:输出为空 → 不发送任何消息。"""
    plugin = make_plugin()
    batch = QuotedBatch(
        image_paths=["/tmp/a.png"],
        forward_nodes=[ForwardNode(uin="1", name="a", image_indices=[0])],
    )
    event = FakeEvent([])
    await send_forward_result(plugin, event, batch, [])

    assert event.sent_chains == []


async def test_one_by_one_max_send_takes_front(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """非转发:max_send_images 截断取前 N 张。"""
    plugin = make_plugin(overrides={"max_send_images": 2})
    outputs = _make_outputs(tmp_path, ["o1.png", "o2.png", "o3.png", "o4.png"])
    event = FakeEvent([])
    await send_one_by_one(plugin, event, outputs)

    assert len(event.sent_chains) == 2
    for index, path in enumerate(outputs[:2]):
        chain = event.sent_chains[index]
        assert len(chain) == 1
        assert isinstance(chain[0], Comp.Image)
        assert chain[0].file == image_file_uri(path)


async def test_one_by_one_max_send_zero_sends_all(make_plugin: MakePlugin, tmp_path: Path) -> None:
    """非转发:max_send_images=0(不限) → 全部发送。"""
    plugin = make_plugin()
    outputs = _make_outputs(tmp_path, ["o1.png", "o2.png", "o3.png"])
    event = FakeEvent([])
    await send_one_by_one(plugin, event, outputs)

    assert len(event.sent_chains) == 3


async def test_one_by_one_empty_no_send(make_plugin: MakePlugin) -> None:
    """非转发:输出为空 → 不发送任何消息。"""
    plugin = make_plugin()
    event = FakeEvent([])
    await send_one_by_one(plugin, event, [])

    assert event.sent_chains == []


async def test_one_by_one_retries_once_after_first_failure(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """非转发:首次发送失败后第二次成功,只等待一次且最终发送图片。"""
    plugin = make_plugin(overrides={"use_direct_file_transfer": False})
    outputs = _make_outputs(tmp_path, ["retry.png"])
    event = FakeEvent([], send_errors=[RuntimeError("first send failed"), None])
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await send_one_by_one(plugin, event, outputs)

    assert event.send_attempts == 2
    assert sleep_calls == [1]
    assert len(event.sent_chains) == 1
    image = event.sent_chains[0][0]
    assert isinstance(image, Comp.Image)
    assert image.file == image_file_uri(outputs[0])
    assert event.sent_texts == []


async def test_one_by_one_skips_after_three_failures_and_continues(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """非转发:一张图片三次失败后跳过,后续图片继续发送并报告失败数量。"""
    plugin = make_plugin(overrides={"use_direct_file_transfer": False})
    outputs = _make_outputs(tmp_path, ["failed.png", "after-failed.png"])
    event = FakeEvent(
        [],
        send_errors=[
            RuntimeError("attempt 1"),
            RuntimeError("attempt 2"),
            RuntimeError("attempt 3"),
            None,
            None,
        ],
    )
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await send_one_by_one(plugin, event, outputs)

    assert event.send_attempts == 5
    assert sleep_calls == [1, 1]
    assert len(event.sent_chains) == 2
    image = event.sent_chains[0][0]
    assert isinstance(image, Comp.Image)
    assert image.file == image_file_uri(outputs[1])
    assert event.sent_texts == ["第 1 张翻译图片发送失败，已跳过。"]


async def test_one_by_one_summary_reports_selected_positions(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """非转发:多张图片失败时汇总报告 selected 列表中的位置。"""
    plugin = make_plugin(overrides={"use_direct_file_transfer": False})
    outputs = _make_outputs(tmp_path, ["failed-1.png", "ok.png", "failed-3.png"])
    event = FakeEvent(
        [],
        send_errors=[
            RuntimeError("attempt 1"),
            RuntimeError("attempt 2"),
            RuntimeError("attempt 3"),
            None,
            RuntimeError("attempt 1"),
            RuntimeError("attempt 2"),
            RuntimeError("attempt 3"),
            None,
        ],
    )

    async def fake_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await send_one_by_one(plugin, event, outputs)

    assert event.sent_texts == ["第 1、3 张翻译图片发送失败，已跳过。"]
    assert all(path not in event.sent_texts[0] for path in outputs)


async def test_onebot_send_error_wraps_platform_and_action_failures() -> None:
    """OneBot 直接发送:平台不可用与 API 异常都转换为 OneBotSendError。"""
    unavailable_client = OneBotClient(as_context(FakeCtx(None)))
    unavailable_event = FakeEvent([])
    with pytest.raises(OneBotSendError, match="直接发送图片"):
        await unavailable_client.send_image(as_event(unavailable_event), "/tmp/a.png")

    bot = FakeBot()
    bot.preset_error("send_private_msg", RuntimeError("action failed"))
    action_client = OneBotClient(as_context(FakeCtx(FakeInst(bot))))
    action_event = FakeEvent([])
    with pytest.raises(OneBotSendError, match="action failed"):
        await action_client.send_image(as_event(action_event), "/tmp/a.png")


async def test_one_by_one_default_uses_event_send(
    make_plugin: MakePlugin,
    tmp_path: Path,
) -> None:
    """非转发:直接传输开关关闭时沿用 event.send 图片组件路径。"""
    bot = FakeBot()
    bot.preset("send_group_msg", {"status": "ok"})
    plugin = make_plugin(context=FakeCtx(FakeInst(bot)))
    outputs = _make_outputs(tmp_path, ["default.png"])
    event = FakeEvent([], group_id="20001")

    await send_one_by_one(plugin, event, outputs)

    assert len(event.sent_chains) == 1
    image = event.sent_chains[0][0]
    assert isinstance(image, Comp.Image)
    assert image.file == image_file_uri(outputs[0])
    assert bot.calls == []


async def test_one_by_one_direct_transfer_sends_group_path_payload(
    make_plugin: MakePlugin,
    tmp_path: Path,
) -> None:
    """非转发:直接传输开关开启时群聊 payload 保留本地路径。"""
    bot = FakeBot()
    bot.preset("send_group_msg", {"status": "ok"})
    plugin = make_plugin(
        overrides={"use_direct_file_transfer": True},
        context=FakeCtx(FakeInst(bot)),
    )
    outputs = _make_outputs(tmp_path, ["direct-group.png"])
    event = FakeEvent([], group_id="20002", sender_id="30002")

    await send_one_by_one(plugin, event, outputs)

    assert event.sent_chains == []
    assert len(bot.calls) == 1
    action, params = bot.calls[0]
    assert action == "send_group_msg"
    assert params["group_id"] == "20002"
    message = cast(list[object], params["message"])
    segment = cast(dict[str, object], message[0])
    assert segment["type"] == "image"
    data = cast(dict[str, object], segment["data"])
    assert data["file"] == outputs[0]


async def test_one_by_one_direct_transfer_retries_onebot_failure(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """非转发:直接传输的 OneBot 调用首次失败后第二次成功。"""
    bot = FakeBot()
    bot.preset_sequence(
        "send_group_msg",
        [RuntimeError("OneBot send failed"), {"status": "ok"}],
    )
    plugin = make_plugin(
        overrides={"use_direct_file_transfer": True},
        context=FakeCtx(FakeInst(bot)),
    )
    outputs = _make_outputs(tmp_path, ["direct-retry.png"])
    event = FakeEvent([], group_id="20004")
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await send_one_by_one(plugin, event, outputs)

    assert len(bot.calls) == 2
    assert [action for action, _ in bot.calls] == [
        "send_group_msg",
        "send_group_msg",
    ]
    assert sleep_calls == [1]
    assert event.sent_chains == []
    assert event.sent_texts == []


async def test_one_by_one_direct_transfer_sends_private_url_payload(
    make_plugin: MakePlugin,
) -> None:
    """非转发:直接传输开关开启时私聊 payload 保留 URL。"""
    bot = FakeBot()
    bot.preset("send_private_msg", {"status": "ok"})
    plugin = make_plugin(
        overrides={"use_direct_file_transfer": True},
        context=FakeCtx(FakeInst(bot)),
    )
    image_url = "https://example.invalid/translated.png"
    event = FakeEvent([], sender_id="30003")

    await send_one_by_one(plugin, event, [image_url])

    assert event.sent_chains == []
    assert len(bot.calls) == 1
    action, params = bot.calls[0]
    assert action == "send_private_msg"
    assert params["user_id"] == "30003"
    message = cast(list[object], params["message"])
    segment = cast(dict[str, object], message[0])
    assert segment["type"] == "image"
    data = cast(dict[str, object], segment["data"])
    assert data["file"] == image_url

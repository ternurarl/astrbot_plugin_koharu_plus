"""集成测试:_run_translation 的队列语义、确认文案、失败释放与发送路径。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

import astrbot.api.message_components as Comp
import main

from _fakes import (
    FakeEvent,
    image_file_uri,
    nodes_to_dict_payload,
    onebot_node_data,
    queue_semaphore,
    run_translation,
)
from conftest import MakePlugin
from main import ForwardNode, KoharuMangaTranslatorPlugin, QuotedBatch


def _install_fake_translate(
    plugin: KoharuMangaTranslatorPlugin,
    monkeypatch: pytest.MonkeyPatch,
    outputs: list[str],
) -> list[tuple[list[str], str]]:
    """把 _translate_images 替换为记录调用并返回 outputs 的 fake。"""
    called: list[tuple[list[str], str]] = []

    async def fake_translate(image_paths: list[str], target_language: str) -> list[str]:
        called.append((image_paths, target_language))
        return outputs

    monkeypatch.setattr(plugin, "_translate_images", fake_translate)
    return called


async def test_queue_full_rejects_without_translating(make_plugin: MakePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    """队列满(信号量被占满)→ 回复"翻译队列已满"且不调用 _translate_images。"""
    plugin = make_plugin(overrides={"queue_depth": 1})
    # queue_depth + 1 = 2 个许可,全部占满 → locked() 为 True
    await queue_semaphore(plugin).acquire()
    await queue_semaphore(plugin).acquire()

    called = _install_fake_translate(plugin, monkeypatch, [])
    event = FakeEvent([])
    batch = QuotedBatch(image_paths=["/tmp/a.png"])
    await run_translation(plugin, event, batch, "Simplified Chinese")

    assert event.sent_texts == ["翻译队列已满（最大等待 1 个），请稍后再试。"]
    assert called == []


async def test_confirm_message_non_forward(make_plugin: MakePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    """非转发确认文案:已收到 N 张图片。"""
    plugin = make_plugin()
    called = _install_fake_translate(plugin, monkeypatch, [])
    event = FakeEvent([])
    batch = QuotedBatch(image_paths=["/tmp/a.png", "/tmp/b.png"])
    await run_translation(plugin, event, batch, "Simplified Chinese")

    assert event.sent_texts[0] == "已收到 2 张图片，开始调用 Koharu 翻译为 简体中文。"
    assert called == [(["/tmp/a.png", "/tmp/b.png"], "Simplified Chinese")]


async def test_confirm_message_forward(make_plugin: MakePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    """转发确认文案:已收到转发记录中的 N 张图片。"""
    plugin = make_plugin()
    called = _install_fake_translate(plugin, monkeypatch, [])
    event = FakeEvent([])
    batch = QuotedBatch(
        image_paths=["/tmp/a.png"],
        forward_nodes=[ForwardNode(uin="1", name="a", image_indices=[0])],
    )
    await run_translation(plugin, event, batch, "Simplified Chinese")

    assert (
        event.sent_texts[0]
        == "已收到转发记录中的 1 张图片，开始调用 Koharu 翻译为 简体中文。"
    )
    assert called == [(["/tmp/a.png"], "Simplified Chinese")]


async def test_translate_failure_replies_and_releases_queue(make_plugin: MakePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    """翻译抛异常 → 回复失败文案,信号量被释放(可再次 acquire)。"""
    plugin = make_plugin()

    async def failing_translate(image_paths: list[str], target_language: str) -> list[str]:
        raise RuntimeError("koharu boom")

    monkeypatch.setattr(plugin, "_translate_images", failing_translate)
    event = FakeEvent([])
    batch = QuotedBatch(image_paths=["/tmp/a.png"])
    await run_translation(plugin, event, batch, "Simplified Chinese")

    # 失败回复在确认文案之后;失败路径的 finally 已释放信号量:带超时 acquire 应成功
    assert event.sent_texts == ["已收到 1 张图片，开始调用 Koharu 翻译为 简体中文。", "漫画翻译失败：koharu boom"]
    await asyncio.wait_for(queue_semaphore(plugin).acquire(), timeout=1)
    queue_semaphore(plugin).release()


async def test_confirm_send_failure_releases_queue(make_plugin: MakePlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    """确认消息发送抛异常 → 异常冒泡、不调用翻译,且信号量被 finally 释放。"""
    plugin = make_plugin()
    called = _install_fake_translate(plugin, monkeypatch, [])
    event = FakeEvent([], send_error=RuntimeError("send boom"))
    batch = QuotedBatch(image_paths=["/tmp/a.png"])
    with pytest.raises(RuntimeError, match="send boom"):
        await run_translation(plugin, event, batch, "Simplified Chinese")

    assert called == []
    # 确认发送在 acquire 之后:异常路径也必须走 finally 释放许可,带超时 acquire 应成功
    await asyncio.wait_for(queue_semaphore(plugin).acquire(), timeout=1)
    queue_semaphore(plugin).release()


async def test_success_non_forward_sends_images_and_cleans_up(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功非转发:逐张发送 image_result,无提示文字;两个 cleanup 方法被调用。"""
    plugin = make_plugin()
    outputs = ["/out/1.png", "/out/2.png"]
    called = _install_fake_translate(plugin, monkeypatch, outputs)

    cleanup_current_calls: list[list[str]] = []
    cleanup_cache_calls: list[bool] = []

    def fake_cleanup_current(output_paths: list[str]) -> None:
        cleanup_current_calls.append(output_paths)

    def fake_cleanup_cache() -> None:
        cleanup_cache_calls.append(True)

    monkeypatch.setattr(plugin, "_cleanup_current_outputs_if_needed", fake_cleanup_current)
    monkeypatch.setattr(plugin, "_cleanup_output_cache", fake_cleanup_cache)

    event = FakeEvent([])
    batch = QuotedBatch(image_paths=["/tmp/a.png"])
    await run_translation(plugin, event, batch, "Simplified Chinese")

    # 1 条确认文案 + 逐张图片,每张图片单独一条消息且无提示文字
    assert len(event.sent_chains) == 3
    assert event.sent_texts == ["已收到 1 张图片，开始调用 Koharu 翻译为 简体中文。"]
    for index, path in enumerate(outputs):
        chain = event.sent_chains[index + 1]
        assert len(chain) == 1
        assert isinstance(chain[0], Comp.Image)
        assert chain[0].file == image_file_uri(path)
    assert called == [(["/tmp/a.png"], "Simplified Chinese")]
    assert cleanup_current_calls == [outputs]
    assert cleanup_cache_calls == [True]


async def test_success_forward_sends_nodes_chain_and_cleans_up(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成功转发:发送含单个 Comp.Nodes 的 chain,OneBot 结构与顺序正确;cleanup 被调用。"""
    plugin = make_plugin()
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    outputs: list[str] = []
    for name in ("o1.png", "o2.png", "o3.png"):
        output_path = outputs_dir / name
        output_path.write_bytes(b"fake-image-bytes")
        outputs.append(str(output_path))
    called = _install_fake_translate(plugin, monkeypatch, outputs)

    cleanup_current_calls: list[list[str]] = []
    cleanup_cache_calls: list[bool] = []

    def fake_cleanup_current(output_paths: list[str]) -> None:
        cleanup_current_calls.append(output_paths)

    def fake_cleanup_cache() -> None:
        cleanup_cache_calls.append(True)

    monkeypatch.setattr(plugin, "_cleanup_current_outputs_if_needed", fake_cleanup_current)
    monkeypatch.setattr(plugin, "_cleanup_output_cache", fake_cleanup_cache)

    batch = QuotedBatch(
        image_paths=["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"],
        forward_nodes=[
            ForwardNode(uin="111", name="alice", image_indices=[0]),
            ForwardNode(uin="222", name="bob", image_indices=[1, 2]),
        ],
    )
    event = FakeEvent([])
    await run_translation(plugin, event, batch, "Simplified Chinese")

    # 确认文案 + 一条 Nodes 消息
    assert len(event.sent_chains) == 2
    assert event.sent_texts[0] == (
        "已收到转发记录中的 3 张图片，开始调用 Koharu 翻译为 简体中文。"
    )
    chain = event.sent_chains[1]
    assert len(chain) == 1
    nodes_comp = chain[0]
    assert isinstance(nodes_comp, Comp.Nodes)
    assert [node.uin for node in nodes_comp.nodes] == ["111", "222"]
    assert [node.name for node in nodes_comp.nodes] == ["alice", "bob"]
    assert [len(node.content) for node in nodes_comp.nodes] == [1, 2]

    payload = await nodes_to_dict_payload(nodes_comp)
    messages = cast(list[object], payload["messages"])
    assert len(messages) == 2
    first_data = onebot_node_data(messages, 0)
    assert first_data["user_id"] == "111"
    assert first_data["nickname"] == "alice"
    first_content = cast(list[object], first_data["content"])
    assert len(first_content) == 1
    first_segment = cast(dict[str, object], first_content[0])
    assert first_segment["type"] == "image"
    first_file = cast(dict[str, object], first_segment["data"])["file"]
    assert str(first_file).startswith("base64://")

    second_data = onebot_node_data(messages, 1)
    assert second_data["user_id"] == "222"
    second_content = cast(list[object], second_data["content"])
    assert len(second_content) == 2

    assert called == [(["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"], "Simplified Chinese")]
    assert cleanup_current_calls == [outputs]
    assert cleanup_cache_calls == [True]


async def test_image_send_and_summary_failures_still_clean_up_and_release_queue(
    make_plugin: MakePlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发送图片三次失败且汇总消息失败时,清理仍执行且队列仍释放。"""
    plugin = make_plugin(
        overrides={"use_direct_file_transfer": False, "queue_depth": 0}
    )
    outputs = ["/out/failed.png"]
    called = _install_fake_translate(plugin, monkeypatch, outputs)

    cleanup_current_calls: list[list[str]] = []
    cleanup_cache_calls: list[bool] = []

    def fake_cleanup_current(output_paths: list[str]) -> None:
        cleanup_current_calls.append(output_paths)

    def fake_cleanup_cache() -> None:
        cleanup_cache_calls.append(True)

    monkeypatch.setattr(plugin, "_cleanup_current_outputs_if_needed", fake_cleanup_current)
    monkeypatch.setattr(plugin, "_cleanup_output_cache", fake_cleanup_cache)

    async def fake_sleep(seconds: float) -> None:
        assert seconds == 1

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)
    event = FakeEvent(
        [],
        send_errors=[
            None,  # 确认消息成功
            RuntimeError("image attempt 1"),
            RuntimeError("image attempt 2"),
            RuntimeError("image attempt 3"),
            RuntimeError("summary send failed"),
        ],
    )
    batch = QuotedBatch(image_paths=["/tmp/a.png"])

    await run_translation(plugin, event, batch, "Simplified Chinese")

    assert called == [(["/tmp/a.png"], "Simplified Chinese")]
    assert event.send_attempts == 5
    assert event.sent_texts == ["已收到 1 张图片，开始调用 Koharu 翻译为 简体中文。"]
    assert cleanup_current_calls == [outputs]
    assert cleanup_cache_calls == [True]
    await asyncio.wait_for(queue_semaphore(plugin).acquire(), timeout=1)
    queue_semaphore(plugin).release()

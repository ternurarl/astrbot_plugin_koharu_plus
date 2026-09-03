"""集成测试共享 fake:AstrBot SDK 表面 / OneBot API / 插件实例工厂。

所有 fake 均为具名类并带全类型注解(遵守项目强类型纪律);涉及 SDK
未类型化边界的转换使用与产品代码一致的 `cast` 边界模式。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import cast

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from main import DEFAULT_CONFIG, KoharuMangaTranslatorPlugin, PluginConfig, QuotedBatch


# --- 图片路径工具 ------------------------------------------------------------------

def image_file_uri(path: str) -> str:
    """构造 file:// URI,使 Comp.Image.convert_to_file_path() 返回 abspath(path)。

    使用四斜杠形式(file:/// + 绝对路径),SDK 的 convert_to_file_path 对
    file:/// 前缀直接截取,不会触发网络下载。
    """
    return f"file:///{os.path.abspath(path)}"


def image_from_path(path: str) -> Comp.Image:
    """构造指向本地路径的 Comp.Image(convert 后得到 abspath(path))。"""
    return Comp.Image(file=image_file_uri(path))


# --- OneBot 段 / 转发响应构造 -------------------------------------------------------

def image_segment(file: str) -> dict[str, object]:
    """OneBot image 段。"""
    return {"type": "image", "data": {"file": file}}


def text_segment(text: str) -> dict[str, object]:
    """OneBot text 段(应被转发/引用解析忽略)。"""
    return {"type": "text", "data": {"text": text}}


def forward_segment(forward_id: str) -> dict[str, object]:
    """OneBot forward 段(合并转发记录占位,如 get_msg 返回的"[聊天记录]")。"""
    return {"type": "forward", "data": {"id": forward_id}}


def forward_message_node(
    user_id: object,
    nickname: str,
    content: list[dict[str, object]],
) -> dict[str, object]:
    """NapCat get_forward_msg 返回的完整消息对象(OB11Message,无 type/data 包装)。"""
    return {
        "user_id": user_id,
        "sender": {"user_id": user_id, "nickname": nickname},
        "message": content,
    }


def forward_node_payload(
    user_id: object,
    nickname: str,
    content: list[dict[str, object]],
) -> dict[str, object]:
    """get_forward_msg 响应中的一个 node 载荷。"""
    return {"type": "node", "data": {"user_id": user_id, "nickname": nickname, "content": content}}


def forward_response(nodes: list[object]) -> dict[str, object]:
    """get_forward_msg 成功响应(节点为运行时数据,允许任意结构)。"""
    return {"messages": nodes}


# --- 事件 --------------------------------------------------------------------------

class FakeResult:
    """MessageEventResult 的最小表面:chain + stop_event()。"""

    def __init__(self, chain: list[Comp.BaseMessageComponent]) -> None:
        self.chain: list[Comp.BaseMessageComponent] = list(chain)

    def stop_event(self) -> "FakeResult":
        return self


class FakeEvent:
    """AstrMessageEvent 的最小表面:插件用到的成员全量实现,发送被记录。"""

    def __init__(
        self,
        messages: list[Comp.BaseMessageComponent],
        *,
        message_str: str = "",
        sender_id: str = "10001",
        session_id: str = "session-1",
        platform_id: str = "aiocqhttp",
        group_id: str = "",
        send_error: Exception | None = None,
        send_errors: list[Exception | None] | None = None,
    ) -> None:
        self._messages: list[Comp.BaseMessageComponent] = list(messages)
        self.message_str: str = message_str
        self._sender_id: str = sender_id
        self._session_id: str = session_id
        self._platform_id: str = platform_id
        self._group_id: str = group_id
        self.send_error: Exception | None = send_error
        self._send_errors: list[Exception | None] | None = (
            list(send_errors) if send_errors is not None else None
        )
        self.send_attempts: int = 0
        self.sent_chains: list[list[Comp.BaseMessageComponent]] = []

    def get_messages(self) -> list[Comp.BaseMessageComponent]:
        return list(self._messages)

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_session_id(self) -> str:
        return self._session_id

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_group_id(self) -> str:
        return self._group_id

    def plain_result(self, text: str) -> FakeResult:
        return FakeResult([Comp.Plain(text)])

    def image_result(self, url_or_path: str) -> FakeResult:
        return FakeResult([image_from_path(url_or_path)])

    def chain_result(self, chain: list[Comp.BaseMessageComponent]) -> FakeResult:
        return FakeResult(chain)

    async def send(self, result: FakeResult) -> None:
        self.send_attempts += 1
        if self._send_errors is not None:
            if not self._send_errors:
                raise AssertionError("send error sequence exhausted")
            error = self._send_errors.pop(0)
        else:
            error = self.send_error
        if error is not None:
            raise error
        self.sent_chains.append(list(result.chain))

    @property
    def last_sent(self) -> list[Comp.BaseMessageComponent]:
        if not self.sent_chains:
            raise AssertionError("no messages have been sent on this event")
        return self.sent_chains[-1]

    @property
    def sent_texts(self) -> list[str]:
        """每条已发送链的 Plain 文本拼接,跳过无文本的链(如图片消息)。"""
        texts: list[str] = []
        for chain in self.sent_chains:
            parts: list[str] = []
            for component in chain:
                if isinstance(component, Comp.Plain):
                    parts.append(component.text)
            if parts:
                texts.append("".join(parts))
        return texts


# --- OneBot bot / 平台实例 / 上下文 -------------------------------------------------

class FakeBot:
    """OneBotBot 最小实现:按 action 返回预设响应/序列/异常,记录调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._responses: dict[str, object] = {}
        self._sequences: dict[str, list[object]] = {}
        self._errors: dict[str, Exception] = {}

    def preset(self, action: str, response: object) -> None:
        """固定响应:该 action 每次调用都返回同一对象。"""
        self._responses[action] = response

    def preset_sequence(self, action: str, responses: list[object]) -> None:
        """按序返回响应;序列中的异常对象会被抛出,耗尽后抛 AssertionError。"""
        self._sequences[action] = list(responses)

    def preset_error(self, action: str, error: Exception) -> None:
        """该 action 每次调用都抛出预设异常。"""
        self._errors[action] = error

    async def call_action(self, action: str, **params: object) -> object:
        self.calls.append((action, params))
        if action in self._errors:
            raise self._errors[action]
        if action in self._sequences:
            sequence = self._sequences[action]
            if not sequence:
                raise AssertionError(f"preset sequence exhausted for action {action!r}")
            response = sequence.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        if action in self._responses:
            return self._responses[action]
        raise AssertionError(f"no preset for action {action!r}")


class FakeInst:
    """平台实例壳:仅携带 .bot(OneBot API 对象)。"""

    def __init__(self, bot: FakeBot | None) -> None:
        self.bot: FakeBot | None = bot


class FakeCtx:
    """Context 最小表面:插件/reader 只用 get_platform_inst。"""

    def __init__(self, inst: FakeInst | None) -> None:
        self._inst: FakeInst | None = inst

    def get_platform_inst(self, platform_id: str) -> FakeInst | None:
        return self._inst


# --- SDK 边界 cast -----------------------------------------------------------------

def as_event(event: FakeEvent) -> AstrMessageEvent:
    """FakeEvent → AstrMessageEvent(与产品代码的 SDK 边界 cast 同模式)。"""
    return cast(AstrMessageEvent, event)


def as_context(ctx: FakeCtx) -> Context:
    """FakeCtx → Context(与产品代码的 SDK 边界 cast 同模式)。"""
    return cast(Context, ctx)


# --- 插件实例工厂 ------------------------------------------------------------------

def default_config() -> PluginConfig:
    """DEFAULT_CONFIG 的独立 dict 拷贝,测试可安全修改。"""
    return cast(PluginConfig, dict(DEFAULT_CONFIG))


def make_plugin(
    *,
    overrides: dict[str, object] | None = None,
    context: FakeCtx | None = None,
    data_dir: Path | None = None,
) -> KoharuMangaTranslatorPlugin:
    """构造插件实例(不调 __init__),注入测试属性;每次调用返回独立实例。"""
    config = default_config()
    if overrides:
        config = cast(PluginConfig, {**config, **overrides})
    plugin = KoharuMangaTranslatorPlugin.__new__(KoharuMangaTranslatorPlugin)
    plugin.config = config
    # 私有属性注入用 setattr:getattr/setattr 的字面量形式不触发 pyright 的
    # reportPrivateUsage(测试必须直接操纵插件内部状态,产品代码无需改动)。
    setattr(
        plugin,
        "_data_dir",
        data_dir if data_dir is not None else Path.cwd() / "data" / "test-data",
    )
    setattr(plugin, "_translate_lock", asyncio.Lock())
    setattr(plugin, "_config_lock", asyncio.Lock())
    setattr(plugin, "_queue_semaphore", asyncio.Semaphore(int(config["queue_depth"]) + 1))
    plugin.context = as_context(context if context is not None else FakeCtx(None))
    return plugin


# --- 插件私有方法访问助手(测试专用) ------------------------------------------------

def queue_semaphore(plugin: KoharuMangaTranslatorPlugin) -> asyncio.Semaphore:
    """取插件的队列信号量(私有属性访问经 getattr,规避 reportPrivateUsage)。"""
    return getattr(plugin, "_queue_semaphore")


async def extract_image_batch(
    plugin: KoharuMangaTranslatorPlugin,
    event: FakeEvent,
) -> QuotedBatch:
    """调用插件的 _extract_image_batch。"""
    method = getattr(plugin, "_extract_image_batch")
    return await method(as_event(event))


async def run_translation(
    plugin: KoharuMangaTranslatorPlugin,
    event: FakeEvent,
    batch: QuotedBatch,
    target_language: str,
) -> None:
    """调用插件的 _run_translation。"""
    method = getattr(plugin, "_run_translation")
    await method(as_event(event), batch, target_language)


async def send_forward_result(
    plugin: KoharuMangaTranslatorPlugin,
    event: FakeEvent,
    batch: QuotedBatch,
    output_paths: list[str],
) -> None:
    """调用插件的 _send_forward_result。"""
    method = getattr(plugin, "_send_forward_result")
    await method(as_event(event), batch, output_paths)


async def send_one_by_one(
    plugin: KoharuMangaTranslatorPlugin,
    event: FakeEvent,
    output_paths: list[str],
) -> None:
    """调用插件的 _send_one_by_one。"""
    method = getattr(plugin, "_send_one_by_one")
    await method(as_event(event), output_paths)


# --- Nodes.to_dict 断言辅助 --------------------------------------------------------

async def nodes_to_dict_payload(nodes_comp: Comp.Nodes) -> dict[str, object]:
    """SDK 边界 cast:Nodes.to_dict()(SDK 未类型化)→ dict[str, object]。"""
    return cast(dict[str, object], await nodes_comp.to_dict())


def onebot_node_data(messages: list[object], index: int) -> dict[str, object]:
    """取 Nodes.to_dict() 消息列表中第 index 个节点的 data 字典。"""
    message = cast(dict[str, object], messages[index])
    data = message.get("data")
    assert isinstance(data, dict)
    return cast(dict[str, object], data)

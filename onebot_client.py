"""OneBot (aiocqhttp) 统一封装:读取被引用消息与合并转发消息。

按项目"统一封装"约定,所有 OneBot API 调用集中在本模块,main.py 不直接
接触 bot 对象与原始 dict。本模块只做"原始数据 → AstrBot 组件"的转换,
不负责图片下载/翻译——那是 main.py 的职责。

被引用消息与合并转发记录的内容只提取 image 段(用户已确认:只保留有图
节点、丢弃原文文本),其他段(text/face/at 等)一律忽略。合并转发记录里
嵌套的 forward / node 段会递归展开为独立节点(对应记录中的"[聊天记录]"
占位),保证嵌套聊天记录中的图片也能被提取。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast, runtime_checkable

import astrbot.api.message_components as Comp

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 独立运行时的兜底。
    logger = logging.getLogger(__name__)

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

__all__ = [
    "OneBotClient",
    "OneBotBot",
    "OneBotSegmentData",
    "OneBotSegment",
    "ForwardNodeData",
    "ForwardNodePayload",
    "ForwardMessageResponse",
    "QuotedMessageResponse",
    "ForwardNodeContent",
    "QuotedMessageContent",
    "QuotedMessageReader",
    "QuotedMessageReadError",
    "OneBotSendError",
]

# 合并转发嵌套深度上限:防止畸形记录构造深链导致递归过深。
_MAX_FORWARD_DEPTH = 10


class QuotedMessageReadError(RuntimeError):
    """读取被引用消息 / 合并转发消息失败(含平台不支持)。消息可直接展示给用户。"""


class OneBotSendError(RuntimeError):
    """通过 OneBot 直接发送消息失败。"""



@runtime_checkable
class OneBotBot(Protocol):
    """aiocqhttp CQHttp 的最小类型面(避免依赖 aiocqhttp 包)。"""

    async def call_action(self, action: str, **params: object) -> object: ...


class OneBotSegmentData(TypedDict, total=False):
    text: str
    file: str
    url: str
    path: str
    file_unique: str


class OneBotSegment(TypedDict, total=False):
    type: str
    data: OneBotSegmentData


class ForwardNodeData(TypedDict, total=False):
    user_id: str
    nickname: str
    content: list[OneBotSegment]
    message: list[OneBotSegment]  # 部分实现(NapCat parseForward)用 message 键存放节点内容


class ForwardNodePayload(TypedDict, total=False):
    type: str
    data: ForwardNodeData


class ForwardMessageResponse(TypedDict, total=False):
    messages: list[ForwardNodePayload]


class QuotedMessageResponse(TypedDict, total=False):
    message: list[OneBotSegment]


class ForwardSegmentData(TypedDict, total=False):
    id: str
    content: list[object]  # NapCat(parseMultMsg)内联展开的嵌套转发消息


class Ob11Sender(TypedDict, total=False):
    user_id: str
    nickname: str


class ForwardMessageNode(TypedDict, total=False):
    """NapCat get_forward_msg 返回的完整消息对象(OB11Message)。"""

    user_id: str
    sender: Ob11Sender
    message: list[object]


class NodeListData(TypedDict, total=False):
    messages: list[object]
    nodes: list[object]


@dataclass
class ForwardNodeContent:
    uin: str  # 节点发送者 QQ 号(字符串)
    name: str  # 节点发送者昵称
    components: list[Comp.BaseMessageComponent]  # 该节点内容转换后的 AstrBot 组件(只含 Comp.Image;嵌套转发已展开为独立节点)


@dataclass
class QuotedMessageContent:
    """get_msg 兜底读取结果:直接图片段 + 展开后的合并转发节点。"""

    images: list[Comp.Image]
    forward_nodes: list[ForwardNodeContent]


class OneBotClient:
    """通过 OneBot API 读取消息或发送不经 AstrBot 编码的消息。"""

    def __init__(self, context: Context) -> None:
        self.context = context

    async def send_image(self, event: AstrMessageEvent, file: str) -> None:
        """直接发送一个图片消息段,保留本地路径或 URL 给 OneBot 实现端处理。"""
        try:
            bot = self._resolve_send_bot(event)
            message: list[OneBotSegment] = [
                {"type": "image", "data": {"file": file}},
            ]
            group_id: str = event.get_group_id()
            if group_id:
                await bot.call_action(
                    "send_group_msg",
                    group_id=group_id,
                    message=message,
                )
                return
            user_id: str = event.get_sender_id()
            await bot.call_action(
                "send_private_msg",
                user_id=user_id,
                message=message,
            )
        except OneBotSendError:
            raise
        except Exception as exc:
            raise OneBotSendError(f"直接发送图片失败:{exc}") from exc

    def _resolve_send_bot(self, event: AstrMessageEvent) -> OneBotBot:
        platform_id: str = event.get_platform_id()
        inst = self.context.get_platform_inst(platform_id)
        if inst is None:
            raise OneBotSendError("当前平台不支持直接发送图片。")
        bot = getattr(inst, "bot", None)
        if not isinstance(bot, OneBotBot):
            raise OneBotSendError("当前平台不支持直接发送图片。")
        return bot

    def _resolve_bot(self, event: AstrMessageEvent) -> OneBotBot:
        # get_platform_id 在 AstrBot SDK 中无返回类型注解(在本地 SDK 中可推断为 str),
        # 此处做显式注解边界处理。
        platform_id: str = event.get_platform_id()
        inst = self.context.get_platform_inst(platform_id)
        if inst is None:
            raise QuotedMessageReadError("当前平台不支持读取转发消息内容。")
        # aiocqhttp 适配器实例的 OneBot API 对象是 .bot(CQHttp),而非适配器本身。
        bot = getattr(inst, "bot", None)
        if not isinstance(bot, OneBotBot):
            raise QuotedMessageReadError("当前平台不支持读取转发消息内容。")
        return bot


class QuotedMessageReader(OneBotClient):
    """通过 OneBot API 读取被引用消息与合并转发消息。"""

    async def fetch_forward(
        self,
        event: AstrMessageEvent,
        forward_id: str,
    ) -> list[ForwardNodeContent]:
        """读取合并转发记录,返回按原始顺序排列的、含图片的节点列表。

        节点兼容两种形态:标准 node 段与 NapCat 返回的完整消息对象
        (OB11Message)。节点内的嵌套合并转发 / 内联 node 段会递归展开为
        独立节点。无图节点会被整体跳过;单个节点解析失败只跳过该节点,
        不导致整体失败。
        """
        return await self._fetch_forward(event, forward_id, 0)

    async def _fetch_forward(
        self,
        event: AstrMessageEvent,
        forward_id: str,
        depth: int,
    ) -> list[ForwardNodeContent]:
        """fetch_forward 的实现:depth 用于截断嵌套合并转发的递归深度。"""
        logger.debug("[onebot-client] get_forward_msg forward_id=%s depth=%d", forward_id, depth)
        result = await self._call_action(
            event,
            "get_forward_msg",
            "读取转发消息失败",
            message_id=forward_id,
        )
        # 边界单点:外部 JSON → 命名 TypedDict。
        response = cast(ForwardMessageResponse, result)
        messages = response.get("messages")
        if not isinstance(messages, list):
            logger.warning("[onebot-client] get_forward_msg 响应缺少 messages 字段")
            return []
        nodes: list[ForwardNodeContent] = []
        for index, node in enumerate(messages):
            parsed = await self._parse_forward_node(event, node, depth)
            if parsed:
                nodes.extend(parsed)
            else:
                logger.warning(
                    "[onebot-client] 转发节点 %s 解析失败或无图片,已跳过", index
                )
        logger.debug(
            "[onebot-client] get_forward_msg forward_id=%s nodes=%s",
            forward_id,
            len(nodes),
        )
        return nodes

    async def fetch_quoted_message(
        self,
        event: AstrMessageEvent,
        message_id: str,
    ) -> QuotedMessageContent:
        """读取被引用消息内容,解析其中的图片与合并转发记录。

        引用过旧消息时 OneBot 适配器的 Reply.chain 可能为空,此方法通过
        get_msg 兜底读取原始消息。被引用消息本身是合并转发时,get_msg 返回
        的 forward / node 段会被展开为转发节点(嵌套合并转发递归展开),
        结果同时包含直接图片段与转发节点。
        """
        # OneBot 的 message_id 在部分实现中是 int,优先转 int 传参;转失败就传原值。
        try:
            message_id_param: str | int = int(message_id)
        except ValueError:
            message_id_param = message_id
        logger.debug("[onebot-client] get_msg message_id=%s", message_id)
        result = await self._call_action(
            event,
            "get_msg",
            "读取被引用消息失败",
            message_id=message_id_param,
        )
        # 边界单点:外部 JSON → 命名 TypedDict。
        response = cast(QuotedMessageResponse, result)
        segments = response.get("message")
        if not isinstance(segments, list):
            raise QuotedMessageReadError("读取被引用消息失败:响应缺少 message 字段")
        images: list[Comp.Image] = []
        forward_nodes: list[ForwardNodeContent] = []
        for segment in segments:
            seg_type = segment.get("type")
            if seg_type == "image":
                image = _extract_image_segment(segment)
                if image is not None:
                    images.append(image)
            elif seg_type == "forward":
                forward_id = _forward_id_from_segment(segment)
                if forward_id is not None:
                    forward_nodes.extend(await self._fetch_forward(event, forward_id, 0))
            elif seg_type in ("node", "nodes"):
                for node in _node_list_from_segment(segment):
                    forward_nodes.extend(await self._parse_forward_node(event, node, 0))
        return QuotedMessageContent(images=images, forward_nodes=forward_nodes)

    async def _parse_forward_node(
        self,
        event: AstrMessageEvent,
        node: object,
        depth: int,
    ) -> list[ForwardNodeContent]:
        """防御解析单个转发节点,返回该节点展开后的节点列表(可能为空)。

        兼容两种节点形态:
        - 标准 node 段:{"type":"node","data":{user_id,nickname,content/message}};
        - NapCat get_forward_msg 实际返回的完整消息对象(OB11Message):
          {user_id,sender:{nickname},message:[...]},无 type/data 包装。
        嵌套合并转发 / 内联 node 段展开为独立节点;结构异常或无图片时
        返回空列表(该节点被跳过)。
        """
        if not isinstance(node, dict):
            logger.warning("[onebot-client] 转发节点不是 dict,已跳过")
            return []
        # 边界:外部 JSON → 命名 TypedDict。
        payload = cast(ForwardNodePayload, node)
        data = payload.get("data")
        if isinstance(data, dict):
            user_id = data.get("user_id")
            nickname = data.get("nickname")
            uin = str(user_id) if user_id is not None else ""
            name = str(nickname) if nickname is not None else ""
            content = data.get("content")
            if not isinstance(content, list):
                content = data.get("message")
            if not isinstance(content, list):
                logger.warning("[onebot-client] 转发节点 content 不是 list,已跳过")
                return []
            return await self._expand_node_content(event, uin, name, content, depth)
        # 形态 B:NapCat 返回的完整消息对象(OB11Message)
        return await self._expand_message_node(event, cast(object, node), depth)

    async def _expand_message_node(
        self,
        event: AstrMessageEvent,
        message: object,
        depth: int,
    ) -> list[ForwardNodeContent]:
        """解析 NapCat get_forward_msg 返回的完整消息对象(OB11Message)。

        字段:user_id / sender.nickname / message(段数组);嵌套转发的段
        为 forward 段且 data.content 已由 NapCat 内联展开(parseMultMsg)。
        """
        if not isinstance(message, dict):
            logger.warning("[onebot-client] 转发消息对象不是 dict,已跳过")
            return []
        # 边界:外部 JSON → 命名 TypedDict。
        ob11 = cast(ForwardMessageNode, message)
        user_id = ob11.get("user_id")
        nickname = ""
        sender = ob11.get("sender")
        if isinstance(sender, dict):
            sender_user_id = sender.get("user_id")
            if user_id is None and sender_user_id is not None:
                user_id = sender_user_id
            sender_nickname = sender.get("nickname")
            nickname = str(sender_nickname) if sender_nickname is not None else ""
        uin = str(user_id) if user_id is not None else ""
        content = ob11.get("message")
        if not isinstance(content, list):
            logger.warning("[onebot-client] 转发消息对象 message 不是 list,已跳过")
            return []
        return await self._expand_node_content(event, uin, nickname, content, depth)

    async def _expand_node_content(
        self,
        event: AstrMessageEvent,
        uin: str,
        name: str,
        content: Sequence[object],
        depth: int,
    ) -> list[ForwardNodeContent]:
        """展开一个转发节点的内容,保持原始记录顺序。

        图片段归入当前节点;嵌套 forward / node 段按出现位置展开为独立
        节点,当前节点按需拆分为多段,使输出顺序与原文一致。深度超过
        _MAX_FORWARD_DEPTH 时截断,防止畸形记录构造深链。
        """
        if depth > _MAX_FORWARD_DEPTH:
            logger.warning("[onebot-client] 合并转发嵌套过深,已截断 depth=%d", depth)
            return []
        result: list[ForwardNodeContent] = []
        pending_images: list[Comp.BaseMessageComponent] = []
        for segment in content:
            if isinstance(segment, Comp.Image):
                pending_images.append(segment)
                continue
            if not isinstance(segment, dict):
                continue
            # 边界:外部 JSON → 命名 TypedDict。
            seg = cast(OneBotSegment, segment)
            seg_type = seg.get("type")
            if seg_type == "image":
                image = _extract_image_segment(cast(object, segment))
                if image is not None:
                    pending_images.append(image)
                continue
            if seg_type == "forward":
                data = seg.get("data")
                inline_content = (
                    cast(ForwardSegmentData, data).get("content")
                    if isinstance(data, dict)
                    else None
                )
                if isinstance(inline_content, list):
                    # NapCat(parseMultMsg)已把嵌套转发内容内联展开在
                    # data.content,直接解析,避免再次调用 get_forward_msg。
                    if pending_images:
                        result.append(
                            ForwardNodeContent(uin=uin, name=name, components=pending_images)
                        )
                        pending_images = []
                    for item in inline_content:
                        result.extend(await self._parse_forward_node(event, item, depth + 1))
                    continue
                nested = await self._expand_nested_forward(event, seg, depth)
                if not pending_images and not nested:
                    continue
                if pending_images:
                    result.append(ForwardNodeContent(uin=uin, name=name, components=pending_images))
                    pending_images = []
                result.extend(nested)
                continue
            if seg_type in ("node", "nodes"):
                if pending_images:
                    result.append(ForwardNodeContent(uin=uin, name=name, components=pending_images))
                    pending_images = []
                for node in _node_list_from_segment(seg):
                    result.extend(await self._parse_forward_node(event, node, depth + 1))
        if pending_images:
            result.append(ForwardNodeContent(uin=uin, name=name, components=pending_images))
        return result

    async def _expand_nested_forward(
        self,
        event: AstrMessageEvent,
        segment: OneBotSegment,
        depth: int,
    ) -> list[ForwardNodeContent]:
        """展开节点内的嵌套合并转发段;读取失败只跳过该段,不拖垮整个节点。"""
        if depth >= _MAX_FORWARD_DEPTH:
            logger.warning("[onebot-client] 合并转发嵌套过深,已截断 depth=%d", depth)
            return []
        forward_id = _forward_id_from_segment(segment)
        if forward_id is None:
            logger.warning("[onebot-client] 嵌套 forward 段缺少 id,已跳过")
            return []
        try:
            return await self._fetch_forward(event, forward_id, depth + 1)
        except QuotedMessageReadError as exc:
            logger.warning(
                "[onebot-client] 嵌套合并转发读取失败,已跳过 id=%s error=%s",
                forward_id,
                exc,
            )
            return []

    async def _call_action(
        self,
        event: AstrMessageEvent,
        action: str,
        error_prefix: str,
        **params: object,
    ) -> dict[str, object]:
        """执行 OneBot API 调用,统一把失败包装为可展示给用户的 QuotedMessageReadError。"""
        bot = self._resolve_bot(event)
        try:
            result = await bot.call_action(action, **params)
        except Exception as exc:
            raise QuotedMessageReadError(f"{error_prefix}:{exc}") from exc
        if not isinstance(result, dict):
            raise QuotedMessageReadError(f"{error_prefix}:响应格式异常(非 dict)")
        return cast(dict[str, object], result)

def _extract_image_segment(segment: object) -> Comp.Image | None:
    """只提取 image 段;其他段(text/face/at 等)与畸形段一律忽略。"""
    if not isinstance(segment, dict):
        return None
    # 边界:外部 JSON → 命名 TypedDict。
    seg = cast(OneBotSegment, segment)
    if seg.get("type") != "image":
        return None
    data = seg.get("data")
    if not isinstance(data, dict):
        return None
    return _image_from_segment_data(data)


def _forward_id_from_segment(segment: OneBotSegment) -> str | None:
    """从 forward 段取转发记录 ID;缺失返回 None。"""
    data = segment.get("data")
    if not isinstance(data, dict):
        return None
    # 边界:外部 JSON → 命名 TypedDict。
    forward_data = cast(ForwardSegmentData, data)
    forward_id = forward_data.get("id")
    if not forward_id:
        return None
    return forward_id


def _node_list_from_segment(segment: OneBotSegment) -> list[object]:
    """从 node / nodes 段取节点列表(node 段本身即单个节点)。"""
    if segment.get("type") == "node":
        items: list[object] = [segment]
        return items
    data = segment.get("data")
    if not isinstance(data, dict):
        return []
    # 边界:外部 JSON → 命名 TypedDict。
    node_list = cast(NodeListData, data)
    messages = node_list.get("messages")
    if isinstance(messages, list):
        return messages
    nodes = node_list.get("nodes")
    if isinstance(nodes, list):
        return nodes
    return []


def _image_from_segment_data(data: OneBotSegmentData) -> Comp.Image | None:
    """从图片段 data 中取 file/url/path/file_unique 存在键构造 Comp.Image。

    只传存在的键;全部缺失(或为空串)则返回 None,该段被跳过。
    Comp.Image 构造器要求 file 位置参数,file 键缺失时传 None(其
    convert_to_file_path 会优先使用 url)。
    """
    kwargs: dict[str, str] = {}
    for key in ("file", "url", "path", "file_unique"):
        value = data.get(key)
        if isinstance(value, str) and value:
            kwargs[key] = value
    if not kwargs:
        return None
    file_value = kwargs.pop("file", None)
    return Comp.Image(file=file_value, **kwargs)

from __future__ import annotations

import asyncio
import copy
import io
import shutil
import time
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import SessionController, session_waiter

if TYPE_CHECKING:
    from PIL import Image as PILImage

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - compatibility fallback for older AstrBot.
    get_astrbot_data_path = None

try:
    from .koharu_client import (
        IMAGE_EXTENSIONS,
        AppConfig,
        KoharuApiError,
        KoharuClient,
        PatchBody,
        extract_project_id,
        save_exported_images,
    )
except ImportError:  # AstrBot may load plugin files without package context.
    from koharu_client import (
        IMAGE_EXTENSIONS,
        AppConfig,
        KoharuApiError,
        KoharuClient,
        PatchBody,
        extract_project_id,
        save_exported_images,
    )

try:
    from .onebot_client import (
        ForwardNodeContent,
        OneBotClient,
        QuotedMessageReadError,
        QuotedMessageReader,
    )
except ImportError:  # AstrBot may load plugin files without package context.
    from onebot_client import (
        ForwardNodeContent,
        OneBotClient,
        QuotedMessageReadError,
        QuotedMessageReader,
    )


PLUGIN_NAME = "astrbot_plugin_koharu_plus"


@dataclass
class ForwardNode:
    """转发记录中一个含图节点，image_indices 指向 QuotedBatch.image_paths 的下标。"""

    uin: str
    name: str
    image_indices: list[int]


@dataclass
class QuotedBatch:
    """一次翻译请求的提取结果。forward_nodes 为 None 表示非转发；[] 表示转发但无图节点。"""

    image_paths: list[str]
    forward_nodes: list[ForwardNode] | None = None


@register(
    PLUGIN_NAME,
    "ABCwewe+CodeX",
    "使用 Koharu HTTP API 翻译聊天中的漫画图片。",
    "1.7.0",
)
class KoharuMangaTranslatorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: PluginConfig = cast(PluginConfig, config or {})
        self._translate_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._data_dir = self._resolve_data_dir()
        self._queue_semaphore = asyncio.Semaphore(self._int_conf("queue_depth") + 1)
        self._startup_task: asyncio.Task[None] | None = None

    async def initialize(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(
            "[koharu-plugin] initialized data_dir=%s api_base=%s target_language=%s",
            self._data_dir,
            self._str_conf("koharu_api_base_url"),
            self._str_conf("target_language"),
        )
        self._cleanup_output_cache()
        # 后台应用一次持久化配置；task 引用保存在实例上便于 terminate 取消。
        self._startup_task = asyncio.create_task(self._apply_config_on_startup())

    @filter.command("漫画翻译", alias={"manga_translate", "manga-translate"})
    async def manga_translate(
        self,
        event: AstrMessageEvent,
        target_language: str = "",
    ):
        """Translate manga image(s) with Koharu. Usage: /漫画翻译 [target_language] + image(s)."""

        event.stop_event()
        logger.info(
            "[koharu-plugin] command triggered sender=%s session=%s message=%r",
            event.get_sender_id(),
            event.get_session_id(),
            event.message_str,
        )
        target_language = (target_language or self._str_conf("target_language")).strip()
        batch = await self._try_extract_image_batch(event)
        if batch is None:
            return
        has_quote = _contains_quote(event.get_messages())
        logger.debug(
            "[koharu-plugin] command image extraction done count=%d forward=%s",
            len(batch.image_paths),
            batch.forward_nodes is not None,
        )

        if batch.image_paths:
            await self._run_translation(event, batch, target_language)
            return

        if has_quote:
            await event.send(
                event.plain_result(
                    "未能从被引用消息中提取到图片，请引用带图片的消息或直接发送图片。"
                )
            )
            return

        await event.send(
            event.plain_result(
                f"请在 {self._int_conf('wait_image_timeout_seconds')} 秒内发送需要翻译的漫画图片。"
                "发送“取消”可退出。"
            )
        )

        @session_waiter(
            timeout=self._int_conf("wait_image_timeout_seconds"),
            record_history_chains=False,
        )
        async def wait_for_images(
            controller: SessionController,
            next_event: AstrMessageEvent,
        ) -> None:
            next_event.stop_event()
            logger.debug(
                "[koharu-plugin] waiter received event sender=%s session=%s message=%r",
                next_event.get_sender_id(),
                next_event.get_session_id(),
                next_event.message_str,
            )
            if (next_event.message_str or "").strip().lower() in {"取消", "退出", "cancel"}:
                await next_event.send(next_event.plain_result("已取消漫画翻译。"))
                controller.stop()
                return

            next_batch = await self._try_extract_image_batch(next_event)
            if next_batch is None:
                controller.stop()
                return
            logger.debug(
                "[koharu-plugin] waiter image extraction done count=%d",
                len(next_batch.image_paths),
            )
            if not next_batch.image_paths:
                logger.debug("[koharu-plugin] waiter got no images; keep waiting")
                await next_event.send(
                    next_event.plain_result("未检测到图片，请重新发送图片或发送“取消”。")
                )
                controller.keep(
                    timeout=self._int_conf("wait_image_timeout_seconds"),
                    reset_timeout=True,
                )
                return

            await self._run_translation(next_event, next_batch, target_language)
            controller.stop()

        try:
            logger.debug("[koharu-plugin] registering session waiter for image input")
            await wait_for_images(event)
        except TimeoutError:
            logger.info("[koharu-plugin] waiter timeout")
            await event.send(event.plain_result("等待图片超时，已退出漫画翻译。"))

    @filter.command("koharu-config")
    async def koharu_config(self, event: AstrMessageEvent):
        """手动重放 Koharu 持久化配置与密钥（管线模型/提供商/字体/语言/密钥）。"""

        event.stop_event()
        logger.info(
            "[koharu-plugin] koharu-config command triggered sender=%s session=%s",
            event.get_sender_id(),
            event.get_session_id(),
        )
        await event.send(event.plain_result("正在应用 Koharu 持久化配置与密钥..."))
        try:
            async with KoharuClient(
                self._str_conf("koharu_api_base_url"),
                timeout=float(self._int_conf("http_timeout_seconds")),
                connect_timeout=float(self._int_conf("http_connect_timeout_seconds")),
            ) as client:
                await client.wait_until_ready(
                    timeout_seconds=float(self._int_conf("koharu_ready_timeout_seconds"))
                )
                result = await self._apply_config_once(client)
        except Exception as exc:
            logger.exception("koharu-config apply failed")
            await event.send(event.plain_result(f"Koharu 配置应用失败：{exc}"))
            return
        patched = (
            "、".join(result.patched_sections)
            if result.patched_sections
            else "无（与现有配置一致）"
        )
        secrets = "、".join(result.replayed_secrets) if result.replayed_secrets else "无"
        await event.send(
            event.plain_result(
                f"Koharu 配置已应用。PATCH section：{patched}；密钥重放：{secrets}"
            )
        )

    async def _run_translation(
        self,
        event: AstrMessageEvent,
        batch: QuotedBatch,
        target_language: str,
    ) -> None:
        """按队列语义执行一次翻译并发送结果（转发场景输出合并转发记录）。"""
        image_count = len(batch.image_paths)
        if self._queue_semaphore.locked():
            logger.info("[koharu-plugin] queue full; rejecting translation request")
            await event.send(
                event.plain_result(
                    f"翻译队列已满（最大等待 {self._int_conf('queue_depth')} 个），请稍后再试。"
                )
            )
            return
        await self._queue_semaphore.acquire()
        try:
            logger.info("[koharu-plugin] sending accepted message before translation")
            forward_prefix = "转发记录中的 " if batch.forward_nodes is not None else " "
            confirm_text = (
                f"已收到{forward_prefix}{image_count} 张图片，"
                f"开始调用 Koharu 翻译为 {display_language(target_language)}。"
            )
            await event.send(event.plain_result(confirm_text))
            logger.info("[koharu-plugin] accepted message sent; starting translation")
            try:
                output_paths = await self._translate_images(batch.image_paths, target_language)
            except Exception as exc:
                logger.exception("Koharu manga translation failed")
                await event.send(event.plain_result(f"漫画翻译失败：{exc}"))
                return
            logger.info(
                "[koharu-plugin] translation finished; sending output count=%d",
                len(output_paths),
            )
            if batch.forward_nodes is not None:
                try:
                    await self._send_forward_result(event, batch, output_paths)
                finally:
                    self._cleanup_current_outputs_if_needed(output_paths)
                    self._cleanup_output_cache()
            else:
                try:
                    await self._send_one_by_one(event, output_paths)
                finally:
                    self._cleanup_current_outputs_if_needed(output_paths)
                    self._cleanup_output_cache()
        finally:
            self._release_queue()

    async def _send_forward_result(
        self,
        event: AstrMessageEvent,
        batch: QuotedBatch,
        output_paths: list[str],
    ) -> None:
        """按原聊天记录格式（合并转发）发送译文图，只保留含图节点，无任何提示文字。"""
        max_send = self._int_conf("max_send_images")
        budget = max_send if max_send > 0 else len(output_paths)
        nodes: list[Comp.Node] = []
        for node in batch.forward_nodes or []:
            if budget <= 0:
                break
            images: list[Comp.BaseMessageComponent] = []
            for index in node.image_indices:
                if budget <= 0:
                    break
                if index >= len(output_paths):
                    logger.warning(
                        "[koharu-plugin] forward node image index out of range "
                        "index=%d output_count=%d",
                        index,
                        len(output_paths),
                    )
                    continue
                images.append(_image_from_path(output_paths[index]))
                budget -= 1
            if images:
                nodes.append(Comp.Node(uin=node.uin, name=node.name, content=images))
        if not nodes:
            logger.warning(
                "[koharu-plugin] no image nodes to send in forward result output_count=%d",
                len(output_paths),
            )
            return
        await event.send(event.chain_result([Comp.Nodes(nodes)]).stop_event())

    async def _send_one_by_one(self, event: AstrMessageEvent, output_paths: list[str]) -> None:
        """非转发场景：翻译结果逐张单独发送，无提示文字。"""
        max_send = self._int_conf("max_send_images")
        selected = output_paths if max_send <= 0 else output_paths[:max_send]
        direct_file_transfer = self._bool_conf("use_direct_file_transfer")
        onebot_client = OneBotClient(self.context) if direct_file_transfer else None
        failed_positions: list[int] = []
        for position, path in enumerate(selected, start=1):
            sent = False
            for attempt in range(1, 4):
                try:
                    if onebot_client is None:
                        await event.send(event.image_result(path))
                    else:
                        await onebot_client.send_image(event, path)
                    sent = True
                    break
                except Exception as exc:
                    if attempt < 3:
                        logger.warning(
                            "[koharu-plugin] image send failed; retrying "
                            "path=%s attempt=%d/3 error=%s",
                            _safe_path(path),
                            attempt,
                            exc,
                        )
                        await asyncio.sleep(1)
                    else:
                        logger.exception(
                            "[koharu-plugin] image send failed after 3 attempts "
                            "path=%s",
                            _safe_path(path),
                        )
            if not sent:
                failed_positions.append(position)
        if failed_positions:
            positions = "、".join(str(position) for position in failed_positions)
            try:
                await event.send(
                    event.plain_result(
                        f"第 {positions} 张翻译图片发送失败，已跳过。"
                    )
                )
            except Exception as exc:
                logger.warning(
                    "[koharu-plugin] failed to send image failure summary "
                    "failed_count=%d error=%s",
                    len(failed_positions),
                    exc,
                )

    async def _try_extract_image_batch(self, event: AstrMessageEvent) -> QuotedBatch | None:
        """提取图片批次;读取被引用消息失败时向用户播报错误并返回 None。"""
        try:
            return await self._extract_image_batch(event)
        except QuotedMessageReadError as exc:
            logger.info(
                "[koharu-plugin] failed to read quoted message content error=%s",
                exc,
            )
            await event.send(event.plain_result(str(exc)))
            return None

    async def _extract_image_batch(self, event: AstrMessageEvent) -> QuotedBatch:
        """提取当前消息中的图片；引用消息或合并转发按引用场景处理。"""
        messages = event.get_messages()
        logger.debug(
            "[koharu-plugin] extracting images from message_chain component_count=%d component_types=%s",
            len(messages),
            [type(component).__name__ for component in messages],
        )
        reader = QuotedMessageReader(self.context)
        raw_paths: list[str] = []
        pending_nodes: list[tuple[str, str, int, int]] = []
        is_forward = False

        for component in messages:
            if isinstance(component, Comp.Image):
                path = await self._try_convert_image_path(component)
                if path is not None:
                    raw_paths.append(path)
                continue
            if isinstance(component, Comp.Reply):
                chain = component.chain
                chain_extracted = False
                if chain:
                    for nested in chain:
                        if isinstance(nested, Comp.Image):
                            chain_extracted = True
                            path = await self._try_convert_image_path(nested)
                            if path is not None:
                                raw_paths.append(path)
                        elif isinstance(nested, Comp.Forward):
                            chain_extracted = True
                            is_forward = True
                            await self._collect_forward_node_images(
                                reader,
                                event,
                                nested.id,
                                raw_paths,
                                pending_nodes,
                            )
                # chain 为空或只有占位内容(如合并转发渲染成的 "[聊天记录]" 文本)
                # 时,按被引用消息 ID 兜底拉取,兼容引用合并转发记录的场景。
                if not chain_extracted and component.id:
                    if await self._collect_quoted_fallback_images(
                        reader,
                        event,
                        component.id,
                        raw_paths,
                        pending_nodes,
                    ):
                        is_forward = True
                continue
            if isinstance(component, Comp.Forward):
                is_forward = True
                await self._collect_forward_node_images(
                    reader,
                    event,
                    component.id,
                    raw_paths,
                    pending_nodes,
                )

        unique_paths, index_map = _dedupe_mapped(raw_paths)
        forward_nodes = [
            ForwardNode(
                uin=uin,
                name=name,
                image_indices=[
                    index_map[index] for index in range(start, start + count)
                ],
            )
            for uin, name, start, count in pending_nodes
        ]
        logger.debug(
            "[koharu-plugin] extracted image paths count=%d paths=%s",
            len(unique_paths),
            [_safe_path(path) for path in unique_paths],
        )
        return QuotedBatch(
            image_paths=unique_paths,
            forward_nodes=forward_nodes if is_forward else None,
        )

    async def _collect_forward_node_images(
        self,
        reader: QuotedMessageReader,
        event: AstrMessageEvent,
        forward_id: str,
        raw_paths: list[str],
        pending_nodes: list[tuple[str, str, int, int]],
    ) -> None:
        """读取合并转发记录,收集各含图节点的图片路径(嵌套转发已展开)。

        整体读取失败时抛 QuotedMessageReadError,由调用方提示用户。
        """
        contents = await reader.fetch_forward(event, forward_id)
        await self._record_forward_contents(contents, raw_paths, pending_nodes)

    async def _record_forward_contents(
        self,
        contents: list[ForwardNodeContent],
        raw_paths: list[str],
        pending_nodes: list[tuple[str, str, int, int]],
    ) -> None:
        """把转发节点内容记录为 raw_paths 片段与 pending_nodes 下标区间。"""
        for content in contents:
            node_paths: list[str] = []
            for component in content.components:
                if not isinstance(component, Comp.Image):
                    continue
                path = await self._try_convert_image_path(component)
                if path is not None:
                    node_paths.append(path)
            if not node_paths:
                continue
            start = len(raw_paths)
            raw_paths.extend(node_paths)
            pending_nodes.append((content.uin, content.name, start, len(node_paths)))

    async def _collect_quoted_fallback_images(
        self,
        reader: QuotedMessageReader,
        event: AstrMessageEvent,
        message_id: str | int,
        raw_paths: list[str],
        pending_nodes: list[tuple[str, str, int, int]],
    ) -> bool:
        """Reply.chain 为空或无可提取内容时,按被引用消息 ID 拉取消息兜底。

        被引用消息本身是合并转发时,其 forward / node 段会展开为转发节点。
        返回是否检测到合并转发(调用方据此标记转发输出)。
        整体读取失败时抛 QuotedMessageReadError,由调用方提示用户。
        """
        content = await reader.fetch_quoted_message(event, str(message_id))
        for component in content.images:
            path = await self._try_convert_image_path(component)
            if path is not None:
                raw_paths.append(path)
        if content.forward_nodes:
            await self._record_forward_contents(content.forward_nodes, raw_paths, pending_nodes)
            return True
        return False

    async def _convert_image_path(self, component: Comp.Image) -> str:
        logger.debug(
            "[koharu-plugin] converting image component file=%r url=%r path=%r",
            component.file,
            component.url,
            component.path,
        )
        path = await component.convert_to_file_path()
        logger.debug("[koharu-plugin] image component converted path=%s", _safe_path(path))
        return path

    async def _try_convert_image_path(self, component: Comp.Image) -> str | None:
        """转换图片为本地路径;单张失败时记录日志并返回 None(不拖垮整批)。"""
        try:
            return await self._convert_image_path(component)
        except Exception as exc:
            logger.warning(
                "[koharu-plugin] failed to convert image component error=%s",
                exc,
            )
            return None

    async def _translate_images(
        self,
        image_paths: list[str],
        target_language: str,
    ) -> list[str]:
        logger.debug(
            "[koharu-plugin] translate requested image_count=%d target_language=%s",
            len(image_paths),
            target_language,
        )
        max_images = self._int_conf("max_images_per_request")
        if max_images > 0 and len(image_paths) > max_images:
            raise ValueError(f"单次最多支持 {max_images} 张图片，请减少图片数量后重试。")

        logger.debug("[koharu-plugin] waiting for translate lock")
        async with self._translate_lock:
            request_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            output_dir = self._data_dir / "outputs" / request_id
            project_name = f"astrbot-koharu-{request_id}"
            logger.debug(
                "[koharu-plugin] translate lock acquired request_id=%s project=%s output_dir=%s api_base=%s",
                request_id,
                project_name,
                output_dir,
                self._str_conf("koharu_api_base_url"),
            )

            async with KoharuClient(
                self._str_conf("koharu_api_base_url"),
                timeout=float(self._int_conf("http_timeout_seconds")),
                connect_timeout=float(self._int_conf("http_connect_timeout_seconds")),
            ) as client:
                logger.debug("[koharu-plugin] waiting koharu ready")
                await client.wait_until_ready(
                    timeout_seconds=float(self._int_conf("koharu_ready_timeout_seconds"))
                )
                await self._ensure_config_applied(client)
                logger.debug("[koharu-plugin] closing existing koharu project before creating a new one")
                closed_existing = await client.close_project_if_any()
                logger.debug(
                    "[koharu-plugin] close existing project result closed=%s",
                    closed_existing,
                )
                logger.debug("[koharu-plugin] koharu ready; creating project")
                project = await client.create_project(project_name)
                logger.debug("[koharu-plugin] project created response=%s", project)
                project_name_from_response = extract_project_id(project)
                logger.debug(
                    "[koharu-plugin] project identity=%s",
                    project_name_from_response,
                )
                try:
                    (
                        cached_image_paths,
                        upload_cache_dir,
                        source_white_ratios,
                    ) = self._cache_ordered_upload_images(image_paths)
                    try:
                        logger.debug(
                            "[koharu-plugin] uploading pages count=%d cache_dir=%s paths=%s",
                            len(cached_image_paths),
                            upload_cache_dir,
                            [_safe_path(path) for path in cached_image_paths],
                        )
                        pages = await client.create_pages(cached_image_paths)
                    finally:
                        self._delete_upload_cache(upload_cache_dir)
                    logger.debug("[koharu-plugin] pages uploaded response=%s", pages)
                    steps = await self._resolve_pipeline_steps(client)
                    logger.debug("[koharu-plugin] resolved pipeline steps=%s", steps)
                    if not steps:
                        raise KoharuApiError(
                            "未能确定 Koharu pipeline steps。请在插件配置中填写 "
                            "pipeline_steps，或先在 Koharu 配置中选择 pipeline 引擎。"
                        )
                    logger.info("[koharu-plugin] starting pipeline")
                    operation_id = await client.start_pipeline(steps)
                    logger.info("[koharu-plugin] pipeline started operation_id=%s", operation_id)
                    operation = await client.wait_operation(
                        operation_id,
                        timeout_seconds=float(self._int_conf("pipeline_timeout_seconds")),
                        interval_seconds=float(self._int_conf("operation_poll_interval_seconds")),
                    )
                    logger.info("[koharu-plugin] pipeline completed operation=%s", operation)
                    logger.debug("[koharu-plugin] exporting rendered project")
                    content, content_type = await client.export_project("rendered")
                    logger.debug(
                        "[koharu-plugin] export received bytes=%d content_type=%s",
                        len(content),
                        content_type,
                    )
                    content, content_type = await self._guard_rendered_export(
                        client,
                        request_id,
                        content,
                        content_type,
                        source_white_ratios,
                    )
                    output_paths = save_exported_images(
                        content,
                        content_type,
                        output_dir,
                        base_name="translated",
                    )
                    output_paths = self._compress_output_images_if_enabled(
                        output_paths,
                        output_dir,
                    )
                    logger.debug(
                        "[koharu-plugin] export saved output_count=%d paths=%s",
                        len(output_paths),
                        [_safe_path(path) for path in output_paths],
                    )
                    return output_paths
                finally:
                    if self._bool_conf("close_project_after_export"):
                        try:
                            logger.debug("[koharu-plugin] closing koharu project")
                            await client.close_project()
                            logger.debug("[koharu-plugin] koharu project closed")
                        except Exception as exc:
                            logger.warning(f"Failed to close Koharu project: {exc}")
                    if (
                        self._bool_conf("delete_project_after_export")
                        and project_name_from_response
                    ):
                        try:
                            logger.debug(
                                "[koharu-plugin] deleting koharu project name=%s",
                                project_name_from_response,
                            )
                            await client.delete_project(str(project_name_from_response))
                            logger.debug("[koharu-plugin] koharu project deleted")
                        except Exception as exc:
                            logger.warning(f"Failed to delete Koharu project: {exc}")

    async def _apply_config_on_startup(self) -> None:
        """插件启动后后台应用一次持久化配置（等 Koharu 就绪；失败重试，不阻塞启动）。"""
        for attempt in range(1, 4):
            try:
                async with KoharuClient(
                    self._str_conf("koharu_api_base_url"),
                    timeout=float(self._int_conf("http_timeout_seconds")),
                    connect_timeout=float(self._int_conf("http_connect_timeout_seconds")),
                ) as client:
                    await client.wait_until_ready(
                        timeout_seconds=float(self._int_conf("koharu_ready_timeout_seconds"))
                    )
                    result = await self._apply_config_once(client)
                    logger.info(
                        "[koharu-plugin] persistent config applied on startup "
                        "patched=%s secrets=%s",
                        result.patched_sections,
                        result.replayed_secrets,
                    )
                return
            except Exception as exc:
                logger.warning(
                    "[koharu-plugin] startup config apply attempt %d failed: %s",
                    attempt,
                    exc,
                )
                await asyncio.sleep(5 * attempt)

    async def _ensure_config_applied(self, client: KoharuClient) -> None:
        """翻译前确保持久化配置已应用；失败仅告警，不阻断翻译。"""
        try:
            result = await self._apply_config_once(client)
            if result.patched_sections or result.replayed_secrets:
                logger.debug(
                    "[koharu-plugin] applied persistent config patched=%s secrets=%s",
                    result.patched_sections,
                    result.replayed_secrets,
                )
        except Exception as exc:
            logger.warning("[koharu-plugin] failed to apply persistent config: %s", exc)

    async def _apply_config_once(self, client: KoharuClient) -> ConfigApplyResult:
        """GET /config 全量 → 组装期望值 → 差异 section 整段 PATCH → 重放密钥。

        用 _config_lock 与启动任务/手动指令串行化，避免与翻译流程并发 PATCH
        造成「翻译前一刻服务端模型被改回插件默认」的竞态。
        """
        async with self._config_lock:
            current = await client.get_config()
            expected = build_expected_config(current, self.config)
            patched: list[str] = []
            for section in ("pipeline", "providers", "typesetting"):
                if section in config_differs(current, expected):
                    section_value = cast(dict[str, object], expected).get(section)
                    patch = cast(PatchBody, {section: section_value})
                    await client.patch_config(patch)
                    patched.append(section)
            replayed = await self._replay_api_keys(client)
            return ConfigApplyResult(patched_sections=patched, replayed_secrets=replayed)

    async def _replay_api_keys(self, client: KoharuClient) -> list[str]:
        """把插件配置里的 api key 重放到当前选中的提供商 keyring（幂等，重启即丢需重放）。

        key 跟随 translation_provider 的选择：选 deepseek 写入 deepseek keyring，
        选 openai-compatible 写入 openai-compatible keyring。
        """
        provider = str(self.config.get("translation_provider") or "").strip()
        api_key = str(self.config.get("api_key") or "").strip()
        if provider in _PROVIDER_IDS and api_key:
            await client.set_provider_secret(provider, api_key)
            return [provider]
        return []

    async def _resolve_pipeline_steps(self, client: KoharuClient) -> list[str]:
        configured = self._str_conf("pipeline_steps").strip()
        if configured:
            steps = [step.strip() for step in configured.split(",") if step.strip()]
            logger.debug("[koharu-plugin] using configured pipeline_steps=%s", steps)
            return steps
        logger.debug("[koharu-plugin] pipeline_steps empty; reading koharu /config")
        steps = await client.get_pipeline_steps_from_config()
        logger.debug("[koharu-plugin] pipeline steps from koharu config=%s", steps)
        return steps

    def _cache_ordered_upload_images(
        self, image_paths: list[str]
    ) -> tuple[list[str], Path, list[float | None]]:
        """缓存上传图片副本,并顺带计算每张源图的白占比基准(导出守卫用)。

        返回 (缓存路径列表, 缓存目录, 源图白占比列表)。白占比与缓存路径
        按下标对齐;源图无法解析时对应基准为 None(守卫走绝对阈值兜底)。
        """
        upload_cache_dir = self._data_dir / "uploads" / uuid.uuid4().hex
        cached_paths: list[str] = []
        source_white_ratios: list[float | None] = []
        try:
            upload_cache_dir.mkdir(parents=True, exist_ok=False)
            for index, image_path in enumerate(image_paths, start=1):
                source = Path(image_path)
                source_white_ratios.append(_source_white_ratio(source))
                suffix = source.suffix or ".jpg"
                target = upload_cache_dir / f"{index}{suffix}"
                shutil.copy2(source, target)
                cached_paths.append(str(target))
            logger.debug(
                "[koharu-plugin] cached ordered upload images dir=%s paths=%s source_white=%s",
                upload_cache_dir,
                [_safe_path(path) for path in cached_paths],
                source_white_ratios,
            )
            return cached_paths, upload_cache_dir, source_white_ratios
        except Exception:
            self._delete_upload_cache(upload_cache_dir)
            raise

    def _delete_upload_cache(self, upload_cache_dir: Path) -> None:
        try:
            if upload_cache_dir.exists():
                shutil.rmtree(upload_cache_dir)
                logger.debug("[koharu-plugin] deleted upload cache dir=%s", upload_cache_dir)
        except Exception as exc:
            logger.warning(
                "[koharu-plugin] failed to delete upload cache dir=%s error=%s",
                upload_cache_dir,
                exc,
            )

    async def _guard_rendered_export(
        self,
        client: KoharuClient,
        request_id: str,
        content: bytes,
        content_type: str,
        source_white_ratios: Sequence[float | None],
    ) -> tuple[bytes, str]:
        """成品守卫:检测渲染缺背景层(白底只有文字)并重试导出一次。

        0.66 渲染器在场景快照缺 source 资产时静默跳过背景层,导出 PNG
        呈大面积透明;压缩铺白后即 08-14 事故的「白底只有文字」成品。
        透明占比是硬信号,白占比与源图基准(缺失时用绝对阈值兜底)对比
        是软信号。命中则告警并重试导出;仍失败的两次导出中返回较优的
        一次(宁可发图也不中断翻译),并记录 error 附 request_id。
        """
        if not self._bool_conf("guard_blank_retry"):
            return content, content_type
        try:
            suspicious, stats = _render_guard_issues(
                content,
                content_type,
                source_white_ratios,
                self._float_conf("guard_max_white_absolute"),
            )
        except Exception as exc:
            logger.warning(
                "[koharu-plugin] export guard: unable to inspect render "
                "request_id=%s error=%s; skipping guard",
                request_id,
                exc,
            )
            return content, content_type
        if not suspicious:
            return content, content_type
        logger.warning(
            "[koharu-plugin] export guard: suspicious render request_id=%s "
            "transparent=%.2f white=%.2f source_white=%s",
            request_id,
            max(transparent for transparent, _ in stats),
            max(white for _, white in stats),
            list(source_white_ratios),
        )
        await asyncio.sleep(1)
        try:
            retry_content, retry_content_type = await client.export_project("rendered")
            retry_suspicious, retry_stats = _render_guard_issues(
                retry_content,
                retry_content_type,
                source_white_ratios,
                self._float_conf("guard_max_white_absolute"),
            )
        except Exception as exc:
            logger.error(
                "[koharu-plugin] export guard: retry export failed "
                "request_id=%s error=%s; keeping first export",
                request_id,
                exc,
            )
            return content, content_type
        if not retry_suspicious:
            logger.info(
                "[koharu-plugin] export guard: retry recovered request_id=%s; using retry export",
                request_id,
            )
            return retry_content, retry_content_type
        max_white_absolute = self._float_conf("guard_max_white_absolute")
        first_score = _render_guard_score(stats, source_white_ratios, max_white_absolute)
        retry_score = _render_guard_score(
            retry_stats, source_white_ratios, max_white_absolute
        )
        better = (
            (retry_content, retry_content_type)
            if retry_score < first_score
            else (content, content_type)
        )
        logger.error(
            "[koharu-plugin] export guard: retry still suspicious request_id=%s "
            "first_score=%.3f retry_score=%.3f; sending better export",
            request_id,
            first_score,
            retry_score,
        )
        return better

    def _compress_output_images_if_enabled(
        self,
        output_paths: list[str],
        output_dir: Path,
    ) -> list[str]:
        if not self._bool_conf("compress_return_images"):
            return output_paths

        image_format = self._str_conf("return_image_format").strip().lower()
        if image_format not in {"jpg", "jpeg", "webp"}:
            logger.warning(
                "[koharu-plugin] invalid return_image_format=%r; fallback to webp",
                image_format,
            )
            image_format = "webp"

        extension = ".jpg" if image_format in {"jpg", "jpeg"} else ".webp"
        pillow_format = "JPEG" if extension == ".jpg" else "WEBP"
        quality = min(100, max(1, self._int_conf("return_image_quality")))

        logger.info(
            "[koharu-plugin] compressing return images count=%d format=%s quality=%d",
            len(output_paths),
            extension.lstrip("."),
            quality,
        )
        compressed_paths: list[str] = []
        for index, output_path in enumerate(output_paths, start=1):
            source = Path(output_path)
            target = output_dir / f"{source.stem}.compressed-{index}{extension}"
            try:
                _compress_image(source, target, pillow_format, quality)
                compressed_paths.append(str(target))
                try:
                    source.unlink()
                except OSError as exc:
                    logger.debug(
                        "[koharu-plugin] failed to delete uncompressed output path=%s error=%s",
                        source,
                        exc,
                    )
            except Exception as exc:
                logger.warning(
                    "[koharu-plugin] failed to compress output image path=%s error=%s; "
                    "using original image",
                    source,
                    exc,
                )
                compressed_paths.append(str(source))
        return compressed_paths

    def _cleanup_current_outputs_if_needed(self, output_paths: list[str]) -> None:
        if self._str_conf("result_retention_policy") != "none":
            return
        outputs_root = (self._data_dir / "outputs").resolve()
        for output_path in output_paths:
            path = Path(output_path)
            try:
                resolved = path.resolve()
                if not _is_relative_to(resolved, outputs_root):
                    logger.warning(
                        "[koharu-plugin] skip deleting output outside cache path=%s",
                        resolved,
                    )
                    continue
                if resolved.exists():
                    resolved.unlink()
                    logger.debug("[koharu-plugin] deleted non-retained output=%s", resolved)
                self._remove_empty_parents(resolved.parent, outputs_root)
            except Exception as exc:
                logger.warning(
                    "[koharu-plugin] failed to delete non-retained output %s: %s",
                    path,
                    exc,
                )

    def _cleanup_output_cache(self) -> None:
        policy = self._str_conf("result_retention_policy")
        outputs_root = self._data_dir / "outputs"
        if policy == "forever" or not outputs_root.exists():
            return
        if policy == "none":
            retention_seconds = 0
        else:
            retention_days = max(0, self._int_conf("result_retention_days"))
            retention_seconds = retention_days * 86400

        cutoff = time.time() - retention_seconds
        for child in outputs_root.iterdir():
            try:
                if child.stat().st_mtime > cutoff:
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                    logger.debug("[koharu-plugin] deleted expired output directory=%s", child)
                else:
                    child.unlink()
                    logger.debug("[koharu-plugin] deleted expired output file=%s", child)
            except Exception as exc:
                logger.warning(
                    "[koharu-plugin] failed to delete cached output %s: %s",
                    child,
                    exc,
                )

    def _remove_empty_parents(self, start: Path, stop: Path) -> None:
        current = start.resolve()
        stop = stop.resolve()
        while _is_relative_to(current, stop) and current != stop:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def _resolve_data_dir(self) -> Path:
        if get_astrbot_data_path is not None:
            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        return Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME

    def _raw_config_value(self, key: str) -> str | int | float | bool:
        """取配置值；缺失时回退到默认值。默认值缺失视为配置键错误。"""
        value = self.config.get(key)
        if value is not None:
            return value
        default = DEFAULT_CONFIG.get(key)
        if default is None:
            raise KeyError(f"unknown plugin config key: {key}")
        return default

    def _str_conf(self, key: str) -> str:
        return str(self._raw_config_value(key))

    def _int_conf(self, key: str) -> int:
        value = self._raw_config_value(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            default = DEFAULT_CONFIG.get(key)
            if default is None:
                raise
            return int(default)

    def _float_conf(self, key: str) -> float:
        value = self._raw_config_value(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            default = DEFAULT_CONFIG.get(key)
            if default is None:
                raise
            return float(default)

    def _bool_conf(self, key: str) -> bool:
        value = self._raw_config_value(key)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}

    def _release_queue(self) -> None:
        """Release a queue slot."""
        self._queue_semaphore.release()

    async def terminate(self) -> None:
        task = self._startup_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class PluginConfig(TypedDict):
    """插件配置键值类型，与 _conf_schema.json 保持一致。"""

    koharu_api_base_url: str
    target_language: str
    pipeline_steps: str
    system_prompt: str
    wait_image_timeout_seconds: int
    koharu_ready_timeout_seconds: int
    pipeline_timeout_seconds: int
    operation_poll_interval_seconds: int
    http_timeout_seconds: int
    http_connect_timeout_seconds: int
    max_images_per_request: int
    max_send_images: int
    use_direct_file_transfer: bool
    queue_depth: int
    compress_return_images: bool
    return_image_format: str
    return_image_quality: int
    close_project_after_export: bool
    delete_project_after_export: bool
    guard_blank_retry: bool
    guard_max_white_absolute: float
    result_retention_policy: str
    result_retention_days: int
    # --- Koharu 0.66 持久化配置（PATCH /config） ---
    pipeline_ocr_model: str
    pipeline_inpainting_model: str
    pipeline_inpainting_prompt: str
    pipeline_inpainting_negative_prompt: str
    pipeline_detection_text_threshold: float
    pipeline_detection_bubble_threshold: float
    pipeline_detection_panel_threshold: float
    translation_provider: str
    translation_model: str
    openai_compatible_base_url: str
    api_key: str
    font_families: str


DEFAULT_CONFIG: PluginConfig = {
    "koharu_api_base_url": "http://koharu-headless:4000/api/v1",
    "target_language": "zh-CN",
    "pipeline_steps": "full",
    "system_prompt": "",
    "wait_image_timeout_seconds": 120,
    "koharu_ready_timeout_seconds": 60,
    "pipeline_timeout_seconds": 900,
    "operation_poll_interval_seconds": 2,
    "http_timeout_seconds": 120,
    "http_connect_timeout_seconds": 10,
    "max_images_per_request": 20,
    "max_send_images": 0,
    "use_direct_file_transfer": False,
    "queue_depth": 3,
    "compress_return_images": False,
    "return_image_format": "webp",
    "return_image_quality": 85,
    "close_project_after_export": True,
    "delete_project_after_export": True,
    "guard_blank_retry": True,
    "guard_max_white_absolute": 0.9,
    "result_retention_policy": "days",
    "result_retention_days": 7,
    # --- Koharu 0.66 持久化配置默认值（留空=不覆盖服务端对应字段） ---
    "pipeline_ocr_model": "",
    "pipeline_inpainting_model": "",
    "pipeline_inpainting_prompt": "",
    "pipeline_inpainting_negative_prompt": "",
    "pipeline_detection_text_threshold": -1.0,
    "pipeline_detection_bubble_threshold": -1.0,
    "pipeline_detection_panel_threshold": -1.0,
    "translation_provider": "deepseek",
    "translation_model": "deepseek-v4-flash",
    "openai_compatible_base_url": "",
    "api_key": "",
    "font_families": "CCWildWords,Adobe 黑体 Std",
}


def _dedupe_mapped(paths: list[str]) -> tuple[list[str], dict[int, int]]:
    """去重保序，返回（唯一列表, 原位置 → 唯一下标映射）。

    重复路径映射到首次出现的唯一下标（同一张图只翻译一次），
    保证 index_map 对每个原位置都有值。
    """
    first_index: dict[str, int] = {}
    unique: list[str] = []
    index_map: dict[int, int] = {}
    for index, path in enumerate(paths):
        if path not in first_index:
            first_index[path] = len(unique)
            unique.append(path)
        index_map[index] = first_index[path]
    return unique, index_map


def _contains_quote(messages: list[Comp.BaseMessageComponent]) -> bool:
    """消息链顶层是否含引用(Reply)或合并转发(Forward)组件。"""
    return any(isinstance(component, (Comp.Reply, Comp.Forward)) for component in messages)


def _safe_path(path: str) -> str:
    try:
        return str(Path(path))
    except Exception:
        return str(path)


def _image_from_path(path: str) -> Comp.Image:
    """将本地图片路径构造为 Comp.Image（SDK 未类型化成员的边界 helper，唯一 cast 点之一）。"""
    from_file_system = getattr(Comp.Image, "fromFileSystem")
    return cast(Comp.Image, from_file_system(path))


def _compress_image(source: Path, target: Path, image_format: str, quality: int) -> None:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is required when compress_return_images is enabled") from exc

    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        if image_format == "JPEG":
            image = _prepare_jpeg_image(image)
            image.save(
                target,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            return

        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if _image_has_alpha(image) else "RGB")

        image.save(
            target,
            "WEBP",
            quality=quality,
            method=6,
        )


def _prepare_jpeg_image(image: PILImage.Image) -> PILImage.Image:
    from PIL import Image

    if not _image_has_alpha(image):
        return image.convert("RGB")

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


# --- 成品守卫（渲染缺背景层检测） ---------------------------------------------------

# 渲染缺背景层的硬信号阈值:导出 PNG 透明占比超过该值即判定异常。
_GUARD_TRANSPARENT_LIMIT = 0.05
# 软信号阈值:渲染白占比超过源图基准该余量即判定异常(纯白页面靠基准免疫误报)。
_GUARD_WHITE_EXCESS_LIMIT = 0.30


def _render_quality_stats(image: PILImage.Image) -> tuple[float, float]:
    """返回 (透明占比, 铺白后白像素占比)。0.66 渲染 PNG 正常时应接近 (0, 源图白占比)。"""
    from PIL import Image

    rgba = image.convert("RGBA")
    a = rgba.getchannel("A")
    total = a.width * a.height
    transparent = 1.0 - sum(a.histogram()[1:]) / total
    rgb = Image.new("RGB", rgba.size, (255, 255, 255))
    rgb.paste(rgba, mask=a)
    hist = rgb.convert("L").histogram()
    white = sum(hist[241:]) / total
    return transparent, white


def _source_white_ratio(image_path: Path) -> float | None:
    """源图白占比基准(导出守卫用);解析失败返回 None(守卫走绝对阈值兜底)。"""
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            return _render_quality_stats(image)[1]
    except Exception as exc:
        logger.debug(
            "[koharu-plugin] failed to compute source white ratio path=%s error=%s",
            image_path,
            exc,
        )
        return None


def _export_images_from_content(content: bytes, content_type: str) -> list[PILImage.Image]:
    """把导出字节解析为图片列表(zip 多页或单图),用于成品守卫质检。"""
    from PIL import Image

    images: list[PILImage.Image] = []
    if "zip" in content_type.lower() or content.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                if Path(member.filename).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                with archive.open(member) as source:
                    images.append(Image.open(io.BytesIO(source.read())))
    else:
        images.append(Image.open(io.BytesIO(content)))
    return images


def _render_guard_issues(
    content: bytes,
    content_type: str,
    source_white_ratios: Sequence[float | None],
    max_white_absolute: float,
) -> tuple[bool, list[tuple[float, float]]]:
    """检查渲染导出是否缺背景层(透明占比硬信号 + 白占比超基准软信号)。

    返回 (是否可疑, 每页 (transparent, white) 统计)。无法解析的页面跳过
    不计入可疑——守卫宁可漏报也不阻断正常导出;全部无法解析视为不可疑。
    """
    try:
        images = _export_images_from_content(content, content_type)
    except Exception as exc:
        logger.warning(
            "[koharu-plugin] export guard: cannot parse render content_type=%s error=%s; "
            "treating as not suspicious",
            content_type,
            exc,
        )
        return False, []
    stats: list[tuple[float, float]] = []
    baselines: list[float | None] = []
    for page_index, image in enumerate(images):
        baseline = (
            source_white_ratios[page_index]
            if page_index < len(source_white_ratios)
            else None
        )
        try:
            stats.append(_render_quality_stats(image))
            baselines.append(baseline)
        except Exception as exc:
            logger.debug(
                "[koharu-plugin] export guard: skipped unreadable page index=%d error=%s",
                page_index,
                exc,
            )
    if not stats:
        return False, stats
    suspicious = False
    for (transparent, white), baseline in zip(stats, baselines):
        if transparent > _GUARD_TRANSPARENT_LIMIT:
            suspicious = True
            continue
        if baseline is not None:
            if white > baseline + _GUARD_WHITE_EXCESS_LIMIT:
                suspicious = True
        elif white > max_white_absolute:
            suspicious = True
    return suspicious, stats


def _render_guard_score(
    stats: list[tuple[float, float]],
    source_white_ratios: Sequence[float | None],
    max_white_absolute: float,
) -> float:
    """导出批次的「缺背景层」严重度评分:透明占比 + 白占比超出基准的余量。

    分值越低越接近源图,用于两次异常导出中挑选较优者。
    """
    score = 0.0
    for index, (transparent, white) in enumerate(stats):
        baseline = (
            source_white_ratios[index] if index < len(source_white_ratios) else None
        )
        expected = baseline if baseline is not None else max_white_absolute
        score += transparent + max(0.0, white - expected)
    return score


def _image_has_alpha(image: PILImage.Image) -> bool:
    return image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


# --- Koharu 0.66 持久化配置（语言目录 / config 组装）-----------------------------

# 服务端支持的 provider id（koharu_translator::Provider，wire 名）。
_PROVIDER_IDS: tuple[str, ...] = (
    "atlas-cloud",
    "openai",
    "gemini",
    "claude",
    "deepseek",
    "openai-compatible",
    "openrouter",
    "lm-studio",
    "deepl",
    "google-cloud-translation",
    "caiyun",
)

# 0.66 服务端语言目录（koharu_translator::Language，canonical tag → 显示名）。
# 展示用英文名（与语言.rs 的 to_string 一致）。
_LANGUAGE_NAME_BY_TAG: dict[str, str] = {
    "zh-CN": "Simplified Chinese",
    "en-US": "English",
    "fr-FR": "French",
    "pt-PT": "Portuguese",
    "pt-BR": "Brazilian Portuguese",
    "es-ES": "Spanish",
    "ja-JP": "Japanese",
    "tr-TR": "Turkish",
    "ru-RU": "Russian",
    "ar-SA": "Arabic",
    "ko-KR": "Korean",
    "th-TH": "Thai",
    "it-IT": "Italian",
    "de-DE": "German",
    "vi-VN": "Vietnamese",
    "ms-MY": "Malay",
    "id-ID": "Indonesian",
    "fil-PH": "Filipino",
    "hi-IN": "Hindi",
    "zh-TW": "Traditional Chinese",
    "pl-PL": "Polish",
    "cs-CZ": "Czech",
    "nl-NL": "Dutch",
    "km-KH": "Khmer",
    "my-MM": "Burmese",
    "fa-IR": "Persian",
    "gu-IN": "Gujarati",
    "ur-PK": "Urdu",
    "te-IN": "Telugu",
    "mr-IN": "Marathi",
    "he-IL": "Hebrew",
    "bn-BD": "Bengali",
    "bg-BG": "Bulgarian",
    "ta-IN": "Tamil",
    "uk-UA": "Ukrainian",
    "bo-CN": "Tibetan",
    "kk-KZ": "Kazakh",
    "mn-MN": "Mongolian",
    "ug-CN": "Uyghur",
    "yue-HK": "Cantonese",
    "be-BY": "Belarusian",
    "hu-HU": "Hungarian",
}

# canonical tag / 别名（serialize 接受串）/ 显示名 → canonical tag（全部 lowercase）。
_LANGUAGE_TAG_BY_ALIAS: dict[str, str] = {
    **{tag.lower(): tag for tag in _LANGUAGE_NAME_BY_TAG},
    **{name.lower(): tag for tag, name in _LANGUAGE_NAME_BY_TAG.items()},
    # 别名（language.rs 的 serialize 额外串）
    "zh": "zh-CN",
    "zh-hans": "zh-CN",
    "en": "en-US",
    "fr": "fr-FR",
    "pt": "pt-PT",
    "es": "es-ES",
    "ja": "ja-JP",
    "tr": "tr-TR",
    "ru": "ru-RU",
    "ar": "ar-SA",
    "ko": "ko-KR",
    "th": "th-TH",
    "it": "it-IT",
    "de": "de-DE",
    "vi": "vi-VN",
    "ms": "ms-MY",
    "id": "id-ID",
    "fil": "fil-PH",
    "tl": "fil-PH",
    "hi": "hi-IN",
    "zh-hant": "zh-TW",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "nl": "nl-NL",
    "km": "km-KH",
    "my": "my-MM",
    "fa": "fa-IR",
    "gu": "gu-IN",
    "ur": "ur-PK",
    "te": "te-IN",
    "mr": "mr-IN",
    "he": "he-IL",
    "bn": "bn-BD",
    "bg": "bg-BG",
    "ta": "ta-IN",
    "uk": "uk-UA",
    "bo": "bo-CN",
    "kk": "kk-KZ",
    "mn": "mn-MN",
    "ug": "ug-CN",
    "yue": "yue-HK",
    "be": "be-BY",
    "hu": "hu-HU",
}

# 展示文案特例：中文界面直接用中文名。
_LANGUAGE_DISPLAY_ZH: dict[str, str] = {
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
}


def normalize_language(raw: str) -> str | None:
    """把配置值规范化为 0.66 服务端接受的 BCP47 tag。

    接受 canonical tag / 别名（zh、zh-Hans）/ 显示名（"Simplified Chinese"），
    大小写不敏感；无法识别返回 None（调用方跳过覆盖，避免 PATCH 422）。
    """
    lowered = raw.strip().lower()
    return _LANGUAGE_TAG_BY_ALIAS.get(lowered)


def display_language(raw: str) -> str:
    """把语言配置值转换为用户可读的展示文案（zh-CN → 简体中文）。"""
    tag = normalize_language(raw)
    if tag is None:
        return raw.strip()
    return _LANGUAGE_DISPLAY_ZH.get(tag) or _LANGUAGE_NAME_BY_TAG[tag]


@dataclass
class ConfigApplyResult:
    """一次持久化配置应用的结果。"""

    patched_sections: list[str]
    replayed_secrets: list[str]


def _cfg_str(cfg: Mapping[str, object], key: str, default: str = "") -> str:
    """安全读取字符串配置：None/非 str（旧配置缺键或值为 None）回落默认。"""
    value = cfg.get(key)
    return value if isinstance(value, str) else default


def _cfg_float(cfg: Mapping[str, object], key: str, default: float) -> float:
    """安全读取 float 配置：兼容数字与数字字符串，None/错型回落默认。"""
    value = cfg.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _is_valid_url(raw: str) -> bool:
    """宽松 URL 校验：必须带 scheme 与 netloc（服务端 base_url 是 Url 类型，非法会 422）。"""
    try:
        parsed = urlparse(raw)
        return bool(parsed.scheme and parsed.netloc)
    except ValueError:
        return False




def build_expected_config(current: AppConfig, cfg: Mapping[str, object]) -> AppConfig:
    """从服务端现有 config 拷贝，覆盖插件已配置的字段。

    语义：配置项**留空/默认未配置值 = 不覆盖**服务端对应字段；**非空默认值
    （如 target_language=zh-CN、translation_provider=deepseek）即声明值**，
    会在启动/翻译前强制对齐服务端。0.66 PATCH /config 是「顶层稀疏、
    section 整段替换」：期望值必须以现有 config 为底，只改插件声明的字段。
    """
    expected = copy.deepcopy(current)
    pipeline = expected.get("pipeline") or {}
    expected["pipeline"] = pipeline

    # --- detection/ocr/inpainting 模型选择 ---
    ocr_model = _cfg_str(cfg, "pipeline_ocr_model").strip()
    if ocr_model:
        ocr = pipeline.get("ocr") or {}
        ocr["model"] = ocr_model
        pipeline["ocr"] = ocr
    inpainting_model = _cfg_str(cfg, "pipeline_inpainting_model").strip()
    if inpainting_model:
        inpainting = pipeline.get("inpainting") or {}
        inpainting["model"] = inpainting_model
        pipeline["inpainting"] = inpainting

    # --- processor（保留现有键，只覆盖配置的） ---
    processor = pipeline.get("processor") or {}
    pipeline["processor"] = processor
    text_threshold = _threshold_or_none(_cfg_float(cfg, "pipeline_detection_text_threshold", -1.0))
    bubble_threshold = _threshold_or_none(
        _cfg_float(cfg, "pipeline_detection_bubble_threshold", -1.0)
    )
    panel_threshold = _threshold_or_none(_cfg_float(cfg, "pipeline_detection_panel_threshold", -1.0))
    if any(v is not None for v in (text_threshold, bubble_threshold, panel_threshold)):
        detection_processor = processor.get("koharu-layout-rfdetr-seg-2xl") or {}
        if text_threshold is not None:
            detection_processor["text_threshold"] = text_threshold
        if bubble_threshold is not None:
            detection_processor["bubble_threshold"] = bubble_threshold
        if panel_threshold is not None:
            detection_processor["panel_threshold"] = panel_threshold
        processor["koharu-layout-rfdetr-seg-2xl"] = detection_processor
    inpainting_prompt = _cfg_str(cfg, "pipeline_inpainting_prompt").strip()
    negative_prompt = _cfg_str(cfg, "pipeline_inpainting_negative_prompt").strip()
    if inpainting_prompt:
        for key in ("flux2-klein", "rorem-mixed"):
            model_processor = processor.get(key) or {}
            model_processor["prompt"] = inpainting_prompt
            processor[key] = model_processor
    if negative_prompt:
        rorem_processor = processor.get("rorem-mixed") or {}
        rorem_processor["negative_prompt"] = negative_prompt
        processor["rorem-mixed"] = rorem_processor

    # --- translation（全部走远端提供商，无 local） ---
    translation = pipeline.get("translation") or {}
    pipeline["translation"] = translation
    provider = _cfg_str(cfg, "translation_provider").strip()
    model_name = _cfg_str(cfg, "translation_model").strip()
    # ModelSelection 的 provider/vision 必填：provider 不在白名单或 model 为空时
    # 不重建（避免 provider:"" 触发服务端 422）。远程提供商 quantization 恒 null。
    if provider in _PROVIDER_IDS and model_name:
        translation["model"] = {
            "provider": provider,
            "model": model_name,
            "quantization": None,
            "vision": False,
        }
    target_language = normalize_language(_cfg_str(cfg, "target_language"))
    if target_language is not None:
        translation["target_language"] = target_language
    instructions = _cfg_str(cfg, "system_prompt").strip()
    if instructions:
        translation["instructions"] = instructions

    # --- providers（端点由 provider 选择驱动） ---
    providers = expected.get("providers") or {}
    expected["providers"] = providers
    compatible_base_url = _cfg_str(cfg, "openai_compatible_base_url").strip()
    if provider == "openai-compatible" and compatible_base_url and _is_valid_url(compatible_base_url):
        settings = providers.get("openai-compatible") or {}
        settings["base_url"] = compatible_base_url
        providers["openai-compatible"] = settings

    # --- typesetting（字体，合并保留现有键） ---
    font_families = _cfg_str(cfg, "font_families").strip()
    if font_families:
        families = [item.strip() for item in font_families.split(",") if item.strip()]
        if families:
            typesetting = expected.get("typesetting") or {}
            typesetting["font_families"] = families
            expected["typesetting"] = typesetting

    return expected


def _threshold_or_none(value: float) -> float | None:
    """检测阈值：服务端校验 0.0..=1.0（运行时才报错），这里直接钳制到合法范围。"""
    if value > 0:
        return min(1.0, max(0.0, value))
    return None


def config_differs(current: AppConfig, expected: AppConfig) -> set[str]:
    """按 section 比较，返回有差异的 section 名（pipeline/providers/typesetting）。

    None 与 {} 视为等价：服务端缺失 section 且期望无内容时不触发空 PATCH
    （空 section PATCH 会把服务端该 section 重置为默认值）。
    """
    return {
        section
        for section in ("pipeline", "providers", "typesetting")
        if (current.get(section) or {}) != (expected.get(section) or {})
    }

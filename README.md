# astrbot_plugin_koharu_plus

Koharu 漫画翻译插件。通过 Koharu HTTP API 在 AstrBot 聊天中翻译漫画图片。

> 变更记录见 [CHANGELOG.md](./CHANGELOG.md)。

## 功能

- 使用指令 `漫画翻译` 触发漫画图片翻译。
- 支持单图和多图。
- 支持两种使用方式：
  - 发送 `/漫画翻译 Simplified Chinese` 并附带图片。
  - 先发送 `/漫画翻译`，再按提示发送图片。
- 支持引用翻译：引用（回复）一条消息后发送 `/漫画翻译`，自动翻译被引用消息中的漫画图片；引用合并转发聊天记录时，按队列翻译其中所有含图片的消息节点，并以同样的合并转发格式输出译文。
- 翻译请求按队列顺序自动处理：并发固定为 1，队列深度由 `queue_depth` 配置控制，队列满时提示稍后再试。
- 支持 AstrBot WebUI 配置管理。
- 支持中文和英文 WebUI 文案，资源位于 `.astrbot-plugin/i18n/`。
- 可选将返回图片压缩为 WebP 或 JPG 后发送。
- 翻译结果保存到 `data/plugin_data/astrbot_plugin_koharu_plus/outputs/`。

## 前置条件

- 请先完成 `Koharu(>=0.66.0, headless)` 本体部署（0.66 重建后 REST API 仅在 headless 模式提供）。
- 部署仓库：[ternurarl/koharu-docker](https://github.com/ternurarl/koharu-docker)（dev 分支）
- 插件在 AstrBot 容器内访问 Koharu 时使用 Docker 网络名，如 `http://koharu-headless:4000/api/v1`。

插件可接受 `http://host:port` 或 `http://host:port/api/v1`。

## Koharu 调用流程

每次翻译请求会执行：

1. 调用 `GET /meta` 等待 Koharu 就绪。
2. 应用持久化配置（`GET /config` 对比 → 有差异才 `PATCH` 整段；非空密钥 `PUT /config/providers/{id}/secret` 重放）。
3. 调用 `POST /projects` 创建 Koharu 项目（0.66 项目无 id，身份标识为 name）。
4. 调用 `POST /projects/current/pages` 上传图片（multipart，直接追加，无 replace 语义）。
5. 可选调用 `PUT /llm/current` 选择翻译模型（0.66 模型翻译时懒加载，返回 204 即完成）。
6. 调用 `POST /pipelines` 启动翻译 pipeline（`{"operation":{"operation":"full"},"scope":{"scope":"project"}}`；legacy 步骤名自动映射为 stages）。
7. 调用 `GET /operations` 轮询任务状态（completed/failed/cancelled）。
8. 调用 `POST /projects/current/export` 导出 rendered 图片。
9. 可选压缩导出图片后，将翻译后的图片发送回聊天。

## 持久化配置

插件配置里的管线引擎/提供商/字体/密钥会在**启动时**和**每次翻译前**应用到 Koharu 服务端
`/api/v1/config`（`GET` 全量 → 对比 → 有差异才整段 `PATCH`，配置写入服务端 config.toml 持久化，
容器重启后直接复用；密钥存 Linux keyring，重启即丢，插件自动重放）。也可用 `/koharu-config`
指令手动重放。留空/默认值 = 不覆盖服务端对应字段。

- `pipeline_ocr_model`：OCR 模型（baberu-ocr / manga-ocr / paddleocr-vl-1.6）。
- `pipeline_inpainting_model`：修补模型（lama / aot-inpainting / flux2-klein / rorem-mixed）。
- `pipeline_inpainting_prompt` / `pipeline_inpainting_negative_prompt`：修补模型 prompt（写入 processor）。
- `pipeline_detection_text_threshold` / `_bubble_threshold` / `_panel_threshold`：检测置信度阈值（0-1，-1=不覆盖）。
- `translation_provider` / `translation_model`：翻译提供商与模型（写 pipeline.translation.model，全部为远端提供商）。
- `target_language`：BCP47 语言码（默认 `zh-CN`），兼容旧文案（`Simplified Chinese` 等自动映射）。
- `system_prompt`：翻译系统提示词（写 pipeline.translation.instructions）。
- `llm_temperature` / `llm_max_tokens`：写 pipeline.translation.generation（-1/0=不覆盖）。
- `api_key`：翻译提供商 API Key——跟随 `translation_provider` 的选择自动重放到对应提供商 keyring（选 deepseek 写 deepseek，选 openai-compatible 写 openai-compatible；容器重启即丢，插件自动重放，明文存储）。
- `openai_compatible_base_url`：**自定义端点**——`translation_provider` 选 `openai-compatible` 时生效（选 deepseek 等其余提供商时用服务端内置端点，端点不生效）。
- `font_families`：渲染字体族，逗号分隔（写 typesetting.font_families）。**替代旧配置项 `default_font`**。

## 配置项

- `koharu_api_base_url`：Koharu HTTP API 地址（0.66 容器默认 `http://koharu-headless:4000/api/v1`）。
- `pipeline_steps`：`full`（默认，执行全部阶段）或逗号分隔的 0.66 阶段名 `detection/ocr/translation/inpainting`；legacy 0.61 步骤名（detector/ocr/translator 等）会自动映射。留空则从 Koharu `/config` 读取（0.66 下同样得到 full）。
- `pipeline_timeout_seconds`：等待 Koharu 翻译完成的最长时间。
- `max_images_per_request`：单次输入图片数限制。
- `max_send_images`：最多返回图片数，`0` 表示全部返回。
- `compress_return_images`：是否在发送前压缩返回图片，默认关闭。
- `return_image_format`：压缩返回图片格式，可选 `webp` 或 `jpg`。
- `return_image_quality`：压缩质量，范围 `1-100`，默认 `85`。
- `result_retention_policy`：翻译结果缓存策略，可选 `days`、`forever`、`none`。
- `result_retention_days`：按天保留时的保留天数，默认 `7` 天。

## 指令

```text
/漫画翻译
/漫画翻译 Simplified Chinese
/manga_translate English
```

如果指令消息没有附带图片，插件会等待同一会话中的下一条图片消息。

### 引用翻译

引用（回复）一条消息后发送 `/漫画翻译 [目标语言]`，插件会自动翻译被引用消息中的漫画图片：

- 被引用消息为普通消息（含一张或多张图片）时，翻译完成后逐张单独发送译文图片，无额外提示文字。
- 被引用消息为合并转发聊天记录时，插件读取转发记录中所有含图片的消息节点，按队列自动翻译，完成后以同样的合并转发聊天记录格式输出译文（只保留含图片的节点，每个节点仅包含译文图片，原文字内容丢弃；纯文本节点不输出）。
- 合并转发记录中嵌套的聊天记录（记录里出现“[聊天记录]”占位）会被递归读取，其中的图片同样参与翻译，并按原始记录顺序展开为对应节点。
- 引用存在但被引用消息中没有图片时，会提示“未能从被引用消息中提取到图片”。
- 读取与输出合并转发聊天记录依赖 OneBot v11（aiocqhttp / NapCat）平台，其他平台不支持时会提示。

其他说明：

- 引用较旧的消息时，插件会尝试自行拉取被引用消息；被引用消息本身是合并转发记录时同样会被展开读取。
- 等待发送图片期间，若改为引用一条合并转发消息，同样会按合并转发格式输出译文。
- 翻译前的确认消息（“已收到 N 张图片…”）依然保留。
- 多个翻译请求按队列顺序自动处理，队列深度与 `queue_depth` 配置一致，队列满时会提示稍后再试。

---

# English

Koharu manga translation plugin. It translates manga images in AstrBot chats through the Koharu HTTP API.

> See [CHANGELOG.md](./CHANGELOG.md) for the change log.

## Features

- Use the `漫画翻译` command to trigger manga image translation.
- Supports single-image and multi-image translation.
- Supports two usage styles:
  - Send `/漫画翻译 Simplified Chinese` with image(s) attached.
  - Send `/漫画翻译` first, then send image(s) when prompted.
- Supports quote translation: reply to (quote) a message and send `/漫画翻译` to automatically translate manga images in the quoted message; when quoting a merged-forward chat record, all image-bearing message nodes are translated in queue order and returned in the same merged-forward format.
- Translation requests are processed in queue order: concurrency is fixed at 1, the queue depth is controlled by the `queue_depth` configuration, and when the queue is full the plugin asks you to try again later.
- Supports AstrBot WebUI configuration.
- Supports Chinese and English WebUI text under `.astrbot-plugin/i18n/`.
- Optionally compresses returned images as WebP or JPG before sending.
- Stores translated output images under `data/plugin_data/astrbot_plugin_koharu_plus/outputs/`.

## Prerequisites

- Deploy `Koharu(>=0.66.0, headless)` first (the rebuilt 0.66 engine exposes the REST API only in headless mode).
- Deployment repository: [ternurarl/koharu-docker](https://github.com/ternurarl/koharu-docker) (dev branch)
- When the plugin runs inside the AstrBot container, use the Docker network name, e.g. `http://koharu-headless:4000/api/v1`.

The plugin accepts either `http://host:port` or `http://host:port/api/v1`.

## Koharu Workflow

Each translation request runs the following workflow:

1. Call `GET /meta` and wait until Koharu is ready.
2. Apply persistent config (`GET /config`, PATCH only the differing sections as a whole; replay non-empty provider secrets via `PUT /config/providers/{id}/secret`).
3. Call `POST /projects` to create a Koharu project (0.66 projects have no id; the name is the identity).
4. Call `POST /projects/current/pages` to upload image(s) (multipart; appends pages, no replace semantics).
5. Optionally call `PUT /llm/current` to select the translation model (0.66 loads lazily at translation time; a 204 response means done).
6. Call `POST /pipelines` to start the translation pipeline (`{"operation":{"operation":"full"},"scope":{"scope":"project"}}`; legacy step names are mapped to stages).
7. Call `GET /operations` to poll the operation status (completed/failed/cancelled).
8. Call `POST /projects/current/export` to export rendered image(s).
9. Optionally compress exported image(s), then send translated image(s) back to the chat.

## Persistent configuration

Pipeline engines / providers / fonts / secrets from the plugin config are applied to the
Koharu server `/api/v1/config` at startup and before every translation (`GET` the full config,
compare, PATCH a whole section only when it differs). The config is persisted to the server's
config.toml (survives container restarts); secrets live in the Linux keyring (lost on restart)
and are replayed automatically. Use `/koharu-config` to replay manually.

> Semantics: an **empty value means "do not override"** the corresponding server field;
> **non-empty defaults (`target_language`, `translation_provider`/`translation_model`,
> `font_families`, ...) are declared values** that are force-aligned at startup and before
> every translation — leave them empty to let the server decide.

- `pipeline_ocr_model`: OCR model (baberu-ocr / manga-ocr / paddleocr-vl-1.6).
- `pipeline_inpainting_model`: Inpainting model (lama / aot-inpainting / flux2-klein / rorem-mixed).
- `pipeline_inpainting_prompt` / `pipeline_inpainting_negative_prompt`: Inpainting prompts (written to processor).
- `pipeline_detection_text_threshold` / `_bubble_threshold` / `_panel_threshold`: Detection confidence thresholds (0-1, -1 = do not override).
- `translation_provider` / `translation_model`: Translation provider and model (written to pipeline.translation.model; all remote providers).
- `target_language`: BCP-47 language code (default `zh-CN`); legacy display names (`Simplified Chinese`, etc.) are mapped automatically.
- `system_prompt`: Translation system prompt (written to pipeline.translation.instructions).
- `llm_temperature` / `llm_max_tokens`: Written to pipeline.translation.generation (-1/0 = do not override).
- `api_key`: Provider API key — replayed automatically to the keyring of the provider selected in `translation_provider` (deepseek → deepseek keyring, openai-compatible → openai-compatible keyring; lost on container restart, stored in plain text).
- `openai_compatible_base_url`: **Custom endpoint** — active when `translation_provider` is set to `openai-compatible` (other providers such as deepseek use the server's built-in endpoints; the endpoint is ignored otherwise).
- `font_families`: Comma-separated render font families (written to typesetting.font_families). **Replaces the legacy `default_font` option**.


## Configuration

- `koharu_api_base_url`: Koharu HTTP API address (default `http://koharu-headless:4000/api/v1` for the 0.66 container).
- `pipeline_steps`: `full` (default; runs all stages) or a comma-separated list of 0.66 stage names `detection/ocr/translation/inpainting`; legacy 0.61 step names (detector/ocr/translator, etc.) are mapped automatically. Leave empty to read from Koharu `/config` (also resolves to full on 0.66).
- `pipeline_timeout_seconds`: Maximum time to wait for Koharu translation completion.
- `max_images_per_request`: Limit for input image count per request.
- `max_send_images`: Maximum number of images to send back. `0` means send all images.
- `compress_return_images`: Whether to compress returned images before sending. Disabled by default.
- `return_image_format`: Compressed return image format. Available values: `webp`, `jpg`.
- `return_image_quality`: Compression quality. Range: `1-100`. Default: `85`.
- `result_retention_policy`: Translated result cache policy. Available values: `days`, `forever`, `none`.
- `result_retention_days`: Retention days when using the `days` policy. Default is `7` days.

## Commands

```text
/漫画翻译
/漫画翻译 Simplified Chinese
/manga_translate English
```

If the command message has no attached image, the plugin waits for the next image message in the same session.

### Quote translation

Reply to (quote) a message and send `/漫画翻译 [target language]`; the plugin automatically translates manga images in the quoted message:

- If the quoted message is a normal message (one or more images), the translated images are sent one by one after translation completes, without any extra text.
- If the quoted message is a merged-forward chat record, the plugin reads all image-bearing message nodes, translates them in queue order, and returns the result in the same merged-forward format (only image-bearing nodes are kept; each node contains only the translated image, the original text is discarded, and text-only nodes are omitted).
- If the quoted message contains no images, the plugin replies that it could not extract any images from the quoted message.
- Reading and outputting merged-forward chat records depends on the OneBot v11 platform (aiocqhttp / NapCat); on other platforms the plugin notifies you that this is unsupported.

Other notes:

- When the quoted message is old, the plugin tries to fetch the quoted message itself.
- While waiting for images, if you instead quote a merged-forward record, the result is also returned in the merged-forward format.
- The confirmation message before translation ("已收到 N 张图片…", received N images) is still sent.
- Multiple translation requests are processed automatically in queue order; the queue depth matches the `queue_depth` configuration, and when the queue is full the plugin asks you to try again later.

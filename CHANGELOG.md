# Changelog

本文件记录所有用户可见的变更，格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。
版本号遵循语义化版本，与 `metadata.yaml` / `main.py` 中的版本保持一致。

## [v1.6.7] - 2026-08-15

### Added

- 成品守卫（export guard）：导出后检测渲染成品是否缺背景层（“白底只有文字”，08-14 事故根因）。透明占比 > 5% 为硬信号，白占比超过源图基准 +0.30 为软信号（源图基准不可用时用绝对阈值兜底）。命中则告警并重试导出一次；仍异常时发送较优的一次并记录错误。新增配置 `guard_blank_retry`（默认开启）与 `guard_max_white_absolute`（默认 0.9，调试可设 0 强制触发）。

## [v1.6.6] - 2026-08-14

### Fixed

- 修复 NapCat 平台引用合并转发聊天记录仍提取不到图片：NapCat 的 `get_forward_msg` 实际返回完整消息对象（OB11Message，字段为 `user_id` / `sender.nickname` / `message`，无 `type`/`data` 包装）而非标准 node 段，原解析把所有节点判定为畸形而跳过。现在同时兼容标准 node 段与 OB11Message 两种形态。
- 嵌套合并转发：NapCat（parseMultMsg）已在 forward 段 `data.content` 内联展开嵌套内容，直接解析不再重复拉取。

## [v1.6.5] - 2026-08-14

### Added

- 支持引用合并转发聊天记录：`Reply.chain` 为空或只有占位内容（如渲染成的 `"[聊天记录]"` 文本）时，按被引用消息 ID 兜底拉取；`get_msg` 返回的 `forward` / `node` 段会展开为转发节点。
- 合并转发记录内嵌套的聊天记录（记录中出现的 `"[聊天记录]"` 占位）会被递归读取，图片按原始记录顺序展开为独立节点输出；嵌套记录读取失败只跳过该段，不拖垮整体。
- 合并转发嵌套深度上限（10 层），防止畸形记录构造深链导致递归过深。

### Fixed

- 引用合并转发聊天记录后发送 `/漫画翻译` 提取不到图片（原 get_msg 兜底只解析 image 段，`forward` 段被忽略）。

### Changed

- README 移除版本变更标记（如"v1.4.0 新增""v1.6.5 起支持"），变更记录统一收拢到本文件。

## [v1.6.4] - 2026-08-14

### Changed

- `api_key` 改为通用配置项，跟随 `translation_provider` 的选择自动重放到对应提供商 keyring（选 deepseek 写入 deepseek，选 openai-compatible 写入 openai-compatible；重启即丢，插件自动重放）。

## [v1.6.3] - 2026-08-14

### Removed

- 精简插件配置：删除 `auto_load_llm` / `llm_*` 旧机制、`lm-studio` / `deepl` 端点与 `provider_secrets` 嵌套密钥。
- 排除 local 提供商（全部走远端），翻译配置收敛为提供商 / 模型 / API key / openai-compatible 端点四项，端点由提供商选择驱动。

## [v1.6.2] - 2026-08-14

### Fixed

- 修复插件仓库地址（ABCwewe → ternurarl）。

## [v1.6.1] - 2026-08-14

### Fixed

- 对抗性审核修复：配置读取全面 None/错型防御、provider 白名单与本地钳制校验、配置应用并发串行化（避免与翻译流程并发 PATCH 的竞态）。

### Added

- 自定义翻译端点三件套：`openai-compatible` 提供商下端点 / 密钥 / 模型自动切换。

## [v1.5.0] - 2026-08-14

### Changed

- 适配 Koharu 0.66 headless API（`koharu-headless` 容器）：项目身份改 name、页面上传走 `/projects/current/pages`、pipeline 改 Operation/Scope（full/stages）、LLM 懒加载 204 即完成、默认目标语言简体中文（服务端 zh-CN）。
- 清理 0.61 legacy 端点，`default_font` 由 `font_families` 替代。

## [v1.4.0] - 2026-08-13

### Added

- 引用翻译：引用（回复）一条消息后发送 `/漫画翻译`，自动翻译被引用消息中的漫画图片；引用合并转发聊天记录时，按队列翻译其中所有含图片的消息节点，并以同样的合并转发格式输出译文。
- OneBot 统一封装（`onebot_client.py`），全库强类型化并引入 pyright strict，补充单元 / 集成 / E2E 测试。

## [v1.2.0] - 2026-06-09

### Added

- 图片压缩：可选将返回图片压缩为 WebP 或 JPG 后发送（`compress_return_images` / `return_image_format` / `return_image_quality`）。

## [v1.1.2] - 2026-06-09

### Changed

- 默认字体改为 `Noto Sans SC:500`。

## [v1.1.0] / [v1.1.1] - 2026-06-08

早期小版本调整，无功能变更记录。

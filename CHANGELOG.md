# Changelog

## May 31

removed system_prompt injection logic + removed extra `\n`

## May 3

added header message

## march 20 

smart injection ordering

### Fixed
- **RAG 注入顺序**: `user_message_after` 注入时，检测 LivingMemory 的 `<RAG-Faiss-Memory>` 是否已存在于 `req.prompt` 中。若存在，将我们的标签插入到 RAG 标签之前而非追加到末尾，确保注入顺序为：用户消息 → 我们的标签 → RAG 记忆。

## march 18 - changes priority

## v1.1.2
😿

## v1.1.1
changed text to string.

## v1.1.0

### Fixed
- **LivingMemory 兼容性修复**: 使用 `priority=-1000` 将 `on_llm_request` 钩子的执行顺序降到 LivingMemory（默认 `priority=0`）之后。此前我们的标签在 LivingMemory 检索记忆之前就已注入到 `req.prompt` 中，导致记忆搜索查询被自定义标签内容污染。
- **多行文本支持**: 将 `_conf_schema.json` 中标签内容字段的类型从 `"string"`（单行输入框）改为 `"text"`（多行 textarea）。同时在代码中将 AstrBot textarea 存储的字面 `\n` 转义序列还原为真正的换行符。
- **标签尾部换行**: 在闭合标签后追加换行符，避免与后续系统标签（如 `<system_reminder>`）紧贴在一起。

### Changed
- 版本号更新至 `1.1.0`
- 添加 GitHub repo 地址

## v1.0.0

### Added
- 初始版本
- 支持最多 5 个自定义 XML 标签，每个可独立配置启用/禁用、注入位置和内容
- 三种注入位置：`user_message_before`、`user_message_after`、`system_prompt`
- 每轮自动清理上一轮残留的标签内容（覆盖 `req.prompt`、`req.system_prompt`、`req.contexts` 中的字符串、字典、多模态三种消息格式）
- 标签名称合法性校验（仅允许字母、数字、连字符、下划线）

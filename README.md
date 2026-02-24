# PromptTags - 持久化提示词标签注入插件

一个轻量级 AstrBot 插件，在每一轮 LLM 请求中自动注入用户预定义的 XML 标签内容，并在下一轮请求前自动清理上一轮的注入，确保 LLM 每轮只看到一份最新的标签内容。

## 功能特性

- 支持最多 **5 个**自定义标签，每个可独立启用/禁用
- 三种注入位置可选：
  - `user_message_before` — 注入到用户消息**前面**
  - `user_message_after` — 注入到用户消息**后面**
  - `system_prompt` — 追加到系统提示词末尾
- 每轮自动清理上一轮残留的标签内容（包括对话历史中的多模态消息）
- 与 LivingMemory 插件安全共存，互不干扰

## 与 LivingMemory 的共存

本插件使用 `priority=-1000` 确保 `on_llm_request` 钩子在 LivingMemory（默认 `priority=0`）完成记忆检索和注入之后再执行。这意味着：

1. LivingMemory 先执行，使用纯净的用户消息进行记忆检索
2. LivingMemory 注入 `<RAG-Faiss-Memory>` 标签
3. 本插件再注入自定义标签

两个插件使用完全不同的标签名称，各自的清理正则只匹配自己的标签，不会交叉干扰。

## 安装

将 `prompt_tags` 文件夹放入 AstrBot 的插件目录，重启 AstrBot 即可。

## 配置

在 AstrBot 的 Web 后台中配置每个标签：

| 字段 | 说明 |
|------|------|
| **启用** | 是否启用此标签 |
| **注入位置** | 标签注入的位置（用户消息前 / 用户消息后 / 系统提示词） |
| **标签名称** | XML 标签名，如 `Behavior-Guidelines`（不含尖括号，仅限字母、数字、连字符、下划线） |
| **标签内容** | 注入的实际提示词文本 |

## 使用示例

假设配置了一个标签：

- 标签名称：`User-Preference`
- 注入位置：`user_message_after`
- 内容：
  ```
  Below are the user's preferences. Please keep in mind.
  
  - Prefers concise answers.
  - Likes code examples.
  ```

当用户发送消息"今天天气怎么样？"时，实际发给 LLM 的 prompt 会变为：

```
今天天气怎么样？

<User-Preference>
Below are the user's preferences. Please keep in mind.

- Prefers concise answers.
- Likes code examples.
</User-Preference>
```

下一轮对话时，上一条消息中的 `<User-Preference>` 标签会被自动清除，并重新注入到最新消息中。

## 开发信息

- **作者**: Felis Abyssalis
- **版本**: 1.1.0
- **依赖**: 无额外依赖，仅使用 AstrBot 内置 API

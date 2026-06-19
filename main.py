"""
PromptTags - 持久化提示词标签注入插件 (LivingMemory 兼容版)

在每一轮 LLM 请求前：
1. 清理上一轮注入到对话历史中的所有自定义标签
2. 将当前已启用的标签内容重新注入到指定位置

支持最多 5 个自定义标签，每个标签可独立配置注入位置。

LivingMemory 兼容策略：
- 使用 priority=-1000 确保本插件的 on_llm_request 钩子在
  LivingMemory (priority=0) 之后执行，避免我们注入的标签
  污染 LivingMemory 的记忆检索查询
- 各自使用互不相同的标签名称，清理正则不会交叉匹配

F(A) = A(F)
"""

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MAX_TAGS = 5
TAG_SLOT_KEYS = [f"tag_{i}" for i in range(1, MAX_TAGS + 1)]

# 用于校验标签名合法性的正则：仅禁止换行和尖括号
TAG_NAME_PATTERN = re.compile(r"^[^\n\r<>]+$")


@register(
    "PromptTags",
    "FelisAbyssalis",
    "持久化提示词标签注入插件 - 自动向 LLM 请求注入自定义标签内容并在下一轮清理",
    "1.1.1",
    "https://github.com/EmilyCheoh/astrbot_add_prompt_tags_livingmemory_compatible",
)
class PromptTagsPlugin(Star):
    """
    AstrBot 插件：在每一轮 LLM 请求前注入用户定义的 XML 标签内容，
    并在下一轮自动清理上一轮的残留标签。

    设计原理：
    - 利用 AstrBot 的 on_llm_request 钩子，在 LLM 请求发出前修改
      req.prompt（用户消息）或 req.system_prompt（系统提示词）
    - 每轮请求前先清理 req.prompt、req.contexts
      中上一轮注入的标签内容，然后重新注入最新内容
    - 标签名由用户自定义，格式为 <TagName>...</TagName>
    - 与 LivingMemory 互不干扰：LivingMemory 使用 <RAG-Faiss-Memory>
      标签，我们使用用户自定义名称，双方正则不会交叉匹配
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        # 从配置中加载顶部声明和所有标签
        self._disclaimer: str | None = None
        self._load_disclaimer()
        self._tags: list[dict[str, Any]] = []
        self._load_tags()

        # 手动清理标记：当用户发送清理命令时置为 True，
        # 下一轮 on_llm_request 清理阶段将扫描所有配置槽位
        # （包括已禁用的标签）执行一次全量清理，然后重置为 False
        self._full_cleanup_pending: bool = False

        logger.info(
            f"【PromptTags 提示词注入】插件初始化完成，"
            f"已加载 {len(self._tags)} 个有效标签"
            + (f"，顶部声明已启用" if self._disclaimer else "")
        )

    # -----------------------------------------------------------------------
    # 配置加载
    # -----------------------------------------------------------------------

    def _load_disclaimer(self) -> None:
        """从插件配置中加载顶部声明文本。"""
        slot = self.config.get("header_disclaimer", {})
        if not isinstance(slot, dict):
            return

        if not slot.get("enabled", False):
            return

        content = str(slot.get("content", "")).strip()
        if not content:
            logger.warning(
                "【PromptTags 提示词注入】: 顶部声明已启用但内容为空，跳过"
            )
            return

        content = content.replace("\\n", "\n").strip()
        self._disclaimer = content

    def _load_tags(self) -> None:
        """从插件配置中加载所有已启用且合法的标签定义。"""
        self._tags = []

        for slot_key in TAG_SLOT_KEYS:
            slot = self.config.get(slot_key, {})
            if not isinstance(slot, dict):
                continue

            enabled = slot.get("enabled", False)
            if not enabled:
                continue

            tag_name = str(slot.get("tag_name", "")).strip()
            content = str(slot.get("content", ""))

            # AstrBot 的 textarea 将用户按 Enter 产生的换行存储为字面的
            # 两字符序列 "\n"（反斜杠+n），而非真正的换行符。
            # 需要将其还原为实际换行才能正确注入。
            content = content.replace("\\n", "\n").strip()
            position = str(
                slot.get("injection_position", "user_message_after")
            ).strip()

            # 校验
            if not tag_name:
                logger.warning(
                    f"【PromptTags 提示词注入】: {slot_key} 已启用但标签名称为空，跳过"
                )
                continue

            if not TAG_NAME_PATTERN.match(tag_name):
                logger.warning(
                    f"【PromptTags 提示词注入】: {slot_key} 标签名称 '{tag_name}' "
                    f"包含非法字符（不允许换行或尖括号），跳过"
                )
                continue

            if not content:
                logger.warning(
                    f"【PromptTags 提示词注入】: {slot_key} 已启用但内容为空，跳过"
                )
                continue

            if position not in (
                "user_message_before",
                "user_message_after",
            ):
                logger.warning(
                    f"【PromptTags 提示词注入】: {slot_key} 注入位置 '{position}' 无效，"
                    f"回退到 user_message_after"
                )
                position = "user_message_after"

            self._tags.append(
                {
                    "slot": slot_key,
                    "tag_name": tag_name,
                    "content": content,
                    "position": position,
                    "header": f"<{tag_name}>",
                    "footer": f"</{tag_name}>",
                }
            )


    # -----------------------------------------------------------------------
    # 全量清理：加载所有配置槽位的标签（忽略 enabled 状态）
    # -----------------------------------------------------------------------

    def _load_all_tags_for_cleanup(self) -> list[dict[str, Any]]:
        """从配置中加载所有标签名称，无论是否启用。

        仅用于手动清理命令触发的全量清理，确保已禁用的标签
        也能从上下文中被清除。返回的字典仅包含清理所需的字段。
        """
        all_tags: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for slot_key in TAG_SLOT_KEYS:
            slot = self.config.get(slot_key, {})
            if not isinstance(slot, dict):
                continue

            tag_name = str(slot.get("tag_name", "")).strip()
            if not tag_name or not TAG_NAME_PATTERN.match(tag_name):
                continue
            if tag_name in seen_names:
                continue
            seen_names.add(tag_name)

            all_tags.append(
                {
                    "tag_name": tag_name,
                    "header": f"<{tag_name}>",
                    "footer": f"</{tag_name}>",
                }
            )

        return all_tags

    # -----------------------------------------------------------------------
    # 标签格式化
    # -----------------------------------------------------------------------

    @staticmethod
    def _format_tag(tag: dict[str, Any]) -> str:
        """将标签格式化为 XML 包裹的字符串，尾部附加换行以与后续内容分隔。"""
        return f"{tag['header']}\n{tag['content']}\n{tag['footer']}"

    # -----------------------------------------------------------------------
    # 清理逻辑
    # -----------------------------------------------------------------------

    def _build_cleanup_pattern(self, tag: dict[str, Any]) -> re.Pattern:
        """为指定标签构建清理用的正则表达式。"""
        return re.compile(
            re.escape(tag["header"])
            + r".*?"
            + re.escape(tag["footer"]),
            flags=re.DOTALL,
        )

    def _clean_string(self, text: str, pattern: re.Pattern) -> str:
        """从字符串中清除匹配的标签内容，并整理多余换行。"""
        cleaned = pattern.sub("", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _remove_tags_from_context(
        self, req: ProviderRequest, tag: dict[str, Any]
    ) -> int:
        """
        从 ProviderRequest 的所有位置中清除指定标签的内容。

        清理范围：
        - req.system_prompt
        - req.prompt
        - req.contexts（对话历史，支持字符串、字典/字符串内容、字典/列表内容三种格式）

        Returns:
            清除的片段数量
        """
        removed = 0
        pattern = self._build_cleanup_pattern(tag)
        header = tag["header"]
        footer = tag["footer"]

        # --- 清理 system_prompt ---
        if hasattr(req, "system_prompt") and req.system_prompt:
            if isinstance(req.system_prompt, str):
                if header in req.system_prompt and footer in req.system_prompt:
                    original = req.system_prompt
                    req.system_prompt = self._clean_string(original, pattern)
                    if req.system_prompt != original:
                        removed += 1

        # --- 清理 prompt ---
        if hasattr(req, "prompt") and req.prompt:
            if isinstance(req.prompt, str):
                if header in req.prompt and footer in req.prompt:
                    original = req.prompt
                    req.prompt = self._clean_string(original, pattern)
                    if req.prompt != original:
                        removed += 1

        # --- 清理 contexts（对话历史）---
        if hasattr(req, "contexts") and req.contexts:
            filtered_contexts = []

            for msg in req.contexts:
                # 格式 1: 纯字符串
                if isinstance(msg, str):
                    if header in msg and footer in msg:
                        cleaned = self._clean_string(msg, pattern)
                        if not cleaned:
                            removed += 1
                            continue
                        if cleaned != msg:
                            removed += 1
                            filtered_contexts.append(cleaned)
                            continue
                    filtered_contexts.append(msg)

                # 格式 2/3: 字典
                elif isinstance(msg, dict):
                    content = msg.get("content", "")

                    # 字符串内容
                    if isinstance(content, str):
                        if header in content and footer in content:
                            cleaned = self._clean_string(content, pattern)
                            if not cleaned:
                                removed += 1
                                continue
                            if cleaned != content:
                                removed += 1
                                msg_copy = msg.copy()
                                msg_copy["content"] = cleaned
                                filtered_contexts.append(msg_copy)
                                continue
                        filtered_contexts.append(msg)

                    # 列表内容（多模态）
                    elif isinstance(content, list):
                        cleaned_parts = []
                        has_changes = False

                        for part in content:
                            if (
                                isinstance(part, dict)
                                and part.get("type") == "text"
                            ):
                                text = part.get("text", "")
                                if isinstance(text, str):
                                    if header in text and footer in text:
                                        cleaned_text = self._clean_string(
                                            text, pattern
                                        )
                                        if not cleaned_text:
                                            has_changes = True
                                            continue
                                        if cleaned_text != text:
                                            has_changes = True
                                            removed += 1
                                            part_copy = part.copy()
                                            part_copy["text"] = cleaned_text
                                            cleaned_parts.append(part_copy)
                                            continue
                            cleaned_parts.append(part)

                        if not cleaned_parts:
                            removed += 1
                            continue
                        if has_changes:
                            msg_copy = msg.copy()
                            msg_copy["content"] = cleaned_parts
                            filtered_contexts.append(msg_copy)
                            continue
                        filtered_contexts.append(msg)

                else:
                    filtered_contexts.append(msg)

            req.contexts = filtered_contexts

        return removed

    # -----------------------------------------------------------------------
    # 顶部声明清理
    # -----------------------------------------------------------------------

    def _strip_disclaimer(self, text: str) -> str:
        """从字符串中移除顶部声明文本。

        使用精确子串匹配（类似世界书去重逻辑），
        而非正则——因为声明是固定纯文本，不是 XML 结构。
        """
        if not self._disclaimer or self._disclaimer not in text:
            return text
        cleaned = text.replace(self._disclaimer, "")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _remove_disclaimer_from_context(self, req: ProviderRequest) -> int:
        """从 ProviderRequest 的所有文本位置中清除顶部声明。"""
        if not self._disclaimer:
            return 0

        removed = 0
        disclaimer = self._disclaimer

        # --- 清理 prompt ---
        if hasattr(req, "prompt") and isinstance(req.prompt, str):
            if disclaimer in req.prompt:
                req.prompt = self._strip_disclaimer(req.prompt)
                removed += 1

        # --- 清理 system_prompt ---
        if hasattr(req, "system_prompt") and isinstance(req.system_prompt, str):
            if disclaimer in req.system_prompt:
                req.system_prompt = self._strip_disclaimer(req.system_prompt)
                removed += 1

        # --- 清理 contexts ---
        if hasattr(req, "contexts") and req.contexts:
            for i, msg in enumerate(req.contexts):
                if isinstance(msg, str) and disclaimer in msg:
                    cleaned = self._strip_disclaimer(msg)
                    req.contexts[i] = cleaned if cleaned else ""
                    removed += 1
                elif isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str) and disclaimer in content:
                        cleaned = self._strip_disclaimer(content)
                        msg_copy = msg.copy()
                        msg_copy["content"] = cleaned
                        req.contexts[i] = msg_copy
                        removed += 1
                    elif isinstance(content, list):
                        for j, part in enumerate(content):
                            if (
                                isinstance(part, dict)
                                and part.get("type") == "text"
                                and isinstance(part.get("text"), str)
                                and disclaimer in part["text"]
                            ):
                                cleaned = self._strip_disclaimer(part["text"])
                                part_copy = part.copy()
                                part_copy["text"] = cleaned
                                content[j] = part_copy
                                removed += 1

        return removed

    # -----------------------------------------------------------------------
    # 手动清理命令
    # -----------------------------------------------------------------------

    @filter.command(
        "cleartags",
        alias={
            "cleartag",
            "removetags",
            "removetag",
            "cleanup",
            "clearup",
            "clearall",
            "removeall",
            "cleanall"
        },
    )
    async def handle_clear_command(self, event: AstrMessageEvent):
        """手动触发一次全量标签清理（含已禁用的标签）。"""
        self._full_cleanup_pending = True

        # 重新加载配置以确保使用最新的标签定义
        self._tags = []
        self._load_tags()

        yield event.plain_result(
            "🧹 已标记全量清理，将在下一条消息时清除所有标签。"
        )

    # -----------------------------------------------------------------------
    # 事件钩子
    # -----------------------------------------------------------------------

    @filter.on_llm_request(priority=1)
    async def handle_cleanup_tags(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [事件钩子 - 清理阶段] 在 LLM 请求前，优先于 LivingMemory 执行。

        仅负责从 req.prompt / req.contexts 中
        清除上一轮注入的旧标签，不做任何新注入。

        当 _full_cleanup_pending 为 True 时，扫描所有配置槽位
        （包括已禁用的标签）执行一次全量清理，确保已禁用标签
        的残留内容也被移除。

        priority=1 确保本钩子在 LivingMemory (priority=0) 之前执行。
        这样做的原因：
          LivingMemory 的 <RAG-Faiss-Memory> 清理正则使用 DOTALL 非贪婪匹配。
          若我们的标签内容中包含 <RAG-Faiss-Memory>（作为示例提及，无闭合标签），
          且该标签被注入在 user_message_before 位置（历史中位于真实 RAG 注入之前），
          则 LivingMemory 的正则会从示例提及处一直匹配到真实 RAG 的闭合标签，
          吃掉中间所有内容，导致对话历史被截断。
          先于 LivingMemory 清理我们的标签，可从根本上避免此误匹配。
        """
        # 确定本轮使用的清理范围
        if self._full_cleanup_pending:
            cleanup_tags = self._load_all_tags_for_cleanup()
            self._full_cleanup_pending = False
        else:
            cleanup_tags = self._tags

        if not cleanup_tags and not self._disclaimer:
            return

        try:
            total_removed = 0

            # 清理顶部声明
            total_removed += self._remove_disclaimer_from_context(req)

            # 清理 XML 标签
            for tag in cleanup_tags:
                removed = self._remove_tags_from_context(req, tag)
                total_removed += removed

            if total_removed > 0:
                logger.debug(
                    f"【PromptTags 提示词注入】清理 {total_removed} 处历史注入"
                )

        except Exception as e:
            logger.error(
                f"【PromptTags 提示词注入】[清理阶段]: 清理时发生错误: {e}",
                exc_info=True,
            )

    @filter.on_llm_request(priority=-500)
    async def handle_inject_tags(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [事件钩子 - 注入阶段] 在 LLM 请求前，在 LivingMemory 之后执行。

        仅负责将当前已启用的标签内容注入到指定位置，不做清理。

        priority=-500 确保本钩子在 LivingMemory (priority=0)
        完成记忆检索和注入之后再执行，避免我们的标签污染记忆搜索查询。
        """
        if not self._tags and not self._disclaimer:
            return

        try:
            # 按位置分组
            by_position: dict[str, list[str]] = {
                "user_message_before": [],
                "user_message_after": [],
            }

            for tag in self._tags:
                formatted = self._format_tag(tag)
                by_position[tag["position"]].append(formatted)

            # --- user_message_before ---
            if by_position["user_message_before"]:
                block = "\n\n".join(by_position["user_message_before"])
                req.prompt = block + "\n\n" + (req.prompt or "")
                logger.debug(
                    f"【PromptTags 提示词注入】消息前注入 "
                    f"{len(by_position['user_message_before'])} 个标签"
                )

            # --- user_message_after ---
            if by_position["user_message_after"]:
                block = "\n\n".join(by_position["user_message_after"])
                prompt = req.prompt or ""
                # 如果 LivingMemory 已经将 <RAG-Faiss-Memory> 注入到
                # prompt 末尾，将我们的标签插入到它前面，确保我们的标签
                # 在 RAG 记忆之前、用户消息之后。
                rag_marker = "<RAG-Faiss-Memory>"
                rag_pos = prompt.find(rag_marker)
                if rag_pos > 0:
                    # 在 RAG 标签前插入，保留换行分隔
                    before_rag = prompt[:rag_pos].rstrip()
                    from_rag = prompt[rag_pos:]
                    req.prompt = before_rag + "\n\n" + block + "\n\n" + from_rag
                else:
                    req.prompt = prompt + "\n\n" + block
                logger.debug(
                    f"【PromptTags 提示词注入】消息后注入 "
                    f"{len(by_position['user_message_after'])} 个标签"
                )

            # --- 顶部声明注入（最后执行，确保它在 req.prompt 的绝对顶部）---
            if self._disclaimer:
                req.prompt = self._disclaimer + "\n\n" + (req.prompt or "")
                logger.debug("【PromptTags 提示词注入】注入顶部声明")

        except Exception as e:
            logger.error(
                f"【PromptTags 提示词注入】[注入阶段]: 注入时发生错误: {e}",
                exc_info=True,
            )

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

    async def terminate(self):
        """插件停止时清理资源。"""
        self._tags = []
        logger.info("【PromptTags 提示词注入】插件已停止")

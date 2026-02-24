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

# 用于校验标签名合法性的正则：只允许字母、数字、连字符、下划线
TAG_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@register(
    "PromptTags",
    "FelisAbyssalis",
    "持久化提示词标签注入插件 - 自动向 LLM 请求注入自定义标签内容并在下一轮清理",
    "1.1.0",
    "https://github.com/EmilyCheoh/astrbot_add_prompt_tags",
)
class PromptTagsPlugin(Star):
    """
    AstrBot 插件：在每一轮 LLM 请求前注入用户定义的 XML 标签内容，
    并在下一轮自动清理上一轮的残留标签。

    设计原理：
    - 利用 AstrBot 的 on_llm_request 钩子，在 LLM 请求发出前修改
      req.prompt（用户消息）或 req.system_prompt（系统提示词）
    - 每轮请求前先清理 req.prompt、req.system_prompt、req.contexts
      中上一轮注入的标签内容，然后重新注入最新内容
    - 标签名由用户自定义，格式为 <TagName>...</TagName>
    - 与 LivingMemory 互不干扰：LivingMemory 使用 <RAG-Faiss-Memory>
      标签，我们使用用户自定义名称，双方正则不会交叉匹配
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        # 从配置中加载所有标签
        self._tags: list[dict[str, Any]] = []
        self._load_tags()

        logger.info(
            f"PromptTags 插件初始化完成，"
            f"已加载 {len(self._tags)} 个有效标签"
        )

    # -----------------------------------------------------------------------
    # 配置加载
    # -----------------------------------------------------------------------

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
                    f"PromptTags: {slot_key} 已启用但标签名称为空，跳过"
                )
                continue

            if not TAG_NAME_PATTERN.match(tag_name):
                logger.warning(
                    f"PromptTags: {slot_key} 标签名称 '{tag_name}' "
                    f"包含非法字符（仅允许字母、数字、连字符、下划线），跳过"
                )
                continue

            if not content:
                logger.warning(
                    f"PromptTags: {slot_key} 已启用但内容为空，跳过"
                )
                continue

            if position not in (
                "user_message_before",
                "user_message_after",
                "system_prompt",
            ):
                logger.warning(
                    f"PromptTags: {slot_key} 注入位置 '{position}' 无效，"
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

            logger.info(
                f"PromptTags: 已加载标签 [{tag_name}] "
                f"(位置: {position}, 内容长度: {len(content)})"
            )

    # -----------------------------------------------------------------------
    # 标签格式化
    # -----------------------------------------------------------------------

    @staticmethod
    def _format_tag(tag: dict[str, Any]) -> str:
        """将标签格式化为 XML 包裹的字符串，尾部附加换行以与后续内容分隔。"""
        return f"{tag['header']}\n{tag['content']}\n{tag['footer']}\n"

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
    # 事件钩子
    # -----------------------------------------------------------------------

    @filter.on_llm_request(priority=-1000)
    async def handle_inject_tags(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """
        [事件钩子] 在 LLM 请求前（低优先级，在 LivingMemory 之后执行）：
        1. 清理上一轮注入到对话历史中的所有自定义标签
        2. 将当前已启用的标签内容重新注入到指定位置

        priority=-1000 确保本钩子在 LivingMemory (默认 priority=0)
        完成记忆检索和注入之后再执行，避免我们的标签污染记忆搜索查询。
        """
        if not self._tags:
            return

        try:
            session_id = event.unified_msg_origin or "unknown"

            # === 阶段 1: 清理所有已注册标签的旧注入 ===
            total_removed = 0
            for tag in self._tags:
                removed = self._remove_tags_from_context(req, tag)
                total_removed += removed

            if total_removed > 0:
                logger.info(
                    f"[{session_id}] PromptTags: "
                    f"已清理 {total_removed} 处历史标签注入片段"
                )

            # === 阶段 2: 注入当前标签 ===
            #
            # 按位置分组，避免在同一位置多次拼接时出现不必要的换行
            by_position: dict[str, list[str]] = {
                "user_message_before": [],
                "user_message_after": [],
                "system_prompt": [],
            }

            for tag in self._tags:
                formatted = self._format_tag(tag)
                by_position[tag["position"]].append(formatted)

            # --- user_message_before ---
            if by_position["user_message_before"]:
                block = "\n\n".join(by_position["user_message_before"])
                req.prompt = block + "\n\n" + (req.prompt or "")
                logger.info(
                    f"[{session_id}] PromptTags: "
                    f"已向用户消息前注入 "
                    f"{len(by_position['user_message_before'])} 个标签"
                )

            # --- user_message_after ---
            if by_position["user_message_after"]:
                block = "\n\n".join(by_position["user_message_after"])
                req.prompt = (req.prompt or "") + "\n\n" + block
                logger.info(
                    f"[{session_id}] PromptTags: "
                    f"已向用户消息后注入 "
                    f"{len(by_position['user_message_after'])} 个标签"
                )

            # --- system_prompt ---
            if by_position["system_prompt"]:
                block = "\n\n".join(by_position["system_prompt"])
                req.system_prompt = (
                    (req.system_prompt or "") + "\n\n" + block
                )
                logger.info(
                    f"[{session_id}] PromptTags: "
                    f"已向 System Prompt 注入 "
                    f"{len(by_position['system_prompt'])} 个标签"
                )

        except Exception as e:
            logger.error(
                f"PromptTags: 处理标签注入时发生错误: {e}",
                exc_info=True,
            )

    # -----------------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------------

    async def terminate(self):
        """插件停止时清理资源。"""
        self._tags = []
        logger.info("PromptTags 插件已停止")

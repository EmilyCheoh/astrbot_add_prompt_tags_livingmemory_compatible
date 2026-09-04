"""
PromptTags - 持久化提示词标签注入插件 (LivingMemory 兼容版)

在每一轮 LLM 请求前：
1. 无条件扫描所有配置槽位，清理历史中残留的标签内容（无论启用状态）
2. 将当前已启用的标签内容重新注入到指定位置

支持最多 5 个自定义标签，每个标签可独立配置注入位置。

LivingMemory 兼容策略：
- 清理阶段 priority=1，在 LivingMemory (priority=0) 之前执行，
  避免我们的标签内容干扰 LivingMemory 的正则匹配
- 注入阶段 priority=-500，在 LivingMemory 之后执行，
  避免我们的标签污染 LivingMemory 的记忆检索查询
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
    "2.0.0",
    "https://github.com/EmilyCheoh/astrbot_add_prompt_tags_livingmemory_compatible",
)
class PromptTagsPlugin(Star):
    """
    AstrBot 插件：在每一轮 LLM 请求前注入用户定义的 XML 标签内容，
    并在每轮自动清理所有配置槽位的标签残留。

    设计原理：
    - 利用 AstrBot 的 on_llm_request 钩子，在 LLM 请求发出前修改
      req.prompt（用户消息）或 req.system_prompt（系统提示词）
    - 每轮请求前无条件扫描所有配置槽位，清理上一轮的标签残留，
      然后将当前已启用的标签重新注入
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

        logger.info(
            f"【PromptTags 提示词注入】插件初始化完成，"
            f"已加载 {len(self._tags)} 个有效标签"
            + ("，顶部声明已启用" if self._disclaimer else "")
        )

    # -----------------------------------------------------------------------
    # 槽位辅助方法
    # -----------------------------------------------------------------------

    def _resolve_slot(self, index: int) -> dict | None:
        """根据编号 1-5 获取配置槽位字典，无效时返回 None。"""
        slot = self.config.get(f"tag_{index}", {})
        return slot if isinstance(slot, dict) else None

    @staticmethod
    def _get_alias(slot: dict) -> str:
        """获取清理后的 alias。"""
        return str(slot.get("alias", "")).strip()

    @staticmethod
    def _get_tag_name(slot: dict) -> str:
        """获取清理后的 tag_name。"""
        return str(slot.get("tag_name", "")).strip()

    @staticmethod
    def _get_content(slot: dict) -> str:
        """获取清理后的 content，字面 \\n 还原为真正换行。"""
        content = str(slot.get("content", ""))
        return content.replace("\\n", "\n").strip()

    @staticmethod
    def _is_tag_name_valid(tag_name: str) -> bool:
        """检查 tag_name 是否非空且符合命名规则。"""
        return bool(tag_name) and bool(TAG_NAME_PATTERN.match(tag_name))

    def _is_tag_valid(self, slot: dict) -> bool:
        """检查标签配置是否完整有效（tag_name 合法且 content 非空）。"""
        tag_name = self._get_tag_name(slot)
        content = self._get_content(slot)
        return self._is_tag_name_valid(tag_name) and bool(content)

    def _is_tag_active(self, slot: dict) -> bool:
        """检查标签是否真正处于活动状态（enabled 且 valid）。"""
        return slot.get("enabled", False) and self._is_tag_valid(slot)

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

            tag_name = self._get_tag_name(slot)
            content = self._get_content(slot)
            position = str(
                slot.get("injection_position", "user_message_after")
            ).strip()

            # 校验
            if not tag_name:
                logger.warning(
                    f"【PromptTags 提示词注入】: {slot_key} 已启用但标签名称为空，跳过"
                )
                continue

            if not self._is_tag_name_valid(tag_name):
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
        """从配置中加载所有有效标签名称，无论是否启用。

        每轮清理阶段调用，确保已禁用标签的残留内容也被清除。
        返回的字典仅包含清理所需的字段。
        """
        all_tags: list[dict[str, Any]] = []
        seen_names: set[str] = set()

        for slot_key in TAG_SLOT_KEYS:
            slot = self.config.get(slot_key, {})
            if not isinstance(slot, dict):
                continue

            tag_name = self._get_tag_name(slot)
            if not tag_name or not self._is_tag_name_valid(tag_name):
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
        """将标签格式化为 XML 包裹的字符串。"""
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
        - req.contexts（对话历史，支持字符串、字典/字符串内容、
          字典/列表内容三种格式）

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
        if hasattr(req, "system_prompt") and isinstance(
            req.system_prompt, str
        ):
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
    # Command Group: /tag (/pt)
    # -----------------------------------------------------------------------

    @filter.command_group("tag", alias={"pt"})
    def tag(self):
        pass

    @tag.command("on")
    async def enable_tag(
        self,
        event: AstrMessageEvent,
        index: int,
    ):
        """开启指定标签"""
        if not 1 <= index <= MAX_TAGS:
            yield event.plain_result("⚠️ 标签编号必须是 1–5。")
            return

        slot = self._resolve_slot(index)
        if slot is None:
            yield event.plain_result("⚠️ 标签编号必须是 1–5。")
            return

        tag_name = self._get_tag_name(slot)
        display_name = f" <{tag_name}>" if tag_name else ""

        # 验证 tag_name
        if not tag_name:
            yield event.plain_result(
                f"⚠️ 标签 {index} 缺少 tag_name，无法开启。"
            )
            return
        if not self._is_tag_name_valid(tag_name):
            yield event.plain_result(
                f"⚠️ 标签 {index} 的 tag_name 包含非法字符，无法开启。"
            )
            return

        # 验证 content
        content = self._get_content(slot)
        if not content:
            yield event.plain_result(
                f"⚠️ 标签 {index} 缺少 content，无法开启。"
            )
            return

        # 幂等检查
        if slot.get("enabled", False):
            yield event.plain_result(
                f"🏷️ 标签 {index}{display_name} 已经处于开启状态。"
            )
            return

        # 开启并持久化
        slot_key = f"tag_{index}"
        self.config[slot_key]["enabled"] = True
        self.config.save_config()

        self._tags = []
        self._load_tags()

        yield event.plain_result(
            f"🏷️ 标签 {index}{display_name} 已开启。"
        )

    @tag.command("off")
    async def disable_tag(
        self,
        event: AstrMessageEvent,
        index: int,
    ):
        """关闭指定标签"""
        if not 1 <= index <= MAX_TAGS:
            yield event.plain_result("⚠️ 标签编号必须是 1–5。")
            return

        slot = self._resolve_slot(index)
        if slot is None:
            yield event.plain_result("⚠️ 标签编号必须是 1–5。")
            return

        tag_name = self._get_tag_name(slot)
        display_name = (
            f" <{tag_name}>"
            if self._is_tag_name_valid(tag_name)
            else ""
        )

        # 幂等检查
        if not slot.get("enabled", False):
            msg = f"🏷️ 标签 {index}{display_name} 已经处于关闭状态。"
            msg += "\n🧹 下一条普通消息仍会检查并清除历史残留。"
            yield event.plain_result(msg)
            return

        # 关闭并持久化
        slot_key = f"tag_{index}"
        self.config[slot_key]["enabled"] = False
        self.config.save_config()

        self._tags = []
        self._load_tags()

        msg = f"🏷️ 标签 {index}{display_name} 已关闭。"
        msg += "\n🧹 下一条普通消息会清除历史中残留的标签。"
        yield event.plain_result(msg)

    @tag.command("view", alias={"check"})
    async def view_tags(
        self,
        event: AstrMessageEvent,
        index: int | None = None,
    ):
        """查看标签状态"""
        # 带编号：显示单个标签详情
        if index is not None:
            if (
                not isinstance(index, int)
                or not 1 <= index <= MAX_TAGS
            ):
                yield event.plain_result("⚠️ 标签编号必须是 1–5。")
                return
            yield event.plain_result(self._render_tag_detail(index))
            return

        # 无编号：显示总览
        yield event.plain_result(self._render_tag_overview())

    @tag.command("help")
    async def show_tag_help(self, event: AstrMessageEvent):
        """显示 PromptTags 指令帮助"""
        help_text = (
            "📌 PromptTags 指令\n\n"
            "/tag on <1-5>       开启指定标签\n"
            "/tag off <1-5>      关闭指定标签\n"
            "/tag view           查看全部标签及状态\n"
            "/tag view <1-5>     查看指定标签的完整内容\n"
            "/tag check          与 /tag view 相同\n"
            "/tag help           显示这份帮助\n\n"
            "/pt 可以代替 /tag。\n"
            "例如：/pt view 2"
        )
        yield event.plain_result(help_text)

    # -----------------------------------------------------------------------
    # 展示渲染
    # -----------------------------------------------------------------------

    def _render_tag_overview(self) -> str:
        """渲染标签总览。"""
        lines = ["🏷️ PromptTags 状态\n"]

        for i, slot_key in enumerate(TAG_SLOT_KEYS, start=1):
            slot = self.config.get(slot_key, {})
            if not isinstance(slot, dict):
                slot = {}

            alias = self._get_alias(slot)
            tag_name = self._get_tag_name(slot)
            tag_name_valid = self._is_tag_name_valid(tag_name)
            content = self._get_content(slot)
            has_content = bool(content)
            enabled = slot.get("enabled", False)
            active = self._is_tag_active(slot)

            # 构建显示名
            if alias and tag_name_valid:
                display = f"{alias} — <{tag_name}>"
            elif not alias and tag_name_valid:
                display = f"{tag_name} — <{tag_name}>"
            elif alias and not tag_name_valid:
                display = f"{alias} — 未配置"
            else:
                display = "未配置"

            # 构建状态后缀
            if active:
                suffix = " ✅"
            elif enabled and tag_name_valid and not has_content:
                suffix = " ⚠️ 内容为空"
            elif enabled and not (tag_name_valid and has_content):
                suffix = " ⚠️ 配置无效"
            else:
                suffix = ""

            lines.append(f"{i}. {display}{suffix}")

        return "\n".join(lines)

    def _render_tag_detail(self, index: int) -> str:
        """渲染单个标签详情。"""
        slot = self._resolve_slot(index)
        if slot is None:
            slot = {}

        alias = self._get_alias(slot)
        tag_name = self._get_tag_name(slot)
        tag_name_valid = self._is_tag_name_valid(tag_name)
        content = self._get_content(slot)
        enabled = slot.get("enabled", False)
        valid = self._is_tag_valid(slot)
        active = enabled and valid

        # Alias
        alias_section = (
            f"🏷️ Alias\n{alias}" if alias else "🏷️ Alias\n（未填写）"
        )

        # Tag Name
        tag_name_section = (
            f"🔖 Tag Name\n<{tag_name}>"
            if tag_name_valid
            else "🔖 Tag Name\n（未配置）"
        )

        # Status
        if active:
            status_section = "⚡️ Status\nON ✅"
        elif enabled and not valid:
            status_section = "⚡️ Status\nON ⚠️ 配置无效"
        else:
            status_section = "⚡️ Status\nOFF"

        # Content
        if content:
            content_section = f'📝 Content\n"""\n{content}\n"""'
        else:
            content_section = "📝 Content\n（空）"

        return (
            f"📌 PromptTag #{index}\n\n"
            f"{alias_section}\n\n"
            f"{tag_name_section}\n\n"
            f"{status_section}\n\n"
            f"{content_section}"
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

        每轮无条件扫描所有配置槽位（包括已禁用的标签），
        确保已关闭标签的残留内容也被自动清除。

        priority=1 确保本钩子在 LivingMemory (priority=0) 之前执行。
        这样做的原因：
          LivingMemory 的 <RAG-Faiss-Memory> 清理正则使用 DOTALL 非贪婪匹配。
          若我们的标签内容中包含 <RAG-Faiss-Memory>（作为示例提及，无闭合标签），
          且该标签被注入在 user_message_before 位置（历史中位于真实 RAG 注入之前），
          则 LivingMemory 的正则会从示例提及处一直匹配到真实 RAG 的闭合标签，
          吃掉中间所有内容，导致对话历史被截断。
          先于 LivingMemory 清理我们的标签，可从根本上避免此误匹配。
        """
        cleanup_tags = self._load_all_tags_for_cleanup()

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
                    req.prompt = (
                        before_rag + "\n\n" + block + "\n\n" + from_rag
                    )
                else:
                    req.prompt = prompt + "\n\n" + block
                logger.debug(
                    f"【PromptTags 提示词注入】消息后注入 "
                    f"{len(by_position['user_message_after'])} 个标签"
                )

            # --- 顶部声明注入（最后执行，确保在 req.prompt 绝对顶部）---
            if self._disclaimer:
                req.prompt = (
                    self._disclaimer + "\n\n" + (req.prompt or "")
                )
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

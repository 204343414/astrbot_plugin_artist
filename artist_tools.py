"""LLM 工具用到的**辅助函数**（工具方法本身在 main.py 里）。

为什么 `@filter.llm_tool` 的三个方法必须定义在 `main.py`，不能搬到这里：

    AstrBot 的 star_manager 绑定插件实例的条件是
        `handler.__module__ == 插件主模块路径`
    这里定义的话 `__module__` 是 `data.plugins.<插件>.artist_tools`，
    而插件主模块是 `data.plugins.<插件>.main` —— 永远不相等，实例就永远绑不上去。
    结果：模型调用工具时 `event` 被当成 `self` 吃掉，报
        TypeError: ArtistToolsMixin.draw_image() missing 1 required positional argument: 'event'
    实测对照：LLMTempBan 等能正常工作的插件，工具都直接写在 main.py 里。

所以本文件只放**不依赖 self / 不带装饰器**的东西：
`ToolContext` 数据类、返回值拼装、参考图收集、发图记账等辅助逻辑，
通过 `ArtistToolsMixin` 混进宿主类供 `main.py` 里的工具方法调用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

#: 和 `/draw` 保持一致的宽高比选项。多给几种没有意义：
#: `_openai_size_for_aspect_ratio` 只认这几个，Doubao 那边也是。
ASPECT_RATIOS = ("auto", "1:1", "16:9", "9:16", "4:3", "3:4")

#: 一次调用最多带几张参考图。再多会把提示词挤出上下文，
#: 而且审核那张总览图最多也就能放 9 格。
MAX_REFERENCE_IMAGES = 4


@dataclass
class ToolContext:
    """从一次消息事件里摘出来的纯数据，之后不再碰 event。"""

    user_id: str = ""
    group_id: str = ""
    session_id: str = ""
    #: 这条消息直接带的图（URL 或本地路径）
    attached_images: List[str] = field(default_factory=list)

    @property
    def scope(self) -> str:
        """预算/额度按这个键算：有群用群，没群用个人。"""
        return self.group_id or self.user_id


def _result(ok: bool, message: str, **extra: Any) -> str:
    """工具的统一返回。永远返回字符串 —— 这是 llm_tool 的硬要求。"""
    payload: Dict[str, Any] = {"success": bool(ok), "message": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _clamp_ratio(value: str) -> str:
    text = str(value or "auto").strip()
    return text if text in ASPECT_RATIOS else "auto"


class ArtistToolsMixin:
    """混进 `GeminiArtist`。依赖宿主提供的那些 `_review_*` / `_generate_by_api_type`。"""

    # --- 上下文 -------------------------------------------------------------

    def _tool_context(self, event: AstrMessageEvent) -> ToolContext:
        """把 event 里用得着的东西一次性抓出来。

        为什么不在各工具里直接摸 event：工具函数将来会被别的地方调用
        （定时任务、面板按钮），那时候手里根本没有消息事件。
        """
        user_id = str(event.get_sender_id() or "")
        group_id = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")
        attached: List[str] = []
        try:
            for component in event.get_messages() or []:
                source = str(getattr(component, "url", "") or "").strip()
                if source:
                    attached.append(source)
        except Exception:  # noqa: BLE001
            logger.warning("[Artist] 读取消息附件失败", exc_info=True)
        return ToolContext(
            user_id=user_id,
            group_id=group_id,
            session_id=group_id or user_id,
            attached_images=attached,
        )

    # --- 参考图 -------------------------------------------------------------

    async def _tool_collect_references(
        self,
        ctx: ToolContext,
        *,
        image_index: int = 0,
        reference_bot: bool = False,
        use_character: bool = True,
        use_base: bool = False,
    ) -> Tuple[List[Any], Dict[str, int]]:
        """攒参考图，同时如实数清每类是几张。

        顺序有讲究：**离用户意图最近的排最前**。模型（尤其 Gemini 系）对
        「第一张图是什么」很敏感，我们在提示词里也按位置说「参考图 1 是…」。
        角色立绘放最后，避免它盖掉用户真正想参考的那张。

        返回 ``(图列表, 计数)``。计数必须是**真的加进去了几张**，不能按
        「开关开着」推 —— 开关开着但立绘文件不在是常事，那时候还跟模型说
        「用了角色立绘」，模型就会照着立绘的样子描述，用户一眼看出不对。
        """
        images: List[Any] = []
        counts = {"user": 0, "character": 0, "base": 0}

        def _add(image: Any, kind: str) -> None:
            if image is None or len(images) >= MAX_REFERENCE_IMAGES:
                return
            images.append(image)
            counts[kind] += 1

        # ① 临时参考图（set_drawing_reference 记下的）最优先
        temp = (getattr(self, "temp_reference_context", {}) or {}).get(
            (ctx.user_id, ctx.session_id), {}
        ) or {}
        temp_style = temp.get("style")
        temp_identity = temp.get("identity")
        if temp_style is not None:
            _add(temp_style, "user")

        # ② 这条消息自己带的图
        for source in ctx.attached_images[:MAX_REFERENCE_IMAGES]:
            image = await self.download_pil_image_from_url(source, "消息附件参考图")
            _add(image, "user")

        # ③ 缓存里的历史图
        if image_index > 0:
            lookup_user = str(self.robot_id_from_config) if reference_bot else ctx.user_id
            lookup_group = ctx.group_id or ctx.user_id
            for index in range(1, min(int(image_index), MAX_REFERENCE_IMAGES) + 1):
                cached = await self.get_user_recent_image_pil_from_cache(
                    lookup_user, lookup_group, index
                )
                _add(cached, "user")

        # ④ 角色立绘垫底：它管「长什么样」，不管「画什么」
        if use_character:
            _add(self._load_character_reference_image(), "character")
        elif temp_identity is not None and not counts["user"]:
            # 没有临时画风参考、也没立绘可垫的时候，临时人设图才有意义
            _add(temp_identity, "character")

        # ⑤ 什么都没有、又开了「默认参考图」时兜底。
        #    放最后还是因为「用户真正给的图」永远比兜底重要。
        if use_base and not images:
            loader = getattr(self, "_base_reference_image_or_none", None)
            if callable(loader):
                _add(loader(), "base")
        return images, counts

    def _tool_describe_references(self, counts: Dict[str, int]) -> str:
        """给模型一句人能读懂的「用了几张什么图」，让它能如实转述。

        这里的数据必须是**真的加进去了几张**，不能按「开关开着」推。差别很实在：
        开关开着但立绘文件不存在是常事，那时候还跟模型说「用了角色立绘」，
        模型就会照着立绘的样子描述，用户立刻发现对不上。
        """
        labels = (("user", "用户参考图"), ("character", "角色立绘"), ("base", "默认参考图"))
        parts = [f"{name} {counts[key]} 张" for key, name in labels if counts.get(key)]
        return "、".join(parts) if parts else "没有参考图"

    # --- 额度 ---------------------------------------------------------------

    def _tool_quota_block(self, ctx: ToolContext) -> Optional[str]:
        """额度不够就返回一句现成的话，够就返回 None。

        三次检查（用户日限、群预算）放在一处：以前散在各工具里，
        改一个漏一个必然出事。
        """
        used = self._user_usage(ctx.user_id)
        if self.daily_draw_limit > 0 and used >= self.daily_draw_limit:
            return _result(
                False,
                f"今日画图次数已用完（{used}/{self.daily_draw_limit}）。",
                quota_exhausted=True,
                user_instruction_for_llm=(
                    "次数用完了，请用你自己的语气告诉对方今天画不了了，明天再来。"
                    "不要再调用画图工具。"
                ),
            )
        allowed, spent, _remaining = self._check_budget(ctx.scope)
        if not allowed:
            return _result(
                False,
                f"该群今日画图预算已用完（已消耗 ¥{spent:.2f}）。",
                budget_exceeded=True,
                user_instruction_for_llm=(
                    "预算是管理员设的，不是你的问题。用你的语气说明今天这个群画不了了，"
                    "明天零点重置。不要再调用画图工具。"
                ),
            )
        return None

    # --- 工具 1：画 ---------------------------------------------------------


    # --- 工具 2：参考图 -----------------------------------------------------



    # --- 内部 ---------------------------------------------------------------

    @staticmethod
    def _tool_mtime(path: str) -> str:
        import datetime

        try:
            return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except OSError:
            return ""

    @staticmethod
    def _tool_pick_image(result: Any) -> str:
        """从 `_generate_by_api_type` 的返回里挑出第一张真的存在的图。

        「真的存在」这一条不能省：接口有时返回路径但文件是 0 字节，
        那发出去就是裂图。
        """
        paths = result.get("image_paths", []) if isinstance(result, dict) else []
        for path in paths:
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                return str(path)
        return ""

    async def _tool_deliver(
        self,
        ctx: ToolContext,
        event: AstrMessageEvent,
        image_path: str,
        *,
        prompt: str,
        references: List[Any],
    ) -> bool:
        """发图 + 记账 + 让模型补一句观后感。

        为什么不 `yield` 出去让上层发：llm_tool 的返回值是给模型看的，
        不是给用户看的。真想发出去只能在这里自己调发送接口。
        """
        sent = False
        try:
            sent = bool(await self._send_image_message(event, image_path))
        except Exception:  # noqa: BLE001
            logger.error("[Artist] 工具发图失败", exc_info=True)
            sent = False

        try:
            self._record_user_draw(ctx.user_id)
        except Exception:  # noqa: BLE001
            logger.warning("[Artist] 记录画图次数失败", exc_info=True)
        try:
            self._record_spending(ctx.scope, 1)
        except Exception:  # noqa: BLE001
            logger.warning("[Artist] 记录花费失败", exc_info=True)
        try:
            if self.robot_id_from_config:
                self.store_user_image(
                    str(self.robot_id_from_config),
                    ctx.session_id,
                    image_path,
                    os.path.basename(image_path),
                )
        except Exception:  # noqa: BLE001
            pass

        # 观后感交给宿主原来那套（它自己会判断配置开关）。
        # 只在真的发出去之后才补话 —— 没发出去还让模型点评，等于让它在
        # 空气里夸一张图。
        if sent:
            try:
                await self._post_image_commentary_once(event, prompt, "工具画图结果已发送")
            except Exception:  # noqa: BLE001
                logger.warning("[Artist] 观后感生成失败", exc_info=True)
        return sent

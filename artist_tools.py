"""LLM 工具：让模型自己决定画什么、拿哪张图当参考。

为什么单独一个文件，而不是直接给 `main.py` 里那几个 `gemini_draw` 挂装饰器：

那三个方法（`gemini_draw` / `manage_drawing_reference` / `combine_images_draw`）
的文档字符串写得像 LLM 工具，实现却是**命令式**的 —— 它们 `yield` 出一堆
`event.plain_result`，而且离不开 `event`（要读引用消息、要取 group_id）。
`@filter.llm_tool` 要的是「进去 → 返回一个字符串」，返回 `AsyncGenerator`
会直接把调用链搞坏。这大概也正是它们一直没挂上装饰器的原因：
`main.py` 里 `llm_tool` 一次都没出现过。

所以这里的做法是**另起一套真正独立于消息事件的实现**：

* 所有上下文（用户、群、引用图、历史图缓存）在入口处一次性抓成纯数据；
* 审核逻辑复用 `main.py` 里现成的 `_review_prompt_before_generation` 等；
* 生成复用 `_generate_by_api_type`，发送复用 `_send_image_message`；
* **对本函数外只暴露一个字符串**，里面是结构化 JSON，模型照着自己转述。

关键约定：图片由工具自己发出去，返回给模型的话里明确写「已经发出去了，
不要再调一次」。不然模型会以为工具只给了它一个路径，然后重复调用。
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

    @filter.llm_tool(name="draw_image")
    async def draw_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect_ratio: str = "auto",
        use_character: bool = True,
        use_base: bool = False,
        image_index: int = 0,
        reference_bot: bool = False,
    ):
        """画一张新图并直接发到当前会话。

        什么时候用：对方要你画点什么、或者要你「画一下你自己」。

        **画自己**：对方说「画你」「画一下你自己」「你长什么样」时，把
        `use_character` 留在 True（默认），prompt 描述场景和动作即可 ——
        你的长相会从立绘参考图里来，不需要你在提示词里描述外貌。
        对方若刚从后台换了立绘，新图立刻生效，不用重启。

        什么时候别用：对方只是想聊画图这件事、或者在问你上次那张图的细节。
        也不要为了「显得积极」而在对方没要求时主动画。

        Args:
            prompt(string): 画面描述。写清楚**画什么、什么场景、什么动作、什么风格**。
                别写「画一张好看的图」这种空话。
            aspect_ratio(string): 宽高比，可选 "auto"(默认，交给接口决定)、
                "1:1"(方图/头像)、"16:9"(横屏/风景/壁纸)、"9:16"(竖屏/手机)、
                "4:3"(横向)、"3:4"(竖向)。
            use_character(boolean): 是否带上你的角色立绘当参考，用来保持长相一致。
                画自己、或画一个有固定形象的角色时保持 True。画纯风景/静物时可以关掉。
            use_base(boolean): 什么都没参考时，是否用「默认参考图」兜底保持画风统一。
                默认 False —— 只在对方说「还按之前那个画风」但手边又没有具体图时打开。
            image_index(number): 额外引用缓存里的几张历史图当参考。0=不引用（默认），
                1=引用最近 1 张，2=最近 2 张。对方说「照着刚才那张画」时用它。
            reference_bot(boolean): image_index 引用的是不是你之前画过的图。
                对方说「把刚才你画的那张改成…」时设 True。
        """
        ctx = self._tool_context(event)
        if not self.api_keys:
            return _result(
                False,
                "管理员还没配置画图 API 密钥。",
                user_instruction_for_llm="告诉对方现在画不了（管理员没配密钥），不要再重试。",
            )

        text = str(prompt or "").strip()
        if not text:
            return _result(
                False,
                "提示词是空的。",
                user_instruction_for_llm="问对方想画什么，具体一点。",
            )

        blocked = self._tool_quota_block(ctx)
        if blocked:
            return blocked

        ratio = _clamp_ratio(aspect_ratio)
        references, ref_counts = await self._tool_collect_references(
            ctx,
            image_index=max(0, int(image_index or 0)),
            reference_bot=bool(reference_bot),
            use_character=bool(use_character),
            use_base=bool(use_base),
        )

        # 审核：和 /draw 走同一套。别为了"新入口"另写一份规则，
        # 两份迟早不一致，而不一致的那一份会放过去不该放的东西。
        if references:
            reference_review = await self._review_reference_images_safety(
                references, event=event
            )
            if not reference_review.get("allowed", True):
                return _result(
                    False,
                    "参考图没通过安全识别："
                    + str(reference_review.get("reason") or "包含高风险内容"),
                    user_instruction_for_llm="如实告诉对方参考图不能用，换个图或换个描述。",
                )

        review = await self._review_prompt_before_generation(event, text, references)
        if not review.get("allowed", True):
            reason = str(review.get("reason") or "提示词没通过审核")
            return _result(
                False,
                f"提示词被拦下了：{reason}",
                blocked=True,
                user_instruction_for_llm=(
                    f"用你自己的语气拒绝这次画图要求（原因：{reason}），"
                    "不要说教、不要重复对方的原话。"
                ),
            )

        final_prompt = text
        if ratio != "auto":
            final_prompt = f"{text} Image aspect ratio: {ratio}."

        try:
            result = await self._generate_by_api_type(final_prompt, references, ratio)
        except Exception as exc:  # noqa: BLE001
            logger.error("[Artist] 工具画图失败: %s", exc, exc_info=True)
            return _result(
                False,
                f"画图接口报错：{type(exc).__name__}",
                user_instruction_for_llm=(
                    "接口这次没出图。用你的语气说一声没画成，不要编造图片内容。"
                ),
            )

        image_path = self._tool_pick_image(result)
        if not image_path:
            return _result(
                False,
                "画图接口没有返回有效图片。",
                user_instruction_for_llm="说一声这张没画出来，可以再试一次。",
            )

        sent = await self._tool_deliver(
            ctx, event, image_path, prompt=text, references=references
        )
        if not sent:
            # 生成成功但没发出去（QQ 官方分片失败、消息被拦…）。这时**不能**
            # 说「已经发了」：模型会顺着这句话跟对方聊一张图，而对方什么都没看到。
            return _result(
                False,
                "图画出来了，但没能发到会话里。",
                image_sent=False,
                aspect_ratio=ratio,
                references=self._tool_describe_references(ref_counts),
                user_instruction_for_llm=(
                    "图没发出去。用你的语气说一声这次发送失败了，别描述图长什么样，"
                    "也别假装发过了。"
                ),
            )
        return _result(
            True,
            "图片已生成并**已经发到会话里了**。",
            image_sent=True,          # ← 模型最容易忽略的一条，写在最显眼处
            aspect_ratio=ratio,
            references=self._tool_describe_references(ref_counts),
            user_instruction_for_llm=(
                "图已经发出去了，你**不要**再调用画图工具、也不要再描述这张图长什么样 "
                "——对方已经看到了。用一两句自己的话说点什么就好。"
            ),
        )

    # --- 工具 2：参考图 -----------------------------------------------------

    @filter.llm_tool(name="set_drawing_reference")
    async def set_drawing_reference(
        self,
        event: AstrMessageEvent,
        action: str = "set",
        target: str = "style",
        image_index: int = 1,
    ):
        """设置或清除**临时**参考图（只影响当前会话，不写进配置）。

        什么时候用：对方说「以后都按这个画风画」「记住这个小动物」「你现在换成
        这个样子」—— 他们想让接下来的几张图沿用某个参考。

        「临时」是关键：这只活到本会话结束/被清除为止。要永久换掉人设立绘，
        得去后台面板上传（那是管理员的事，不是你的）。

        Args:
            action(string): "set" 设置，"clear" 清除。默认 "set"。
            target(string): "style" 画风/构图参考（推荐，安全）；
                "identity" 身份参考（会改变"你长什么样"，只在对方明确要求
                「你变成…」时用）；"all" 全部清除（仅 clear 时有效）。
            image_index(number): 用缓存里第几张图，1=最新一张（默认）。
        """
        ctx = self._tool_context(event)
        act = str(action or "set").strip().lower()
        kind = str(target or "style").strip().lower()
        if kind not in ("style", "identity", "all"):
            return _result(False, f"不认识的 target：{kind}，只能是 style / identity / all。")

        session_key = (ctx.user_id, ctx.session_id)
        store = getattr(self, "temp_reference_context", None)
        if store is None:
            store = {}
            self.temp_reference_context = store

        if act == "clear":
            if kind == "all":
                store.pop(session_key, None)
                return _result(True, "已清除全部临时参考图。")
            if store.get(session_key, {}).pop(kind, None) is not None:
                return _result(True, f"已清除临时{kind}参考。")
            return _result(True, f"本来就没有设置{kind}参考。")

        if act != "set":
            return _result(False, f"不认识的 action：{act}，只能是 set / clear。")

        image = None
        for source in ctx.attached_images:
            image = await self.download_pil_image_from_url(source, "临时参考图")
            if image is not None:
                break
        if image is None:
            image = await self.get_user_recent_image_pil_from_cache(
                ctx.user_id, ctx.session_id, max(1, int(image_index or 1))
            )
        if image is None:
            return _result(
                False,
                "没找到可用的图。",
                user_instruction_for_llm=(
                    "请对方发一张图或引用一张图，再说一次这个要求。"
                ),
            )

        review = await self._review_reference_images_safety([image], event=event)
        if not review.get("allowed", True):
            return _result(
                False,
                "这张参考图没通过安全识别："
                + str(review.get("reason") or "包含高风险内容"),
            )

        store.setdefault(session_key, {})[kind] = image
        if kind == "identity":
            return _result(
                True,
                "已记下临时人设参考，之后画你会是这个样子。",
                user_instruction_for_llm="确认一下就好：之后画你会用这张图的样子，直到清除。",
            )
        return _result(
            True,
            "已记下临时画风参考。",
            user_instruction_for_llm="确认一下就好：之后的图会参考这张的画风/构图。",
        )

    @filter.llm_tool(name="list_drawing_references")
    async def list_drawing_references(self, event: AstrMessageEvent):
        """查当前会话设置了哪些临时参考图、角色立绘有没有配好。

        对方问「你现在用着什么参考」「画风记没记住」「你的立绘换了吗」时用它。
        """
        ctx = self._tool_context(event)
        store = getattr(self, "temp_reference_context", {}) or {}
        active = store.get((ctx.user_id, ctx.session_id), {}) or {}
        character = ""
        path = str(getattr(self, "character_image_path", "") or "")
        if path and os.path.isfile(path):
            character = os.path.basename(path)
        return _result(
            True,
            "当前参考图状态。",
            temporary=sorted(active),
            character_portrait=character or "(未配置)",
            # 立绘的修改时间是最有用的证据：对方说"我明明换了立绘没生效"，
            # 看一眼时间就知道是不是没上传成功。
            character_mtime=self._tool_mtime(path),
        )

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

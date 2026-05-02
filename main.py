from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Nodes, Reply, BaseMessageComponent
import asyncio
from io import BytesIO
import time
import os
import random
from google import genai
from PIL import Image as PILImage
from google.genai.types import HttpOptions
from astrbot.core.utils.io import download_file
import functools
from typing import List, Optional, Dict, Tuple, AsyncGenerator, Any
from openai import OpenAI
from collections import deque
import base64
import json
from pathlib import Path
import re



@register("gemini_artist_plugin", "nichinichisou", "基于 Google Gemini 和 OpenRouter 格式 API 的AI绘画插件", "1.5.0")
class GeminiArtist(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        self.config = config
        
        # API 类型由用户手动选择
        self.api_type = config.get("api_type", "Google")
        api_key_list_from_config = config.get("api_key", [])
        self.api_base_url_from_config = config.get("api_base_url", "https://generativelanguage.googleapis.com")
        self.model_name_from_config = config.get("model", "gemini-2.0-flash-exp")
        

        
        # 如果配置了单独的API地址，用它覆盖
        image_api_base_override = config.get("image_api_base_url", "")
        if image_api_base_override and image_api_base_override.strip():
            self.api_base_url_from_config = image_api_base_override.strip()
            logger.info(f"使用配置的画图API地址覆盖: {self.api_base_url_from_config}")
        
        self.group_whitelist = config.get("group_whitelist", [])
        self.robot_id_from_config = config.get("robot_self_id") 
        self.random_api_key_selection = config.get("random_api_key_selection", False)
        self.enable_base_reference_image = config.get("enable_base_reference_image", False)
        self.base_reference_image_path = config.get("base_reference_image_path", "")
        # 存储正在等待输入的用户，键为 (user_id, group_id)
        self.waiting_users = {}  # {(user_id, group_id): expiry_time}
        # 存储用户收集到的文本和图片，键为 (user_id, group_id)
        self.user_inputs = {} # {(user_id, group_id): {'messages': [{'text': '', 'images': [], 'timestamp': float}]}}
        self.wait_time_from_config = config.get("wait_time", 30)

        # 存储用户发送的图片URL缓存
        self.image_history_cache: Dict[Tuple[str, str], deque[Tuple[str, Optional[str]]]] = {}
        # 临时参考图存储 - 区分人设(Identity)和风格(Style)
        # {(user_id, group_id): {'identity': PILImage, 'style': PILImage}}
        self.temp_reference_context: Dict[Tuple[str, str], Dict[str, PILImage.Image]] = {}
        self.max_cached_images = self.config.get("max_cached_images", 5)

        # 设置插件的临时文件目录
        shared_data_path = Path(__file__).resolve().parent.parent.parent
        self.plugin_temp_base_dir = os.path.join(shared_data_path, "gemini_artist_temp")
        os.makedirs(self.plugin_temp_base_dir, exist_ok=True)
        self.temp_dir = self.plugin_temp_base_dir

        self.enable_hinting = self.config.get("enable_hinting", True)

        self.api_keys = [
            key.strip()
            for key in api_key_list_from_config
            if isinstance(key, str) and key.strip()
        ]
        self.current_api_key_index = 0

        if not self.api_keys:
            logger.warning("Gemini API密钥未配置或配置为空。插件可能无法正常工作。")

        # 配置临时文件清理任务
        self.cleanup_interval_seconds = self.config.get("temp_cleanup_interval_seconds", 3600 * 6)
        self.cleanup_older_than_seconds = self.config.get("temp_cleanup_files_older_than_seconds", 86400 * 3)
        self._background_cleanup_task = None

        # 启动后台定时清理任务
                # ===== 角色反应图功能配置 =====
        self.enable_self_reaction = config.get("enable_self_reaction", False)
        self.character_image_path = config.get("character_image_path", "")
        self.enable_self_reaction_review = config.get("enable_self_reaction_review", True)
        self.reaction_cooldown: Dict[Tuple[str, str], float] = {}
        self.reaction_cooldown_seconds = 600  # 10分钟冷却
                # ===== 每日群组预算限制 =====
        self.daily_group_budget = float(config.get("daily_group_budget", 5.0))
        self.cost_per_image = float(config.get("cost_per_image", 0.2))
        # {group_id_str: {'date': 'YYYY-MM-DD', 'spent': float}}
        self.group_spending: Dict[str, Dict[str, Any]] = {}
        if self.cleanup_interval_seconds > 0:
            self._background_cleanup_task = asyncio.create_task(self._periodic_temp_dir_cleanup())
            logger.info(f"GeminiArtist: 已启动定时清理任务，每隔 {self.cleanup_interval_seconds} 秒清理临时目录 {self.temp_dir} 中超过 {self.cleanup_older_than_seconds} 秒的文件。")
        else:
            logger.info("GeminiArtist: 定时清理功能已禁用 (temp_cleanup_interval_seconds <= 0)。")
    def _get_today_str(self) -> str:
        """获取今天的日期字符串，用于预算重置判断。"""
        from datetime import date
        return date.today().isoformat()

    def _check_budget(self, group_id: str) -> Tuple[bool, float, float]:
        """
        检查群组今日预算是否充足。
        Returns: (allowed, spent_today, remaining)
        """
        if self.daily_group_budget <= 0:
            return True, 0.0, float('inf')  # 0表示不限制
        today = self._get_today_str()
        if group_id not in self.group_spending or self.group_spending[group_id].get('date') != today:
            self.group_spending[group_id] = {'date': today, 'spent': 0.0}
        spent = self.group_spending[group_id]['spent']
        remaining = round(max(0, self.daily_group_budget - spent), 2)
        allowed = remaining >= self.cost_per_image
        return allowed, spent, remaining

    def _record_spending(self, group_id: str, num_images: int) -> Tuple[float, float, float]:
        """
        记录生成图片的花费。
        Returns: (cost_this_time, total_spent_today, remaining)
        """
        if self.daily_group_budget <= 0 or num_images <= 0:
            return 0.0, 0.0, float('inf')
        today = self._get_today_str()
        if group_id not in self.group_spending or self.group_spending[group_id].get('date') != today:
            self.group_spending[group_id] = {'date': today, 'spent': 0.0}
        cost = round(num_images * self.cost_per_image, 2)
        self.group_spending[group_id]['spent'] = round(self.group_spending[group_id]['spent'] + cost, 2)
        spent_today = self.group_spending[group_id]['spent']
        remaining = round(max(0, self.daily_group_budget - spent_today), 2)
        logger.info(f"预算记录: 群{group_id} 本次¥{cost}, 今日累计¥{spent_today}/¥{self.daily_group_budget}, 剩余¥{remaining}")
        return cost, spent_today, remaining
    def _blocking_cleanup_temp_dir_logic(self, older_than_seconds: int) -> Tuple[int, int]:
        """
        同步执行临时目录清理的逻辑，移除旧文件。
        """
        if not os.path.isdir(self.temp_dir):
            return 0, 0
        now, cleaned_count, error_count = time.time(), 0, 0
        try:
            for filename in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        if (now - os.path.getmtime(file_path)) > older_than_seconds:
                            os.remove(file_path)
                            cleaned_count += 1
                except Exception as e_file:
                    logger.error(f"清理临时文件 {file_path} 时出错: {e_file}")
                    error_count += 1
        except Exception as e_list:
            logger.error(f"列出目录 {self.temp_dir} 进行清理时出错: {e_list}")
            error_count += 1
        if cleaned_count > 0 or error_count > 0:
            logger.info(f"临时目录清理: 移除 {cleaned_count} 文件, 发生 {error_count} 错误 @ {self.temp_dir}")
        return cleaned_count, error_count
    def _normalize_openai_base_url(self, base_url: str) -> str:
        """把用户填的 api_base_url 归一化成 .../v1 结尾，兼容中转站。"""
        url = (base_url or "").strip().rstrip("/")
        if not url:
            return "https://api.openai.com/v1"
        if not url.endswith("/v1"):
            url = url + "/v1"
        return url

    async def openai_image_generate(
        self,
        text_prompt: str,
        images_pil: Optional[List[PILImage.Image]] = None,
        aspect_ratio: str = "1:1",
    ) -> Dict[str, Any]:
        """
        OpenAI gpt-image-2 生图/改图：
        - 文生图：/v1/images/generations
        - 图生图/编辑：/v1/images/edits
        返回结构与 gemini_generate/doubao_generate 对齐：{'text': str, 'image_paths': [local_path,...]}
        """
        from openai import OpenAI
        import base64

        if not self.api_keys:
            raise ValueError("没有配置API密钥 (api_keys)")

        images_pil = images_pil or []

        # 最稳尺寸：避免不同网关/中转对“任意尺寸”兼容问题
        # OpenAI 图片指南里常用竖横方三档；quality 支持 auto/low/medium/high。 <!--citation:2-->
        if aspect_ratio in ("9:16", "3:4"):
            size = "1024x1536"
        elif aspect_ratio in ("16:9", "4:3"):
            size = "1536x1024"
        else:
            size = "1024x1024"

        quality = str(self.config.get("openai_image_quality", "auto")).strip() or "auto"

        base_url = self._normalize_openai_base_url(self.api_base_url_from_config)
        model = self.model_name_from_config or "gpt-image-2"

        key_indices_to_try = list(range(len(self.api_keys)))
        if self.random_api_key_selection:
            random.shuffle(key_indices_to_try)
        else:
            key_indices_to_try = [
                (self.current_api_key_index + i) % len(self.api_keys)
                for i in range(len(self.api_keys))
            ]

        last_exception = None

        for attempt_num, key_idx_to_use in enumerate(key_indices_to_try):
            api_key = self.api_keys[key_idx_to_use]
            try:
                logger.info(
                    f"openai_image_generate: 尝试API密钥索引 {key_idx_to_use} "
                    f"(尝试 {attempt_num + 1}/{len(self.api_keys)})"
                )
                logger.info(
                    f"openai_image_generate: base_url={base_url}, model={model}, size={size}, quality={quality}"
                )

                client = OpenAI(api_key=api_key, base_url=base_url)

                # 同步 SDK -> 丢到线程，避免阻塞事件循环
                if images_pil:
                    # 参考 OpenAI 图片指南：images.edit + gpt-image-2，返回 data[0].b64_json。 <!--citation:2-->
                    tmp_files: List[str] = []
                    file_handles: List[Any] = []
                    try:
                        os.makedirs(self.temp_dir, exist_ok=True)
                        for i, img in enumerate(images_pil):
                            fp = os.path.join(
                                self.temp_dir,
                                f"openai_ref_{time.time()}_{random.randint(100,999)}_{i}.png",
                            )
                            img.save(fp, format="PNG")
                            tmp_files.append(fp)
                            file_handles.append(open(fp, "rb"))

                        def _call_edit():
                            # 注意：这里不传 mask，做“整体参考图编辑”
                            return client.images.edit(
                                model=model,
                                image=file_handles,
                                prompt=text_prompt,
                                size=size,
                                quality=quality,
                            )

                        rsp = await asyncio.to_thread(_call_edit)
                    finally:
                        for fh in file_handles:
                            try:
                                fh.close()
                            except Exception:
                                pass
                else:
                    def _call_gen():
                        return client.images.generate(
                            model=model,
                            prompt=text_prompt,
                            size=size,
                            quality=quality,
                        )

                    rsp = await asyncio.to_thread(_call_gen)

                if not rsp or not getattr(rsp, "data", None):
                    raise ValueError("OpenAI 图片API返回空 data")

                item0 = rsp.data[0]
                b64 = getattr(item0, "b64_json", None) or (
                    item0.get("b64_json") if isinstance(item0, dict) else None
                )
                revised = getattr(item0, "revised_prompt", None) or (
                    item0.get("revised_prompt") if isinstance(item0, dict) else None
                )

                result = {"text": "", "image_paths": []}
                if revised:
                    result["text"] = str(revised).strip()

                if not b64:
                    raise ValueError("OpenAI 图片API未返回 b64_json")

                img_bytes = base64.b64decode(b64)
                os.makedirs(self.temp_dir, exist_ok=True)
                out_fp = os.path.join(
                    self.temp_dir, f"openai_gen_{time.time()}_{random.randint(100,999)}.png"
                )
                with open(out_fp, "wb") as f:
                    f.write(img_bytes)

                result["image_paths"].append(out_fp)

                if not self.random_api_key_selection:
                    self.current_api_key_index = (key_idx_to_use + 1) % len(self.api_keys)

                return result

            except Exception as e:
                last_exception = e
                logger.error(
                    f"openai_image_generate: API处理失败 (密钥 {key_idx_to_use}): {e}",
                    exc_info=True,
                )

        if last_exception:
            raise last_exception
        raise ValueError("openai_image_generate: 所有API密钥均尝试失败。")

    async def _post_image_commentary_once(
        self,
        event: AstrMessageEvent,
        user_prompt: str,
        image_desc: str,
    ) -> None:
        """
        生图成功后：调用一次“当前会话聊天LLM”让它发观后感（写入对话历史，修复装失忆）。
        AstrBot 官方推荐：get_current_chat_provider_id + llm_generate。 <!--citation:3-->
        """
        if not self.config.get("enable_post_image_commentary", True):
            return

        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)

            commentary_prompt = (
                "你刚刚生成并发送了一张图片给用户。\n"
                f"用户原始需求：{user_prompt}\n"
                f"图片文字描述（工具侧）：{image_desc}\n\n"
                "现在请用你平时的语气，发 1~3 句“交付+观后感”。要求：\n"
                "1) 明确表态：这图是你生成的（不要问用户怎么P的）。\n"
                "2) 简短点评效果（夸/吐槽都行）。\n"
                "3) 给一个下一步引导：要不要我再改比例/表情/构图/风格/文字等。\n"
                "禁止：再调用任何画图工具。\n"
            )

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=commentary_prompt,
            )
            text = (getattr(llm_resp, "completion_text", "") or "").strip()
            if text:
                await event.send(event.plain_result(text))
        except Exception as e:
            logger.warning(f"_post_image_commentary_once 失败（不影响主流程）: {e}", exc_info=True)

    async def _generate_by_api_type(
        self,
        text_prompt: str,
        images_pil: Optional[List[PILImage.Image]] = None,
        aspect_ratio: str = "auto",
    ) -> Dict[str, Any]:
        """统一入口：减少你四处复制 if/elif/else 出错。"""
        images_pil = images_pil or []
        if self.api_type == "OpenRouter":
            return await self.openrouter_generate(text_prompt, images_pil)
        elif self.api_type == "Doubao":
            return await self.doubao_generate(text_prompt, images_pil, aspect_ratio)
        elif self.api_type == "OpenAI":
            return await self.openai_image_generate(text_prompt, images_pil, aspect_ratio)
        else:
            return await self.gemini_generate(text_prompt, images_pil)

    async def _periodic_temp_dir_cleanup(self):
        """
        周期性地清理临时目录的后台任务。
        """
        while True:
            await asyncio.sleep(self.cleanup_interval_seconds)
            logger.info(f"定时清理触发: {self.temp_dir}")
            try:
                cleanup_func = functools.partial(
                    self._blocking_cleanup_temp_dir_logic, self.cleanup_older_than_seconds
                )
                await asyncio.to_thread(cleanup_func)
            except asyncio.CancelledError:
                logger.info("定时清理任务已取消。")
                break
            except Exception as e:
                logger.error(f"定时清理任务出错: {e}", exc_info=True)
    def store_user_image(self, user_id: str, group_id: str, image_url: str, original_filename: Optional[str] = None) -> None:
        """
        将用户发送的图片URL存储到缓存中。
        """
        key = (user_id, group_id)
        if key not in self.image_history_cache:
            self.image_history_cache[key] = deque(maxlen=self.max_cached_images)
        self.image_history_cache[key].append((image_url, original_filename))
        logger.debug(f"已存储用户 {user_id} group_id {group_id} 图片URL: {image_url} (缓存 {len(self.image_history_cache[key])}/{self.max_cached_images})")

    async def download_pil_image_from_url(self, image_url: str, context_description: str = "图片") -> Optional[PILImage.Image]:
        """
        从给定的 URL/本地路径/dataURL/fileURL 获取图片并返回 PIL Image(RGBA)。
        兼容：
        - http(s)://...
        - data:image/...;base64,...
        - 本地路径：D:\\xx\\a.png 或 /home/xx/a.png
        - file://D:\\xx\\a.png 或 file:///D:/xx/a.png
        """
        from urllib.parse import unquote
    
        if not image_url or not isinstance(image_url, str):
            return None
    
        image_url = image_url.strip()
        logger.info(f"尝试加载{context_description}: {image_url[:200]}")
    
        # 1) data url
        if image_url.startswith("data:image"):
            try:
                header, encoded = image_url.split(",", 1)
                image_bytes = base64.b64decode(encoded)
                img_pil = PILImage.open(BytesIO(image_bytes))
                img_pil.load()
                return img_pil.convert("RGBA") if img_pil.mode != "RGBA" else img_pil
            except Exception as e:
                logger.error(f"Data URL 解码失败: {e}", exc_info=True)
                return None
    
        # 2) file:// url  -> 本地路径
        if image_url.lower().startswith("file://"):
            try:
                local_path = unquote(image_url[7:])  # 去掉 file://
                # 兼容 file:///D:/xxx 这种
                if local_path.startswith("/") and len(local_path) >= 3 and local_path[2] == ":":
                    local_path = local_path[1:]
                local_path = local_path.replace("/", os.sep)
    
                if os.path.exists(local_path) and os.path.isfile(local_path):
                    img_pil = PILImage.open(local_path)
                    img_pil.load()
                    return img_pil.convert("RGBA") if img_pil.mode != "RGBA" else img_pil
    
                logger.warning(f"file:// 指向的本地文件不存在: {local_path}")
                return None
            except Exception as e:
                logger.error(f"解析 file:// 失败: {e}", exc_info=True)
                return None
    
        # 3) 直接本地路径
        if os.path.exists(image_url) and os.path.isfile(image_url):
            try:
                img_pil = PILImage.open(image_url)
                img_pil.load()
                return img_pil.convert("RGBA") if img_pil.mode != "RGBA" else img_pil
            except Exception as e:
                logger.error(f"加载本地图片失败: {e}", exc_info=True)
                return None
    
        # 4) http(s) 下载
        if not (image_url.startswith("http://") or image_url.startswith("https://")):
            logger.warning(f"未知图片引用格式，无法处理: {image_url[:120]}")
            return None
    
        # ===== 以下保留你原来的 download_file 逻辑（精简版）=====
        ext = ".png"
        try:
            path_part = image_url.split("?")[0].split("#")[0]
            base_name = os.path.basename(path_part)
            _, url_ext = os.path.splitext(base_name)
            if url_ext and url_ext.startswith(".") and len(url_ext) <= 5:
                ext = url_ext.lower()
        except Exception:
            pass
    
        filename = f"gemini_artist_temp_{time.time()}_{random.randint(1000,9999)}{ext}"
        target_file_path = os.path.join(self.temp_dir, filename)
        os.makedirs(self.temp_dir, exist_ok=True)
    
        try:
            await download_file(url=image_url, path=target_file_path, show_progress=False)
    
            if os.path.exists(target_file_path) and os.path.getsize(target_file_path) > 0:
                img_pil = PILImage.open(target_file_path)
                img_pil.load()
                return img_pil.convert("RGBA") if img_pil.mode != "RGBA" else img_pil
    
            logger.error(f"download_file 下载后文件无效: {target_file_path}")
            return None
    
        except Exception as e:
            logger.error(f"HTTP 下载图片失败: {e}", exc_info=True)
            return None
    def _load_base_reference_image(self) -> Optional[PILImage.Image]:
        """
        从配置的路径加载默认的基础参考图。
        """
        if not self.base_reference_image_path:
            return None

        # 将路径相对于 AstrBot 根目录解析
        astrbot_root = Path(__file__).resolve().parent.parent.parent.parent
        image_path = Path(self.base_reference_image_path)
        if not image_path.is_absolute():
            image_path = astrbot_root / image_path

        if image_path.exists() and image_path.is_file():
            try:
                logger.info(f"正在加载默认参考图: {image_path}")
                img_pil = PILImage.open(image_path)
                img_pil.load()  # 确保图片数据已加载
                # 转换为RGBA以获得最佳兼容性
                return img_pil.convert('RGBA') if img_pil.mode != 'RGBA' else img_pil
            except Exception as e:
                logger.error(f"加载默认参考图失败: {image_path}, 错误: {e}")
                return None
        else:
            logger.warning(f"配置的默认参考图路径不存在或不是一个文件: {image_path}")
            return None
    def _load_character_reference_image(self) -> Optional[PILImage.Image]:
        """
        加载角色人设参考图（用于反应图功能）。
        """
        if not self.character_image_path:
            logger.warning("角色人设参考图路径未配置 (character_image_path)")
            return None

        astrbot_root = Path(__file__).resolve().parent.parent.parent.parent
        image_path = Path(self.character_image_path)
        if not image_path.is_absolute():
            image_path = astrbot_root / image_path

        if image_path.exists() and image_path.is_file():
            try:
                logger.info(f"正在加载角色人设参考图: {image_path}")
                img_pil = PILImage.open(image_path)
                img_pil.load()
                return img_pil.convert('RGBA') if img_pil.mode != 'RGBA' else img_pil
            except Exception as e:
                logger.error(f"加载角色人设参考图失败: {image_path}, 错误: {e}")
                return None
        else:
            logger.warning(f"角色人设参考图路径不存在: {image_path}")
            return None

    async def _review_generated_image(self, event: AstrMessageEvent, image_path: str, scene_description: str) -> bool:
        """
        使用当前LLM审核生成的图片，返回True通过，False不通过。
        """
        if not self.enable_self_reaction_review:
            return True
        
        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            if not provider:
                logger.warning("无法获取当前LLM provider，跳过图片审核")
                return True
            
            review_prompt = f"""快速检查这张图：
1. 只有一个主要角色（可有局部肢体从边缘伸入）
2. 背景简洁
3. 角色有清晰表情
4. 无严重画面问题

预期：{scene_description}

只回复OK或NO"""

            llm_resp = await provider.text_chat(
                prompt=review_prompt,
                image_urls=[image_path]
            )
            
            if llm_resp and hasattr(llm_resp, 'completion_text'):
                result_text = llm_resp.completion_text.strip().upper()
                is_pass = "OK" in result_text
                logger.info(f"图片审核: {'通过' if is_pass else '未通过'}, 回复: {result_text}")
                return is_pass
            return True
        except Exception as e:
            logger.error(f"图片审核出错: {e}", exc_info=True)
            return True

    def _build_reaction_prompt(self, scene_description: str, has_reference_meme: bool = False) -> str:
        """
        构建角色反应图的生成prompt。
        """
        if has_reference_meme:
            return f"""参考图1是角色人设，参考图2是用户提供的表情包。
生成新图让角色模仿表情包内容。

要求：
- 保持角色外貌与参考图1一致
- 不要改动参考图2的表情、动作、氛围、画风、

补充：{scene_description}"""
        else:
            return f"""基于参考图1角色形象，完全把参考图1内的角色外貌特征移植到参考图2内的角色。

场景：{scene_description}

要求：
- POV主观视角但镜头微微偏斜（3/4侧面或轻微倾斜角度），不要完全正面呆视镜头，要自然有动态感像抓拍
- 如果场景只涉及角色自己，则只画角色solo
- 如果场景涉及与他人互动（如被公主抱、拥抱、牵手等）：
  * 方案A：只画互动方的无任何信息特征的局部肢体（手、手臂）从画面边缘伸入
  * 方案B：互动方画为极简无脸人形——正常肤色、完全空白的脸（无五官）、无发型细节、身体只有简单轮廓，类似火柴人或mannequin人偶，素描练习模特的风格
  * 绝对不要给互动方任何身份特征！
- 背景简洁干净（纯色或简单渐变）
- 参考图中的角色为画面绝对主体。
- 镜头不要
- 严格保持角色外貌与参考图绝对一致！！！
- 图中不要出现任何文字
- 氛围可以是可爱、搞笑、温馨、或擦边"""
    
    async def get_user_recent_image_pil_from_cache(self, user_id: str, group_id: str, index: int = 1) -> Optional[PILImage.Image]:
        """
        从用户图片缓存中获取指定索引的图片并下载为PIL Image对象。
        """
        key = (user_id, group_id)
        if key not in self.image_history_cache or not self.image_history_cache[key]:
            logger.debug(f"缓存中未找到用户 {user_id} group_id {group_id} 的图片URL。")
            return None
        cached_items = list(self.image_history_cache[key])
        if not (0 < index <= len(cached_items)):
            logger.debug(f"请求的图片URL索引 {index} 超出用户 {user_id} group_id {group_id} 缓存范围 ({len(cached_items)} 条)。")
            return None
        image_ref_str, _ = cached_items[-index]
        if image_ref_str.startswith("data:image"):
            logger.info(f"从缓存加载Base64 Data URL (用户 {user_id}, 上下文 {group_id}, 索引 {index})")
            try:
                header, encoded = image_ref_str.split(",", 1)
                image_bytes = base64.b64decode(encoded)
                pil_image = PILImage.open(BytesIO(image_bytes))
                return pil_image.convert('RGBA') if pil_image.mode != 'RGBA' else pil_image
            except Exception as e:
                logger.error(f"从缓存的Data URL解码图片失败: {e}")
                return None
        elif image_ref_str.startswith("http://") or image_ref_str.startswith("https://"):
            logger.info(f"从缓存加载HTTP URL并下载 (用户 {user_id}, 上下文 {group_id}, 索引 {index}): {image_ref_str}")
            return await self.download_pil_image_from_url(image_ref_str, f"缓存图片 (HTTP)")
        elif os.path.exists(image_ref_str): # 假设是本地文件路径
             logger.info(f"从缓存加载本地文件路径 (用户 {user_id}, 上下文 {group_id}, 索引 {index}): {image_ref_str}")
             try:
                pil_image = PILImage.open(image_ref_str)
                return pil_image.convert('RGBA') if pil_image.mode != 'RGBA' else pil_image
             except Exception as e:
                logger.error(f"从缓存的本地路径加载图片失败: {e}")
                return None
        else:
            logger.warning(f"缓存中的图片引用格式未知或无效: {image_ref_str[:100]}...")
            return None

    @filter.event_message_type(EventMessageType.ALL)
    async def cache_user_images(self, event: AstrMessageEvent):
        """
        监听所有消息，将用户发送的图片URL缓存起来。
        """
        if not hasattr(event, 'message_obj') or not hasattr(event.message_obj, 'type'):
            return
        user_id = event.get_sender_id()
        if self.robot_id_from_config and user_id == self.robot_id_from_config:
            return
        group_id = event.message_obj.group_id
        if self.group_whitelist:
            identifier_to_check = event.message_obj.group_id if event.message_obj.group_id else user_id
            if str(identifier_to_check) not in [str(whitelisted_id) for whitelisted_id in self.group_whitelist]:
                return
        if group_id == "":
            group_id = user_id
            logger.debug(f"收到来自用户 {user_id} group_id {group_id} 的消息。")
        for msg_component in event.get_messages():
            if isinstance(msg_component, Image) and hasattr(msg_component, 'url') and msg_component.url:
                self.store_user_image(user_id, group_id, msg_component.url, getattr(msg_component, 'file', None))

    @filter.llm_tool(name="gemini_draw")
    async def gemini_draw(self, event: AstrMessageEvent, prompt: str, image_index: int = 0, reference_bot: bool = False, aspect_ratio: str = "1:1") -> AsyncGenerator[Any, None]:
        '''
        AI图像生成与编辑工具。支持文生图、图生图、图像编辑等多种功能。
        Args:
            prompt (string): 图像生成或编辑的详细描述。
            image_index (number, optional): 引用历史图片数量。0=不引用，1=引用最新1张，2=引用最新2张。默认0。
            reference_bot (boolean, optional): 是否引用机器人之前生成的图片。默认False。
            aspect_ratio (string, optional): 图片宽高比。可选: "1:1"(方形), "16:9"(横屏), "9:16"(竖屏), "4:3"(横向), "3:4"(竖向)。根据内容自动选择，风景用16:9，人像用3:4。默认"1:1"。
        '''
        if not self.api_keys:
            yield event.plain_result("请联系管理员配置API密钥。")
            return
        if not hasattr(event, 'message_obj') or not hasattr(event.message_obj, 'type'):
            logger.error(f"gemini_draw: 事件对象缺少 message_obj 或 type 属性。")
            yield event.plain_result("处理消息时出错。")
            return

        command_sender_id = event.get_sender_id()
        group_id = event.message_obj.group_id

        if self.group_whitelist and str(event.message_obj.group_id or command_sender_id) not in map(str, self.group_whitelist):
            return
        if self.robot_id_from_config and command_sender_id == self.robot_id_from_config:
            return
        # ===== 预算检查 =====
        budget_group_id = str(group_id) if group_id else command_sender_id
        budget_allowed, budget_spent, budget_remaining = self._check_budget(budget_group_id)
        if not budget_allowed:
            budget_msg = f"今日该群画图额度已用完（已消耗 ¥{budget_spent:.2f}/¥{self.daily_group_budget:.2f}），明天零点重置哦~"
            yield json.dumps({
                "success": False,
                "budget_exceeded": True,
                "message": budget_msg,
                "user_instruction_for_llm": f"绘图预算已耗尽，请你用自己的语气转告群友这个消息：{budget_msg}"
            }, ensure_ascii=False)
            return
        all_text = prompt.strip()
        all_images_pil: List[PILImage.Image] = []
        used_default_image = False
        used_temp_reference = False

        # 优先处理回复消息中的图片
        replied_image_pil: Optional[PILImage.Image] = None
        message_chain = event.get_messages()

        for msg_component in message_chain:
            if isinstance(msg_component, Reply):
                logger.debug(f"检测到回复消息。尝试解析被引用的图片。")
                source_chain: Optional[List[BaseMessageComponent]] = None
                if hasattr(msg_component, 'chain') and isinstance(msg_component.chain, list):
                    source_chain = msg_component.chain
                elif hasattr(msg_component, 'message') and isinstance(msg_component.message, list):
                    source_chain = msg_component.message
                elif hasattr(msg_component, 'source') and hasattr(msg_component.source, 'message_chain') and isinstance(msg_component.source.message_chain, list):
                    source_chain = msg_component.source.message_chain

                if source_chain:
                    for replied_part in source_chain:
                        if isinstance(replied_part, Image) and hasattr(replied_part, 'url') and replied_part.url:
                            replied_image_pil = await self.download_pil_image_from_url(replied_part.url, "直接引用的消息中的图片")
                            if replied_image_pil:
                                logger.info("成功从直接引用的消息中加载了图片作为参考。")
                                all_images_pil.append(replied_image_pil)
                if replied_image_pil:
                    image_index = 0
                    reference_bot = False
                    break
                break

        # 如果没有直接引用的图片，且指定了图片索引，则尝试从缓存中获取
        if not all_images_pil and image_index > 0:
            num_images_to_fetch = image_index
            if reference_bot == True:
                user_id_for_cache_lookup = self.robot_id_from_config
            else:
                user_id_for_cache_lookup = command_sender_id
            group_id_for_cache_lookup = event.message_obj.group_id or command_sender_id

            key_for_cache = (user_id_for_cache_lookup, group_id_for_cache_lookup)
            if key_for_cache in self.image_history_cache and self.image_history_cache[key_for_cache]:
                cached_items_list = list(self.image_history_cache[key_for_cache])
                actual_num_to_fetch = min(num_images_to_fetch, len(cached_items_list))
                for i in range(1, actual_num_to_fetch + 1):
                    pil_image_from_cache = await self.get_user_recent_image_pil_from_cache(
                        user_id_for_cache_lookup,
                        group_id_for_cache_lookup,
                        i 
                    )
                    if pil_image_from_cache:
                        all_images_pil.append(pil_image_from_cache)

        # 组装图片逻辑：用户发的图 + (可选)风格参考 + (可选)人设参考
        
        session_key_for_temp = (command_sender_id, str(group_id) if group_id else command_sender_id)
        temp_context = self.temp_reference_context.get(session_key_for_temp, {})

        # 1. 如果有【风格/动作】参考图，加进去 (Style)
        if 'style' in temp_context:
            all_images_pil.insert(0, temp_context['style'])
            used_temp_reference = True
            logger.info("gemini_draw: 注入了临时风格参考图")

        # 2. 如果没提供任何图片，且启用了默认图逻辑
        if not all_images_pil:
            # 优先看有没有【临时人设】(Identity)
            if 'identity' in temp_context:
                all_images_pil.append(temp_context['identity'])
                used_temp_reference = True
                logger.info("gemini_draw: 使用了临时人设图")
            # 否则用默认立绘
            elif self.enable_base_reference_image:
                base_image = self._load_base_reference_image()
                if base_image:
                    all_images_pil.append(base_image)
                    used_default_image = True
        

        if not all_text and not all_images_pil:
            yield event.plain_result("请提供文本描述或参考图片。")
            event.stop_event()
            return

        # 在提示词前添加英文前缀
        if all_text:
            all_text = f"Generate/modify images using the following prompt: {all_text}"
        
        # 处理图片比例
        if aspect_ratio and aspect_ratio != "auto":
            all_text = all_text + f" Image aspect ratio: {aspect_ratio}."

        if self.enable_hinting:
            await event.send(event.plain_result("🎨#(#!&...✍!"))

        try:
            logger.debug(f"gemini_draw: 调用 API 生成 (API类型: {self.api_type})")
            
        result = await self._generate_by_api_type(all_text, all_images_pil, aspect_ratio)

            if result is None or not isinstance(result, dict):
                logger.error(f"gemini_draw: API 返回无效结果")
                yield event.plain_result("处理图片时发生内部错误。")
                event.stop_event()
                return

            text_response = result.get('text', '').strip()
            image_paths = result.get('image_paths', [])
            
            if image_paths:
                owner_id = str(self.robot_id_from_config) if self.robot_id_from_config else str(command_sender_id)
                gid = str(group_id) if group_id else str(command_sender_id)
            
                for i, img_path in enumerate(image_paths):
                    if img_path and os.path.exists(img_path):
                        self.store_user_image(
                            owner_id,
                            gid,
                            img_path,
                            f"generated_{i+1}_{os.path.basename(img_path)}"
                        )

            if not text_response and not image_paths:
                yield event.plain_result("未能从API获取任何内容。")
                event.stop_event()
                return

            if len(image_paths) < 2:
                chain = []
                for img_path in image_paths:
                    if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                        chain.append(Image.fromFileSystem(img_path))
                if chain:
                    # 记录花费
                    cost, spent_today, remaining = self._record_spending(budget_group_id, len(image_paths))
                    cost_info = f"💰本次消耗¥{cost:.2f} | 今日已用¥{spent_today:.2f}/¥{self.daily_group_budget:.2f} | 剩余¥{remaining:.2f}"
                    
                    # 先把工具结果告诉LLM（必须在发图之前yield，LLM才能收到）
                    image_desc = text_response if text_response else "（API未返回文字描述，但图片已成功生成并发送）"
                    llm_feedback = f"图片已成功生成并发送给用户。不要再次调用画图工具！"
                    if used_temp_reference:
                        llm_feedback += " 使用了临时参考图。"
                    elif used_default_image:
                        llm_feedback += " 使用了默认参考图。"
                    tool_output_data = {
                        "success": True,
                        "image_already_sent": True,
                        "image_description": image_desc,
                        "number_of_images_generated": len(image_paths),
                        "cost_this_time": cost,
                        "budget_spent_today": spent_today,
                        "budget_remaining": remaining,
                        "user_instruction_for_llm": f"{llm_feedback} 图片内容：{image_desc}。{cost_info}。请勿重复调用画图工具。"
                    }
                    yield json.dumps(tool_output_data, ensure_ascii=False)
                    # 然后发图片和预算提示给用户
                    await event.send(event.chain_result(chain))
                    await event.send(event.plain_result(cost_info))
                    await self._post_image_commentary_once(event, prompt, image_desc)
                else:
                    if text_response:
                        yield event.plain_result(text_response)
                    else:
                        yield event.plain_result("抱歉，未能生成有效内容。")
                return
            else:
                bot_id_for_node_str = event.message_obj.self_id or self.robot_id_from_config or self.config.get("bot_id")
                bot_id_for_node = int(str(bot_id_for_node_str).strip()) if bot_id_for_node_str and str(bot_id_for_node_str).strip().isdigit() else None
                if bot_id_for_node is None:
                    chain = []
                    if text_response:
                        chain.append(Plain(text_response))
                    for img_path in image_paths:
                        if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                            chain.append(Image.fromFileSystem(img_path))
                    if chain:
                        yield event.chain_result(chain)
                    else:
                        yield event.plain_result("抱歉，未能生成有效内容。")
                    return

                bot_name_for_node = str(self.config.get("bot_name", "绘图助手")).strip() or "绘图助手"
                ns = Nodes([])
                if text_response:
                    ns.nodes.append(Node(user_id=bot_id_for_node, nickname=bot_name_for_node, content=[Plain(text_response)]))
                for img_path in image_paths:
                    if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                        ns.nodes.append(Node(user_id=bot_id_for_node, nickname=bot_name_for_node, content=[Image.fromFileSystem(img_path)]))
                if ns.nodes:
                    # 记录花费
                    cost, spent_today, remaining = self._record_spending(budget_group_id, len(image_paths))
                    cost_info = f"💰本次消耗¥{cost:.2f} | 今日已用¥{spent_today:.2f}/¥{self.daily_group_budget:.2f} | 剩余¥{remaining:.2f}"
                    
                    # 先把工具结果告诉LLM
                    image_desc = text_response if text_response else "（API未返回文字描述，但图片已成功生成并发送）"
                    tool_output_data = {
                        "success": True,
                        "image_already_sent": True,
                        "image_description": image_desc,
                        "number_of_images_generated": len(image_paths),
                        "cost_this_time": cost,
                        "budget_spent_today": spent_today,
                        "budget_remaining": remaining,
                        "user_instruction_for_llm": f"已生成{len(image_paths)}张图片并全部发送给用户。不要再次调用画图工具！图片内容：{image_desc}。{cost_info}"
                    }
                    yield json.dumps(tool_output_data, ensure_ascii=False)
                    # 然后发图片和预算提示
                    await event.send(event.chain_result([ns]))
                    await event.send(event.plain_result(cost_info))
                    await self._post_image_commentary_once(event, prompt, image_desc)
                else:
                    yield event.plain_result("抱歉，未能生成有效内容。")

        except Exception as e:
            logger.error(f"gemini_draw 未知错误: {e}", exc_info=True)
            yield event.plain_result(f"处理请求时发生意外错误: {str(e)}")

    @filter.llm_tool(name="generate_self_reaction")
    async def generate_self_reaction(self, event: AstrMessageEvent, scene_description: str) -> AsyncGenerator[Any, None]:
        '''
        对方希望画你自己时优先调用此画图工具而不是使用draw，用于魔改或表情包等。诸如与用户进行互动整活时使用。
        该函数会自动使用你角色的参考图，比文字提示词精确，你只需描述场景，不要额外描述外貌特征。同时要提醒告知用户，画图API非常昂贵。

        Args:
            scene_description (string): 画面描述。包括你的表情、情绪、动作、与对方当前的互动等。
        '''
        # 检查功能是否启用
        if not self.enable_self_reaction:
            yield json.dumps({"success": False, "message": "此功能未启用"}, ensure_ascii=False)
            return

        if not self.api_keys:
            yield json.dumps({"success": False, "message": "API密钥未配置"}, ensure_ascii=False)
            return

        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id if hasattr(event.message_obj, 'group_id') and event.message_obj.group_id else user_id
        session_key = (user_id, str(group_id))
        # ===== 预算检查 =====
        budget_group_id = str(group_id)
        budget_allowed, budget_spent, budget_remaining = self._check_budget(budget_group_id)
        if not budget_allowed:
            budget_msg = f"今日该群画图额度已用完（已消耗 ¥{budget_spent:.2f}/¥{self.daily_group_budget:.2f}），明天零点重置~"
            yield json.dumps({
                "success": False,
                "budget_exceeded": True,
                "message": budget_msg,
                "user_instruction_for_llm": f"绘图预算已耗尽，请你用自己的语气转告群友：{budget_msg}"
            }, ensure_ascii=False)
            return
        # 冷却检查（软性提醒，不强制阻止）
        current_time = time.time()
        if session_key in self.reaction_cooldown:
            elapsed = current_time - self.reaction_cooldown[session_key]
            if elapsed < self.reaction_cooldown_seconds:
                remaining = int(self.reaction_cooldown_seconds - elapsed)
                logger.info(f"generate_self_reaction: 用户 {user_id} 冷却中，剩余 {remaining} 秒")
                # 不强制阻止，只是记录日志，让LLM自己判断

        
        # 1. 确定角色人设图 (Identity)
        character_img = None
        used_temp_identity = False
        
        # 检查是否有临时人设 (通过 manage_drawing_reference 设置的)
        if session_key in self.temp_reference_context and 'identity' in self.temp_reference_context[session_key]:
            character_img = self.temp_reference_context[session_key]['identity']
            used_temp_identity = True
            logger.info("generate_self_reaction: 使用了临时设置的【人设】图")
        
        # 如果没有临时人设，使用配置文件里的默认立绘
        if not character_img:
            character_img = self._load_character_reference_image()

        if not character_img:
            yield json.dumps({"success": False, "message": "角色参考图未配置，无法生成反应图"}, ensure_ascii=False)
            return

        # 检查是否有用户回复的表情包（让角色模仿）
        reference_meme_img: Optional[PILImage.Image] = None
        message_chain = event.get_messages()
        
        for msg_component in message_chain:
            if isinstance(msg_component, Reply):
                source_chain = None
                if hasattr(msg_component, 'chain') and isinstance(msg_component.chain, list):
                    source_chain = msg_component.chain
                elif hasattr(msg_component, 'message') and isinstance(msg_component.message, list):
                    source_chain = msg_component.message
                
                if source_chain:
                    for replied_part in source_chain:
                        if isinstance(replied_part, Image) and hasattr(replied_part, 'url') and replied_part.url:
                            reference_meme_img = await self.download_pil_image_from_url(
                                replied_part.url, "用户回复的表情包"
                            )
                            if reference_meme_img:
                                logger.info("成功获取用户回复的表情包作为模仿参考")
                            break
                break

        # 构建图片列表和prompt
        images_for_api: List[PILImage.Image] = [character_img]
        has_reference_meme = False
        
        if reference_meme_img:
            images_for_api.append(reference_meme_img)
            has_reference_meme = True

        final_prompt = self._build_reaction_prompt(scene_description, has_reference_meme)

        try:
            logger.info(f"generate_self_reaction: 开始生成，场景: {scene_description[:50]}...")
            
            # 根据API类型调用生成
            result = await self._generate_by_api_type(final_prompt, images_for_api, "3:4")

            if not result or not isinstance(result, dict):
                logger.error("generate_self_reaction: API返回无效")
                yield json.dumps({"success": False, "message": "生成失败"}, ensure_ascii=False)
                return

            image_paths = result.get('image_paths', [])
            
            if not image_paths:
                logger.warning("generate_self_reaction: 未生成图片")
                yield json.dumps({"success": False, "message": "未能生成图片，请用文字回应"}, ensure_ascii=False)
                return

            # 审核图片
            image_path = image_paths[0]  # 取第一张
            review_passed = await self._review_generated_image(event, image_path, scene_description)
            
            if not review_passed:
                logger.info("generate_self_reaction: 图片审核未通过，静默不发送")
                yield json.dumps({"success": False, "message": "图片效果不佳，请用文字回应"}, ensure_ascii=False)
                return

            # 审核通过，更新冷却时间
            self.reaction_cooldown[session_key] = current_time

            # 缓存生成的图片
            if self.robot_id_from_config:
                self.store_user_image(
                    str(self.robot_id_from_config),
                    str(group_id),
                    image_path,
                    f"self_reaction_{os.path.basename(image_path)}"
                )

            # 记录花费
            cost, spent_today, remaining = self._record_spending(budget_group_id, 1)
            cost_info = f"💰本次消耗¥{cost:.2f} | 今日已用¥{spent_today:.2f}/¥{self.daily_group_budget:.2f} | 剩余¥{remaining:.2f}"
            
            # 先告诉LLM结果（必须在发图之前）
            yield json.dumps({
                "success": True,
                "image_already_sent": True,
                "image_description": f"已按场景'{scene_description}'生成角色反应图并发送给用户",
                "cost_this_time": cost,
                "budget_spent_today": spent_today,
                "budget_remaining": remaining,
                "user_instruction_for_llm": f"角色反应图已成功生成并发送！不要再次调用任何画图工具！场景：{scene_description}。{cost_info}"
            }, ensure_ascii=False)
            # 然后发图片和预算提示给用户
            chain = [Image.fromFileSystem(image_path)]
            await event.send(event.chain_result(chain))
            await event.send(event.plain_result(cost_info))
            await self._post_image_commentary_once(event, scene_description, f"角色反应图场景：{scene_description}")

        except Exception as e:
            logger.error(f"generate_self_reaction 错误: {e}", exc_info=True)
            yield json.dumps({"success": False, "message": "生成过程出错，请用文字回应"}, ensure_ascii=False)    
    

    @filter.llm_tool(name="manage_drawing_reference")
    async def manage_drawing_reference(self, event: AstrMessageEvent, action: str, target: str = "style", image_index: int = 1) -> AsyncGenerator[Any, None]:
        '''
        管理绘图的参考图像/记忆。当用户想改变你的外貌设定，或者固定某种画风时使用。

        Args:
            action (string): 操作类型。可选: 
                - "set": 设置参考图。
                - "clear": 清除参考图。
            target (string): 目标类型。
                - "style": 风格/动作参考。用于"以后都用这种画风"、"记住这个构图"。不会改变你的角色长相。
                - "identity": 身份/人设参考。用于"你变成猫娘"、"把你的立绘换成这张"。慎用！这会改变你的脸。
                *注意：如果用户说"把这个表情包换成你自己"，请不要调用此工具！直接调用 generate_self_reaction 即可。*
            image_index (number): 引用历史图片的索引（仅set时有效）。1=最新1张。默认1。
        '''
        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id if hasattr(event.message_obj, 'group_id') and event.message_obj.group_id else user_id
        session_key = (user_id, str(group_id))

        if action == "clear":
            if session_key in self.temp_reference_context:
                if target == "all":
                    del self.temp_reference_context[session_key]
                    msg = "已清除所有临时参考（人设和风格），恢复默认状态。"
                elif target in self.temp_reference_context[session_key]:
                    del self.temp_reference_context[session_key][target]
                    msg = f"已清除临时{target}参考。"
                else:
                    msg = f"当前没有设置临时{target}参考。"
            else:
                msg = "当前没有设置任何临时参考图。"
            
            yield json.dumps({"success": True, "message": msg}, ensure_ascii=False)
            return

        elif action == "set":
            # 获取图片逻辑
            replied_image_pil = None
            message_chain = event.get_messages()
            for msg_component in message_chain:
                if isinstance(msg_component, Reply):
                    # ... (简化的回复解析逻辑，保持原有代码复用或如下) ...
                    source_chain = getattr(msg_component, 'chain', []) or getattr(msg_component, 'message', [])
                    if hasattr(msg_component, 'source') and hasattr(msg_component.source, 'message_chain'):
                        source_chain = msg_component.source.message_chain
                    if source_chain:
                        for part in source_chain:
                            if isinstance(part, Image) and hasattr(part, 'url') and part.url:
                                replied_image_pil = await self.download_pil_image_from_url(part.url, "参考图")
                                break
                    break
            
            if not replied_image_pil:
                replied_image_pil = await self.get_user_recent_image_pil_from_cache(user_id, str(group_id), image_index)

            if not replied_image_pil:
                yield json.dumps({"success": False, "message": "未找到图片，请确保回复了图片或最近发送过图片。"}, ensure_ascii=False)
                return

            if session_key not in self.temp_reference_context:
                self.temp_reference_context[session_key] = {}
            
            self.temp_reference_context[session_key][target] = replied_image_pil
            
            # 区分反馈话术
            if target == "identity":
                feedback = "已更新临时【人设】。后续绘图我将长成这张图的样子（直到你清除它）。"
            else:
                feedback = "已记录【风格/动作】参考。后续绘图将参考这张图的构图或画风。"

            yield json.dumps({"success": True, "message": feedback}, ensure_ascii=False)

    @filter.llm_tool(name="combine_images_draw")
    async def combine_images_draw(self, event: AstrMessageEvent, prompt: str, image_indices: str = "1,2", use_character_ref: bool = True) -> AsyncGenerator[Any, None]:
        '''
        将多张图片合并/融合生成新图。当用户想把多张图结合在一起时使用。
        例如："把这两张图合在一起"、"用第一张的风格画第二张的内容"、"融合这几张图"。

        Args:
            prompt (string): 描述如何合并这些图片。如"把图1的角色放到图2的场景中"。
            image_indices (string): 要使用的图片索引，用逗号分隔。如"1,2"表示最新的两张，"1,2,3"表示最新的三张。默认"1,2"。
            use_character_ref (boolean): 是否同时使用角色参考图来保持角色一致性。默认True。
        '''
        if not self.api_keys:
            yield json.dumps({"success": False, "message": "API密钥未配置"}, ensure_ascii=False)
            return

        user_id = event.get_sender_id()
        group_id = event.message_obj.group_id if hasattr(event.message_obj, 'group_id') and event.message_obj.group_id else user_id
        # ===== 预算检查 =====
        budget_group_id = str(group_id)
        budget_allowed, budget_spent, budget_remaining = self._check_budget(budget_group_id)
        if not budget_allowed:
            budget_msg = f"今日该群画图额度已用完（已消耗 ¥{budget_spent:.2f}/¥{self.daily_group_budget:.2f}），明天零点重置~"
            yield json.dumps({
                "success": False,
                "budget_exceeded": True,
                "message": budget_msg,
                "user_instruction_for_llm": f"绘图预算已耗尽，请你用自己的语气转告群友：{budget_msg}"
            }, ensure_ascii=False)
            return
        # 解析图片索引
        try:
            indices = [int(x.strip()) for x in image_indices.split(',') if x.strip().isdigit()]
        except:
            indices = [1, 2]
        
        if not indices:
            indices = [1, 2]

        images_for_api: List[PILImage.Image] = []

        # 如果需要角色参考图，先加载
        if use_character_ref:
            char_img = self._load_character_reference_image()
            if char_img:
                images_for_api.append(char_img)
                logger.info("combine_images_draw: 添加了角色参考图")

        # 按索引获取用户图片
        for idx in indices:
            pil_img = await self.get_user_recent_image_pil_from_cache(user_id, str(group_id), idx)
            if pil_img:
                images_for_api.append(pil_img)
                logger.info(f"combine_images_draw: 添加了用户图片索引 {idx}")

        if len(images_for_api) < 2:
            yield json.dumps({
                "success": False, 
                "message": f"需要至少2张图片来合并，但只找到 {len(images_for_api)} 张。请确保用户最近发送过足够的图片。"
            }, ensure_ascii=False)
            return

        # 构建提示词
        final_prompt = f"合并/融合以下图片: {prompt}"
        if use_character_ref:
            final_prompt = f"参考图1是角色形象参考。" + final_prompt

        try:
            result = await self._generate_by_api_type(final_prompt, images_for_api, "1:1")

            if not result or not result.get('image_paths'):
                yield json.dumps({"success": False, "message": "图片合并生成失败"}, ensure_ascii=False)
                return

            image_path = result['image_paths'][0]
            
            # 缓存生成的图片
            if self.robot_id_from_config:
                self.store_user_image(
                    str(self.robot_id_from_config),
                    str(group_id),
                    image_path,
                    f"combined_{os.path.basename(image_path)}"
                )

            chain = [Image.fromFileSystem(image_path)]
             # 记录花费
            cost, spent_today, remaining = self._record_spending(budget_group_id, 1)
            cost_info = f"💰本次消耗¥{cost:.2f} | 今日已用¥{spent_today:.2f}/¥{self.daily_group_budget:.2f} | 剩余¥{remaining:.2f}"
            
            # 先告诉LLM
            yield json.dumps({
                "success": True,
                "image_already_sent": True,
                "message": f"已合并 {len(images_for_api)} 张图片并发送。不要再次调用画图工具！",
                "cost_this_time": cost,
                "budget_spent_today": spent_today,
                "budget_remaining": remaining,
                "user_instruction_for_llm": f"图片合并完成并已发送给用户。{cost_info}。请勿重复调用。"
            }, ensure_ascii=False)
            # 然后发图片
            await event.send(event.chain_result(chain))
            await event.send(event.plain_result(cost_info))
            await self._post_image_commentary_once(event, prompt, "多图合并结果已发送")
        except Exception as e:
            logger.error(f"combine_images_draw 错误: {e}", exc_info=True)
            yield json.dumps({"success": False, "message": "合并过程出错"}, ensure_ascii=False)    
    @filter.command("draw")
    async def initiate_creation_session(self, event: AstrMessageEvent):
        """处理 /draw 命令，启动绘图会话。(旧版功能)"""
        if not self.api_keys:
            yield event.plain_result("请联系管理员配置Gemini API密钥 (api_keys)")
            return

        if not hasattr(event, 'message_obj') or not hasattr(event.message_obj, 'type'):
             logger.error(f"initiate_creation_session: 事件对象缺少 message_obj 或 type 属性: {type(event)}")
             yield event.plain_result("处理消息类型时出错，请联系管理员。")
             return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        group_id = user_id 
        is_group_message = hasattr(event.message_obj, 'group_id') and event.message_obj.group_id is not None and event.message_obj.group_id != ""


        if is_group_message:
            group_id = event.message_obj.group_id
        
        if self.group_whitelist:
            identifier_to_check = group_id if is_group_message else user_id
            if str(identifier_to_check) not in [str(whitelisted_id) for whitelisted_id in self.group_whitelist]:
                logger.info(f"initiate_creation_session: 用户/群组 {identifier_to_check} 不在白名单中，已忽略 /draw 命令。")
                return # No reply for non-whitelisted

        session_key = (user_id, str(group_id)) # Ensure group_id is string for key consistency

        if session_key in self.waiting_users and time.time() < self.waiting_users[session_key]: # Check expiry
             expiry_time = self.waiting_users[session_key]
             remaining_time = int(expiry_time - time.time())
             yield event.plain_result(f"您已经在当前会话有一个正在进行的绘制任务，请先完成或等待超时 ({remaining_time}秒后)。")
             return
        elif session_key in self.waiting_users: # Expired entry
            del self.waiting_users[session_key]
            if session_key in self.user_inputs:
                del self.user_inputs[session_key]


        self.waiting_users[session_key] = time.time() + self.wait_time_from_config
        self.user_inputs[session_key] = {'messages': []}
        
        logger.debug(f"Gemini_Draw (Command): User {user_id} started draw. Session ID: {group_id}, Session Key: {session_key}. Waiting state set.")
        yield event.plain_result(f"好的 {user_name}，请在{self.wait_time_from_config}秒内发送文本描述和可能需要的图片, 然后发送包含'start'或'开始'的消息开始生成。")

    @filter.event_message_type(EventMessageType.ALL)
    async def collect_user_inputs(self, event: AstrMessageEvent):
        """处理后续消息，收集用户输入或触发 /draw 会话的生成。(旧版功能)"""
        if not hasattr(event, 'message_obj') or not hasattr(event.message_obj, 'type'):
             return 

        user_id = event.get_sender_id()
        
        # 忽略机器人自身消息
        if self.robot_id_from_config and user_id == self.robot_id_from_config:
            return

        current_group_id = user_id 
        is_group_message = hasattr(event.message_obj, 'group_id') and event.message_obj.group_id is not None and event.message_obj.group_id != ""
        if is_group_message:
            current_group_id = event.message_obj.group_id
        
        # 白名单检查 (同样应用于后续消息，确保只有授权会话可以继续)
        if self.group_whitelist:
            identifier_to_check = current_group_id if is_group_message else user_id
            if str(identifier_to_check) not in [str(whitelisted_id) for whitelisted_id in self.group_whitelist]:
                return

        current_session_key = (user_id, str(current_group_id))

        # logger.debug(f"collect_user_inputs: Processing message. User ID: {user_id}, Session ID: {current_group_id}, Session Key: {current_session_key}")
        # logger.debug(f"collect_user_inputs: Current waiting users keys: {list(self.waiting_users.keys())}")

        if current_session_key not in self.waiting_users:
            # logger.debug(f"collect_user_inputs: Session key {current_session_key} not found in waiting users. Ignoring message for /draw flow.")
            return # Not a user we are waiting for in the /draw flow

        # logger.debug(f"collect_user_inputs: Session key {current_session_key} IS in waiting users. Processing for /draw flow.")

        if time.time() > self.waiting_users[current_session_key]:
            logger.debug(f"collect_user_inputs: Session {current_group_id} for user {user_id} (key {current_session_key}) timed out for /draw flow.")
            del self.waiting_users[current_session_key]
            if current_session_key in self.user_inputs:
                del self.user_inputs[current_session_key]
            yield event.plain_result("等待超时，您的 /draw 会话已结束。请重新使用 /draw 命令。")
            return

        message_text_raw = event.message_str.strip()
        keywords = ["start", "开始"] # 触发生成的关键词
        # 检查是否包含关键词，同时确保不是在输入另一个命令 (如 /draw 本身)
        contains_keyword = any(keyword in message_text_raw.lower() for keyword in keywords)
        
        # 如果消息是 `/draw` 命令本身，则它应该由 `initiate_creation_session` 处理，而不是在这里收集
        is_command = message_text_raw.startswith("/") or message_text_raw.lower().startswith("draw")
        if is_command and not contains_keyword:
            # logger.debug(f"collect_user_inputs: 消息是命令 ({message_text_raw}) 且不包含 start/开始，已忽略。")
            return


        current_text_for_prompt = message_text_raw
        current_images_pil: List[PILImage.Image] = []

        message_chain = event.get_messages()
        for msg_component in message_chain:
            if isinstance(msg_component, Image) and hasattr(msg_component, 'url') and msg_component.url:
                try:
                    # 旧版使用 download_image_by_url，返回本地路径
                    # 然后用 PILImage.open 打开。
                    # 新版有 download_pil_image_from_url 直接返回 PIL Image。
                    # 为了保持"整块添加"，我们暂时用旧的方式，或者适配到新的。
                    # 适配到新的：
                    pil_img = await self.download_pil_image_from_url(msg_component.url, "用户为/draw会话发送的图片")
                    if pil_img:
                        current_images_pil.append(pil_img)
                        logger.info(f"collect_user_inputs: Successfully downloaded and converted image via new method: {msg_component.url} for /draw session key {current_session_key}")
                    else:
                        yield event.plain_result(f"无法处理您发送的一张图片（下载或转换失败），请尝试其他图片。") # Inform user
                        # return # Optional: stop processing if one image fails
                except Exception as e:
                    logger.error(f"collect_user_inputs: 处理 /draw 会话的图片失败 (key {current_session_key}): {str(e)}", exc_info=True)
                    yield event.plain_result(f"处理图片时发生错误: {str(e)}。请稍后再试或尝试其他图片。")
                    return # Stop processing on error

        # 确保 user_inputs 中有此会话 (理论上 initiate 时已创建)
        if current_session_key not in self.user_inputs:
             logger.error(f"collect_user_inputs: 用户 {user_id} 在会话 {current_group_id} (key {current_session_key}) 中等待，但 user_inputs 状态丢失。正在清理。")
             if current_session_key in self.waiting_users:
                 del self.waiting_users[current_session_key]
             yield event.plain_result("您的 /draw 会话状态异常，请重试。")
             return

        # 存储本次消息的内容
        # 只有在文本或图片非空时才记录，避免空消息污染
        if current_text_for_prompt or current_images_pil:
            message_data = {
              'text': current_text_for_prompt, # Store raw text, keyword removal happens at generation
              'images': current_images_pil,   # Store PIL images
              'timestamp': time.time()
            }
            self.user_inputs[current_session_key]['messages'].append(message_data)
            logger.debug(f"collect_user_inputs: Stored message for /draw session {current_session_key}. Text: '{current_text_for_prompt[:30]}...', Images: {len(current_images_pil)}")


        if contains_keyword:
            logger.debug(f"Gemini_Draw (Command): Start keyword detected in session {current_group_id} (key {current_session_key}). Processing messages.")
            
            # 从 user_inputs 中聚合所有为此会话收集的消息
            collected_session_messages = self.user_inputs[current_session_key].get('messages', [])
            # 按时间戳排序确保顺序
            collected_session_messages.sort(key=lambda x: x['timestamp'])

            all_text_parts = []
            all_pil_images_for_api: List[PILImage.Image] = []

            for msg_data in collected_session_messages:
                text_part = msg_data.get('text', '')
                # 从文本中移除触发关键词，避免它们进入最终的prompt
                for kw in keywords:
                    # Regex to remove whole word, case insensitive
                    text_part = re.sub(r'\b' + re.escape(kw) + r'\b', '', text_part, flags=re.IGNORECASE).strip()
                if text_part:
                    all_text_parts.append(text_part)
                
                all_pil_images_for_api.extend(msg_data.get('images', [])) # images are already PIL

            final_prompt_text = '\n'.join(all_text_parts).strip()

            # 清理会话状态
            del self.waiting_users[current_session_key]
            del self.user_inputs[current_session_key]


            if not final_prompt_text and not all_pil_images_for_api:
                yield event.plain_result("您没有提供任何文本描述或图片内容给 /draw 会话。")
                return
            # ===== 预算检查 =====
            budget_group_id = str(current_group_id)
            budget_allowed, budget_spent, budget_remaining = self._check_budget(budget_group_id)
            if not budget_allowed:
                yield event.plain_result(
                    f"⚠️ 今日该群画图额度已用完（已消耗 ¥{budget_spent:.2f}/¥{self.daily_group_budget:.2f}），明天零点重置哦~"
                )
                return
            yield event.plain_result("收到开始指令，正在为您生成图片，请稍候...")
            
            try:
                # 根据API类型调用相应的生成方法
                api_result = await self._generate_by_api_type(final_prompt_text, all_pil_images_for_api, "auto")
                
                if api_result is None or not isinstance(api_result, dict): # Should be caught by gemini_generate raising error
                    logger.error(f"collect_user_inputs: gemini_generate 返回无效结果 for /draw session: {type(api_result)}")
                    yield event.plain_result("处理图片时发生内部错误（生成器未返回有效数据）。")
                    return

                text_response = api_result.get('text', '').strip()
                image_paths = api_result.get('image_paths', []) # List of local file paths

                logger.debug(f"collect_user_inputs (/draw): gemini_generate returned - Text: '{text_response[:50]}...', Images: {len(image_paths)}")
                # ===== 记录花费 =====
                if image_paths:
                    draw_cost, draw_spent, draw_remaining = self._record_spending(budget_group_id, len(image_paths))
                    cost_info_text = f"\n💰 本次消耗 ¥{draw_cost:.2f} | 今日已用 ¥{draw_spent:.2f}/¥{self.daily_group_budget:.2f} | 剩余 ¥{draw_remaining:.2f}"
                    if text_response:
                        text_response += cost_info_text
                    else:
                        text_response = cost_info_text.strip()
                # 缓存机器人自己生成的图片 (对于 /draw 指令，图片的"owner"是触发指令的用户，但图片本身是机器人发的)
                # 如果希望这些图片能被 LLM 工具通过 reference_bot=True 引用，则需要用 robot_id 缓存
                if image_paths and self.robot_id_from_config:
                    logger.info(f"准备缓存 {len(image_paths)} 张 /draw 生成的图片路径到机器人 {self.robot_id_from_config} 在上下文 {current_group_id} 的历史中...")
                    for i, img_path in enumerate(image_paths):
                        if os.path.exists(img_path):
                            self.store_user_image(
                                str(self.robot_id_from_config), # Image belongs to the bot
                                str(current_group_id),        # In the current chat context
                                img_path,                   # Store the local file path
                                f"draw_cmd_generated_{i+1}_{os.path.basename(img_path)}"
                            )

                if not text_response and not image_paths:
                    logger.warning("collect_user_inputs (/draw): API未返回任何文本或图片内容。")
                    yield event.plain_result("未能从API获取任何文本或图片内容。")
                    return

                # 发送结果给用户 (旧版的发送逻辑)
                if len(image_paths) < 2: # 单图或无图（只有文本）
                    chain_to_send = []
                    if text_response:
                        chain_to_send.append(Plain(text_response))
                    for img_path in image_paths:
                        if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                            chain_to_send.append(Image.fromFileSystem(img_path))
                    
                    if chain_to_send:
                        yield event.chain_result(chain_to_send)
                    else: # Should not happen if previous checks pass
                        yield event.plain_result("抱歉，未能生成有效内容。")
                else: # 多张图片，使用 Nodes 合并发送
                    bot_id_for_node_str = event.message_obj.self_id or self.robot_id_from_config or self.config.get("bot_id")
                    bot_id_for_node = int(str(bot_id_for_node_str).strip()) if bot_id_for_node_str and str(bot_id_for_node_str).strip().isdigit() else None
                    
                    if bot_id_for_node is None:
                        logger.error("collect_user_inputs (/draw): 无法确定有效的 bot_id 用于合并转发。降级为逐条发送。")
                        if text_response: yield event.plain_result(text_response)
                        for img_path in image_paths:
                            if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                                yield event.chain_result([Image.fromFileSystem(img_path)])
                        return

                    bot_name_for_node = str(self.config.get("bot_name", "绘图助手")).strip() or "绘图助手"
                    
                    # 构建 Nodes
                    # 旧版逻辑是 text_response 分段对应图片，这里简化：先发总文本，再逐个发图片
                    nodes_message_list: List[Node] = []
                    if text_response:
                         nodes_message_list.append(Node(
                            user_id=bot_id_for_node, 
                            nickname=bot_name_for_node, 
                            content=[Plain(text_response)]
                        ))
                    
                    for img_path in image_paths: 
                        if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 0:
                            # Optionally add a small text like "图片 {idx+1}"
                            # content_for_node = [Plain(f"图片 {idx+1}/{len(image_paths)}"), Image.fromFileSystem(img_path)]
                            content_for_node = [Image.fromFileSystem(img_path)] # Simpler: just image
                            nodes_message_list.append(Node(
                                user_id=bot_id_for_node,
                                nickname=bot_name_for_node,
                                content=content_for_node
                            ))
                    
                    if nodes_message_list:
                        yield event.chain_result([Nodes(nodes_message_list)])
                    else:
                        yield event.plain_result("抱歉，未能生成有效内容进行合并转发。")
                return

            except Exception as e_gen:
                logger.error(f"collect_user_inputs (/draw): 在 /draw 会话的生成或回复阶段发生错误: {str(e_gen)}", exc_info=True)
                yield event.plain_result(f"处理您的 /draw 请求时发生错误: {str(e_gen)}")
                # Ensure session is cleaned up on error too
                if current_session_key in self.waiting_users: del self.waiting_users[current_session_key]
                if current_session_key in self.user_inputs: del self.user_inputs[current_session_key]
                return
        
        else: # 未包含触发关键词，且不是命令
            if current_text_for_prompt.strip() or current_images_pil: 
                logger.debug(f"collect_user_inputs (/draw): 未检测到开始指令 (key {current_session_key})，收到输入: text='{current_text_for_prompt[:30]}...', images_count={len(current_images_pil)}")
                yield event.plain_result("已收到您的输入，请继续发送或发送包含'start'或'开始'的消息结束您的 /draw 会话。")
            # else: (空消息，不回复)
            #    logger.debug(f"collect_user_inputs (/draw): 收到空消息，不含开始指令 (key {current_session_key})，已忽略。")


    async def doubao_generate(self, text_prompt: str, images_pil: Optional[List[PILImage.Image]] = None, aspect_ratio: str = "auto"):
        """
        调用豆包 Doubao-Seedream API 生成图片。
        """
        if not self.api_keys:
            raise ValueError("没有配置API密钥 (api_keys)")
        
        images_pil = images_pil or []
        max_retries, last_exception = len(self.api_keys), None
        key_indices_to_try = list(range(len(self.api_keys)))
        
        if self.random_api_key_selection:
            random.shuffle(key_indices_to_try)
        else:
            key_indices_to_try = [(self.current_api_key_index + i) % len(self.api_keys) for i in range(len(self.api_keys))]
        
        for attempt_num, key_idx_to_use in enumerate(key_indices_to_try):
            current_key_to_try = self.api_keys[key_idx_to_use]
            try:
                logger.info(f"doubao_generate: 尝试API密钥索引 {key_idx_to_use} (尝试 {attempt_num + 1}/{max_retries})")
                
                # 构建请求 URL
                base_url = self.api_base_url_from_config.rstrip('/')
                if not base_url.endswith('/v1'):
                    if '/api' in base_url:
                        base_url = base_url + '/v1'
                    else:
                        base_url = base_url + '/api/v1'
                full_url = base_url + '/images/generations'
                logger.info(f"Doubao 请求 URL: {full_url}")
                
                # 根据比例确定尺寸
                size_map = {
                    "1:1": "1024x1024",
                    "16:9": "1280x720",
                    "9:16": "720x1280",
                    "4:3": "1024x768",
                    "3:4": "768x1024",
                    "auto": "1024x768"  # 默认横向
                }
                image_size = size_map.get(aspect_ratio, "1024x768")
                
                
                # 构建请求参数
                request_input = {
                    "prompt": text_prompt,
                    "negative_prompt": "模糊，低质量，变形，多余肢体，文字",
                    "size": image_size
                }
                logger.info(f"doubao_generate: 使用尺寸 {image_size} (比例: {aspect_ratio})")
                
                # 如果有参考图片，添加第一张作为参考
                if images_pil:
                    # 将第一张PIL图片转为base64
                    buffered = BytesIO()
                    images_pil[0].save(buffered, format="PNG")
                    img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                    request_input["image"] = f"data:image/png;base64,{img_base64}"
                    logger.info(f"doubao_generate: 添加了 1 张参考图片")
                
                logger.info(f"调用 Doubao API，模型: {self.model_name_from_config}, 提示词: {text_prompt[:50]}...")
                
                # 构建请求参数 - aiping.cn 需要 input 嵌套结构
                request_body = {
                    "model": self.model_name_from_config,
                    "input": {
                        "prompt": text_prompt,
                        "negative_prompt": "模糊，低质量，变形，多余肢体",
                        "size": image_size
                    }
                }
                
                # 如果有参考图片，添加到 input 里
                if images_pil:
                    buffered = BytesIO()
                    images_pil[0].save(buffered, format="PNG")
                    img_base64 = base64.b64encode(buffered.getvalue()).decode('utf-8')
                    request_body["input"]["image"] = f"data:image/png;base64,{img_base64}"
                    logger.info(f"doubao_generate: 添加了 1 张参考图片")
                
                logger.info(f"Doubao 请求体: {json.dumps(request_body, ensure_ascii=False, default=str)[:500]}")
                
                # 直接用 requests 调用，避免 OpenAI 客户端的问题
                import httpx
                
                
                async with httpx.AsyncClient(timeout=60.0) as http_client:
                    response = await http_client.post(
                        full_url,
                        json=request_body,
                        headers={
                            "Authorization": f"Bearer {current_key_to_try}",
                            "Content-Type": "application/json"
                        }
                    )
                
                result = {'text': '', 'image_paths': []}
                
                # 调试：打印原始响应
                logger.info(f"Doubao API 响应状态码: {response.status_code}")
                raw_text = response.text
                logger.info(f"Doubao API 原始响应: {raw_text[:500] if raw_text else '(空)'}")
                
                if not raw_text or not raw_text.strip():
                    raise ValueError("Doubao API 返回空响应")
                
                try:
                    response_data = response.json()
                except json.JSONDecodeError as e:
                    logger.error(f"Doubao API 返回非JSON格式: {raw_text[:200]}")
                    raise ValueError(f"Doubao API 返回非JSON格式: {e}")
                
                logger.debug(f"Doubao API 响应: {json.dumps(response_data, ensure_ascii=False)[:500]}")
                
                # 解析响应中的图片
                if 'data' in response_data:
                    for item in response_data['data']:
                        image_data = None
                        
                        # 尝试获取 base64 数据
                        if 'b64_json' in item:
                            image_data = item['b64_json']
                        elif 'url' in item:
                            # 如果是URL，下载图片
                            img_url = item['url']
                            logger.info(f"Doubao 返回图片URL: {img_url}")
                            pil_img = await self.download_pil_image_from_url(img_url, "Doubao生成的图片")
                            if pil_img:
                                os.makedirs(self.temp_dir, exist_ok=True)
                                temp_fp = os.path.join(
                                    self.temp_dir,
                                    f"doubao_gen_{time.time()}_{random.randint(100,999)}.png"
                                )
                                pil_img.save(temp_fp)
                                result['image_paths'].append(temp_fp)
                                logger.info(f"Doubao 生成并保存图片(URL): {temp_fp}")
                            continue
                        
                        # 处理 base64 数据
                        if image_data:
                            try:
                                img_bytes = base64.b64decode(image_data)
                                img_pil = PILImage.open(BytesIO(img_bytes))
                                
                                os.makedirs(self.temp_dir, exist_ok=True)
                                temp_fp = os.path.join(
                                    self.temp_dir,
                                    f"doubao_gen_{time.time()}_{random.randint(100,999)}.png"
                                )
                                img_pil.save(temp_fp)
                                result['image_paths'].append(temp_fp)
                                logger.info(f"Doubao 生成并保存图片(base64): {temp_fp}")
                            except Exception as e:
                                logger.error(f"处理 Doubao base64 图片失败: {e}")
                
                if result['image_paths']:
                    logger.info(f"Doubao 成功生成 {len(result['image_paths'])} 张图片")
                else:
                    logger.warning(f"Doubao API 响应中未找到图片数据")
                
                if not self.random_api_key_selection:
                    self.current_api_key_index = (key_idx_to_use + 1) % len(self.api_keys)
                return result
                
            except Exception as e:
                logger.error(f"doubao_generate: API处理失败 (密钥 {key_idx_to_use}): {str(e)}", exc_info=True)
                last_exception = e
                
            if attempt_num < max_retries - 1:
                logger.info(f"doubao_generate: 尝试下个API密钥")
            else:
                logger.error("doubao_generate: 所有API密钥均尝试失败。")
        
        if last_exception:
            raise last_exception
        raise ValueError("Doubao API处理失败，无可用密钥或未记录错误。")    
    async def openrouter_generate(self, text_prompt: str, images_pil: Optional[List[PILImage.Image]] = None):
        """
        调用OpenAI格式的API生成图片 (Flow2API 专用异步流式版 V4.0)
        """
        # 必须导入异步客户端
        from openai import AsyncOpenAI
        import httpx

        if not self.api_keys:
            raise ValueError("没有配置API密钥 (api_keys)")
        
        images_pil = images_pil or []
        # 增加超时时间，画图很慢，设置 120秒
        timeout_config = httpx.Timeout(120.0, connect=10.0)
        
        key_indices = list(range(len(self.api_keys)))
        if self.random_api_key_selection: random.shuffle(key_indices)
        
        last_exception = None

        for key_idx in key_indices:
            api_key = self.api_keys[key_idx]
            try:
                logger.info(f"openrouter_generate: 尝试密钥索引 {key_idx}")
                
                # 1. 使用 AsyncOpenAI (异步客户端)
                # 这里的 base_url 必须带 /v1
                base_url = self.api_base_url_from_config
                
                client = AsyncOpenAI(
                    api_key=api_key, 
                    base_url=base_url,
                    timeout=timeout_config
                )
                
                # 2. 构建消息
                content = [{"type": "text", "text": text_prompt}]
                for img in images_pil:
                    try:
                        buf = BytesIO()
                        img.save(buf, format="PNG")
                        b64 = base64.b64encode(buf.getvalue()).decode()
                        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                    except: pass

                result = {'text': '', 'image_paths': []}
                
                # 3. 发送请求 (异步+流式)
                logger.info(f"发送流式请求到: {base_url} (Model: {self.model_name_from_config})")
                
                response = await client.chat.completions.create(
                    model=self.model_name_from_config,
                    messages=[{"role": "user", "content": content}],
                    stream=True 
                )

                # 4. 异步接收数据流
                full_content = ""
                chunk_count = 0
                
                logger.info("开始接收数据流...")
                async for chunk in response:
                    chunk_count += 1
                    # 检查 delta 内容
                    if chunk.choices and chunk.choices[0].delta.content:
                        delta = chunk.choices[0].delta.content
                        full_content += delta
                        # 打印前几个包的内容用于调试
                        if chunk_count <= 3:
                            logger.info(f"收到数据包 #{chunk_count}: {delta[:50]}...")
                
                logger.info(f"流接收完毕，共 {chunk_count} 个包，总长度: {len(full_content)}")
                
                # 如果收到了内容，记录下来
                if full_content:
                    result['text'] = full_content
                    logger.warning(f"DEBUG - 完整响应内容: {full_content}")

                # 5. 提取链接
                import re
                urls = re.findall(r'(https?://[^\s\)"\'<>\]]+)', full_content)
                for url in urls:
                    logger.info(f"发现链接，下载中: {url}")
                    try:
                        pil_img = await self.download_pil_image_from_url(url, "Flow2API Image")
                        if pil_img:
                            os.makedirs(self.temp_dir, exist_ok=True)
                            fp = os.path.join(self.temp_dir, f"flow_{time.time()}_{random.randint(100,999)}.png")
                            pil_img.save(fp)
                            result['image_paths'].append(fp)
                            logger.info(f"图片保存成功: {fp}")
                    except Exception as e:
                        logger.warning(f"下载失败: {e}")

                if result['image_paths']:
                    return result
                
                logger.warning("本次尝试未获取到图片，尝试下一个Key...")

            except Exception as e:
                logger.error(f"密钥 {key_idx} 失败: {e}", exc_info=True)
                last_exception = e
        
        if last_exception: raise last_exception
        raise ValueError("所有尝试均失败")    
    async def gemini_generate(self, text_prompt: str, images_pil: Optional[List[PILImage.Image]] = None):
        """
        调用Gemini API生成文本和图片。
        支持多API密钥轮询和随机选择。
        """
        if not self.api_keys:
            raise ValueError("没有配置API密钥 (api_keys)")
        images_pil = images_pil or []
        http_options = HttpOptions(base_url=self.api_base_url_from_config)
        max_retries, last_exception = len(self.api_keys), None
        key_indices_to_try = list(range(len(self.api_keys)))
        if self.random_api_key_selection:
            random.shuffle(key_indices_to_try)
        else:
            key_indices_to_try = [(self.current_api_key_index + i) % len(self.api_keys) for i in range(len(self.api_keys))]

        for attempt_num, key_idx_to_use in enumerate(key_indices_to_try):
            current_key_to_try = self.api_keys[key_idx_to_use]
            try:
                logger.info(f"gemini_generate: 尝试API密钥索引 {key_idx_to_use} (尝试 {attempt_num + 1}/{max_retries})")
                logger.info(f"gemini_generate: base_url={self.api_base_url_from_config}, model={self.model_name_from_config}")
                client = genai.Client(api_key=current_key_to_try, http_options=http_options)
                contents = []
                if text_prompt:
                    contents.append(text_prompt)
                    # +"。请使用中文回复,文字段与图片对应,除非特意要求，图片中不要有文字。"
                for img_item in images_pil:
                    contents.append(img_item)
                if not contents:
                    raise ValueError("没有有效的内容发送给Gemini API")

                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model="models/" + self.model_name_from_config,
                    contents=contents,
                    config=genai.types.GenerateContentConfig(response_modalities=['Text', 'Image'])
                )
                result = {'text': '', 'image_paths': []}
                if not response:
                    logger.warning("gemini_generate: API响应为空。")
                    raise ValueError("Gemini API返回空响应。")


                if not hasattr(response, 'candidates') or not response.candidates:
                    logger.warning("gemini_generate: API响应中无候选。")
                    raise ValueError("Gemini API响应中无有效候选。")

                candidate = response.candidates[0]
                if hasattr(candidate, 'finish_reason') and candidate.finish_reason.name == 'SAFETY':
                    s_info = f" 安全评级: {candidate.safety_ratings}" if hasattr(candidate, 'safety_ratings') else ""
                    msg = f"内容因安全策略被阻止 (finish_reason: SAFETY).{s_info}"
                    logger.warning(f"gemini_generate: {msg}")
                    raise genai.types.SafetyFeedbackError(msg)

                if not (hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts') and candidate.content.parts):
                    f_info = f"(finish_reason: {candidate.finish_reason.name})" if hasattr(candidate, 'finish_reason') else ""
                    logger.warning(f"gemini_generate: Candidate content/parts为空 {f_info}.")
                    raise ValueError(f"Gemini API返回候选内容或部分为空 {f_info}.")

                for part in candidate.content.parts:
                    if hasattr(part, 'text') and part.text is not None:
                        result['text'] += part.text
                    elif hasattr(part, 'inline_data') and part.inline_data and hasattr(part.inline_data, 'mime_type') and part.inline_data.mime_type.startswith('image/'):
                        img_data = part.inline_data.data
                        gen_img = PILImage.open(BytesIO(img_data))
                        ext = part.inline_data.mime_type.split('/')[-1]
                        if ext not in ['png', 'jpeg', 'jpg', 'webp', 'gif']:
                            ext = 'png'
                        os.makedirs(self.temp_dir, exist_ok=True)
                        temp_fp = os.path.join(self.temp_dir, f"gemini_gen_{time.time()}_{random.randint(100,999)}.{ext}")
                        gen_img.save(temp_fp)
                        result['image_paths'].append(temp_fp)
                        logger.info(f"Gemini API 生成并保存图片: {temp_fp} (MIME: {part.inline_data.mime_type})")

                if not result['text'] and not result['image_paths']:
                    logger.warning(f"Gemini API返回空文本和图片. Candidate: {candidate}")
                if not self.random_api_key_selection:
                    self.current_api_key_index = (key_idx_to_use + 1) % len(self.api_keys)
                return result
            except Exception as e:
                logger.error(f"gemini_generate: API处理失败 (密钥 {key_idx_to_use}): {str(e)}", exc_info=True)
                last_exception = e

            if attempt_num < max_retries - 1:
                logger.info(f"gemini_generate: 尝试下个API密钥 (下个索引: {key_indices_to_try[attempt_num+1]})")
            else:
                logger.error("gemini_generate: 所有API密钥均尝试失败。")
        if last_exception:
            raise last_exception
        logger.error("gemini_generate: 未能从API获取数据且无明确异常。")
        raise ValueError("Gemini API处理失败，无可用密钥或未记录错误。")
    @filter.command("test_provider")
    async def test_provider(self, event: AstrMessageEvent):
        """测试命令，查看提供商属性"""
        image_provider_id = self.config.get("image_provider", "")
        if not image_provider_id:
            yield event.plain_result("未配置 image_provider")
            return
        
        try:
            provider = self.context.get_provider_by_id(image_provider_id)
            if provider:
                info = f"提供商ID: {image_provider_id}\n"
                info += f"类型: {type(provider).__name__}\n"
                for attr in ['api_keys', 'base_url', 'api_base', 'endpoint', 'model_name', 'origin_config']:
                    if hasattr(provider, attr):
                        val = getattr(provider, attr)
                        if 'key' in attr.lower() and val:
                            val = f"{str(val)[:15]}..."
                        info += f"{attr}: {val}\n"
                logger.info(f"提供商详情:\n{info}")
                yield event.plain_result(f"已打印提供商详情到日志")
            else:
                yield event.plain_result(f"未找到提供商: {image_provider_id}")
        except Exception as e:
            yield event.plain_result(f"获取提供商失败: {e}")    
    async def terminate(self):
        """
        插件终止时执行清理操作，包括清空图片缓存和取消后台清理任务。
        """
        logger.info("GeminiArtist: 执行 terminate 清理...")
        # 清理会话状态
        if hasattr(self, 'waiting_users'):
            self.waiting_users.clear()
        if hasattr(self, 'user_inputs'):
            self.user_inputs.clear()
        if hasattr(self, 'image_history_cache'):
            self.image_history_cache.clear()
            logger.info("用户图片URL缓存已清空。")
        if hasattr(self, 'temp_reference_context'):
            self.temp_reference_context.clear()
            logger.info("临时参考图缓存已清空。")
        if self._background_cleanup_task and not self._background_cleanup_task.done():
            logger.info("取消后台定时清理任务...")
            self._background_cleanup_task.cancel()
            try:
                await self._background_cleanup_task
            except asyncio.CancelledError:
                logger.info("后台清理任务已取消。")
            except Exception as e:
                logger.error(f"等待后台清理任务结束时异常: {e}", exc_info=True)
        else:
            logger.info("无活动后台清理任务或已完成。")
        logger.info(f"最终临时文件清理 ({self.temp_dir})...")
        try:
            await asyncio.to_thread(self._blocking_cleanup_temp_dir_logic, 0)
        except Exception as e:
            logger.error(f"最终清理失败: {e}", exc_info=True)
        # 仅当临时目录是插件特有的且为空时才尝试移除
        if hasattr(self, 'temp_dir') and os.path.exists(self.temp_dir) and self.temp_dir == self.plugin_temp_base_dir:
            try:
                if not os.listdir(self.temp_dir):
                    os.rmdir(self.temp_dir)
                    logger.info(f"已移除空临时目录: {self.temp_dir}")
                else:
                    logger.info(f"临时目录 {self.temp_dir} 非空，未移除。")
            except OSError as e:
                logger.warning(f"移除临时目录 {self.temp_dir} 失败: {e}")
        else:
            logger.info("插件临时目录未找到/定义/非预期，无需移除。")
        logger.info("GeminiArtist: terminate 清理完毕。")

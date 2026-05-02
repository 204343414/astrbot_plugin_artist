import os
import json
import re
import time
import random
import base64
import logging
import asyncio
import aiohttp
import calendar
from io import BytesIO
from dataclasses import dataclass
from urllib.parse import urlparse
from datetime import datetime
from typing import List

from lxml import etree
from bs4 import BeautifulSoup
from PIL import Image
from jinja2 import Environment, BaseLoader
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

# [防冲突] 模块级变量追踪当前活跃的调度器
# 插件热更新时新实例先通过此引用杀掉老调度器，避免新老并行双推
_ACTIVE_SCHED = None
_ALL_SCHEDS = set()  # 追踪所有调度器，防多实例


@dataclass
class RSSItem:
    chan_title: str
    title: str
    link: str
    description: str
    pubDate: str
    pubDate_timestamp: int
    pic_urls: list


class DataHandler:
    def __init__(self, config_path="data/astrbot_plugin_myrss_data.json"):
        self.config_path = config_path
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.config_path):
            d = {"rsshub_endpoints": []}
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            return d
        # [防冲突] 共享读锁，等待排他写锁释放后再读，避免读到写了一半的JSON
        with open(self.config_path, "r", encoding="utf-8") as f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            except (ImportError, OSError):
                pass
            return json.load(f)

    def save(self):
        # [防冲突] 文件排他锁，防止新老实例同时写JSON导致数据丢失
        # 场景：老实例的job推送完更新seen_links写文件，同时新实例也在写→后写的覆盖前面的
        # fcntl仅Linux/Mac可用，Windows环境静默跳过
        with open(self.config_path, "w", encoding="utf-8") as f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get_subs(self, user_id):
        urls = []
        for url, info in self.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            if user_id in info.get("subscribers", {}):
                urls.append(url)
        return urls

    def parse_channel_info(self, text):
        root = etree.fromstring(text)
        title = root.xpath("//title")[0].text
        desc_nodes = root.xpath("//description")
        desc = desc_nodes[0].text if desc_nodes else ""
        avatar = ""
        img_nodes = root.xpath("//channel/image/url")
        if img_nodes and img_nodes[0].text:
            avatar = img_nodes[0].text
        return title, desc or "", avatar

    def strip_html_pic(self, html):
        """从HTML中提取所有图片URL，包含暴力正则匹配YouTube封面"""
        if not html:
            return []
        
        soup = BeautifulSoup(html, "html.parser")
        urls = []
        
        # 1. 常规 <img src="...">
        for img in soup.find_all("img"):
            src = img.get("src")
            if src and src not in urls:
                urls.append(src)
                
        # 2. <video poster="...">
        for vid in soup.find_all("video"):
            poster = vid.get("poster")
            if poster and poster not in urls:
                urls.append(poster)
                
        # 3. [暴力增强] 直接正则扫描整个HTML文本匹配YouTube ID
        # 因为有时候 RSSHub 返回的 description 里只有纯文本链接，没有 <a> 标签
        # 匹配 youtube.com/watch?v=xxx 或 youtu.be/xxx
        patterns = [
            r'youtube\.com/watch\?v=([\w-]{11})',
            r'youtu\.be/([\w-]{11})',
            r'youtube\.com/embed/([\w-]{11})',
            r'youtube\.com/v/([\w-]{11})'
        ]
        
        found_ids = set()
        # 先搜 soup 里的 a 标签
        for a in soup.find_all("a", href=True):
            for pat in patterns:
                m = re.search(pat, a["href"])
                if m: found_ids.add(m.group(1))

        # 再暴力搜全文（兜底）
        for pat in patterns:
            for vid_id in re.findall(pat, html):
                found_ids.add(vid_id)

        # 构造封面地址
        for vid_id in found_ids:
            # 存两个分辨率，优先高清(maxres)，其次中等(hq)，防止maxres不存在
            u1 = f"https://i.ytimg.com/vi/{vid_id}/maxresdefault.jpg"
            u2 = f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"
            if u1 not in urls: urls.append(u1)
            if u2 not in urls: urls.append(u2)
        
        return urls

    def strip_html(self, html):
        soup = BeautifulSoup(html, "html.parser")
        return re.sub(r"\n+", "\n", soup.get_text())

    def get_root_url(self, url):
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"


class PicHandler:
    def __init__(self, adjust=False):
        self.adjust = adjust

    async def to_base64(self, image_url):
        try:
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                async with s.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        return None
                    raw = BytesIO(await r.read())
                    if self.adjust:
                        img = Image.open(raw).convert("RGB")
                        w, h = img.size
                        px = img.load()
                        cx, cy = random.choice([(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)])
                        px[cx, cy] = (255, 255, 255)
                        buf = BytesIO()
                        img.save(buf, format="JPEG")
                        buf.seek(0)
                        return base64.b64encode(buf.read()).decode()
                    else:
                        return base64.b64encode(raw.getvalue()).decode()
        except Exception:
            return None


class URLMapper:
    RULES = [
        (r"space\.bilibili\.com/(\d+)/dynamic", "/bilibili/user/dynamic/{0}", "B站UP主动态"),
        (r"space\.bilibili\.com/(\d+)", "/bilibili/user/dynamic/{0}", "B站UP主动态"),
        (r"bilibili\.com/bangumi/media/md(\d+)", "/bilibili/bangumi/media/{0}", "B站番剧"),
        (r"live\.bilibili\.com/(\d+)", "/bilibili/live/room/{0}", "B站直播间"),
        (r"manga\.bilibili\.com/detail/mc(\d+)", "/bilibili/manga/update/{0}", "B站漫画"),
        (r"youtube\.com/channel/([\w-]+)", "/youtube/channel/{0}", "YouTube频道"),
        # [修复] 优先匹配 YouTube 的动态(community/posts)、Shorts、直播等特定页面
        # 必须放在通用的 @user 规则之前，否则会被通用规则拦截
        (r"youtube\.com/@([\w.-]+)/(?:posts|community)", "/youtube/community/@{0}", "YouTube动态"),
        (r"youtube\.com/@([\w.-]+)/shorts", "/youtube/user/@{0}/shorts", "YouTube Shorts"),
        (r"youtube\.com/@([\w.-]+)/streams", "/youtube/user/@{0}/live", "YouTube直播记录"),
        # [原规则] 通用用户规则放在最后作为兜底
        (r"youtube\.com/@([\w.-]+)", "/youtube/user/@{0}", "YouTube用户"),
        (r"youtube\.com/playlist\?list=([\w-]+)", "/youtube/playlist/{0}", "YouTube播放列表"),
        (r"(?:twitter|x)\.com/(?!home|explore|search|settings|i/)([\w]+)", "/twitter/user/{0}", "Twitter/X"),
        (r"weibo\.com/u/(\d+)", "/weibo/user/{0}", "微博"),
        (r"zhihu\.com/people/([\w-]+)", "/zhihu/people/activities/{0}", "知乎"),
        (r"zhihu\.com/column/([\w-]+)", "/zhihu/zhuanlan/{0}", "知乎专栏"),
        (r"xiaohongshu\.com/user/profile/([\w]+)", "/xiaohongshu/user/{0}/notes", "小红书"),
        (r"github\.com/([\w.-]+)/([\w.-]+)/releases", "/github/release/{0}/{1}", "GitHub Release"),
        (r"github\.com/([\w.-]+)/([\w.-]+)(?:$|[/?#])", "/github/commits/{0}/{1}", "GitHub仓库"),
        (r"github\.com/([\w.-]+)(?:$|[/?#])", "/github/repos/{0}", "GitHub用户"),
        (r"t\.me/s?/?([\w]+)", "/telegram/channel/{0}", "Telegram"),
        (r"douyin\.com/user/([\w]+)", "/douyin/user/{0}", "抖音"),
        (r"instagram\.com/([\w.]+)(?:$|[/?#])", "/instagram/user/{0}", "Instagram"),
        (r"pixiv\.net/users/(\d+)", "/pixiv/user/{0}", "Pixiv"),
        (r"sspai\.com/u/([\w]+)", "/sspai/author/{0}", "少数派"),
        (r"okjike\.com/u/([\w-]+)", "/jike/user/{0}", "即刻"),
        (r"podcasts\.apple\.com/.*/id(\d+)", "/apple/podcast/{0}", "Apple Podcast"),
    ]

    HINTS = {
        "bilibili": (
            "B站可用路由(uid在space.bilibili.com/{uid}找):\n"
            "  UP主视频: /bilibili/user/video/{uid}\n"
            "  UP主动态: /bilibili/user/dynamic/{uid}\n"
            "  所有视频: /bilibili/user/video-all/{uid}\n"
            "  UP主图文: /bilibili/user/article/{uid}\n"
            "  UP主合集: /bilibili/user/collection/{uid}/{sid}\n"
            "  综合热门: /bilibili/popular/all\n"
            "  每周必看: /bilibili/weekly\n"
            "  排行榜: /bilibili/ranking/all\n"
            "  热搜: /bilibili/hot-search\n"
            "  番剧: /bilibili/bangumi/media/{mediaid}\n"
            "  直播: /bilibili/live/room/{roomID}\n"
            "  搜索: /bilibili/vsearch/{keyword}"
        ),
        "youtube": "YouTube路由:\n  频道: /youtube/channel/{id}\n  用户: /youtube/user/@{name}\n  播放列表: /youtube/playlist/{id}",
        "twitter": "Twitter/X路由:\n  用户: /twitter/user/{name}\n  媒体: /twitter/media/{name}\n  搜索: /twitter/keyword/{kw}",
        "x.com": "Twitter/X路由:\n  用户: /twitter/user/{name}\n  媒体: /twitter/media/{name}",
        "weibo": "微博路由:\n  用户: /weibo/user/{uid}\n  热搜: /weibo/search/hot",
        "zhihu": "知乎路由:\n  用户: /zhihu/people/activities/{id}\n  专栏: /zhihu/zhuanlan/{id}\n  热榜: /zhihu/hot",
        "github": "GitHub路由:\n  Release: /github/release/{owner}/{repo}\n  Commits: /github/commits/{owner}/{repo}",
        "xiaohongshu": "小红书路由:\n  用户笔记: /xiaohongshu/user/{id}/notes",
        "douyin": "抖音路由:\n  用户: /douyin/user/{uid}",
        "instagram": "Instagram路由:\n  用户: /instagram/user/{name}",
        "telegram": "Telegram路由:\n  频道: /telegram/channel/{name}",
        "pixiv": "Pixiv路由:\n  用户: /pixiv/user/{uid}\n  排行: /pixiv/ranking/{mode}",
    }

    @classmethod
    def match(cls, url):
        for pat, tpl, name in cls.RULES:
            m = re.search(pat, url)
            if m:
                return tpl.format(*m.groups()), name
        return None

    @classmethod
    def suggest(cls, url):
        try:
            netloc = urlparse(url).netloc.lower()
        except Exception:
            return "无法解析，请提供http开头的链接或/开头的路由。"
        for kw, hint in cls.HINTS.items():
            if kw in netloc:
                return hint
        return "未收录此平台。请到 https://docs.rsshub.app 查找路由后用/开头调用。"


class CardGen:
    """HTML模板 + Browserless 截图的卡片生成器（替代 Pillow）
    
    优势：
    - Emoji 原生彩色渲染
    - CSS 排版，不用手算像素坐标
    - 图片/头像用 <img> data URI，不怕防盗链
    """
    REC_CARD_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Noto Sans SC","Noto Sans CJK SC","PingFang SC",
     -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#fff;width:{{width}}px;-webkit-font-smoothing:antialiased}
.rc{padding:20px;border:1px solid #EFF3F4;border-radius:16px;margin:8px}
.rc-ban{background:#1D9BF0;color:#fff;font-size:14px;font-weight:700;
        padding:8px 16px;border-radius:10px 10px 0 0;margin:-20px -20px 16px;
        text-align:center;letter-spacing:1px}
.rc-hdr{display:flex;align-items:center;margin-bottom:14px}
.rc-avt{width:64px;height:64px;border-radius:50%;margin-right:14px;
        overflow:hidden;border:2px solid #EFF3F4;flex-shrink:0}
.rc-avt img{width:100%;height:100%;object-fit:cover}
.rc-avt-ph{width:64px;height:64px;border-radius:50%;margin-right:14px;
           background:#1D9BF0;display:flex;align-items:center;justify-content:center;
           color:#fff;font-size:28px;font-weight:700;flex-shrink:0}
.rc-nm{font-size:18px;font-weight:700;color:#0F1419}
.rc-rt{font-size:13px;color:#536471;margin-top:2px;word-break:break-all}
.rc-bio{font-size:14px;color:#0F1419;line-height:1.6;margin-bottom:14px;
        white-space:pre-line;word-break:break-word}
.rc-hr{border:none;border-top:1px solid #EFF3F4;margin:12px 0}
.rc-pvt{font-size:13px;font-weight:700;color:#536471;margin-bottom:8px}
.rc-pi{padding:10px 12px;background:#F7F9F9;border-radius:10px;margin-bottom:8px}
.rc-pit{font-size:14px;color:#0F1419;font-weight:500;line-height:1.4;
        display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rc-ptm{font-size:12px;color:#536471;margin-top:4px}
.rc-vt{background:#F0F7FF;border-radius:12px;padding:14px;margin-top:14px;text-align:center}
.rc-vid{font-size:12px;color:#536471;margin-bottom:6px}
.rc-vp{font-size:15px;color:#0F1419;font-weight:600;margin-bottom:4px}
.rc-vi{font-size:12px;color:#536471}
</style></head><body>
<div class="rc">
  <div class="rc-ban">📢 频道推荐</div>
  <div class="rc-hdr">
    {% if avatar_b64 %}
    <div class="rc-avt"><img src="data:image/png;base64,{{avatar_b64}}"></div>
    {% else %}
    <div class="rc-avt-ph">{{avatar_char}}</div>
    {% endif %}
    <div>
      <div class="rc-nm">{{title}}</div>
      <div class="rc-rt">{{route}}</div>
    </div>
  </div>
  {% if description %}<div class="rc-bio">{{description}}</div>{% endif %}
  {% if previews %}
  <hr class="rc-hr">
  <div class="rc-pvt">📋 最近动态预览</div>
  {% for p in previews %}
  <div class="rc-pi">
    <div class="rc-pit">{{p.title}}</div>
    {% if p.time %}<div class="rc-ptm">{{p.time}}</div>{% endif %}
  </div>
  {% endfor %}
  {% endif %}
  <div class="rc-vt">
    <div class="rc-vid">推荐编号: {{rec_id}}</div>
    <div class="rc-vp">回复「同意」订阅 / 回复「拒绝」取消</div>
    <div class="rc-vi">1人回复即生效 · 1小时无人回复自动订阅</div>
  </div>
</div>
</body></html>"""
    CARD_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:"Noto Sans SC","Noto Sans CJK SC","PingFang SC",
              -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
              "Helvetica Neue",sans-serif;
  background:#fff;
  width:{{width}}px;
  -webkit-font-smoothing:antialiased;
}
.card{padding:14px 16px;border-bottom:1px solid #EFF3F4}
.hdr{display:flex;align-items:center;margin-bottom:10px}
.avt{width:48px;height:48px;border-radius:50%;flex-shrink:0;
     margin-right:12px;overflow:hidden;border:1px solid #EFF3F4}
.avt img{width:100%;height:100%;object-fit:cover}
.avt-ph{width:48px;height:48px;border-radius:50%;flex-shrink:0;
        margin-right:12px;background:#1D9BF0;
        display:flex;align-items:center;justify-content:center;
        color:#fff;font-size:20px;font-weight:700;line-height:1}
.meta{overflow:hidden;display:flex;align-items:baseline;flex-wrap:nowrap}
.name{font-weight:700;font-size:15px;color:#0F1419;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
      max-width:260px}
.tm{font-size:13px;color:#536471;white-space:nowrap;margin-left:2px}
.body{margin-left:60px}
.ttl{font-size:17px;font-weight:700;color:#0F1419;line-height:1.5;
     margin-bottom:6px;
     display:-webkit-box;-webkit-line-clamp:4;
     -webkit-box-orient:vertical;overflow:hidden}
.dsc{font-size:15px;color:#536471;line-height:1.6;margin-bottom:10px;
     white-space:pre-line;word-break:break-word;
     display:-webkit-box;-webkit-line-clamp:15;
     -webkit-box-orient:vertical;overflow:hidden}
.pic{border-radius:14px;border:1px solid #EFF3F4;
     width:100%;max-height:500px;object-fit:cover;
     margin-bottom:12px;display:block}
.hr{border:none;border-top:1px solid #EFF3F4;margin:8px 0}
.lnk{font-size:13px;color:#1D9BF0;word-break:break-all}
.cmt{margin-top:8px;padding-top:10px;border-top:1px solid #EFF3F4;
     display:flex;align-items:flex-start}
.bavt{width:32px;height:32px;border-radius:50%;flex-shrink:0;
      margin-right:8px;overflow:hidden;border:1px solid #EFF3F4}
.bavt img{width:100%;height:100%;object-fit:cover}
.bavt-ph{width:32px;height:32px;border-radius:50%;flex-shrink:0;
         margin-right:8px;background:#646464;
         display:flex;align-items:center;justify-content:center;
         color:#fff;font-size:14px;font-weight:700;line-height:1}
.ctx{font-size:13px;color:#536471;line-height:1.5;flex:1}
.pvd{font-size:11px;color:#B4B4B4;margin-top:4px}
</style></head><body>
<div class="card">
  <div class="hdr">
    {% if avatar_b64 %}
    <div class="avt"><img src="data:image/png;base64,{{avatar_b64}}"></div>
    {% else %}
    <div class="avt-ph">{{avatar_char}}</div>
    {% endif %}
    <div class="meta">
      <span class="name">{{channel}}</span>
      {% if time_str %}<span class="tm">&middot; {{time_str}}</span>{% endif %}
    </div>
  </div>
  <div class="body">
    {% if title %}<div class="ttl">{{title}}</div>{% endif %}
    {% if desc %}<div class="dsc">{{desc}}</div>{% endif %}
    {% if thumb_b64 %}
    <img class="pic" src="data:image/jpeg;base64,{{thumb_b64}}">
    {% endif %}
    <hr class="hr">
    {% if link %}<div class="lnk">🔗 {{link_display}}</div>{% endif %}
    {% if comment %}
    <div class="cmt">
      {% if bot_avatar_b64 %}
      <div class="bavt"><img src="data:image/png;base64,{{bot_avatar_b64}}"></div>
      {% else %}
      <div class="bavt-ph">B</div>
      {% endif %}
      <div>
        <div class="ctx">{{comment}}</div>
        {% if bot_provider_name %}<div class="pvd">via {{bot_provider_name}}</div>{% endif %}
      </div>
    </div>
    {% endif %}
  </div>
</div>
</body></html>"""

    def __init__(self, width=480, browserless_url="http://browserless:3000"):
        self.w = width
        self.browserless_url = browserless_url.rstrip("/")
        self._env = Environment(loader=BaseLoader(), autoescape=True)
        self._tpl = self._env.from_string(self.CARD_HTML)
        self._rec_tpl = self._env.from_string(self.REC_CARD_HTML)
        self.logger = logging.getLogger("astrbot")
        self._sema = asyncio.Semaphore(2)  # 最多同时 2 个截图请求

    def _format_time(self, ts_str):
        """把RSS时间字符串简化为 YYYY-MM-DD HH:MM"""
        if not ts_str:
            return ""
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(ts_str)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        for fmt in ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"]:
            try:
                dt = datetime.strptime(ts_str.replace("Z", "+0000"), fmt)
                return dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                continue
        return ts_str[:25] if len(ts_str) > 25 else ts_str

    async def make(self, channel="", title="", desc="", link="", ts="",
                   thumb=None, avatar=None, comment="", bot_avatar=None,
                   bot_provider_name=""):
        """渲染 HTML → browserless 截图 → 返回 base64 PNG"""

        display_name = (channel or "未知频道")
        display_name = display_name.replace(" - Community Posts - YouTube", "").replace(" - YouTube", "")
        time_str = self._format_time(ts)

        # 头像 → base64 data URI
        avatar_b64 = ""
        avatar_char = "?"
        if avatar and isinstance(avatar, bytes) and len(avatar) > 100:
            avatar_b64 = base64.b64encode(avatar).decode()
        for c in (channel or ""):
            if c.strip():
                avatar_char = c
                break

        # 缩略图
        thumb_b64 = ""
        if thumb and isinstance(thumb, bytes) and len(thumb) > 100:
            thumb_b64 = base64.b64encode(thumb).decode()

        # Bot 头像
        bot_avatar_b64 = ""
        if bot_avatar and isinstance(bot_avatar, bytes) and len(bot_avatar) > 100:
            bot_avatar_b64 = base64.b64encode(bot_avatar).decode()

        # 链接截断
        link_display = link if len(link) <= 50 else link[:50] + "..."

        # 去重：desc 与 title 相同则不重复显示
        desc_clean = (desc or "").strip()
        if title and desc_clean == (title or "").strip():
            desc_clean = ""
        show_title = title and title not in ("无标题", "")

        html = self._tpl.render(
            width=self.w,
            channel=display_name,
            time_str=time_str,
            avatar_b64=avatar_b64,
            avatar_char=avatar_char,
            title=title if show_title else "",
            desc=desc_clean,
            thumb_b64=thumb_b64,
            link=link,
            link_display=link_display,
            comment=comment,
            bot_avatar_b64=bot_avatar_b64,
            bot_provider_name=bot_provider_name,
        )

        try:
            return await self._screenshot(html)
        except Exception as e:
            self.logger.error("[CardGen] browserless 截图失败: %s (%s)", type(e).__name__, e)
            return ""

    async def _screenshot(self, html: str) -> str:
        payload = {
            "html": html,
            "options": {
                "fullPage": True,
                "type": "png",
            },
            "viewport": {
                "width": self.w,
                "height": 1,
                "deviceScaleFactor": 2,
            },
            "gotoOptions": {
                "waitUntil": "domcontentloaded",
            },
        }

        endpoints = [
            f"{self.browserless_url}/chromium/screenshot",
        ]

        async with self._sema:
            conn = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=30)

            async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
                for ep in endpoints:
                    # 429 重试（最多 3 次，间隔递增）
                    for attempt in range(3):
                        try:
                            async with session.post(
                                ep, json=payload,
                                headers={"Content-Type": "application/json"},
                            ) as resp:
                                if resp.status == 200:
                                    ct = resp.headers.get("Content-Type", "")
                                    data = await resp.read()
                                    if len(data) > 500 and (
                                        "image" in ct or data[:4] == b'\x89PNG'
                                    ):
                                        return base64.b64encode(data).decode()
                                elif resp.status == 429:
                                    wait = 2 ** attempt
                                    self.logger.warning("[CardGen] 429, retry in %ds (attempt %d/3)", wait, attempt + 1)
                                    await asyncio.sleep(wait)
                                    continue
                                body = await resp.text()
                                self.logger.warning("[CardGen] %s -> HTTP %d: %s", ep, resp.status, body[:200])
                        except aiohttp.ClientError as e:
                            self.logger.warning("[CardGen] %s 连接失败: %s", ep, e)
                        break  # 非429错误不重试

        raise RuntimeError("browserless 不可用")
    async def make_rec_card(self, title="", description="", avatar=None,
                            route="", previews=None, rec_id=""):
        """生成推荐卡片"""
        avatar_b64 = ""
        avatar_char = "?"
        if avatar and isinstance(avatar, bytes) and len(avatar) > 100:
            avatar_b64 = base64.b64encode(avatar).decode()
        for c in (title or ""):
            if c.strip():
                avatar_char = c
                break

        preview_data = []
        if previews:
            for p in previews[:3]:
                preview_data.append({
                    "title": (p.get("title", "") or "")[:80],
                    "time": p.get("time", ""),
                })

        html = self._rec_tpl.render(
            width=self.w,
            title=title or "未知频道",
            description=(description or "")[:200],
            avatar_b64=avatar_b64,
            avatar_char=avatar_char,
            route=route,
            previews=preview_data,
            rec_id=rec_id,
        )
        try:
            return await self._screenshot(html)
        except Exception as e:
            self.logger.error("[CardGen] 推荐卡片截图失败: %s", e)
            return ""
@register("astrbot_plugin_myrss", "MyRSS", "RSS订阅插件(LLM增强版)", "1.0.0", "")
class MyRssPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot")
        self.ctx = context
        self.cfg = config
        self.dh = DataHandler()

        self.title_max = config.get("title_max_length", 60)
        self.desc_max = config.get("description_max_length", 200)
        self.max_poll = config.get("max_items_per_poll", 5)
        self.t2i = config.get("t2i", False)
        self.hide_url = config.get("is_hide_url", False)
        self.read_pic = config.get("is_read_pic", True)
        self.adjust_pic = config.get("is_adjust_pic", False)
        self.max_pic = config.get("max_pic_item", 3)
        self.compose = config.get("compose", True)
        self.enable_comment = config.get("enable_comment", True)
        self.comment_provider_id = config.get("comment_provider_id", "")
        self.comment_persona = config.get("comment_persona", "")
        self.comment_max_length = config.get("comment_max_length", 80)
        self.bot_qq = config.get("bot_qq", "")
        self.bot_provider_name = config.get("bot_provider_name", "")
        self.content_filter = config.get("content_filter", True)
        self._comment_cache = {}  # key=item_link, value=comment_text
        self._safe_cache = {}  # key=item_link, value=bool(safe)
        # 活跃群检测（学新闻插件）
        self._active_groups = set()  # 内存中记录有人说话的群
        self._group_data_file = "data/astrbot_plugin_myrss_groups.json"
        self._group_data = self._load_group_data()

        # 全局订阅
        self.global_feeds = [
            line.strip() for line in config.get("global_feeds", "").split("\n")
            if line.strip() and line.strip().startswith("/")
        ]
        self.global_feed_interval = max(config.get("global_feed_interval", 15), 5)
        self.global_feed_max_interval = max(config.get("global_feed_max_interval", 1440), self.global_feed_interval)
        self._feed_miss_count = {}  # key=url, value=连续无更新次数
        self._feed_tick = {}  # key=url, value=全局订阅触发次数（用于退避skip计数）
        self.push_delay_min = config.get("push_delay_min", 5.0)
        self.push_delay_max = config.get("push_delay_max", 8.0)
        self.filter_provider_id = config.get("filter_provider_id", "")
        self.safe_mode = config.get("safe_mode", True)
        self.safe_mode_groups = [g.strip() for g in config.get("safe_mode_groups", "").split(",") if g.strip()]
        self.group_cooldown_seconds = max(config.get("group_cooldown_minutes", 60), 1) * 60
        self._group_cooldown = {}  # key=group_id, value=上次推送的时间戳
        self.image_caption_provider_id = config.get("image_caption_provider_id", "")

        self.pic = PicHandler(self.adjust_pic)
        self.browserless_url = config.get("browserless_url", "http://browserless:3000")
        self.card = CardGen(browserless_url=self.browserless_url)

        # 防并发锁，key = (url, user)
        self._locks: dict = {}
        self._data_lock = asyncio.Lock()  # 保护 dh.data 读写
        # 推荐系统
        self._recs_file = "data/astrbot_plugin_myrss_recs.json"
        self._pending_recs = self._load_recs()
        self._last_preview = {}
        self._aiocqhttp_bot = None  # 缓存 aiocqhttp 的 bot client，用于直连发送
        self._bot_ready = False  # 收到第一条消息后才开始全局推送
        self._push_lock = asyncio.Lock()  # 全局推送发送锁，防止多源同时推同一个群
        # [防冲突] 在创建新调度器前，先杀掉模块级残留的老调度器
        # 场景：插件热更新时框架直接创建新实例，老实例的destroy()可能未被调用
        # 如果不杀，老调度器继续运行老代码的job，和新调度器同时推送→双推
        global _ACTIVE_SCHED, _ALL_SCHEDS
        # 杀掉所有残留的调度器（不只是上一个）
        for old_sched in list(_ALL_SCHEDS):
            try:
                if old_sched.running:
                    old_sched.shutdown(wait=False)
                    self.logger.warning("MyRSS: 停止残留调度器 id=%s", id(old_sched))
            except Exception:
                pass
        _ALL_SCHEDS.clear()
        _ACTIVE_SCHED = None

        self.sched = AsyncIOScheduler()
        _ACTIVE_SCHED = self.sched
        _ALL_SCHEDS.add(self.sched)
        self.sched.start()
        self._reload_jobs()
        # 推荐超时检查：每10分钟检查一次
        self.sched.add_job(
            self._check_rec_timeout, "interval", minutes=10,
            id="myrss_rec_timeout", replace_existing=True,
            misfire_grace_time=120,
        )
    async def destroy(self):
        """插件卸载/禁用时停止调度器"""
        global _ACTIVE_SCHED
        try:
            if self.sched.running:
                # [防冲突] wait=True：等正在执行的job跑完再关，避免推送到一半被掐断
                # 之前用wait=False会导致job还在跑但调度器已标记关闭，行为未定义
                self.sched.shutdown(wait=True)
                self.logger.info("MyRSS: 调度器已停止")
            # [防冲突] 清除全局引用，防止下次init误杀已关闭的对象
            if _ACTIVE_SCHED is self.sched:
                _ACTIVE_SCHED = None
            _ALL_SCHEDS.discard(self.sched)
        except Exception as e:
            self.logger.error("MyRSS: 停止调度器失败: %s", e)

    def _get_lock(self, url: str, user: str) -> asyncio.Lock:
        key = (url, user)
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _cron(self, expr: str) -> dict:
        f = expr.split(" ")
        return {"minute": f[0], "hour": f[1], "day": f[2], "month": f[3], "day_of_week": f[4]}

    def _reload_jobs(self) -> None:
        self.sched.remove_all_jobs()
        for url, info in self.dh.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            subs = info.get("subscribers", {})
            if not subs:
                continue
            # 取所有订阅者中间隔最大的cron（最保守，减少拉取频率）
            def cron_to_minutes(expr: str) -> int:
                """支持 */15 * * * * 和 0 */1 * * * 两种格式"""
                try:
                    f = expr.split(" ")
                    if f[0].startswith("*/"):
                        return int(f[0][2:])
                    if f[1].startswith("*/"):
                        return int(f[1][2:]) * 60
                    return 60
                except Exception:
                    return 60

            max_minutes = max(cron_to_minutes(si["cron_expr"]) for si in subs.values())

            if max_minutes < 60:
                merged_cron = f"*/{max_minutes} * * * *"
            else:
                merged_cron = f"0 */{max_minutes // 60} * * *"
            # 每个URL只注册一个job，拉取后分发给所有订阅者
            # [防冲突] id + replace_existing 保证同一个url在调度器里只有一个job
            # 没有id时APScheduler会自动生成随机id，reload_jobs就无法识别"已存在"
            # replace_existing=True：如果id已存在就替换而非报错，适配热更新场景
            # misfire_grace_time=120：job错过触发时间后120秒内还可以补执行，超时则跳过
            # 防止调度器shutdown/restart期间堆积的job全部同时涌入
            job_id = f"myrss_{url}"
            self.sched.add_job(
                self._cron_cb_url, "cron",
                **self._cron(merged_cron),
                args=[url],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=120,
            )
            if max_minutes < 60:
                self.logger.info("RSS调度: %s 每%d分钟拉取，%d个订阅者", url, max_minutes, len(subs))
            else:
                self.logger.info("RSS调度: %s 每%d小时拉取，%d个订阅者", url, max_minutes // 60, len(subs))
                    # 重建全局订阅 job（因为 remove_all_jobs 会一并清除）
        if self.global_feeds:
            self._setup_global_feeds()

    async def _fetch(self, url: str):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        conn = aiohttp.TCPConnector(ssl=False)
        to = aiohttp.ClientTimeout(total=30, connect=10)

        async def _try(u: str):
            try:
                async with aiohttp.ClientSession(trust_env=True, connector=conn, timeout=to, headers=headers) as s:
                    async with s.get(u) as r:
                        if r.status != 200:
                            return None
                        return await r.read()
            except Exception:
                return None

        for attempt in range(3):
            data = await _try(url)
            if data is not None:
                # 检查是否返回了HTML错误页而非XML
                if data[:5] == b'<?xml' or data[:1] == b'<' and b'<item>' in data[:5000]:
                    return data
                if b'<html>' not in data[:500].lower():
                    return data
                # 拿到HTML错误页，等几秒重试（等RSSHub内部缓存刷新）
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
                return data  # 第3次不管什么都返回
            if attempt < 2:
                await asyncio.sleep(3)
        if data is not None:
            return data

        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            return None

        parsed = urlparse(url)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        cur_root = f"{parsed.scheme}://{parsed.netloc}"
        norm_eps = [(e[:-1] if e.endswith("/") else e) for e in eps]

        for ep in norm_eps:
            if ep == cur_root:
                continue
            alt = ep + path
            data = await _try(alt)
            if data is not None:
                self.logger.warning("rss: 端点不可用，已自动切换 %s -> %s", url, alt)
                return data

        return None

    def _parse_pubdate(self, pd: str):
        """解析各种日期格式，失败返回None"""
        if not pd:
            return None
        pd = pd.strip()

        # 优先用标准库的RFC2822解析器（最稳，不受locale影响）
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pd)
            return int(dt.timestamp())
        except Exception:
            pass

        # 补充ISO8601等格式
        pd_clean = pd.replace("GMT", "+0000").replace("Z", "+0000")
        for fmt in [
            "%a, %d %b %Y %H:%M:%S %z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
        ]:
            try:
                dt = datetime.strptime(pd_clean, fmt)
                return int(dt.timestamp())
            except Exception:
                continue
        return None

    async def _poll(self, url: str, num: int = -1, after_ts: int = 0, after_link: str = "") -> List[RSSItem]:
        text = await self._fetch(url)
        if text is None:
            return []
        try:
            root = etree.fromstring(text)
        except ValueError:
            try:
                root = etree.fromstring(
                    text.replace(b'encoding="gb2312"', b'')
                        .replace(b'encoding="GB2312"', b'')
                )
            except Exception:
                return []

        items = root.xpath("//item")
        ns = {"media": "http://search.yahoo.com/mrss/"}
        result = []
        cnt = 0

        for it in items:
            try:
                ch = self.dh.data[url]["info"]["title"] if url in self.dh.data else "未知"

                tn = it.xpath("title")
                title = tn[0].text if tn else "无标题"
                if len(title) > self.title_max:
                    title = title[:self.title_max] + "..."

                ln = it.xpath("link")
                link = (ln[0].text or "").strip() if ln else ""
                if link and not re.match(r"^https?://", link):
                    link = self.dh.get_root_url(url) + link

                dn = it.xpath("description")
                raw = dn[0].text if dn else ""
                pics = self.dh.strip_html_pic(raw) if raw else []
                desc = self.dh.strip_html(raw) if raw else ""
                if len(desc) > self.desc_max:
                    desc = desc[:self.desc_max] + "..."

                # [增强] 从多种XML标签提取图片URL
                # media:thumbnail  → RSS标准缩略图（视频路由常用）
                # media:content    → 有些源把封面图放这里（YouTube等）
                # enclosure        → RSS附件
                # local-name()通配 → 兼容不同命名空间写法
                for u in (
                    it.xpath("media:thumbnail/@url", namespaces=ns)
                    + it.xpath("media:content/@url", namespaces=ns)
                    + it.xpath("media:content/media:thumbnail/@url", namespaces=ns)
                    + it.xpath(".//*[local-name()='thumbnail']/@url")
                    + it.xpath(".//*[local-name()='content']/@url")
                    + it.xpath("enclosure[contains(@type,'image')]/@url")
                    + it.xpath("enclosure/@url")
                ):
                    if u and u not in pics:
                        # 过滤掉视频/音频文件，只保留图片
                        low = u.lower()
                        if not any(low.endswith(e) for e in ('.mp4', '.webm', '.mp3', '.m4a', '.ogg')):
                            pics.append(u)

                pub_nodes = it.xpath("pubDate")
                if pub_nodes:
                    pd = pub_nodes[0].text or ""
                    pts = self._parse_pubdate(pd)

                    if pts is None:
                        # 解析失败，用 link 兜底去重
                        if link and link != after_link:
                            result.append(RSSItem(ch, title, link, desc, pd, 0, pics))
                            cnt += 1
                    elif pts > after_ts:
                        result.append(RSSItem(ch, title, link, desc, pd, pts, pics))
                        cnt += 1
                    else:
                        break
                else:
                    if link and link != after_link:
                        result.append(RSSItem(ch, title, link, desc, "", 0, pics))
                        cnt += 1
                    else:
                        break

                if num != -1 and cnt >= num:
                    break

            except Exception as e:
                self.logger.error("rss: 解析条目失败 %s: %s", url, e)
                break

        return result

    async def _add(self, url: str, cron_expr: str, event: AstrMessageEvent):
        user = event.unified_msg_origin

        async def poll_with_retry(u: str, retries: int = 3, sleep_s: int = 5):
            last = []
            for i in range(retries):
                last = await self._poll(u)
                if last:
                    return last
                if i < retries - 1:
                    await asyncio.sleep(sleep_s)
            return last

        # 已存在订阅源：只加订阅者
        if url in self.dh.data:
            items = await poll_with_retry(url)
            if not items:
                return event.plain_result("连续3次无法从该源获取内容，源可能暂时不可用，请稍后重试。")

            self.dh.data[url].setdefault("subscribers", {})
            self.dh.data[url]["subscribers"][user] = {
                "cron_expr": cron_expr,
                "last_update": items[0].pubDate_timestamp,
                "latest_link": items[0].link,
                "seen_links": [it.link for it in items if it.link][:200],
            }

            self.dh.save()
            return self.dh.data[url]["info"]

        # 新订阅源：先解析频道信息
        text = await self._fetch(url)
        if text is None:
            return event.plain_result("无法访问: " + url + "\n请检查RSSHub端点是否可用。")

        try:
            title, desc, avatar = self.dh.parse_channel_info(text)
        except Exception as e:
            return event.plain_result("解析失败: " + str(e))

        items = await poll_with_retry(url)
        if not items:
            return event.plain_result("源可访问但连续3次获取不到内容，可能是该平台接口不稳定，请稍后重试。")

        self.dh.data[url] = {
            "subscribers": {
                user: {
                    "cron_expr": cron_expr,
                    "last_update": items[0].pubDate_timestamp,
                    "latest_link": items[0].link,
                    "seen_links": [it.link for it in items if it.link][:200],
                }
            },
            "info": {"title": title, "description": desc, "avatar": avatar},
        }
        self.dh.save()
        return self.dh.data[url]["info"]
    # ============================================================
    #  活跃群检测
    # ============================================================

    def _load_group_data(self) -> dict:
        if os.path.exists(self._group_data_file):
            try:
                with open(self._group_data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"groups": {}, "blocked_feeds": {}}
        # blocked_feeds 结构: { "群unified_id": ["/twitter/user/elonmusk", ...] }

    def _save_group_data(self):
        try:
            with open(self._group_data_file, "w", encoding="utf-8") as f:
                json.dump(self._group_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error("[MyRSS] save group data failed: %s", e)
    def _load_recs(self) -> dict:
        if os.path.exists(self._recs_file):
            try:
                with open(self._recs_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                expired = [k for k, v in data.items() if now - v.get("created_at", 0) > 86400]
                for k in expired:
                    del data[k]
                return data
            except Exception:
                pass
        return {}

    def _save_recs(self):
        try:
            with open(self._recs_file, "w", encoding="utf-8") as f:
                json.dump(self._pending_recs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error("[MyRSS] save recs failed: %s", e)

    async def _auto_subscribe(self, url: str, group_id: str, interval: int = 30) -> bool:
        """投票通过后自动订阅"""
        try:
            items = await self._poll(url, num=5)
            cron_expr = f"*/{interval} * * * *" if interval < 60 else f"0 */{interval // 60} * * *"

            if url not in self.dh.data:
                text = await self._fetch(url)
                if text:
                    t, d, a = self.dh.parse_channel_info(text)
                    self.dh.data[url] = {
                        "info": {"title": t, "description": d, "avatar": a},
                        "subscribers": {},
                    }
                else:
                    return False

            self.dh.data[url].setdefault("subscribers", {})
            self.dh.data[url]["subscribers"][group_id] = {
                "cron_expr": cron_expr,
                "last_update": items[0].pubDate_timestamp if items else 0,
                "latest_link": items[0].link if items else "",
                "seen_links": [it.link for it in items if it.link][:200] if items else [],
            }
            self.dh.save()
            self._reload_jobs()
            return True
        except Exception as e:
            self.logger.error("[MyRSS] auto subscribe failed: %s", e)
            return False

    async def _process_vote(self, event: AstrMessageEvent):
        """处理群友对推荐的投票：1人同意即订阅，1人拒绝即取消"""
        group_id = event.unified_msg_origin
        text = ""
        try:
            text = (event.message_str or "").strip()
        except Exception:
            return
        if not text:
            return

        is_agree = text in ("同意", "订阅", "可以", "好", "好的", "赞成", "支持", "ok", "OK")
        is_reject = text in ("拒绝", "反对", "不要", "不行", "取消", "不")
        if not is_agree and not is_reject:
            return

        target_rec_id = None
        target_rec = None
        for rec_id, rec in sorted(self._pending_recs.items(), key=lambda x: x[1].get("created_at", 0), reverse=True):
            if group_id in rec.get("groups", {}):
                gs = rec["groups"][group_id]
                if gs.get("status") == "pending":
                    target_rec_id = rec_id
                    target_rec = rec
                    break

        if not target_rec:
            return

        gs = target_rec["groups"][group_id]
        title = target_rec.get("title", "未知")

        if is_agree:
            gs["status"] = "approved"
            self._save_recs()
            ok = await self._auto_subscribe(
                target_rec["url"], group_id, target_rec.get("interval", 30)
            )
            if ok:
                await self.ctx.send_message(group_id, MessageChain(chain=[
                    Comp.Plain(f"✅ 已订阅「{title}」\n⏰ 每{target_rec.get('interval', 30)}分钟检查更新")
                ]))
            else:
                await self.ctx.send_message(group_id, MessageChain(chain=[
                    Comp.Plain(f"⚠️ 订阅失败，请手动订阅: {target_rec.get('route', '')}")
                ]))

        elif is_reject:
            gs["status"] = "rejected"
            self._save_recs()
            await self.ctx.send_message(group_id, MessageChain(chain=[
                Comp.Plain(f"❌ 推荐「{title}」已取消")
            ]))

        elif is_reject:
            if voter not in gs.get("rejects", []):
                gs.setdefault("rejects", []).append(voter)
                self._save_recs()
                reject_count = len(gs["rejects"])

                is_admin = await self._is_group_admin(group_id, voter)
                if reject_count >= 3 or is_admin:
                    gs["status"] = "rejected"
                    self._save_recs()
                    await self.ctx.send_message(group_id, MessageChain(chain=[
                        Comp.Plain(f"❌ 推荐「{target_rec.get('title', '')}」已被拒绝（{reject_count}人反对）")
                    ]))
                else:
                    await self.ctx.send_message(group_id, MessageChain(chain=[
                        Comp.Plain(f"📊 已记录反对票（{reject_count}/3），3票拒绝则取消推荐")
                    ]))
    def _mark_active(self, unified_id: str):
        """标记某个群有人说话"""
        if "GroupMessage" not in unified_id:
            return
        self._active_groups.add(unified_id)
        if unified_id not in self._group_data["groups"]:
            self._group_data["groups"][unified_id] = {
                "active": True,
                "dormant": False,
                "last_activity": int(time.time()),
            }
        else:
            self._group_data["groups"][unified_id]["active"] = True
            self._group_data["groups"][unified_id]["dormant"] = False
            self._group_data["groups"][unified_id]["last_activity"] = int(time.time())

    def _get_active_groups(self) -> list:
        """获取所有活跃群的unified_id列表"""
        result = []
        for uid, info in self._group_data["groups"].items():
            if info.get("active") and not info.get("dormant"):
                result.append(uid)
        return result

    def _reset_activity(self):
        """推送后重置活跃状态，等下次有人说话"""
        for uid in self._group_data["groups"]:
            self._group_data["groups"][uid]["active"] = uid in self._active_groups
        self._active_groups.clear()
        self._save_group_data()

    @filter.regex(r"[\s\S]*")
    async def _catch_activity(self, event: AstrMessageEvent):
        """捕获所有消息：活跃度 + bot缓存 + 投票检测"""
        if hasattr(event, "unified_msg_origin"):
            self._mark_active(event.unified_msg_origin)
        if self._aiocqhttp_bot is None:
            try:
                if hasattr(event, 'bot') and event.bot is not None:
                    self._aiocqhttp_bot = event.bot
                    self.logger.info("[MyRSS] cached aiocqhttp bot client")
                    self._bot_ready = True
            except Exception:
                pass
        if not self._bot_ready:
            self._bot_ready = True
        # 投票检测
        if self._pending_recs and "GroupMessage" in getattr(event, 'unified_msg_origin', ''):
            try:
                await self._process_vote(event)
            except Exception as e:
                self.logger.error("[MyRSS] vote error: %s", e)
    # ============================================================
    #  全局订阅
    # ============================================================

    def _setup_global_feeds(self):
        """为全局订阅源注册定时任务（用最短间隔，在job内部动态跳过）"""
        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            self.logger.warning("[MyRSS] no endpoints for global feeds")
            return

        for route in self.global_feeds:
            url = eps[0].rstrip("/") + route

            if url not in self.dh.data:
                self.dh.data[url] = {
                    "subscribers": {},
                    "info": {"title": route, "description": "全局订阅"},
                    "global": True,
                }
            self.dh.data[url]["global"] = True
            self._feed_miss_count[url] = 0
            self._feed_tick[url] = 0

            # 用基础间隔注册，退避在job内部通过跳过实现
            base = self.global_feed_interval
            if base < 60:
                cron = f"*/{base} * * * *"
            else:
                cron = f"0 */{base // 60} * * *"

            job_id = f"myrss_global_{url}"
            self.sched.add_job(
                self._global_feed_job, "cron",
                **self._cron(cron),
                args=[url],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=120,
            )
            self.logger.info("[MyRSS] global feed: %s base=%dmin", route, base)

        self.dh.save()

    def _get_current_interval(self, url: str) -> int:
        """根据连续miss次数计算当前间隔（分钟）"""
        miss = self._feed_miss_count.get(url, 0)
        base = self.global_feed_interval

        # 每3次miss翻倍: 0-2次=base, 3-5次=2x, 6-8次=4x, 9-11次=8x...
        multiplier = 2 ** (miss // 3)
        current = base * multiplier

        return min(current, self.global_feed_max_interval)

    def _should_skip_this_tick(self, url: str) -> bool:
        """判断本次tick是否应该跳过（实现退避）
        修复点：不能用 miss 做取模（skip 时 miss 不变，会导致永远 skip）
        改为用 tick（每次触发都会增长）来决定“每N次触发执行一次”
        """
        miss = self._feed_miss_count.get(url, 0)
        if miss < 3:
            return False  # 前3次不跳

        current_interval = self._get_current_interval(url)
        base = self.global_feed_interval
        if base <= 0:
            return False

        # 需要每多少次基础tick执行一次
        skip_ratio = max(1, current_interval // base)

        tick = self._feed_tick.get(url, 0)
        # 每 skip_ratio 次触发执行 1 次，其它时候跳过
        return (tick % skip_ratio) != 0
    async def _global_feed_job(self, url: str):
        """全局订阅的定时推送（带指数退避）"""
        await self._global_feed_job_inner(url)

    async def _global_feed_job_inner(self, url: str):
        """全局推送实际逻辑（被 _data_lock 保护）"""
        # tick自增：每次触发都+1，用于退避skip计数
        self._feed_tick[url] = self._feed_tick.get(url, 0) + 1

        # 退避检查：是否跳过本次
        if self._should_skip_this_tick(url):
            return
        # 等 bot 就绪（收到过至少一条消息）
        if not self._bot_ready:
            self.logger.info("[MyRSS] bot not ready yet (no message received since startup), skip")
            return
        current_interval = self._get_current_interval(url)
        miss = self._feed_miss_count.get(url, 0)
        self.logger.info("[MyRSS] global feed job: %s (miss=%d, interval=%dmin)", url, miss, current_interval)

        async with self._data_lock:
            self.dh.data = self.dh._load()

        # 获取全局seen_links
        if url not in self.dh.data:
            return
        feed_data = self.dh.data[url]
        seen = set(feed_data.get("global_seen_links", []))

        items = await self._poll(url, num=self.max_poll, after_ts=feed_data.get("global_last_update", 0))
        if not items:
            self._feed_miss_count[url] = self._feed_miss_count.get(url, 0) + 1
            new_interval = self._get_current_interval(url)
            self.logger.info("[MyRSS] no new items, miss=%d, next check ~%dmin",
                           self._feed_miss_count[url], new_interval)
            return

        def item_key(it):
            if it.link:
                # 归一化：去掉 query / fragment，避免同一条推文因为参数不同被当成新内容
                return it.link.split("#", 1)[0].split("?", 1)[0]
            return f"{it.title}|{it.pubDate_timestamp}"

        new_items = [it for it in items if item_key(it) not in seen]
        if not new_items:
            self._feed_miss_count[url] = self._feed_miss_count.get(url, 0) + 1
            return

        # 内容过滤
        safe_items = []
        for it in new_items:
            if self.content_filter:
                if await self._check_content_safe(it):
                    safe_items.append(it)
                else:
                    self.logger.info("[MyRSS] global feed filtered: %s", it.title[:30])
            else:
                safe_items.append(it)

        if not safe_items:
            # 更新seen即使被过滤
            new_keys = [item_key(it) for it in new_items]
            feed_data["global_seen_links"] = (new_keys + feed_data.get("global_seen_links", []))[:200]
            ts_list = [it.pubDate_timestamp for it in new_items if it.pubDate_timestamp > 0]
            if ts_list:
                feed_data["global_last_update"] = max(ts_list)
            self.dh.save()
            return

        # 更新seen_links（推送前，防重复）
        new_keys = [item_key(it) for it in safe_items]
        feed_data["global_seen_links"] = (new_keys + feed_data.get("global_seen_links", []))[:200]
        ts_list = [it.pubDate_timestamp for it in safe_items if it.pubDate_timestamp > 0]
        if ts_list:
            feed_data["global_last_update"] = max(ts_list)
        self.dh.save()

        # 生成卡片（只生成一次，推给所有群）
        batch = safe_items[:5]
        if len(batch) > 1:
            cards = [await self._make_card_b64(it) for it in batch]
            cards = [c for c in cards if c]
            if cards:
                merged = self._merge_cards_b64(cards)
                comps = [Comp.Image.fromBase64(merged)] if merged else None
            else:
                comps = None
        elif batch:
            comps = await self._make_comps(batch[0])
        else:
            return

        if not comps:
            return

        # 推送给所有活跃群（排除屏蔽了该源的群）
        # [安全模式] 如果开启，只推送到指定的测试群
        if self.safe_mode:
            if not self.safe_mode_groups:
                self.logger.warning("[MyRSS] safe_mode ON but no test groups configured, skip push")
                return
            # 构造测试群的 unified_id
            active_groups = []
            for gid in self.safe_mode_groups:
                active_groups.append(f"aiocqhttp:GroupMessage:{gid}")
            self.logger.info("[MyRSS] safe_mode ON, only pushing to test groups: %s", self.safe_mode_groups)
        else:
            active_groups = self._get_active_groups()
        # 从URL提取路由部分用于匹配屏蔽列表
        eps = self.dh.data.get("rsshub_endpoints", [])
        feed_route = url
        for ep in eps:
            ep = ep.rstrip("/")
            if url.startswith(ep):
                feed_route = url[len(ep):]
                break

        blocked = self._group_data.get("blocked_feeds", {})
        push_groups = [g for g in active_groups if feed_route not in blocked.get(g, [])]
        # 如果某群已经“自己订阅”了同一个源，就不再给它推全局，避免重复
        personal_subs = set(self.dh.data.get(url, {}).get("subscribers", {}).keys())
        push_groups = [g for g in push_groups if g not in personal_subs]
        # 冷却期检查：跳过最近已经推送过的群
        now = time.time()
        cooled_groups = []
        skipped_cooldown = 0
        for g in push_groups:
            last_push = self._group_cooldown.get(g, 0)
            if now - last_push >= self.group_cooldown_seconds:
                cooled_groups.append(g)
            else:
                skipped_cooldown += 1
                remaining = int((self.group_cooldown_seconds - (now - last_push)) / 60)
                self.logger.info("[MyRSS] group %s in cooldown, skip (%d min remaining)", g, remaining)
        push_groups = cooled_groups

        self.logger.info("[MyRSS] global push %d items to %d groups (skipped %d blocked, %d cooldown)",
                        len(batch), len(push_groups),
                        len(active_groups) - len(push_groups) - skipped_cooldown, skipped_cooldown)

        # 给推送内容加上退订提示
        push_comps = list(comps)  # 复制一份，不污染原始 comps
        push_comps.append(Comp.Plain("\n💡 如需退订本群推送，请@我说「屏蔽xxx」"))

        async with self._push_lock:
            for group_id in push_groups:
                # 再次检查冷却（可能被另一个源刚设上）
                if time.time() - self._group_cooldown.get(group_id, 0) < self.group_cooldown_seconds:
                    self.logger.info("[MyRSS] group %s cooldown set by another source, skip", group_id)
                    continue
                try:
                    pn = group_id.split(":")[0]
                    ret = None

                    try:
                        if pn == "aiocqhttp" and self.compose:
                            node = Comp.Node(uin=0, name="Astrbot", content=push_comps)
                            ret = await self.ctx.send_message(group_id, MessageChain(chain=[node], use_t2i_=self.t2i))
                        else:
                            ret = await self.ctx.send_message(group_id, MessageChain(chain=push_comps, use_t2i_=self.t2i))
                    except Exception:
                        ret = False

                    # ctx.send_message 失败 → 用 aiocqhttp 底层 API 直连
                    if (ret is None or ret is False) and pn == "aiocqhttp" and self._aiocqhttp_bot:
                        try:
                            gid_num = int(group_id.split(":")[-1])
                            segments = []
                            for comp in push_comps:
                                if hasattr(comp, 'file') and comp.file and comp.file.startswith("base64://"):
                                    segments.append({"type": "image", "data": {"file": comp.file}})
                                elif hasattr(comp, 'text'):
                                    segments.append({"type": "text", "data": {"text": comp.text}})

                            if self.compose:
                                forward_node = {
                                    "type": "node",
                                    "data": {"uin": "0", "name": "Astrbot", "content": segments}
                                }
                                await self._aiocqhttp_bot.send_group_forward_msg(
                                    group_id=gid_num, messages=[forward_node]
                                )
                            else:
                                await self._aiocqhttp_bot.send_group_msg(
                                    group_id=gid_num, message=segments
                                )
                            ret = True
                            self.logger.info("[MyRSS] push ok (direct API) to %s", group_id)
                        except Exception as e2:
                            self.logger.error("[MyRSS] direct API push also failed to %s: %s", group_id, e2)
                            ret = False

                    if ret is not None and ret is not False:
                        self._group_cooldown[group_id] = time.time()
                        if ret is not True:
                            self.logger.info("[MyRSS] push ok to %s", group_id)
                    else:
                        self.logger.warning("[MyRSS] push to %s failed all methods", group_id)

                    delay = random.uniform(self.push_delay_min, self.push_delay_max)
                    await asyncio.sleep(delay)

                except Exception as e:
                    self.logger.error("[MyRSS] global push failed to %s: %s", group_id, e)
        # 有新内容推送了，重置退避
        self._feed_miss_count[url] = 0
        self._feed_tick[url] = 0
        self._reset_activity()
        self.logger.info("[MyRSS] global push done")
    async def _get_provider_id(self) -> str:
        """获取锐评用的provider ID"""
        if self.comment_provider_id:
            return self.comment_provider_id
        # 自动获取默认provider
        try:
            cfg = self.ctx.get_config()
            default_id = cfg.get("provider_settings", {}).get("default_provider_id", "")
            if default_id:
                return default_id
        except Exception:
            pass
        return ""

    async def _generate_comment(self, item: RSSItem) -> str:
        """调用LLM生成锐评，带缓存"""
        norm_link = item.link.split("#", 1)[0].split("?", 1)[0] if item.link else ""
        cache_key = norm_link or (item.title + "|" + str(item.pubDate_timestamp))

        # 命中缓存直接返回
        if cache_key in self._comment_cache:
            return self._comment_cache[cache_key]

        provider_id = self.comment_provider_id if self.comment_provider_id else await self._get_provider_id()
        if not provider_id:
            self.logger.warning("[MyRSS] no provider for comment")
            return ""

        # 构造prompt
        content_summary = item.title
        if item.description:
            desc_short = item.description[:200]
            content_summary += "\n" + desc_short

        # 获取人格设定（v4 正统：PersonaManager）
        system_prompt = None
        if self.comment_persona:
            try:
                persona = self.ctx.persona_manager.get_persona(self.comment_persona)
                if persona:
                    system_prompt = persona.system_prompt
            except Exception:
                system_prompt = None

        prompt = (
            f"你正在看一条来自「{item.chan_title}」的动态更新，内容如下：\n"
            f"---\n{content_summary}\n---\n"
            f"请用你的人设风格，对这条动态写一句简短锐评（{self.comment_max_length}字以内）。"
            f"要求：自然、有个性、可以吐槽或夸奖。不要加引号。如果是推特消息和外语，尽可能通俗易懂的转为中文并简单讲讲发生了什么。"
        )

        try:
            resp = await self.ctx.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=system_prompt,
            )
            comment = (resp.completion_text or "").strip()
            # 截断
            if len(comment) > self.comment_max_length:
                comment = comment[:self.comment_max_length] + "..."
            # 过滤锐评本身
            if self.content_filter and comment:
                unsafe_words = ["习近平", "共产党", "六四", "天安门", "法轮", "台独",
                               "藏独", "疆独", "反共", "颠覆", "推翻", "操你", "傻逼"]
                for w in unsafe_words:
                    if w in comment:
                        self.logger.warning("[MyRSS] comment contains unsafe word '%s', discarding", w)
                        comment = ""
                        break
            # 缓存
            if comment:
                self._comment_cache[cache_key] = comment
                # 限制缓存大小
                if len(self._comment_cache) > 500:
                    keys = list(self._comment_cache.keys())
                    for k in keys[:200]:
                        del self._comment_cache[k]
            return comment
        except Exception as e:
            self.logger.error("[MyRSS] comment generation failed: %s", e)
            return ""

    async def _check_content_safe(self, item: RSSItem) -> bool:
        """检查内容是否安全，不安全返回False"""
        if not self.content_filter:
            return True
        norm_link = item.link.split("#", 1)[0].split("?", 1)[0] if item.link else ""
        cache_key = norm_link or (item.title + "|" + str(item.pubDate_timestamp))
        if cache_key in self._safe_cache:
            return self._safe_cache[cache_key]
        # 硬编码关键词兜底（不依赖LLM）
        check_text = (item.title + " " + (item.description or "")).lower()
        unsafe_words = ["习近平", "共产党", "六四", "天安门", "法轮", "台独",
                       "藏独", "疆独", "反共", "颠覆", "推翻政权", "轮子功"]
        for w in unsafe_words:
            if w in check_text:
                self.logger.warning("[MyRSS] content hard-filter hit '%s': %s", w, item.title[:30])
                self._safe_cache[cache_key] = False
                return False
        provider_id = self.filter_provider_id if self.filter_provider_id else await self._get_provider_id()
        if not provider_id:
            self._safe_cache[cache_key] = True
            return True  # 没有provider就不过滤，放行

        content = (item.title + " " + (item.description or ""))[:300]

        prompt = (
            "你是内容安全审核员。判断以下内容是否包含明确的不当信息。\n"
            "只有以下情况才算 UNSAFE：\n"
            "1. 明确攻击中国政府/领导人的反动言论（科技/商业/国际新闻不算）\n"
            "2. 血腥暴力的详细描写\n"
            "3. 露骨色情内容\n"
            "4. 明确的违法犯罪教唆\n\n"
            "注意：正常的科技新闻、商业动态、AI讨论、国际时事报道都是 SAFE。\n"
            "宁可放行也不要误杀正常内容。如果不确定，判定为 SAFE。\n\n"
            f"内容：{content}\n\n"
            "只回答 SAFE 或 UNSAFE，不要解释。"
        )

        try:
            resp = await self.ctx.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            result = (resp.completion_text or "").strip().upper()
            if "UNSAFE" in result:
                self.logger.warning("[MyRSS] content filtered: %s", item.title[:50])
                self._safe_cache[cache_key] = False
                return False
            self._safe_cache[cache_key] = True
            return True
        except Exception as e:
            self.logger.error("[MyRSS] content filter failed, blocking for safety: %s", e)
            self._safe_cache[cache_key] = False
            return False  # 过滤出错时拦截，宁可不推也不冒险
    def _get_avatar_url(self, item: RSSItem) -> str:
        """从存储的订阅数据里获取频道头像URL"""
        for url, info in self.dh.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            if info.get("info", {}).get("title") == item.chan_title:
                return info.get("info", {}).get("avatar", "")
        return ""
    async def _make_card_b64(self, item: RSSItem) -> str:
        # 下载频道头像
        avt_data = None
        if item.chan_title and item.chan_title != "未知":
            avt_url = self._get_avatar_url(item)
            if avt_url:
                try:
                    conn = aiohttp.TCPConnector(ssl=False)
                    async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                        async with s.get(avt_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                            if r.status == 200:
                                avt_data = await r.read()
                except Exception:
                    pass
        tb = None
        if self.read_pic and item.pic_urls:
            # [修改] 遍历图片列表尝试下载，直到成功一个
            # 解决YouTube封面可能是404的问题
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                for pu in item.pic_urls:
                    try:
                        async with s.get(pu, timeout=aiohttp.ClientTimeout(total=5)) as r:
                            if r.status == 200:
                                data = await r.read()
                                # 简单校验数据长度，防止下载到空的
                                if len(data) > 100: 
                                    tb = data
                                    break
                    except Exception:
                        continue
        # 生成锐评
        comment = ""
        bot_avt = None
        if self.enable_comment:
            comment = await self._generate_comment(item)

            # 下载bot头像
            if self.bot_qq and comment:
                bot_avt_url = f"https://q1.qlogo.cn/g?b=qq&nk={self.bot_qq}&s=640"
                try:
                    conn3 = aiohttp.TCPConnector(ssl=False)
                    async with aiohttp.ClientSession(trust_env=True, connector=conn3) as s3:
                        async with s3.get(bot_avt_url, timeout=aiohttp.ClientTimeout(total=5)) as r3:
                            if r3.status == 200:
                                bot_avt = await r3.read()
                except Exception:
                    pass

        return await self.card.make(
            channel=item.chan_title,
            title=item.title,
            desc=item.description,
            link="" if self.hide_url else item.link,
            ts=item.pubDate or "",
            thumb=tb,
            avatar=avt_data,
            comment=comment,
            bot_avatar=bot_avt,
            bot_provider_name=self.bot_provider_name,
        )

    def _merge_cards_b64(self, cards_b64: list) -> str:
        imgs = []
        for b64 in cards_b64:
            raw = base64.b64decode(b64)
            imgs.append(Image.open(BytesIO(raw)).convert("RGB"))

        if not imgs:
            return ""

        width = max(im.width for im in imgs)
        # [修改] 间距设为0，让每条卡片底部自带的分割线直接充当
        # 条目之间的分隔，拼出来就像推特时间线一样无缝衔接
        pad = 0
        resized = []
        total_h = 0
        for im in imgs:
            if im.width != width:
                nh = int(im.height * (width / im.width))
                im = im.resize((width, nh), Image.LANCZOS)
            resized.append(im)
            total_h += im.height

        # [修改] 白底画布，间距为0紧密拼接
        canvas = Image.new("RGB", (width, total_h), (255, 255, 255))
        y = 0
        for im in resized:
            canvas.paste(im, (0, y))
            y += im.height

        out = BytesIO()
        canvas.save(out, format="PNG")
        out.seek(0)
        return base64.b64encode(out.read()).decode("utf-8")

    async def _make_comps(self, item: RSSItem) -> list:
        comps = []
        tb = None
        if self.read_pic and item.pic_urls:
            conn = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                for pu in item.pic_urls:
                    try:
                        async with s.get(pu, timeout=aiohttp.ClientTimeout(total=5)) as r:
                            if r.status == 200:
                                data = await r.read()
                                if len(data) > 100:
                                    tb = data
                                    break
                    except Exception:
                        continue
        # 下载频道头像
        avt_data = None
        avt_url = self._get_avatar_url(item)
        if avt_url:
            try:
                conn2 = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(trust_env=True, connector=conn2) as s2:
                    async with s2.get(avt_url, timeout=aiohttp.ClientTimeout(total=5)) as r2:
                        if r2.status == 200:
                            avt_data = await r2.read()
            except Exception:
                pass
        # 生成锐评
        comment = ""
        bot_avt = None
        if self.enable_comment:
            comment = await self._generate_comment(item)

            if self.bot_qq and comment:
                bot_avt_url = f"https://q1.qlogo.cn/g?b=qq&nk={self.bot_qq}&s=640"
                try:
                    conn3 = aiohttp.TCPConnector(ssl=False)
                    async with aiohttp.ClientSession(trust_env=True, connector=conn3) as s3:
                        async with s3.get(bot_avt_url, timeout=aiohttp.ClientTimeout(total=5)) as r3:
                            if r3.status == 200:
                                bot_avt = await r3.read()
                except Exception:
                    pass

        try:
            b64 = await self.card.make(
                channel=item.chan_title, title=item.title, desc=item.description,
                link="" if self.hide_url else item.link, ts=item.pubDate or "", thumb=tb,
                avatar=avt_data,
                comment=comment,
                bot_avatar=bot_avt,
                bot_provider_name=self.bot_provider_name,
            )
            if b64:
                comps.append(Comp.Image.fromBase64(b64))
            else:
                comps.append(Comp.Plain("📡 " + item.chan_title + "\n📝 " + item.title + "\n" + item.description))
        except Exception as e:
            self.logger.error("卡片生成失败: %s", e)
            comps.append(Comp.Plain("📡 " + item.chan_title + "\n📝 " + item.title + "\n" + item.description))

        if self.read_pic and item.pic_urls:
            mx = len(item.pic_urls) if self.max_pic == -1 else self.max_pic
            for pu in item.pic_urls[1:mx]:
                try:
                    b = await self.pic.to_base64(pu)
                    if b:
                        comps.append(Comp.Image.fromBase64(b))
                except Exception:
                    pass
        return comps

    async def _cron_cb_url(self, url: str) -> None:
        """每个URL只拉取一次，结果分发给所有订阅者"""
        # [诊断] 打印实例ID和调度器ID，如果日志里同一url出现两个不同的id就是双实例并行
        self.logger.info("RSS拉取开始: instance=%s sched=%s url=%s", id(self), id(self.sched), url)
        if url not in self.dh.data:
            return
        subs = self.dh.data[url].get("subscribers", {})
        if not subs:
            return

        self.logger.info("RSS公共拉取: %s -> %d个订阅者", url, len(subs))

        # 所有订阅者中最早的 last_update（拉最多内容，再各自过滤）
        min_ts = min(si.get("last_update", 0) for si in subs.values())
        min_link = ""  # 公共拉取不用after_link过滤，靠seen_links去重

        items = await self._poll(url, num=self.max_poll, after_ts=min_ts, after_link=min_link)
        if not items:
            return

        # 分发给每个订阅者（各自独立去重）
        for i, user in enumerate(list(subs.keys())):
            lock = self._get_lock(url, user)
            async with lock:
                await self._cron_cb_inner(url, user, prefetched_items=items)
            # 多个订阅者间随机延迟防风控
            if i < len(subs) - 1:
                delay = random.uniform(self.push_delay_min, self.push_delay_max)
                await asyncio.sleep(delay)

    async def _cron_cb(self, url: str, user: str) -> None:
        """带锁的定时回调入口，防止同一订阅并发执行"""
        lock = self._get_lock(url, user)
        async with lock:
            await self._cron_cb_inner(url, user)

    async def _cron_cb_inner(self, url: str, user: str, prefetched_items=None) -> None:
        await self._cron_cb_inner_impl(url, user, prefetched_items)

    async def _cron_cb_inner_impl(self, url: str, user: str, prefetched_items=None) -> None:
        async with self._data_lock:
            self.dh.data = self.dh._load()

        if url not in self.dh.data or user not in self.dh.data[url].get("subscribers", {}):
            return

        self.logger.info("RSS定时触发: %s -> %s", url, user)
        si = self.dh.data[url]["subscribers"][user]

        if prefetched_items is not None:
            # 使用公共拉取的结果，再按该用户的断点过滤一次
            items = [
                it for it in prefetched_items
                if it.pubDate_timestamp > si.get("last_update", 0)
                or (it.pubDate_timestamp == 0 and it.link != si.get("latest_link", ""))
            ]
        else:
            items = await self._poll(
                url,
                num=self.max_poll,
                after_ts=si["last_update"],
                after_link=si["latest_link"],
            )
        if not items:
            return

        def item_key(it: RSSItem) -> str:
            if it.link:
                return it.link.split("#", 1)[0].split("?", 1)[0]
            return f"{it.title}|{it.pubDate_timestamp}"

        # 去重
        seen = set(si.get("seen_links", []))
        new_items = [it for it in items if item_key(it) not in seen]

        if not new_items:
            si["latest_link"] = items[0].link
            self.dh.save()
            return

        # 先更新去重记录再发送，防止并发重推
        new_keys = [item_key(it) for it in new_items]
        si["seen_links"] = (new_keys + si.get("seen_links", []))[:200]
        si["latest_link"] = items[0].link
        ts_candidates = [it.pubDate_timestamp for it in new_items if it.pubDate_timestamp > 0]
        if ts_candidates:
            si["last_update"] = max(ts_candidates)
        self.dh.save()
        # 内容过滤（去重后、推送前）
        if self.content_filter:
            filtered = []
            for it in new_items:
                if await self._check_content_safe(it):
                    filtered.append(it)
                else:
                    self.logger.info("[MyRSS] filtered: %s", it.title[:30])
            new_items = filtered
            if not new_items:
                return
        pn = user.split(":")[0]
        merge_limit = 5
        batch = new_items[:merge_limit]

        if len(batch) > 1:
            cards_raw = [await self._make_card_b64(it) for it in batch]
            cards = [c for c in cards_raw if c]  # 过滤掉被内容审核拦截的空卡片
            if not cards:
                self.logger.info("[MyRSS] all items filtered, skip push")
                return
            merged = self._merge_cards_b64(cards)
            if not merged:
                for it in batch:
                    comps = await self._make_comps(it)
                    await self.ctx.send_message(user, MessageChain(chain=comps, use_t2i_=self.t2i))
            else:
                comps = [Comp.Image.fromBase64(merged)]
                if pn == "aiocqhttp" and self.compose:
                    node = Comp.Node(uin=0, name="Astrbot", content=comps)
                    await self.ctx.send_message(user, MessageChain(chain=[node], use_t2i_=self.t2i))
                else:
                    await self.ctx.send_message(user, MessageChain(chain=comps, use_t2i_=self.t2i))
        else:
            it = batch[0]
            comps = await self._make_comps(it)
            if pn == "aiocqhttp" and self.compose:
                node = Comp.Node(uin=0, name="Astrbot", content=comps)
                await self.ctx.send_message(user, MessageChain(chain=[node], use_t2i_=self.t2i))
            else:
                await self.ctx.send_message(user, MessageChain(chain=comps, use_t2i_=self.t2i))

        self.logger.info("RSS推送完成: %s -> %s (%d条)", url, user, len(batch))

    # ============================================================
    #  LLM 工具
    # ============================================================

    @filter.llm_tool(name="myrss_subscribe")
    async def tool_sub(self, event: AstrMessageEvent, url: str = "https://example.com", interval: int = 15, target_group: str = ""):
        """用户想订阅某个网站/博主更新时调用。

        Args:
            url(string): 链接或路由路径
            interval(int): 间隔分钟数，默认15
            target_group(string): 指定推送到的群号(可选，不填则推到当前会话)
        """
        if not url or url == "https://example.com":
            yield event.plain_result(
                "需要用户提供链接或路由。支持平台：B站、YouTube、Twitter/X、微博、知乎等。\n"
                "路由示例：/youtube/community/@用户名、/twitter/user/用户名、/bilibili/user/dynamic/UID\n"
                "可选参数：interval(分钟)、target_group(指定群号)\n"
                "详见 https://docs.rsshub.app"
            )
            return
        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            yield event.plain_result(
                "尚未配置RSSHub端点，请告诉用户执行以下命令之一：\n"
                "/myrss rsshub add https://rsshub.rssforever.com\n"
                "/myrss rsshub add https://rsshub.app\n"
                "配置后即可订阅。"
            )
            return
        if url.startswith("/"):
            furl = eps[0] + url
        elif url.startswith("http"):
            r = URLMapper.match(url)
            if r:
                route, pn = r
                furl = eps[0] + route
            else:
                yield event.plain_result("无法自动识别该链接。\n\n" + URLMapper.suggest(url) + "\n\n请选择路由后用/开头再次调用。")
                return
        else:
            yield event.plain_result("请提供http开头的链接或/开头的路由。")
            return
        if interval < 15:
            interval = 15

        # 如果已有订阅者，间隔只能取更大值（保护公共源）
        if furl in self.dh.data:
            existing_subs = self.dh.data[furl].get("subscribers", {})
            if existing_subs:
                def cron_to_minutes(expr: str) -> int:
                    try:
                        f = expr.split(" ")
                        # */15 * * * *
                        if f[0].startswith("*/"):
                            return int(f[0][2:])
                        # 0 */1 * * *
                        if f[1].startswith("*/"):
                            return int(f[1][2:]) * 60
                        return 60
                    except Exception:
                        return 60

                max_existing = max(cron_to_minutes(si["cron_expr"]) for si in existing_subs.values())
                if interval < max_existing:
                    interval = max_existing
                    yield event.plain_result(f"⚠️ 已有订阅者使用{max_existing}分钟间隔，为保护公共源已自动调整为{max_existing}分钟。")

        # 分钟制cron
        if interval < 60:
            cron_expr = f"*/{interval} * * * *"
        else:
            cron_expr = f"0 */{interval // 60} * * *"
        unit = "分钟" if interval < 60 else "小时"
        show_interval = interval if interval < 60 else interval // 60
        # 如果指定了目标群
        if target_group:
            # 构造目标群的unified_msg_origin
            pn = event.unified_msg_origin.split(":")[0]
            target_umo = f"{pn}:GroupMessage:{target_group}"
            # 临时替换event的origin
            original_umo = event.unified_msg_origin
            event._unified_msg_origin = target_umo
            ret = await self._add(furl, cron_expr, event)
            event._unified_msg_origin = original_umo
            if isinstance(ret, MessageEventResult):
                yield ret
                return
            self._reload_jobs()
            yield event.plain_result(
                "✅ 订阅成功！\n📡 " + ret["title"] +
                "\n⏰ 每" + str(show_interval) + unit +
                "\n📍 推送到群 " + target_group +
                "\n🔗 " + furl
            )
            return
        ret = await self._add(furl, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        self._reload_jobs()
        yield event.plain_result("✅ 订阅成功！\n📡 " + ret["title"] + "\n📝 " + ret["description"] + "\n⏰ 每" + str(show_interval) + unit + "\n🔗 " + furl)

    @filter.llm_tool(name="myrss_list")
    async def tool_list(self, event: AstrMessageEvent, query: str = "all"):
        """用户问订阅了什么时调用。
    
        Args:
            query(string): 固定传all
        """
        user = event.unified_msg_origin
        urls = self.dh.get_subs(user)
        if not urls:
            yield event.plain_result("当前没有任何订阅。")
            return
        txt = "📋 订阅列表：\n"
        for i, u in enumerate(urls):
            info = self.dh.data[u]["info"]
            cr = self.dh.data[u]["subscribers"][user]["cron_expr"]
            txt += "  " + str(i) + ". " + info["title"] + " [" + cr + "]\n"
        yield event.plain_result(txt)
    @filter.llm_tool(name="myrss_block_feed")
    async def tool_block(self, event: AstrMessageEvent, feed_keyword: str = ""):
        """当群友说不想看某个订阅源的推送时调用（如"别发推特了""不要马斯克的"）。

        Args:
            feed_keyword(string): 要屏蔽的源关键词（如elonmusk、flag__chan、bilibili等）
        """
        if not feed_keyword:
            yield event.plain_result(
                "请告诉我要屏蔽哪个源。当前全局订阅源：\n" +
                "\n".join(f"  {i}. {r}" for i, r in enumerate(self.global_feeds)) +
                "\n回复关键词即可屏蔽，如 'elonmusk' 或 'bilibili'"
            )
            return

        group_id = event.unified_msg_origin
        if "GroupMessage" not in group_id:
            yield event.plain_result("此功能仅在群聊中可用。")
            return

        # 模糊匹配
        matched = []
        for route in self.global_feeds:
            if feed_keyword.lower() in route.lower():
                matched.append(route)

        if not matched:
            yield event.plain_result(
                f"没找到包含 '{feed_keyword}' 的订阅源。当前全局源：\n" +
                "\n".join(f"  {r}" for r in self.global_feeds)
            )
            return

        blocked = self._group_data.setdefault("blocked_feeds", {})
        group_blocked = blocked.setdefault(group_id, [])

        newly_blocked = []
        for route in matched:
            if route not in group_blocked:
                group_blocked.append(route)
                newly_blocked.append(route)

        if not newly_blocked:
            yield event.plain_result("这些源在本群已经屏蔽了：\n" + "\n".join(matched))
            return

        self._save_group_data()
        yield event.plain_result(
            "已在本群屏蔽以下推送：\n" +
            "\n".join(f"  ✅ {r}" for r in newly_blocked) +
            "\n其他群不受影响。如需恢复，说'恢复推送xxx'即可。"
        )

    @filter.llm_tool(name="myrss_unblock_feed")
    async def tool_unblock(self, event: AstrMessageEvent, feed_keyword: str = ""):
        """当群友说想恢复某个被屏蔽的推送时调用。

        Args:
            feed_keyword(string): 要恢复的源关键词
        """
        group_id = event.unified_msg_origin
        if "GroupMessage" not in group_id:
            yield event.plain_result("此功能仅在群聊中可用。")
            return

        blocked = self._group_data.get("blocked_feeds", {})
        group_blocked = blocked.get(group_id, [])

        if not group_blocked:
            yield event.plain_result("本群没有屏蔽任何全局推送源。")
            return

        if not feed_keyword:
            yield event.plain_result(
                "本群当前屏蔽的源：\n" +
                "\n".join(f"  {i}. {r}" for i, r in enumerate(group_blocked)) +
                "\n告诉我要恢复哪个即可。"
            )
            return

        matched = [r for r in group_blocked if feed_keyword.lower() in r.lower()]
        if not matched:
            yield event.plain_result(f"没找到包含 '{feed_keyword}' 的已屏蔽源。")
            return

        for r in matched:
            group_blocked.remove(r)
        self._save_group_data()

        yield event.plain_result(
            "已在本群恢复以下推送：\n" +
            "\n".join(f"  ✅ {r}" for r in matched)
        )
    @filter.llm_tool(name="myrss_preview")
    async def tool_preview(self, event: AstrMessageEvent, url: str = ""):
        """用户想查看/搜索某个频道的信息时调用。生成频道预览卡片。
        
        常用路由格式（直接填到url参数里）：
        - 推特/X: /twitter/user/用户名  （如 /twitter/user/Google）
        - B站: /bilibili/user/dynamic/UID
        - YouTube: /youtube/user/@用户名
        - 也可以传完整链接如 https://x.com/Google

        Args:
            url(string): 频道链接或RSSHub路由，如 /twitter/user/Google 或 https://x.com/Google
        """
        if not url:
            yield event.plain_result(
                "请提供频道链接或路由。例如：\n"
                "  推特: https://x.com/用户名\n"
                "  B站: https://space.bilibili.com/UID\n"
                "  YouTube: https://youtube.com/@用户名\n"
                "  或直接用路由: /twitter/user/用户名"
            )
            return

        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            yield event.plain_result("未配置RSSHub端点，请先 /myrss rsshub add <url>")
            return

        if url.startswith("http"):
            matched = URLMapper.match(url)
            if matched:
                route, platform = matched
                yield event.plain_result(f"🔄 识别为 {platform}，路由: {route}")
            else:
                yield event.plain_result("无法识别该链接。\n\n" + URLMapper.suggest(url))
                return
        elif url.startswith("/"):
            route = url
        else:
            route = "/" + url

        full_url = eps[0].rstrip("/") + route
        yield event.plain_result(f"📡 正在获取频道信息...")

        text = None
        for _attempt in range(3):
            raw = await self._fetch(full_url)
            if raw and b'<item>' in raw[:10000]:
                text = raw
                break
            await asyncio.sleep(3)
        if not text:
            yield event.plain_result("❌ 无法访问该源（已重试3次），请稍后再试。")
            return

        try:
            title, desc, avatar_url = self.dh.parse_channel_info(text)
        except Exception as e:
            yield event.plain_result(f"❌ 解析失败: {e}")
            return

        items = await self._poll(full_url, num=3)
        previews = []
        for it in items:
            previews.append({
                "title": it.title,
                "time": self.card._format_time(it.pubDate) if it.pubDate else "",
            })

        avt_data = None
        if avatar_url:
            try:
                conn = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                    async with s.get(avatar_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            avt_data = await r.read()
            except Exception:
                pass

        rec_id = f"R{int(time.time()) % 100000:05d}"
        b64 = await self.card.make_rec_card(
            title=title, description=desc, avatar=avt_data,
            route=route, previews=previews, rec_id=rec_id,
        )

        if b64:
            self._last_preview = {
                "route": route, "url": full_url, "title": title,
                "description": desc, "avatar_url": avatar_url, "rec_id": rec_id,
            }
            comps = [Comp.Image.fromBase64(b64)]
            pn = event.unified_msg_origin.split(":")[0]
            if pn == "aiocqhttp" and self.compose:
                yield event.chain_result([Comp.Node(uin=0, name="频道预览", content=comps)])
            else:
                yield event.chain_result(comps)
            yield event.plain_result(
                f"📡 {title}\n📝 {desc[:100]}\n🔗 {route}\n\n"
                f"如需推荐到群，请说「推荐到群XXX」（群号用逗号分隔）"
            )
        else:
            yield event.plain_result(f"📡 {title}\n📝 {desc[:100]}\n🔗 {route}\n\n（卡片生成失败，但信息已获取）")

    @filter.llm_tool(name="myrss_recommend")
    async def tool_recommend(self, event: AstrMessageEvent, route: str = "", group_ids: str = "", interval: int = 30):
        """用户说"推荐到群""发到群""推到群"时调用此工具（不是preview）。把上次预览的频道推荐到指定群，群友投票同意后自动订阅。
        route参数可以留空，会自动使用上次预览的频道。

        Args:
            route(string): RSSHub路由，留空则用上次预览的。如 /twitter/user/hachi_08
            group_ids(string): 目标群号，逗号分隔，如 "721058477,123456"。传"all"推到所有群
            interval(int): 订阅间隔分钟数，默认30
        """
        if not route:
            if hasattr(self, '_last_preview') and self._last_preview:
                route = self._last_preview["route"]
                yield event.plain_result(f"📡 使用上次预览的频道: {route}")
            else:
                yield event.plain_result("请先预览一个频道，或直接提供路由。")
                return

        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            yield event.plain_result("未配置RSSHub端点。")
            return

        if not route.startswith("/"):
            matched = URLMapper.match(route)
            if matched:
                route = matched[0]
            else:
                route = "/" + route

        full_url = eps[0].rstrip("/") + route

        if not group_ids:
            yield event.plain_result("请指定目标群号（逗号分隔），或说「all」推到所有群。\n可以先用 /myrss groups 查看群列表。")
            return

        pn = event.unified_msg_origin.split(":")[0]

        if group_ids.strip().lower() == "all":
            target_groups = self._get_active_groups()
            if not target_groups:
                yield event.plain_result("没有活跃群。需要群里有人说过话。")
                return
        else:
            gids = [g.strip() for g in re.split(r'[,，\s]+', group_ids) if g.strip()]
            target_groups = [f"{pn}:GroupMessage:{gid}" for gid in gids]

        if not target_groups:
            yield event.plain_result("没有有效的目标群。")
            return

        yield event.plain_result(f"📡 正在准备推荐卡片...")

        text = await self._fetch(full_url)
        title, desc, avatar_url = "未知", "", ""
        if text:
            try:
                title, desc, avatar_url = self.dh.parse_channel_info(text)
            except Exception:
                pass

        if hasattr(self, '_last_preview') and self._last_preview and self._last_preview.get("route") == route:
            lp = self._last_preview
            title = lp.get("title", title)
            desc = lp.get("description", desc)
            avatar_url = lp.get("avatar_url", avatar_url)

        items = await self._poll(full_url, num=3)
        previews = [{"title": it.title, "time": self.card._format_time(it.pubDate) if it.pubDate else ""} for it in items]

        avt_data = None
        if avatar_url:
            try:
                conn = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                    async with s.get(avatar_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            avt_data = await r.read()
            except Exception:
                pass

        rec_id = f"R{int(time.time()) % 100000:05d}"

        b64 = await self.card.make_rec_card(
            title=title, description=desc, avatar=avt_data,
            route=route, previews=previews, rec_id=rec_id,
        )

        if not b64:
            yield event.plain_result("❌ 推荐卡片生成失败。")
            return

        groups_state = {}
        for gid in target_groups:
            groups_state[gid] = {"agrees": [], "rejects": [], "status": "pending"}

        self._pending_recs[rec_id] = {
            "route": route, "url": full_url, "title": title,
            "description": desc, "avatar_url": avatar_url,
            "recommender": event.unified_msg_origin,
            "groups": groups_state,
            "interval": max(interval, 15),
            "created_at": time.time(),
        }
        self._save_recs()

        card_comps = [
            Comp.Image.fromBase64(b64),
            Comp.Plain(f"\n📢 有人推荐订阅「{title}」\n回复「同意」订阅 / 回复「拒绝」取消\n（1人回复即生效，1小时无人回复自动订阅）"),
        ]

        sent_count = 0
        fail_count = 0
        for gid in target_groups:
            try:
                gpn = gid.split(":")[0]
                if gpn == "aiocqhttp" and self.compose:
                    node = Comp.Node(uin=0, name="频道推荐", content=card_comps)
                    ret = await self.ctx.send_message(gid, MessageChain(chain=[node]))
                else:
                    ret = await self.ctx.send_message(gid, MessageChain(chain=card_comps))

                if ret is not None and ret is not False:
                    sent_count += 1
                else:
                    fail_count += 1

                await asyncio.sleep(random.uniform(2, 4))
            except Exception as e:
                fail_count += 1
                self.logger.error("[MyRSS] recommend send failed to %s: %s", gid, e)

        yield event.plain_result(
            f"✅ 推荐已发送！\n"
            f"  📡 频道: {title}\n"
            f"  🔗 路由: {route}\n"
            f"  📮 成功: {sent_count}群 / 失败: {fail_count}群\n"
            f"  🆔 编号: {rec_id}\n"
            f"  ⏰ 通过后订阅间隔: {max(interval, 15)}分钟\n\n"
            f"群友回复「同意」或「拒绝」即可，1小时无人回复自动订阅"
        )
    @filter.llm_tool(name="myrss_unsubscribe")
    async def tool_unsub(self, event: AstrMessageEvent, idx: int = 0, idxs: str = ""):
        """取消订阅（支持多选/清空）。

        Args:
            idx(int): 单个编号（兼容旧用法）
            idxs(string): 多个编号，如 "0、2、4" / "0,2,4" / "0 2 4"
                         或 "all"/"清空"/"全部"
        """
        user = event.unified_msg_origin
        urls = self.dh.get_subs(user)
        if not urls:
            yield event.plain_result("当前没有任何订阅。")
            return

        # 解析要删除的编号列表
        to_remove = []

        if idxs and str(idxs).strip():
            s = str(idxs).strip().lower()
            if s in ("all", "清空", "全部"):
                to_remove = list(range(len(urls)))
            else:
                nums = [int(x) for x in re.findall(r"\d+", s)]
                to_remove = sorted(set(n for n in nums if 0 <= n < len(urls)))
                if not to_remove:
                    yield event.plain_result("没解析到有效编号。示例：0、2、4 或 all/清空/全部")
                    return
        else:
            # 兼容旧逻辑：只删一个 idx
            if idx < 0 or idx >= len(urls):
                yield event.plain_result("编号" + str(idx) + "不存在，有效范围0~" + str(len(urls) - 1))
                return
            to_remove = [idx]

        removed_titles = []
        # 注意：这里不要边删边重新取urls；我们用同一份 urls 快照一次性删完
        for n in to_remove:
            u = urls[n]
            t = self.dh.data.get(u, {}).get("info", {}).get("title", u)
            try:
                self.dh.data[u]["subscribers"].pop(user, None)
                removed_titles.append(t)
            except Exception:
                pass

        self.dh.save()
        self._reload_jobs()

        if removed_titles:
            msg = "✅ 已取消以下订阅：\n" + "\n".join(f"  - {x}" for x in removed_titles)
            yield event.plain_result(msg)
        else:
            yield event.plain_result("没有取消任何订阅（可能已经被删过）。")
    # ============================================================
    #  手动命令
    # ============================================================

    @filter.command_group("myrss")
    def myrss(self):
        pass

    @myrss.group("rsshub")
    def rsshub(self, event: AstrMessageEvent):
        pass

    @rsshub.command("add")
    async def rsshub_add(self, event: AstrMessageEvent, url: str):
        """添加RSSHub端点"""
        if url.endswith("/"):
            url = url[:-1]
        if url in self.dh.data["rsshub_endpoints"]:
            yield event.plain_result("已存在")
            return
        self.dh.data["rsshub_endpoints"].append(url)
        self.dh.save()
        yield event.plain_result("✅ 已添加: " + url)

    @rsshub.command("list")
    async def rsshub_list(self, event: AstrMessageEvent):
        """列出所有RSSHub端点"""
        eps = self.dh.data["rsshub_endpoints"]
        if not eps:
            yield event.plain_result("暂无端点，请先 /myrss rsshub add <url>")
            return
        txt = "RSSHub端点：\n"
        for i, x in enumerate(eps):
            txt += "  " + str(i) + ": " + x + "\n"
        yield event.plain_result(txt)

    @rsshub.command("remove")
    async def rsshub_rm(self, event: AstrMessageEvent, idx: int):
        """删除RSSHub端点"""
        eps = self.dh.data["rsshub_endpoints"]
        if idx < 0 or idx >= len(eps):
            yield event.plain_result("编号越界")
            return
        removed = eps.pop(idx)
        self.dh.save()
        yield event.plain_result("✅ 已删除: " + removed)

    @myrss.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """列出当前订阅"""
        user = event.unified_msg_origin
        urls = self.dh.get_subs(user)
        if not urls:
            yield event.plain_result("暂无订阅")
            return
        txt = "订阅列表：\n"
        for i, u in enumerate(urls):
            info = self.dh.data[u]["info"]
            txt += "  " + str(i) + ". " + info["title"] + "\n"
        yield event.plain_result(txt)

    @myrss.command("remove")
    async def cmd_rm(self, event: AstrMessageEvent, idx: int):
        """取消订阅"""
        user = event.unified_msg_origin
        urls = self.dh.get_subs(user)
        if idx < 0 or idx >= len(urls):
            yield event.plain_result("编号越界")
            return
        u = urls[idx]
        t = self.dh.data[u]["info"]["title"]
        self.dh.data[u]["subscribers"].pop(user)
        self.dh.save()
        self._reload_jobs()
        yield event.plain_result("✅ 已取消: " + t)

    @myrss.command("get")
    async def cmd_get(self, event: AstrMessageEvent, idx: int):
        """获取最新内容"""
        user = event.unified_msg_origin
        urls = self.dh.get_subs(user)
        if idx < 0 or idx >= len(urls):
            yield event.plain_result("编号越界")
            return
        items = await self._poll(urls[idx])
        if not items:
            yield event.plain_result("暂无内容")
            return
        comps = await self._make_comps(items[0])
        pn = user.split(":")[0]
        if pn == "aiocqhttp" and self.compose:
            yield event.chain_result([Comp.Node(uin=0, name="Astrbot", content=comps)]).use_t2i(self.t2i)
        else:
            yield event.chain_result(comps).use_t2i(self.t2i)
    @myrss.command("clearcache")
    async def cmd_clearcache(self, event: AstrMessageEvent):
        """清空过滤缓存和锐评缓存"""
        safe_count = len(self._safe_cache)
        comment_count = len(self._comment_cache)
        self._safe_cache.clear()
        self._comment_cache.clear()
        yield event.plain_result(f"✅ 缓存已清空\n  过滤缓存: {safe_count} 条已清除\n  锐评缓存: {comment_count} 条已清除")
    @myrss.command("test")
    async def cmd_test(self, event: AstrMessageEvent, route: str = "/twitter/user/AnthropicAI"):
        """测试推送流程：拉取指定源的最新一条，走完整的过滤+锐评+缓存流程。
        用法：
          /myrss test                              （默认 Anthropic 推特）
          /myrss test /twitter/user/elonmusk       （RSSHub 路由）
          /myrss test https://x.com/elonmusk       （自动转路由）
          /myrss test https://space.bilibili.com/2267573/dynamic
        """
        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            yield event.plain_result("没有配置 RSSHub 端点，无法测试。")
            return

        # 支持传入完整URL，自动转成RSSHub路由
        if route.startswith("http"):
            matched = URLMapper.match(route)
            if matched:
                converted_route, platform_name = matched
                yield event.plain_result(f"🔄 识别为 {platform_name}，转换路由: {converted_route}")
                route = converted_route
            else:
                yield event.plain_result("❌ 无法识别该链接。\n\n" + URLMapper.suggest(route) + "\n\n请用 /开头的路由重试。")
                return

        if not route.startswith("/"):
            route = "/" + route

        url = eps[0].rstrip("/") + route
        yield event.plain_result(f"⏳ 开始测试推送流程...\n源: {route}\n10秒后拉取（模拟真实延迟）")

        await asyncio.sleep(10)

        # 第1步：拉取
        yield event.plain_result("📡 [1/4] 正在拉取 RSS...")
        # [test] 先抓一次频道信息，写入 dh.data，让 _poll() 能拿到 chan_title（否则显示“未知”）
        try:
            txt = await self._fetch(url)
            if txt:
                t, d, a = self.dh.parse_channel_info(txt)
                self.dh.data[url] = {
                    "info": {"title": t, "description": d, "avatar": a},
                    "subscribers": {},
                    "is_test": True,
                }
        except Exception:
            pass
        items = await self._poll(url, num=1)
        if not items:
            yield event.plain_result("❌ 拉取失败，源无内容或不可访问。")
            return
        item = items[0]
        # [Hack] 临时把测试源的信息注入 data，让 _make_card_b64 能查到头像/标题
        if url not in self.dh.data:
            # 尝试再 fetch 一次拿 channel info
            try:
                txt = await self._fetch(url)
                if txt:
                    t, d, a = self.dh.parse_channel_info(txt)
                    self.dh.data[url] = {
                        "info": {"title": t, "description": d, "avatar": a},
                        "subscribers": {},  # 空订阅
                        "is_test": True     # 标记为测试
                    }
            except Exception:
                pass
        yield event.plain_result(f"✅ 拉取成功: {item.title[:80]}")

        # 第2步：内容过滤（走真实函数，会用缓存）
        yield event.plain_result("🔍 [2/4] 正在过滤内容（LLM审核）...")
        norm_link = item.link.split("#", 1)[0].split("?", 1)[0] if item.link else ""
        cache_key = norm_link or (item.title + "|" + str(item.pubDate_timestamp))
        was_cached = cache_key in self._safe_cache
        safe = await self._check_content_safe(item)
        if not safe:
            yield event.plain_result(
                f"🚫 内容被过滤（判定不安全），不会推送。\n"
                f"  缓存命中: {was_cached}\n"
                f"  标题: {item.title[:60]}\n"
                f"  如果这是误杀，可能需要调整过滤 prompt 或换一个过滤 provider。\n"
                f"  提示: 可以临时关闭 content_filter 再测试，确认是过滤器问题还是其他问题。"
            )
            return
        yield event.plain_result(f"✅ 内容安全。缓存命中: {was_cached}")

        # 第3步：生成锐评（走真实函数，会用缓存）
        yield event.plain_result("💬 [3/4] 正在生成锐评（LLM评论）...")
        comment = ""
        if self.enable_comment:
            comment_was_cached = cache_key in self._comment_cache
            comment = await self._generate_comment(item)
            if comment:
                yield event.plain_result(f"✅ 锐评: {comment[:80]}\n  缓存命中: {comment_was_cached}")
            else:
                yield event.plain_result("⚠️ 锐评生成失败或为空")
        else:
            yield event.plain_result("⏭️ 锐评已关闭，跳过")

        # 第4步：生成卡片并发送（走真实函数）
        yield event.plain_result("🎨 [4/4] 正在生成卡片...")
        comps = await self._make_comps(item)

        user = event.unified_msg_origin
        pn = user.split(":")[0]
        if pn == "aiocqhttp" and self.compose:
            yield event.chain_result([Comp.Node(uin=0, name="[测试]Astrbot", content=comps)]).use_t2i(self.t2i)
        else:
            yield event.chain_result(comps).use_t2i(self.t2i)

        yield event.plain_result(
            "✅ 测试完成！\n"
            f"  过滤缓存大小: {len(self._safe_cache)}\n"
            f"  锐评缓存大小: {len(self._comment_cache)}\n"
            f"  安全模式: {'开启' if self.safe_mode else '关闭'}\n"
            f"  测试群: {','.join(self.safe_mode_groups) if self.safe_mode_groups else '未配置'}\n"
            "再次执行同样的命令可验证缓存是否命中（应显示 True）"
        )
    @myrss.command("groups")
    async def cmd_groups(self, event: AstrMessageEvent):
        """列出机器人加入的群（需要 aiocqhttp / NapCat）"""
        try:
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("当前平台不支持获取群列表（仅 aiocqhttp/NapCat 支持）。")
                return

            # AstrBot 官方文档要求的调用方式
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if not isinstance(event, AiocqhttpMessageEvent):
                yield event.plain_result("事件类型不匹配，无法调用协议端 API。")
                return

            client = event.bot
            if not client:
                yield event.plain_result("无法获取协议端 client。")
                return

            ret = await client.api.call_action('get_group_list')

            # NapCat 返回格式可能是 list 或 dict{"data": list}
            if isinstance(ret, list):
                data = ret
            elif isinstance(ret, dict):
                data = ret.get("data", [])
            else:
                data = []

            if not data:
                yield event.plain_result("群列表为空，或协议端未返回数据。\n返回值类型: " + str(type(ret).__name__))
                return

            lines = ["📋 机器人所在群列表："]
            for i, g in enumerate(data):
                if isinstance(g, dict):
                    gid = g.get("group_id", "")
                    gname = g.get("group_name", "")
                    lines.append(f"  {i}. {gname} ({gid})")
                else:
                    lines.append(f"  {i}. {g}")

            yield event.plain_result("\n".join(lines))
        except Exception as e:
            self.logger.error("[MyRSS] get group list failed: %s", e, exc_info=True)
            yield event.plain_result("获取群列表失败：" + str(e))
    @myrss.command("cooldown")
    async def cmd_cooldown(self, event: AstrMessageEvent):
        """查看各群的全局推送冷却状态"""
        if not self._group_cooldown:
            yield event.plain_result("当前没有任何群在冷却期内。")
            return
        now = time.time()
        lines = ["📋 全局推送冷却状态："]
        for gid, ts in sorted(self._group_cooldown.items(), key=lambda x: x[1], reverse=True):
            elapsed = now - ts
            remaining = self.group_cooldown_seconds - elapsed
            if remaining > 0:
                lines.append(f"  🔴 {gid} - 剩余 {int(remaining/60)} 分钟")
            else:
                lines.append(f"  🟢 {gid} - 已就绪")
        yield event.plain_result("\n".join(lines))
    @myrss.command("resetglobal")
    async def cmd_resetglobal(self, event: AstrMessageEvent):
        """重置全局订阅的已推送记录，下次检查时重新推送"""
        count = 0
        for url, info in self.dh.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            if info.get("global"):
                info["global_seen_links"] = []
                info["global_last_update"] = 0
                count += 1
        self.dh.save()
        self._feed_miss_count.clear()
        self._feed_tick.clear()
        self._group_cooldown.clear()
        yield event.plain_result(f"✅ 已重置 {count} 个全局源的推送记录和冷却\n下次检查（~5分钟内）将重新推送")
    @myrss.command("recommend")
    async def cmd_recommend(self, event: AstrMessageEvent, group_id: str = "", route: str = ""):
        """手动推荐上次预览的频道到指定群
        用法：/myrss recommend 721058477
              /myrss recommend 721058477 /twitter/user/hachi_08
        """
        if not group_id:
            yield event.plain_result("用法: /myrss recommend <群号> [路由]\n群号必填，路由不填则用上次预览的频道")
            return

        if not route:
            if self._last_preview:
                route = self._last_preview["route"]
            else:
                yield event.plain_result("没有上次预览的频道。请先预览一个频道或提供路由。")
                return

        eps = self.dh.data.get("rsshub_endpoints", [])
        if not eps:
            yield event.plain_result("未配置RSSHub端点。")
            return

        if not route.startswith("/"):
            route = "/" + route

        full_url = eps[0].rstrip("/") + route
        pn = event.unified_msg_origin.split(":")[0]
        gids = [g.strip() for g in re.split(r'[,，\s]+', group_id) if g.strip()]
        target_groups = [f"{pn}:GroupMessage:{gid}" for gid in gids]

        yield event.plain_result(f"📡 正在准备推荐卡片 {route} → {gids}...")

        text = await self._fetch(full_url)
        title, desc, avatar_url = "未知", "", ""
        if text:
            try:
                title, desc, avatar_url = self.dh.parse_channel_info(text)
            except Exception:
                pass

        if self._last_preview and self._last_preview.get("route") == route:
            title = self._last_preview.get("title", title)
            desc = self._last_preview.get("description", desc)
            avatar_url = self._last_preview.get("avatar_url", avatar_url)

        items = await self._poll(full_url, num=3)
        previews = [{"title": it.title, "time": self.card._format_time(it.pubDate) if it.pubDate else ""} for it in items]

        avt_data = None
        if avatar_url:
            try:
                conn = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(trust_env=True, connector=conn) as s:
                    async with s.get(avatar_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            avt_data = await r.read()
            except Exception:
                pass

        rec_id = f"R{int(time.time()) % 100000:05d}"
        b64 = await self.card.make_rec_card(
            title=title, description=desc, avatar=avt_data,
            route=route, previews=previews, rec_id=rec_id,
        )

        if not b64:
            yield event.plain_result("❌ 推荐卡片生成失败。")
            return

        groups_state = {}
        for gid in target_groups:
            groups_state[gid] = {"agrees": [], "rejects": [], "status": "pending"}

        self._pending_recs[rec_id] = {
            "route": route, "url": full_url, "title": title,
            "description": desc, "avatar_url": avatar_url,
            "recommender": event.unified_msg_origin,
            "groups": groups_state,
            "interval": 30,
            "created_at": time.time(),
        }
        self._save_recs()

        card_comps = [
            Comp.Image.fromBase64(b64),
            Comp.Plain(f"\n📢 有人推荐订阅「{title}」\n回复「同意」订阅 / 回复「拒绝」取消\n（1人回复即生效，1小时无人回复自动订阅）"),
        ]

        sent_count = 0
        for gid in target_groups:
            try:
                gpn = gid.split(":")[0]
                if gpn == "aiocqhttp" and self.compose:
                    node = Comp.Node(uin=0, name="频道推荐", content=card_comps)
                    ret = await self.ctx.send_message(gid, MessageChain(chain=[node]))
                else:
                    ret = await self.ctx.send_message(gid, MessageChain(chain=card_comps))
                if ret is not None and ret is not False:
                    sent_count += 1
                await asyncio.sleep(2)
            except Exception as e:
                self.logger.error("[MyRSS] recommend send failed: %s", e)

        yield event.plain_result(f"✅ 推荐已发送到 {sent_count}/{len(target_groups)} 个群\n编号: {rec_id}\n群友回复「同意」或「拒绝」即可，1小时无人回复自动订阅")
    async def _check_rec_timeout(self):
        """检查超时的推荐，1小时无人拒绝自动通过"""
        now = time.time()
        for rec_id, rec in list(self._pending_recs.items()):
            if now - rec.get("created_at", 0) < 3600:
                continue
            for gid, gs in rec.get("groups", {}).items():
                if gs.get("status") != "pending":
                    continue
                gs["status"] = "approved"
                self._save_recs()
                try:
                    ok = await self._auto_subscribe(
                        rec["url"], gid, rec.get("interval", 30)
                    )
                    title = rec.get("title", "未知")
                    if ok:
                        await self.ctx.send_message(gid, MessageChain(chain=[
                            Comp.Plain(f"✅ 推荐「{title}」1小时无人拒绝，已自动订阅！\n⏰ 每{rec.get('interval', 30)}分钟检查更新")
                        ]))
                except Exception as e:
                    self.logger.error("[MyRSS] auto-approve failed for %s: %s", gid, e)
    async def _is_group_admin(self, group_id: str, user_id: str) -> bool:
        """判断用户是否为群主或管理员"""
        if not self._aiocqhttp_bot:
            return False
        try:
            gid = int(group_id.split(":")[-1]) if ":" in group_id else int(group_id)
            uid = int(user_id)
            info = await self._aiocqhttp_bot.get_group_member_info(
                group_id=gid, user_id=uid, no_cache=True
            )
            if isinstance(info, dict):
                return info.get("role", "member") in ("owner", "admin")
            return False
        except Exception as e:
            self.logger.warning("[MyRSS] get member info failed: %s", e)
            return False
    @myrss.command("recs")
    async def cmd_recs(self, event: AstrMessageEvent):
        """查看所有待处理的推荐"""
        if not self._pending_recs:
            yield event.plain_result("当前没有待处理的推荐。")
            return
        lines = ["📋 推荐列表："]
        now = time.time()
        for rec_id, rec in sorted(self._pending_recs.items(), key=lambda x: x[1].get("created_at", 0), reverse=True):
            title = rec.get("title", "未知")
            route = rec.get("route", "")
            elapsed = int((now - rec.get("created_at", 0)) / 60)
            remaining = max(0, 60 - elapsed)
            groups_info = []
            for gid, gs in rec.get("groups", {}).items():
                status = gs.get("status", "pending")
                gid_short = gid.split(":")[-1]
                if status == "pending":
                    groups_info.append(f"  {gid_short} ⏳待定")
                elif status == "approved":
                    groups_info.append(f"  {gid_short} ✅已订阅")
                elif status == "rejected":
                    groups_info.append(f"  {gid_short} ❌已拒绝")
                elif status == "cancelled":
                    groups_info.append(f"  {gid_short} 🚫已撤回")
            lines.append(f"\n🆔 {rec_id} | {title}")
            lines.append(f"  路由: {route}")
            lines.append(f"  已过{elapsed}分钟 | {'已超时' if remaining == 0 else f'剩{remaining}分钟自动通过'}")
            lines.extend(groups_info)
        yield event.plain_result("\n".join(lines))

    @myrss.command("cancelrec")
    async def cmd_cancelrec(self, event: AstrMessageEvent, rec_id: str = ""):
        """撤回推荐（取消所有待定群的订阅）
        用法：/myrss cancelrec R78813
              /myrss cancelrec all
        """
        if not rec_id:
            yield event.plain_result("用法: /myrss cancelrec <编号或all>\n先用 /myrss recs 查看编号")
            return

        if rec_id.lower() == "all":
            count = 0
            for rid, rec in self._pending_recs.items():
                for gid, gs in rec.get("groups", {}).items():
                    if gs.get("status") == "pending":
                        gs["status"] = "cancelled"
                        count += 1
            self._save_recs()
            yield event.plain_result(f"✅ 已撤回所有待定推荐（{count}个群）")
            return

        if rec_id not in self._pending_recs:
            yield event.plain_result(f"找不到编号 {rec_id}，用 /myrss recs 查看")
            return

        rec = self._pending_recs[rec_id]
        cancelled = []
        for gid, gs in rec.get("groups", {}).items():
            if gs.get("status") == "pending":
                gs["status"] = "cancelled"
                cancelled.append(gid.split(":")[-1])
        self._save_recs()

        if cancelled:
            yield event.plain_result(f"✅ 已撤回推荐 {rec_id}「{rec.get('title', '')}」\n取消了 {len(cancelled)} 个群: {', '.join(cancelled)}")
        else:
            yield event.plain_result(f"推荐 {rec_id} 没有待定的群（可能已全部通过/拒绝）")
    @myrss.command("subs")
    async def cmd_subs(self, event: AstrMessageEvent):
        """查看所有订阅源及其订阅群列表"""
        lines = ["📋 所有订阅源："]
        idx = 0
        for url, info in self.dh.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            subs = info.get("subscribers", {})
            if not subs and not info.get("global"):
                continue
            title = info.get("info", {}).get("title", url)
            lines.append(f"\n{idx}. 📡 {title}")
            lines.append(f"   路由: {url.split(':1200')[-1] if ':1200' in url else url}")
            if subs:
                for sub_id in subs:
                    gid_short = sub_id.split(":")[-1]
                    platform = sub_id.split(":")[0]
                    cron = subs[sub_id].get("cron_expr", "?")
                    lines.append(f"   └ {gid_short} ({platform}) [{cron}]")
            else:
                lines.append(f"   └ (无订阅者)")
            idx += 1
        if idx == 0:
            yield event.plain_result("当前没有任何订阅源。")
            return
        yield event.plain_result("\n".join(lines))

    @myrss.command("unsub")
    async def cmd_unsub(self, event: AstrMessageEvent, route: str = "", group_ids: str = ""):
        """从指定源批量退订群
        用法：/myrss unsub /bilibili/user/dynamic/2107422684 721058477,123456
              /myrss unsub /bilibili/user/dynamic/2107422684 all
        """
        if not route:
            yield event.plain_result(
                "用法: /myrss unsub <路由> <群号列表>\n"
                "  群号用逗号分隔，或填 all 退订所有群\n"
                "  先用 /myrss subs 查看路由和群号"
            )
            return

        # 找到匹配的URL
        target_url = None
        for url in self.dh.data:
            if url in ("rsshub_endpoints", "settings"):
                continue
            if route in url:
                target_url = url
                break

        if not target_url:
            yield event.plain_result(f"找不到包含 '{route}' 的订阅源\n用 /myrss subs 查看")
            return

        subs = self.dh.data[target_url].get("subscribers", {})
        if not subs:
            yield event.plain_result("该源没有订阅者。")
            return

        title = self.dh.data[target_url].get("info", {}).get("title", route)

        if not group_ids or group_ids.strip().lower() == "all":
            removed = list(subs.keys())
            subs.clear()
        else:
            gids = [g.strip() for g in re.split(r'[,，\s]+', group_ids) if g.strip()]
            removed = []
            for gid in gids:
                # 模糊匹配：群号可能只传了数字
                to_del = [k for k in subs if gid in k]
                for k in to_del:
                    del subs[k]
                    removed.append(k.split(":")[-1])

        if removed:
            self.dh.save()
            self._reload_jobs()
            yield event.plain_result(
                f"✅ 已从「{title}」退订 {len(removed)} 个群:\n" +
                "\n".join(f"  - {g}" for g in removed)
            )
        else:
            yield event.plain_result("没有匹配的群号，请检查输入。")
    @filter.llm_tool(name="myrss_cancel_recommend")
    async def tool_cancel_rec(self, event: AstrMessageEvent, rec_id: str = "all"):
        """用户想撤回/取消之前发出的推荐时调用。

        Args:
            rec_id(string): 推荐编号如R78813，或"all"撤回全部
        """
        if not self._pending_recs:
            yield event.plain_result("当前没有待处理的推荐。")
            return

        if rec_id.lower() == "all":
            count = 0
            titles = []
            for rid, rec in self._pending_recs.items():
                for gid, gs in rec.get("groups", {}).items():
                    if gs.get("status") == "pending":
                        gs["status"] = "cancelled"
                        count += 1
                titles.append(rec.get("title", "未知"))
            self._save_recs()
            yield event.plain_result(f"✅ 已撤回所有待定推荐（{count}个群）\n涉及: {', '.join(set(titles))}")
        else:
            if rec_id not in self._pending_recs:
                yield event.plain_result(f"找不到编号 {rec_id}")
                return
            rec = self._pending_recs[rec_id]
            count = 0
            for gid, gs in rec.get("groups", {}).items():
                if gs.get("status") == "pending":
                    gs["status"] = "cancelled"
                    count += 1
            self._save_recs()
            yield event.plain_result(f"✅ 已撤回推荐「{rec.get('title', '')}」（{count}个群）")

    @filter.llm_tool(name="myrss_batch_unsub")
    async def tool_batch_unsub(self, event: AstrMessageEvent, keyword: str = "", group_ids: str = "all"):
        """用户想从某个源批量退订群时调用。如"取消道爷张志顺在所有群的订阅""退订B站xxx的123群和456群"。

        Args:
            keyword(string): 源名称关键词，如"道爷""张至顺""AnthropicAI"
            group_ids(string): 群号逗号分隔，或"all"退订所有群
        """
        if not keyword:
            yield event.plain_result("请告诉我要退订哪个源。")
            return

        # 模糊匹配源
        target_url = None
        target_title = None
        for url, info in self.dh.data.items():
            if url in ("rsshub_endpoints", "settings"):
                continue
            title = info.get("info", {}).get("title", "")
            if keyword.lower() in title.lower() or keyword.lower() in url.lower():
                target_url = url
                target_title = title
                break

        if not target_url:
            yield event.plain_result(f"找不到包含「{keyword}」的订阅源。\n用 /myrss subs 查看所有源。")
            return

        subs = self.dh.data[target_url].get("subscribers", {})
        if not subs:
            yield event.plain_result(f"「{target_title}」没有订阅者。")
            return

        if group_ids.strip().lower() == "all":
            removed = [k.split(":")[-1] for k in subs]
            subs.clear()
        else:
            gids = [g.strip() for g in re.split(r'[,，\s]+', group_ids) if g.strip()]
            removed = []
            for gid in gids:
                to_del = [k for k in subs if gid in k]
                for k in to_del:
                    del subs[k]
                    removed.append(k.split(":")[-1])

        if removed:
            self.dh.save()
            self._reload_jobs()
            yield event.plain_result(
                f"✅ 已从「{target_title}」退订 {len(removed)} 个群:\n" +
                "\n".join(f"  - {g}" for g in removed)
            )
        else:
            yield event.plain_result("没有匹配的群号。")

"""参考图管理：后台上传 / 预览 / 清除。

为什么需要它：这个插件原来只能靠**手填文件路径**来指定角色立绘和默认参考图
（`character_image_path`、`base_reference_image_path`），而这些路径在配置界面里
根本不存在 —— `base_reference_image_path` 连 `_conf_schema.json` 都没有，
只有 `__init__` 里一行 `config.get(...)`，等于必须手改配置文件。

这里的做法是**把图存进插件自己的数据目录，再把配置项改写成那个绝对路径**。
好处是：

1. 上传之后立刻生效 —— 每次画图都会重读文件（`_load_character_reference_image`
   没有缓存），所以不必重启插件；
2. 配置项仍然是普通路径，用户想手动换回别的文件也照样可以；
3. 旧图留在 `references/history/` 里，换错了能捡回来。

**文件名是固定的**（`character.png` / `base.jpg`），换图等于换内容不换名字。
用哈希名的做法试过，问题是配置项里存的是绝对路径 —— 换一张图就得改写配置，
而改写配置要碰 AstrBot 的持久化，失败了还会留下一半旧一半新的状态。固定名最稳。

接口挂在 `/<插件名>/ui/` 下，走 AstrBot 的插件页面桥接层，鉴权由面板负责 ——
这里的代码不需要、也不应该自己判权限。
"""

from __future__ import annotations

import base64
import datetime
import io
import time
from pathlib import Path
from typing import Any, Dict, Optional

from astrbot.api import logger

#: 两个可管理的槽位。键是接口里用的名字，值是配置项名 + 中文名。
SLOTS: Dict[str, Dict[str, str]] = {
    "character": {
        "config_key": "character_image_path",
        "title": "角色立绘",
        "hint": "画「你自己」时用的那张图。换了立刻生效，不用重启。",
    },
    "base": {
        "config_key": "base_reference_image_path",
        "title": "默认参考图",
        "hint": "没有指定参考图时兜底用的风格图。原来这个配置项在界面上根本不存在。",
    },
}

#: 只收这几种。画图接口本身也只吃得下 png/jpeg/webp，
#: 在这里挡掉可以省得等到画图那一步才报错。
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp"}
EXT_ALIAS = {".jpeg": ".jpg"}
MAX_UPLOAD_BYTES = 12 * 1024 * 1024

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
}


class ReferenceStore:
    """把参考图存进插件数据目录，并维护「当前是哪张」。"""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "references"
        self.history = self.root / "history"
        self.root.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)

    # --- 读 ---------------------------------------------------------------

    def _managed_file(self, slot: str) -> Optional[Path]:
        """本插件目录里当前生效的那个文件（如果有）。"""
        for candidate in sorted(self.root.glob(f"{slot}.*")):
            if candidate.suffix.lower() in ALLOWED_EXT and candidate.is_file():
                return candidate
        return None

    def resolve(self, slot: str, config_path: str = "") -> Optional[Path]:
        """算出这张参考图**实际**是哪一份。

        顺序是「面板传过的」优先，其次才是配置项里手填的路径。反过来的话，
        用户传了新图却因为配置里还指着老路径而不生效，这种 bug 最难查。
        """
        if slot not in SLOTS:
            return None
        managed = self._managed_file(slot)
        if managed is not None:
            return managed
        raw = (config_path or "").strip()
        if not raw:
            return None
        attempt = Path(raw).expanduser()
        if not attempt.is_absolute():
            # 相对路径历来是相对插件目录，不是相对进程 cwd。
            attempt = (self.root.parent / attempt).resolve()
        try:
            return attempt if attempt.is_file() else None
        except OSError:
            return None

    def describe(self, slot: str, config_path: str = "") -> Dict[str, Any]:
        info = SLOTS[slot]
        found = self.resolve(slot, config_path)
        base: Dict[str, Any] = {
            "slot": slot,
            "title": info["title"],
            "hint": info["hint"],
            "configKey": info["config_key"],
            "configuredPath": (config_path or "").strip(),
            "exists": False,
            "path": "",
            "size": 0,
            "mtime": "",
            "managed": False,
        }
        if found is None:
            return base
        try:
            stat = found.stat()
        except OSError:
            return base
        # managed=False 表示这张图是配置项指到别处去的，不是面板传的。
        # 面板要能看出这个区别，否则用户会疑惑「我明明传了怎么还是旧图」。
        managed = found.parent == self.root
        base.update(
            {
                "exists": True,
                "path": str(found),
                "size": stat.st_size,
                "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "managed": managed,
            }
        )
        return base

    def all_slots(
        self, config_paths: Dict[str, str], with_thumb: bool = True
    ) -> list[Dict[str, Any]]:
        return [
            self.thumbnail(slot, config_paths.get(slot, ""))
            if with_thumb
            else self.describe(slot, config_paths.get(slot, ""))
            for slot in SLOTS
        ]

    def thumbnail(
        self, slot: str, config_path: str = "", max_side: int = 640
    ) -> Dict[str, Any]:
        """给面板看的缩略图。

        为什么要缩略图而不是直接让页面 ``img src`` 指向接口：面板的桥接层
        （``AstrBotPluginPage.apiGet``）只会把响应按 JSON 解析，二进制图片
        拿不到；而这里是嵌在 iframe 里的页面，也没法自己带令牌去请求接口。
        所以缩成一张 data URI 塞进 JSON 里 —— 640px 的 JPEG 通常几十 KB，
        面板列表刷新一次完全扛得住，还能省掉一堆缓存/鉴权的坑。

        用最长边限制而不是固定尺寸，是因为参考图有横有竖，硬切成方形会把
        「构图」这个最重要的信息裁掉。
        """
        info = self.describe(slot, config_path)
        if not info["exists"]:
            return info
        try:
            from PIL import Image as _Image

            with _Image.open(info["path"]) as image:
                image.load()
                width, height = image.size
                copy = image.convert("RGB")
                if max(width, height) > max_side:
                    scale = max_side / float(max(width, height))
                    copy = copy.resize(
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        _Image.LANCZOS,
                    )
                buffer = io.BytesIO()
                copy.save(buffer, format="JPEG", quality=82, optimize=True)
            info["thumb"] = "data:image/jpeg;base64," + base64.b64encode(
                buffer.getvalue()
            ).decode("ascii")
            info["width"] = width
            info["height"] = height
        except Exception:  # noqa: BLE001 - 缩略图坏了不该拖垮整个页面
            logger.warning("[Artist] 生成参考图缩略图失败：%s", info.get("path"), exc_info=True)
            info["thumb"] = ""
        return info

    def read_bytes(self, slot: str, config_path: str = "") -> Optional[bytes]:
        found = self.resolve(slot, config_path)
        if found is None:
            return None
        try:
            return found.read_bytes()
        except OSError:
            return None

    def content_type(self, slot: str, config_path: str = "") -> str:
        found = self.resolve(slot, config_path)
        if found is None:
            return "application/octet-stream"
        return CONTENT_TYPES.get(found.suffix.lower(), "application/octet-stream")

    # --- 写 ---------------------------------------------------------------

    def save(self, slot: str, data: bytes, filename: str = "") -> Path:
        if slot not in SLOTS:
            raise ValueError(f"不认识的槽位：{slot}")
        if not data:
            raise ValueError("文件是空的")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"图片超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB，先压一下再传"
            )
        # 先看内容再信扩展名：用户把 .webp 改名成 .png 的情况相当常见。
        sniffed = _sniff_ext(data)
        if not sniffed:
            raise ValueError("这个文件不是图片（内容对不上扩展名）")
        suffix = Path(filename or "").suffix.lower()
        if suffix not in ALLOWED_EXT:
            suffix = sniffed
        suffix = EXT_ALIAS.get(suffix, suffix)
        if suffix != sniffed:
            # 扩展名和真实格式对不上时，以真实格式为准。
            suffix = sniffed

        # 先把旧图挪进 history：换错了能捡回来，而且不占着当前文件名。
        for stale in list(self.root.glob(f"{slot}.*")):
            if stale.is_file():
                self._archive(stale)

        target = self.root / f"{slot}{suffix}"
        tmp = target.with_name(f"{slot}{suffix}.part")
        tmp.write_bytes(data)
        tmp.replace(target)  # 原子替换：面板那边永远不会读到半张图
        self.trim_history()
        logger.info(
            "[Artist] 参考图已更新 %s -> %s (%d 字节)", slot, target.name, len(data)
        )
        return target

    def _archive(self, stale: Path) -> None:
        target = self.history / f"{stale.stem}-{int(time.time() * 1000)}{stale.suffix}"
        try:
            stale.replace(target)
        except OSError:
            logger.warning("[Artist] 归档旧参考图失败 %s", stale, exc_info=True)

    def clear(self, slot: str) -> bool:
        """只清面板传的那份。配置项里手填的路径不归这里管 —— 动它等于
        替用户改配置，那不是「清除参考图」的意思。"""
        removed = False
        for stale in list(self.root.glob(f"{slot}.*")):
            if stale.is_file():
                try:
                    stale.unlink()
                    removed = True
                except OSError:
                    logger.warning("[Artist] 删除参考图失败 %s", stale, exc_info=True)
        return removed

    def restore_latest(self, slot: str) -> Optional[Path]:
        """从 history 里把最近一张捡回来，给「换错了撤销」用。"""
        candidates = sorted(
            (
                p
                for p in self.history.glob(f"{slot}-*")
                if p.is_file() and p.suffix.lower() in ALLOWED_EXT
            ),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        latest = candidates[0]
        try:
            data = latest.read_bytes()
            return self.save(slot, data, latest.name)
        except (OSError, ValueError):
            logger.warning("[Artist] 恢复历史参考图失败 %s", latest, exc_info=True)
            return None

    def trim_history(self, keep: int = 10) -> int:
        """历史留最近 ``keep`` 张就够了。参考图偶尔才换一次，
        留着无限增长只会吃掉磁盘。"""
        files = sorted(
            (p for p in self.history.glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for stale in files[max(0, keep):]:
            try:
                stale.unlink()
                removed += 1
            except OSError:
                continue
        return removed


def _sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def content_type_for(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")

"""插件后台页面的 Web API：上传 / 预览 / 清除参考图。

这一层只做三件事：读请求、把参数交给插件、把结果说成人话。真正存图的是
``reference_store.ReferenceStore``，因为群命令和面板走同一套存储才不会出现
「面板传的图 LLM 用不上」这种事。

接口挂在 ``/<插件名>/ui/`` 下，走 AstrBot 的插件页面桥接层，鉴权由面板
负责 —— 这里的代码不需要、也不应该自己判权限。

请求/响应用的是 **`astrbot.api.web`**（AstrBot 4.29 起的官方接口，底下是
FastAPI）。老版本用的是 quart，这里留了一层兼容：拿不到新接口就退回 quart。
踩过的坑：在两套接口之间猜错的症状很安静 —— 页面能打开、列表是空的、上传
一直「没收到文件」，因为请求上下文根本没绑上。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_artist"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/ui"

# --- 请求/响应兼容层 ---------------------------------------------------------

_NEW_API = True
try:  # AstrBot >= 4.29
    from astrbot.api.web import error_response as _error_response  # type: ignore
    from astrbot.api.web import file_response as _file_response  # type: ignore
    from astrbot.api.web import json_response as _json_response  # type: ignore
    from astrbot.api.web import request as _request  # type: ignore
except Exception:  # noqa: BLE001 - 老版本只有 quart
    _NEW_API = False
    _file_response = None
    try:
        from quart import jsonify as _quart_jsonify  # type: ignore
        from quart import request as _quart_request  # type: ignore
        from quart import send_file as _quart_send_file  # type: ignore
    except Exception:  # noqa: BLE001
        _quart_jsonify = None
        _quart_request = None
        _quart_send_file = None
    # 兼容模式下请求对象换成 quart 的那个 —— 这两行写反过一次，
    # 症状是页面能开、列表空白、上传永远「没收到文件」。
    _request = _quart_request
    _json_response = None
    _error_response = None


async def _read_json() -> Dict[str, Any]:
    if _NEW_API:
        payload = await _request.json(default={})
    elif _request is not None:
        payload = await _request.get_json(force=True, silent=True)
    else:
        return {}
    return payload if isinstance(payload, dict) else {}


async def _read_query() -> Dict[str, Any]:
    if _NEW_API:
        query = _request.query
        return {key: query.get(key) for key in getattr(query, "keys", lambda: [])()}
    if _request is not None:
        return dict(_request.args)
    return {}


async def _read_upload() -> Tuple[bytes, str]:
    """拖进来的文件 → ``(字节, 文件名)``。字段名固定 ``file``（前端桥接层写的）。"""
    if _request is None:
        return b"", ""
    try:
        attribute = _request.files
        # astrbot.api.web 里 files 是方法；quart 里是可等待的属性
        files = attribute() if callable(attribute) else attribute
        files = await files
    except Exception:  # noqa: BLE001
        return b"", ""
    for key in ("file", "files", "upload"):
        handle = files.get(key) if hasattr(files, "get") else None
        if handle is None:
            continue
        name = str(getattr(handle, "filename", "") or "")
        data = handle.read()
        if not isinstance(data, (bytes, bytearray)):
            data = await data
        return bytes(data), name
    return b"", ""


def _ok(data: Any) -> Any:
    if _NEW_API:
        return _json_response({"status": "ok", "data": data})
    if _quart_jsonify is not None:
        return _quart_jsonify({"status": "ok", "data": data})
    return {"status": "ok", "data": data}


def _error(message: str, extra: Dict[str, Any] | None = None) -> Any:
    if _NEW_API:
        return _error_response(message, data=extra)
    body: Dict[str, Any] = {"status": "error", "message": message}
    if extra:
        body["data"] = extra
    if _quart_jsonify is not None:
        return _quart_jsonify(body)
    return body


class ArtistWebUI:
    def __init__(self, context: Any, plugin: Any) -> None:
        self.context = context
        self.plugin = plugin
        self.routes: list = []

    # --- 注册 -------------------------------------------------------------

    def register(self) -> None:
        table = [
            ("state", self.state, ["GET"], "两个参考图槽位的当前状态"),
            ("upload", self.upload, ["POST"], "上传 / 替换某张参考图"),
            ("preview", self.preview, ["GET"], "看某张参考图（避免浏览器缓存）"),
            ("clear", self.clear, ["POST"], "清除面板传的参考图"),
            ("vision", self.vision_api, ["GET"], "有没有能看图的模型"),
            ("test", self.test, ["POST"], "试画一张，验证配置是否可用"),
            ("info", self.info, ["GET"], "插件当前配置摘要（画图接口、额度）"),
        ]
        for name, handler, methods, desc in table:
            route = f"{ROUTE_PREFIX}/{name}"
            try:
                self.context.register_web_api(route, handler, methods, desc)
                self.routes.append(f"{','.join(methods)} {route}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Artist] 注册 Web API 失败 %s：%s", route, exc)
        logger.info(
            "[Artist] 已注册 %s 个页面接口（%s）",
            len(self.routes),
            "astrbot.api.web" if _NEW_API else "quart 兼容模式",
        )

    # --- 各接口 -----------------------------------------------------------

    async def state(self) -> Any:
        try:
            return _ok(await self.plugin.web_reference_state())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Artist] 读取参考图状态失败", exc_info=True)
            return _error(f"读取参考图状态失败：{type(exc).__name__}: {exc}")

    async def vision_api(self) -> Any:
        """有没有能看图的模型。界面要据此提示「上传了也识别不了」。"""
        try:
            return _ok(await self.plugin.web_vision_availability())
        except Exception as exc:  # noqa: BLE001
            return _error(f"读取失败：{exc}")

    async def info(self) -> Any:
        try:
            return _ok(await self.plugin.web_plugin_info())
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Artist] 读取插件信息失败", exc_info=True)
            return _error(f"读取失败：{type(exc).__name__}: {exc}")

    async def upload(self) -> Any:
        slot = ""
        try:
            args = await _read_query()
            slot = str(args.get("slot") or "")
            if not slot:
                payload = await _read_json()
                slot = str(payload.get("slot") or "")
        except Exception:  # noqa: BLE001
            slot = ""
        if not slot:
            # 表单方式上传时 slot 在 form 里
            try:
                form = _request.form
                form = form() if callable(form) else form
                form = await form
                slot = str(form.get("slot") or "")
            except Exception:  # noqa: BLE001
                slot = ""

        data, filename = await _read_upload()
        if not data:
            # 退路：前端也可以用 JSON 传 base64（大图走这条路时桥接层不转发字节）
            payload = await _read_json()
            raw = str(payload.get("data", "") or "")
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            if raw:
                import base64

                try:
                    data = base64.b64decode(raw)
                except Exception:  # noqa: BLE001
                    return _error("图片数据解不开（base64 不完整）")
            filename = filename or str(payload.get("filename") or "")
            slot = slot or str(payload.get("slot") or "")

        if not slot:
            return _error("没说清传的是哪个槽位")
        if not data:
            return _error("没收到文件")
        try:
            await self.plugin.web_save_reference(slot, data, filename)
        except ValueError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Artist] 保存参考图失败", exc_info=True)
            return _error(f"保存失败：{type(exc).__name__}: {exc}")
        return _ok(await self.plugin.web_reference_state())

    async def preview(self) -> Any:
        args = await _read_query()
        slot = str(args.get("slot") or "")
        found = await self.plugin.web_reference_path(slot)
        if found is None:
            return _error("这个槽位还没有图")
        path, content_type = found
        if _NEW_API:
            # no-store：换图后浏览器必须立刻拿新的，否则用户会以为没传上去
            return _file_response(
                path,
                content_type=content_type,
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        if _quart_send_file is not None:
            response = await _quart_send_file(str(path), mimetype=content_type)
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response
        return _error("当前 AstrBot 版本不支持直接预览图片")

    async def clear(self) -> Any:
        payload = await _read_json()
        slot = str(payload.get("slot") or "")
        try:
            args = await _read_query()
            slot = slot or str(args.get("slot") or "")
        except Exception:  # noqa: BLE001
            pass
        if not slot:
            return _error("没说清清哪个槽位")
        try:
            removed = await self.plugin.web_clear_reference(slot)
        except ValueError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Artist] 清除参考图失败", exc_info=True)
            return _error(f"清除失败：{type(exc).__name__}: {exc}")
        state = await self.plugin.web_reference_state()
        return _ok({"removed": removed, "state": state})

    async def test(self) -> Any:
        payload = await _read_json()
        prompt = str(payload.get("prompt") or "").strip()
        try:
            result = await self.plugin.web_test_draw(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Artist] 试画失败", exc_info=True)
            return _error(f"试画失败：{type(exc).__name__}: {exc}")
        if not result.get("ok"):
            return _error(str(result.get("message") or "试画失败"), result)
        return _ok(result)

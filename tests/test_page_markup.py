"""页面静态结构：这些错一个都不会报错，只会「页面能开但一片失灵」。

真机上踩过的：
* 用普通 `<script>` 加载 app.js → 跑在桥接 SDK 之前，bridge 永久 undefined；
* 引用的元素 id 在 HTML 里不存在 → 第一个 addEventListener 就抛 TypeError，
  **后面所有按钮全挂不上**（meme 页为这个专门写过 on() 兜底）。
"""
from __future__ import annotations

import re
from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "artist"


def test_entry_file_exists():
    """AstrBot 只认 `index.html` 作为页面入口（PLUGIN_PAGE_ENTRY_FILE_NAME）。"""
    assert (PAGE_DIR / "index.html").is_file()


def test_app_js_loaded_as_module():
    """必须是 module。

    模块脚本延迟到文档解析完才执行，正好排在 AstrBot 注入的桥接 SDK 之后。
    换成普通 `<script src>` 就会复现「桥接层不可用」——这条测试就是防它被改回去。
    """
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script\b[^>]*>", html)
    app_tags = [t for t in tags if "app.js" in t]
    assert app_tags, "index.html 里没有引用 app.js"
    for tag in app_tags:
        assert 'type="module"' in tag, f"app.js 必须用 type=module 加载：{tag}"


def test_no_hardcoded_bridge_sdk_tag():
    """页面自己不要引桥接 SDK。

    AstrBot 会在 `</body>` 前注入带作用域令牌的版本；自己再引一份会拿到没有
    令牌的，请求照样 401。
    """
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "bridge-sdk.js" not in html


def test_assets_are_relative():
    """CSS/JS 都要用相对路径。

    页面资源是从带路径令牌的 URL 里取的（`/api/v1/plugins/<name>/views/<view>/_t/<token>/`），
    相对路径会自然解析到同一令牌下；写成 `/pages/...` 这种绝对路径会 404。
    """
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    for match in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert not match.startswith("/"), f"绝对路径会绕过令牌：{match}"
        assert match.startswith("./"), f"应当写成 ./xxx：{match}"


def test_every_element_id_used_by_js_exists():
    """app.js 里 el("xxx") 用到的 id，HTML 里必须真的有。

    真机教训：缺一个 id 就整页按钮失灵，控制台只有一句 TypeError，极难定位。
    静态对一遍最省事。
    """
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")

    declared = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r'\bel\(\s*"([^"]+)"\s*\)', js))
    # el(`file-${slot}`) 这种动态 id 单独处理
    dynamic_prefixes = set(re.findall(r'el\(\s*`([a-zA-Z0-9_-]+)-\$\{', js))
    for prefix in dynamic_prefixes:
        declared |= {f"{prefix}-{slot}" for slot in ("character", "base")}

    missing = sorted(used - declared)
    assert not missing, f"app.js 用到了 HTML 里不存在的元素 id：{missing}"


def test_inline_guard_exists():
    """必须有一个**不依赖外部资源**的内联守卫。

    app.js 要是整个没跑起来（模块求值出错/资源被拦），页面会永远停在
    「正在读取…」，用户看不出任何线索。内联脚本一定先执行，负责兜底报错。
    """
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    # 内联 script：有 <script> 但没 src
    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert inline, "没有内联兜底脚本"
    joined = "".join(inline)
    assert "__artistPageBooted" in joined, "兜底脚本没有检查启动标记"
    assert "脚本没能启动" in joined or "脚本报错" in joined


def test_frontend_is_slot_agnostic():
    """前端按后端返回的 slot 泛型渲染，不硬编码槽位名。

    这样加第三个槽位（比如「风格参考」）只要改后端 `SLOTS`，页面自动就有。
    真要硬编码的话，漏一个的症状是「能传但界面不显示」——很难查。
    """
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    # 渲染走 data-slot，说明槽位是从数据里来的
    assert "data-slot" in js or "dataset.slot" in js, "槽位应当从后端数据里取"
    assert 'apiGet("ui/state"' in js, "页面应当从 ui/state 拿槽位列表"


def test_upload_passes_slot_in_query():
    """槽位必须走查询串。

    桥接层 `upload(endpoint, file)` 只收两个参数，第三个参数会被丢掉；
    所以 `ui/upload?slot=xxx` 是唯一能把槽位传过去的方式。
    """
    js = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert "ui/upload?slot=" in js, "上传接口必须把 slot 放在查询串里"

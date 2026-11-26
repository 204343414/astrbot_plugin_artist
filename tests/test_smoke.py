"""把 `main.py` 真的加载起来，验证「线路接对了没有」。

前面两个测试文件测的是新写的模块本身；这个文件测的是**它们有没有被接进
插件**——这正是原来那三个工具栽的地方（实现完整、文档完整、就是没挂上，
`grep llm_tool main.py` 零匹配，谁都没发现）。

用 `test_admin_bypass.py` 里现成的桩写 `load_main()`，不另起一套。
"""
from __future__ import annotations

import pytest

import test_admin_bypass as harness


@pytest.fixture(scope="module")
def mod():
    return harness.load_main()


def test_llm_tools_reachable_from_plugin_class(mod):
    for name in ("draw_image", "set_drawing_reference", "list_drawing_references"):
        assert hasattr(mod.GeminiArtist, name), f"{name} 没被混进插件类"


def test_old_yield_style_tools_are_gone(mod):
    """旧的三个 yield 版方法必须删掉。

    留着它们等于两套画图逻辑并存 —— 迟早各自漂移，而不一致的那一套
    会放过去不该放的东西。`/draw` 命令是另一条路，照旧保留。
    """
    for name in ("gemini_draw", "manage_drawing_reference", "combine_images_draw"):
        assert not hasattr(mod.GeminiArtist, name), f"{name} 还留着"
    assert hasattr(mod.GeminiArtist, "draw_once"), "/draw 命令不能被删"


def test_plugin_class_mro_is_sane(mod):
    """ArtistToolsMixin 必须在 Star 前面，且不能退化成 object。

    退化过一次：导入失败时降级成 ``object`` → ``class GeminiArtist(object, Star)``
    直接抛 MRO 错，整个插件加载不起来。这个是拿测试钉住的。
    """
    names = [base.__name__ for base in mod.GeminiArtist.__mro__]
    assert "ArtistToolsMixin" in names
    assert names.index("ArtistToolsMixin") < names.index("Star")


def test_version_bumped():
    """版本号在 `@register(...)` 里，不是模块变量 —— 只能读源码。"""
    source = (harness.Path(__file__).resolve().parents[1] / "main.py").read_text("utf-8")
    assert '"2.2.0"' in source
    assert '"2.1.3"' not in source.split("@register(")[1].split(")")[0]


def test_conf_schema_has_base_reference_keys():
    """`base_reference_image_path` 原来只有 `config.get`，schema 里根本没有 ——
    界面上填不了，等于必须手改配置文件。"""
    import json

    schema = json.loads(
        (harness.Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text("utf-8")
    )
    assert "base_reference_image_path" in schema
    assert "enable_base_reference_image" in schema
    assert schema["base_reference_image_path"]["type"] == "string"
    assert schema["enable_base_reference_image"]["type"] == "bool"


def test_reference_path_resolution_prefers_store(mod, tmp_path):
    """面板传的图优先于配置里手填的路径。"""
    from reference_store import ReferenceStore

    store = ReferenceStore(tmp_path)
    saved = store.save("character", _png())

    artist = object.__new__(mod.GeminiArtist)
    artist.reference_store = store
    resolved = artist._resolve_reference_path("character", "/somewhere/else.png")

    assert resolved == saved


def test_base_reference_respects_switch(mod, tmp_path):
    """「默认参考图」的开关必须真的管用 —— 以前只有那个从没被调用的
    `gemini_draw` 里认它，等于摆设。"""
    from reference_store import ReferenceStore

    store = ReferenceStore(tmp_path)
    store.save("base", _png())

    artist = object.__new__(mod.GeminiArtist)
    artist.reference_store = store
    artist.base_reference_image_path = ""
    artist.enable_base_reference_image = False

    assert artist._base_reference_image_or_none() is None

    artist.enable_base_reference_image = True
    assert artist._base_reference_image_or_none() is not None


def test_character_portrait_loads_from_store(mod, tmp_path):
    """上传完立刻生效：`_load_character_reference_image` 每次都重读文件，没有缓存。"""
    from reference_store import ReferenceStore

    store = ReferenceStore(tmp_path)
    store.save("character", _png(size=(16, 16)))

    artist = object.__new__(mod.GeminiArtist)
    artist.reference_store = store
    artist.character_image_path = ""

    image = artist._load_character_reference_image()
    assert image is not None
    assert image.size == (16, 16)


def test_web_api_registers_expected_routes():
    """面板接口的路径和名字别乱改 —— 前端 app.js 是照着它们写的。"""
    import web_api

    names = {
        "state", "upload", "preview", "clear", "vision", "test", "info",
    }
    source = web_api.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for name in names:
        assert f'("{name}"' in text, f"接口 {name} 没注册"


def _png(size=(8, 8)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (3, 4, 5)).save(buffer, format="PNG")
    return buffer.getvalue()

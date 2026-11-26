"""参考图存储的行为。这些都是踩过坑之后定下来的规矩，改坏了就会出现
「面板说传上了、画图时却说找不到」这种最难查的问题。"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from reference_store import MAX_UPLOAD_BYTES, ReferenceStore


def png_bytes(size=(32, 24), color=(10, 120, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(size=(20, 20)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture()
def store(tmp_path):
    return ReferenceStore(tmp_path)


def test_save_then_resolve_prefers_managed(store, tmp_path):
    """面板传过的图优先于配置里手填的路径。

    顺序反过来的话，用户传了新图却因为配置还指着老路径而不生效 ——
    这种 bug 用户根本查不出来，只会说「你面板是坏的」。
    """
    legacy = tmp_path / "legacy.png"
    legacy.write_bytes(png_bytes())

    store.save("character", png_bytes(size=(8, 8)))
    resolved = store.resolve("character", str(legacy))

    assert resolved is not None
    assert resolved.parent == store.root
    assert resolved != legacy


def test_resolve_falls_back_to_config_path(store, tmp_path):
    legacy = tmp_path / "legacy.png"
    legacy.write_bytes(png_bytes())

    resolved = store.resolve("base", str(legacy))
    assert resolved == legacy


def test_resolve_returns_none_for_missing_config(store):
    assert store.resolve("base", "") is None
    assert store.resolve("base", "/definitely/not/here.png") is None


def test_extension_follows_content_not_filename(store):
    """扩展名骗人的图片相当常见（.webp 改名成 .png）。

    以内容为准：写进磁盘的文件名如果和真实格式不符，PIL 打不开，
    画图那一步才会报错，报错还看不懂。
    """
    path = store.save("character", png_bytes(), "totally-a-jpeg.jpg")
    assert path.suffix == ".png"


def test_non_image_is_rejected(store):
    with pytest.raises(ValueError):
        store.save("character", b"<html>not an image</html>", "x.png")


def test_empty_upload_is_rejected(store):
    with pytest.raises(ValueError):
        store.save("character", b"", "x.png")


def test_oversize_is_rejected(store):
    with pytest.raises(ValueError):
        store.save("character", b"\x89PNG\r\n\x1a\n" + b"0" * MAX_UPLOAD_BYTES, "x.png")


def test_replacing_archives_old_into_history(store):
    """换图不能直接丢掉旧图：换错了得能捡回来。"""
    store.save("character", png_bytes(color=(1, 2, 3)))
    store.save("character", jpeg_bytes())

    history = list(store.history.iterdir())
    assert len(history) == 1
    assert store.resolve("character").suffix == ".jpg"


def test_history_is_trimmed(store):
    """历史只留 10 张。无限增长只会吃掉磁盘。"""
    for _ in range(14):
        store.save("base", png_bytes())
    assert len(list(store.history.iterdir())) <= 10


def test_clear_only_touches_managed_copy(store, tmp_path):
    """清除只删面板传的那份，不动配置项指着的别的文件。

    动它就等于替用户改配置，那不是「清除参考图」的意思。
    """
    legacy = tmp_path / "keep-me.png"
    legacy.write_bytes(png_bytes())
    store.save("base", png_bytes())

    assert store.clear("base") is True
    assert store.resolve("base", str(legacy)) == legacy
    assert legacy.exists()


def test_describe_marks_managed_vs_external(store, tmp_path):
    """面板必须能看出「这张图是我传的」「这张是配置指到别处的」。"""
    external = tmp_path / "ext.png"
    external.write_bytes(png_bytes())

    store.save("character", png_bytes())
    managed = store.describe("character", "")
    external_info = store.describe("base", str(external))

    assert managed["managed"] is True
    assert external_info["managed"] is False
    assert external_info["configuredPath"].endswith("ext.png")


def test_thumbnail_is_data_uri(store):
    """缩略图必须是 data URI。

    桥接层的 apiGet 只解析 JSON，<img src="接口"> 那套在面板 iframe 里
    拿不到图 —— 这是页面第一版空白的原因。
    """
    store.save("character", png_bytes(size=(1200, 800)))
    info = store.thumbnail("character", "")

    assert info["thumb"].startswith("data:image/jpeg;base64,")
    assert info["width"] == 1200 and info["height"] == 800


def test_thumbnail_survives_corrupt_file(store):
    """文件坏了只该让缩略图空着，不该把整个页面拖垮。"""
    (store.root / "character.png").write_bytes(b"not really a png")
    info = store.thumbnail("character", "")
    assert info["thumb"] == ""
    assert info["exists"] is True


def test_restore_latest_brings_back_previous(store):
    store.save("character", png_bytes(color=(9, 9, 9)))
    first = store.resolve("character").read_bytes()
    store.save("character", jpeg_bytes())

    restored = store.restore_latest("character")
    assert restored is not None
    assert store.resolve("character").read_bytes() == first


def test_unknown_slot_raises(store):
    with pytest.raises(ValueError):
        store.save("nope", png_bytes())

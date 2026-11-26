"""LLM 工具层的行为。

重点不是「函数跑过」，而是这几条**对外承诺**有没有被破坏：

1. `@filter.llm_tool` 真的挂上了（原来三个方法就是因为没挂，从来没被调用过）；
2. 返回值是字符串 —— 返回生成器会把 AstrBot 的调用链搞坏；
3. 图发出去了才告诉模型「发过了」，没发出去绝不能这么说；
4. 额度用完了就不要再去调接口。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from PIL import Image

import artist_tools
from artist_tools import ASPECT_RATIOS, ArtistToolsMixin, ToolContext

import test_admin_bypass as _harness

# 三个 `@filter.llm_tool` 方法在 2.2.5 起挪进了 main.py（AstrBot 只给
# 「定义在主模块里」的工具绑定插件实例，见 tests/test_smoke.py 里的说明）。
# 所以要测到**真实实现**，必须把 main.py 里的方法混进来，而不是只继承
# artist_tools 里的辅助函数。
_main = _harness.load_main()
LLMToolMethods = _main.GeminiArtist


class FakeEvent:
    def __init__(self, sender="u1", group="g1", messages=None):
        self._sender = sender
        self.message_obj = type("Obj", (), {"group_id": group})()
        self._messages = messages or []

    def get_sender_id(self):
        return self._sender

    def get_messages(self):
        return self._messages


class FakeArtist(ArtistToolsMixin):
    """测试用宿主。

    继承关系要覆盖**真实的两半**：工具方法在 main.py，辅助函数在 artist_tools，
    线上由 ``class GeminiArtist(ArtistToolsMixin, Star)`` 合在一起。只继承其一，
    测到的就不是真实组合。
    """
    """只实现工具真正用到的那几个宿主方法，其余全用假数据。"""

    # 真实的工具方法（main.py 里的那份实现）
    draw_image = LLMToolMethods.draw_image
    set_drawing_reference = LLMToolMethods.set_drawing_reference
    list_drawing_references = LLMToolMethods.list_drawing_references

    def __init__(self, **overrides):
        self.api_keys = ["k"]
        self.daily_draw_limit = 0
        self.daily_group_budget = 0.0
        self.cost_per_image = 0.0
        self.character_image_path = ""
        self.robot_id_from_config = ""
        self.temp_reference_context = {}
        self.generated = []
        self.sent = []
        self.spent = []
        self.user_draw_count = 0
        self.character = None
        self.generate_error = None
        self.send_ok = True
        for key, value in overrides.items():
            setattr(self, key, value)

    # 宿主提供的（main.py 里真的那些）
    def _user_usage(self, openid):
        return self.user_draw_count

    def _record_user_draw(self, openid):
        self.user_draw_count += 1
        return self.user_draw_count

    def _check_budget(self, scope):
        return True, 0.0, float("inf")

    def _record_spending(self, scope, n):
        self.spent.append((scope, n))
        return 0.0, 0.0, float("inf")

    def _load_character_reference_image(self):
        return self.character

    def _load_base_reference_image(self):
        return None

    async def download_pil_image_from_url(self, url, desc=""):
        return Image.new("RGB", (4, 4), (1, 1, 1))

    async def get_user_recent_image_pil_from_cache(self, user, group, index=1):
        return None

    async def _review_reference_images_safety(self, images, event=None):
        return {"allowed": True}

    async def _review_prompt_before_generation(self, event, prompt, images):
        return {"allowed": True}

    async def _generate_by_api_type(self, prompt, images, ratio):
        self.generated.append((prompt, len(images), ratio))
        if self.generate_error is not None:
            raise self.generate_error
        return {"image_paths": ["/tmp/does-not-exist.png"]}

    async def _send_image_message(self, event, path, caption=""):
        self.sent.append(path)
        return self.send_ok

    async def _post_image_commentary_once(self, event, prompt, desc):
        return None

    def store_user_image(self, *a, **k):
        return None


def run(coro):
    return asyncio.run(coro)


def _stub_generate(artist, paths, *, prompt_log=-1):
    """替换生成接口。

    不用 `lambda ... : _async(...)` 赋值：那样是**实例属性**，调用时不绑定
    self，`self.generated` 就永远记不进去 —— 这坑让四条测试假通过过。
    写成普通函数赋值给实例，也不会绑定 self，所以这里显式在闭包里用到 artist。
    """

    log = artist.generated if prompt_log == -1 else prompt_log

    async def generate(prompt, images, ratio):
        if log is not None:
            log.append((prompt, len(images), ratio))
        return {"image_paths": list(paths)}

    artist._generate_by_api_type = generate
    return artist


# --- 装饰器：原来那三个方法就是栽在这一步 --------------------------------


def test_llm_tools_are_actually_registered():
    """`grep llm_tool main.py` 曾经是 0 匹配 —— 三个工具写了却从没被注册。

    这条测试存在的唯一目的就是防它再退回去。断言只认「函数身上带没带
    装饰器留下的标记」，不去看那个全局 filter 对象：仓库里别的测试
    （`test_admin_bypass.py`）会用自己的桩把它替换掉，靠全局判断会假失败。
    """
    for name in ("draw_image", "set_drawing_reference", "list_drawing_references"):
        func = getattr(LLMToolMethods, name)
        assert getattr(func, "__llm_tool_name__", "") == name, f"{name} 没挂 @filter.llm_tool"


def test_aspect_ratios_match_command():
    """宽高比就和 /draw 一致，多给没有意义 —— 底下那几个接口只认这几个。"""
    assert ASPECT_RATIOS == ("auto", "1:1", "16:9", "9:16", "4:3", "3:4")


# --- draw_image -----------------------------------------------------------


def test_draw_returns_json_string_not_generator(tmp_path):
    """返回生成器会把 llm_tool 的调用链搞坏 —— 这正是老代码的毛病。"""
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(FakeArtist(), [str(image)], prompt_log=None)

    result = run(artist.draw_image(FakeEvent(), prompt="画只猫"))

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["image_sent"] is True
    assert artist.sent == [str(image)]
    assert artist.user_draw_count == 1
    assert artist.spent == [("g1", 1)]


def test_draw_says_sent_only_when_sent(tmp_path):
    """发送失败还告诉模型「已经发出去了」，模型就会跟用户聊一张不存在的图。"""
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(FakeArtist(send_ok=False), [str(image)])

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫")))

    assert payload["image_sent"] is False
    assert "没" in payload["user_instruction_for_llm"] or "失败" in payload["message"]


def test_draw_reports_empty_file_as_failure(tmp_path):
    """接口有时返回路径但文件是 0 字节，发出去就是裂图。"""
    image = tmp_path / "empty.png"
    image.write_bytes(b"")
    artist = _stub_generate(FakeArtist(), [str(image)], prompt_log=None)

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫")))

    assert payload["success"] is False
    assert artist.sent == []


def test_draw_respects_daily_limit():
    artist = FakeArtist(daily_draw_limit=2)
    artist.user_draw_count = 2

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫")))

    assert payload["success"] is False
    assert payload.get("quota_exhausted") is True
    assert artist.generated == []  # 额度不够就别去烧钱


def test_draw_without_api_key_does_not_call_api():
    artist = FakeArtist(api_keys=[])
    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫")))
    assert payload["success"] is False
    assert artist.generated == []


def test_draw_injects_aspect_ratio_into_prompt(tmp_path):
    """"auto" 之外的比例要同时写进提示词 —— 有些接口只认提示词里那句。"""
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(FakeArtist(), [str(image)])

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫", aspect_ratio="16:9")))

    assert payload["aspect_ratio"] == "16:9"
    prompt, _, ratio = artist.generated[0]
    assert "16:9" in prompt
    assert ratio == "16:9"


def test_draw_clamps_unknown_ratio_to_auto(tmp_path):
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(FakeArtist(), [str(image)], prompt_log=None)

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫", aspect_ratio="7:5")))
    assert payload["aspect_ratio"] == "auto"


def test_draw_counts_character_only_when_it_exists(tmp_path):
    """立绘配置了但文件不在时，「用了角色立绘」这句话不能说 ——
    模型会照着立绘描述，用户一眼看出对不上。"""
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(
        FakeArtist(character_image_path="/nope/missing.png"), [str(image)]
    )

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画你自己")))

    assert payload["references"] == "没有参考图"


def test_draw_counts_character_when_present(tmp_path):
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(
        FakeArtist(character_image_path="/have/portrait.png"), [str(image)]
    )
    artist.character = Image.new("RGB", (8, 8))

    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画你自己")))

    assert "角色立绘 1 张" in payload["references"]
    assert artist.generated[0][1] == 1


def test_reference_cap_is_respected(tmp_path):
    """参考图最多 4 张，再多会把提示词挤出上下文。"""
    image = tmp_path / "out.png"
    Image.new("RGB", (8, 8), (0, 0, 0)).save(image)
    artist = _stub_generate(FakeArtist(), [str(image)])
    event = FakeEvent(messages=[
        type("C", (), {"url": f"https://example.com/{i}.png"})() for i in range(9)
    ])

    run(artist.draw_image(event, prompt="画只猫"))

    assert len(artist.generated) == 1
    assert artist.generated[0][1] <= artist_tools.MAX_REFERENCE_IMAGES


def test_safety_block_is_reported_not_raised():
    artist = FakeArtist()

    async def blocked(event, prompt, images):
        return {"allowed": False, "reason": "含高风险内容"}

    artist._review_prompt_before_generation = blocked
    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="x")))

    assert payload["success"] is False
    assert payload.get("blocked") is True
    assert "含高风险内容" in payload["message"]


def test_generation_error_is_returned_as_text(tmp_path):
    """工具里抛异常会变成「LLM 工具调用失败」，用户看到的是一句莫名其妙的报错。"""
    artist = FakeArtist()
    artist._generate_by_api_type = lambda prompt, images, ratio: _raise(RuntimeError("boom"))
    payload = json.loads(run(artist.draw_image(FakeEvent(), prompt="画只猫")))
    assert payload["success"] is False
    assert "RuntimeError" in payload["message"]


# --- set_drawing_reference / list_drawing_references ----------------------


def test_set_style_reference_is_temporary():
    artist = FakeArtist()
    event = FakeEvent(messages=[type("C", (), {"url": "https://example.com/a.png"})()])

    payload = json.loads(run(artist.set_drawing_reference(event, action="set", target="style")))

    assert payload["success"] is True
    assert "style" in artist.temp_reference_context[("u1", "g1")]


def test_set_reference_rejects_unknown_target():
    artist = FakeArtist()
    payload = json.loads(run(artist.set_drawing_reference(FakeEvent(), target="avatar")))
    assert payload["success"] is False


def test_clearing_missing_reference_is_not_an_error():
    """清一个本来就没有的东西，报错只会让模型去道歉，没意义。"""
    artist = FakeArtist()
    payload = json.loads(run(artist.set_drawing_reference(FakeEvent(), action="clear", target="all")))
    assert payload["success"] is True


def test_list_references_reports_portrait_file(tmp_path):
    portrait = tmp_path / "me.png"
    Image.new("RGB", (4, 4)).save(portrait)
    artist = FakeArtist(character_image_path=str(portrait))

    payload = json.loads(run(artist.list_drawing_references(FakeEvent())))

    assert payload["character_portrait"] == "me.png"
    assert payload["character_mtime"]


def test_list_references_without_portrait():
    artist = FakeArtist(character_image_path="/nope.png")
    payload = json.loads(run(artist.list_drawing_references(FakeEvent())))
    assert payload["character_portrait"] == "(未配置)"


# --- 小工具 ---------------------------------------------------------------


async def _async(value):
    return value


async def _raise(exc):
    raise exc


def test_tool_context_shape():
    ctx = ArtistToolsMixin()._tool_context(FakeEvent(sender="u9", group=""))
    assert isinstance(ctx, ToolContext)
    assert ctx.user_id == "u9"
    # 私聊时预算键回落到用户，不然「群里一份预算」在私聊里就没法算了
    assert ctx.scope == "u9"

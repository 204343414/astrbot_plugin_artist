import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_main():
    for name in list(sys.modules):
        if name == "artist_main_under_test" or name.startswith("astrbot") or name in {"google", "google.genai", "google.genai.types", "openai"}:
            sys.modules.pop(name, None)

    astrbot_api = types.ModuleType("astrbot.api")
    class Logger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
        def debug(self, *args, **kwargs): pass
        def exception(self, *args, **kwargs): pass
    class Store:
        def get(self, key, default=None): return default
        def put(self, key, value): pass
    astrbot_api.logger = Logger()
    astrbot_api.sp = Store()

    event_mod = types.ModuleType("astrbot.api.event")
    class AstrMessageEvent: pass
    class MessageEventResult: pass
    class Filter:
        @staticmethod
        def command(*args, **kwargs):
            def deco(func): return func
            return deco
        @staticmethod
        def event_message_type(*args, **kwargs):
            def deco(func): return func
            return deco
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = MessageEventResult
    event_mod.filter = Filter

    star_mod = types.ModuleType("astrbot.api.star")
    class Context: pass
    class Star:
        def __init__(self, context=None): self.context = context
    class StarTools:
        @staticmethod
        def get_data_dir(name): return "/tmp/artist-test"
    def register(*args, **kwargs):
        def deco(cls): return cls
        return deco
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = register

    greedy_mod = types.ModuleType("astrbot.core.star.filter.command")
    greedy_mod.GreedyStr = str

    all_mod = types.ModuleType("astrbot.api.all")
    all_mod.EventMessageType = types.SimpleNamespace(ALL="ALL")

    comps = types.ModuleType("astrbot.api.message_components")
    class BaseMessageComponent: pass
    class Node: pass
    class Plain: pass
    class Image: pass
    class Nodes: pass
    class Reply: pass
    comps.Node = Node; comps.Plain = Plain; comps.Image = Image; comps.Nodes = Nodes; comps.Reply = Reply; comps.BaseMessageComponent = BaseMessageComponent

    io_mod = types.ModuleType("astrbot.core.utils.io")
    async def download_file(*args, **kwargs): return None
    io_mod.download_file = download_file

    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = astrbot_api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.all"] = all_mod
    sys.modules["astrbot.api.message_components"] = comps
    sys.modules["astrbot.core"] = types.ModuleType("astrbot.core")
    sys.modules["astrbot.core.star"] = types.ModuleType("astrbot.core.star")
    sys.modules["astrbot.core.star.filter"] = types.ModuleType("astrbot.core.star.filter")
    sys.modules["astrbot.core.star.filter.command"] = greedy_mod
    sys.modules["astrbot.core.utils"] = types.ModuleType("astrbot.core.utils")
    sys.modules["astrbot.core.utils.io"] = io_mod

    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    genai.Client = object
    google.genai = genai
    genai_types = types.ModuleType("google.genai.types")
    class HttpOptions: pass
    genai_types.HttpOptions = HttpOptions
    sys.modules["google"] = google
    sys.modules["google.genai"] = genai
    sys.modules["google.genai.types"] = genai_types

    openai = types.ModuleType("openai")
    openai.OpenAI = object
    sys.modules["openai"] = openai

    spec = importlib.util.spec_from_file_location("artist_main_under_test", Path(__file__).resolve().parents[1] / "main.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class AdminBypassTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_main()

    def plugin(self, admins):
        plugin = self.mod.GeminiArtist.__new__(self.mod.GeminiArtist)
        plugin.admin_bypass_review = True
        plugin.context = types.SimpleNamespace(
            astrbot_config={"admins_id": admins},
            get_config=lambda **kwargs: {"admins_id": admins},
        )
        return plugin

    def event(self, sender="sender", *, is_admin=False, raw_author=None):
        raw_author = raw_author or types.SimpleNamespace(member_openid="", user_openid="", id="")
        raw = types.SimpleNamespace(author=raw_author, group_member_openid=getattr(raw_author, "member_openid", ""))
        msg = types.SimpleNamespace(
            sender=types.SimpleNamespace(user_id=sender),
            raw_message=raw,
        )
        return types.SimpleNamespace(
            get_sender_id=lambda: sender,
            is_admin=lambda: is_admin,
            message_obj=msg,
            unified_msg_origin="default:GroupMessage:group",
        )

    def test_role_admin_bypasses_review(self):
        plugin = self.plugin([])
        self.assertTrue(plugin._is_astrbot_admin_for_review_bypass(self.event(is_admin=True)))

    def test_configured_qq_number_bypasses_review(self):
        plugin = self.plugin(["123456"])
        self.assertTrue(plugin._is_astrbot_admin_for_review_bypass(self.event(sender="123456")))

    def test_configured_qq_official_openid_bypasses_review(self):
        plugin = self.plugin(["OPENID_ABC"])
        event = self.event(sender="not-openid", raw_author=types.SimpleNamespace(member_openid="OPENID_ABC", user_openid="", id=""))
        self.assertTrue(plugin._is_astrbot_admin_for_review_bypass(event))

    def test_non_admin_does_not_bypass_review(self):
        plugin = self.plugin(["admin"])
        self.assertFalse(plugin._is_astrbot_admin_for_review_bypass(self.event(sender="member")))


class OpenAIImageSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_main()

    def test_auto_is_forwarded_to_api(self):
        self.assertEqual(
            self.mod.GeminiArtist._openai_size_for_aspect_ratio("auto"),
            "auto",
        )

    def test_missing_ratio_defaults_to_auto(self):
        self.assertEqual(
            self.mod.GeminiArtist._openai_size_for_aspect_ratio(""),
            "auto",
        )
        self.assertEqual(
            self.mod.GeminiArtist._openai_size_for_aspect_ratio(None),
            "auto",
        )

    def test_explicit_ratios_still_use_fixed_sizes(self):
        self.assertEqual(
            self.mod.GeminiArtist._openai_size_for_aspect_ratio("16:9"),
            "1536x1024",
        )
        self.assertEqual(
            self.mod.GeminiArtist._openai_size_for_aspect_ratio("9:16"),
            "1024x1536",
        )
        self.assertEqual(
            self.mod.GeminiArtist._openai_size_for_aspect_ratio("1:1"),
            "1024x1024",
        )


if __name__ == "__main__":
    unittest.main()

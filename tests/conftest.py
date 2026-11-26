"""测试用的桩。

本地没有 AstrBot 运行时（沙盒里不装），而被测的是 `reference_store` 的
存储逻辑、`artist_tools` 的取值逻辑 —— 这些都不该依赖 AstrBot 本身。
所以这里把 `astrbot.api.Logger` 之类的最小面打桩，**只用真代码、不复制实现**。

踩过的坑：给 `filter.llm_tool` 打桩时如果做成「返回原函数」，被装饰的函数
就还是原来那个，测试会「过」但根本没验证装饰器语义；这里让桩记录被装饰的
名字，测试才有意义。
"""
from __future__ import annotations

import sys
import types


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def _log(self, *args, **kwargs):
            pass

        debug = info = warning = error = exception = _log

        def setLevel(self, *args, **kwargs):
            pass

    api.logger = _Logger()
    api.sp = types.SimpleNamespace()

    event = types.ModuleType("astrbot.api.event")

    class _Filter:
        """记录用法：`@filter.llm_tool(name=...)` 必须能挂上并保留函数名。"""

        def __init__(self):
            self.llm_tools = []

        def llm_tool(self, *args, **kwargs):
            name = kwargs.get("name")

            def wrap(func):
                func.__llm_tool_name__ = name or func.__name__
                self.llm_tools.append(func.__llm_tool_name__)
                return func

            return wrap

    event.filter = _Filter()
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})

    started = types.ModuleType("astrbot.api.star")

    class _Star:
        def __init__(self, context=None):
            self.context = context

    class _StarTools:
        @staticmethod
        def get_data_dir(name):
            raise RuntimeError("测试里不该真的取 data_dir")

    started.Star = _Star
    started.StarTools = _StarTools
    started.register = lambda *a, **k: (lambda cls: cls)
    started.Context = type("Context", (), {})

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.star"] = started

    # main.py 里还有几个只在运行时才用到的导入，测试不碰它们，
    # 但 import 语句得能过。
    for name, attrs in {
        "astrbot.api.all": {},
        "astrbot.api.message_components": {
            "Node": type("Node", (), {}),
            "Plain": type("Plain", (), {}),
            "Image": type("Image", (), {}),
            "Nodes": type("Nodes", (), {}),
            "Reply": type("Reply", (), {}),
            "BaseMessageComponent": type("BaseMessageComponent", (), {}),
        },
        "astrbot.core.star.filter.command": {"GreedyStr": str},
        "astrbot.core.utils.io": {"download_file": lambda *a, **k: None},
        "astrbot.core.utils.astrbot_path": {
            "get_astrbot_data_path": lambda: "/tmp/astrbot-test-data"
        },
    }.items():
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module


_install_astrbot_stubs()

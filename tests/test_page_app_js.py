"""页面脚本的回归测试：**桥接层晚到时必须能自己恢复**。

背景（真机踩过的坑）：面板里打开「画图参考图」，页面能显示、CSS 正常，但每一处
都报「桥接层不可用（AstrBotPluginPage.apiGet 缺失）」。

原因：AstrBot 把桥接 SDK 注入在 `</body>` 之前，也就是页面自己的 `<script>` 之后。
原来 `app.js` 顶部写着 `const bridge = window.AstrBotPluginPage` —— 取值那一刻
SDK 还没执行，bridge 永久是 undefined。CSS 和 HTML 没问题，所以看着像「页面好的、
接口坏了」。

两处修复一起钉住：

1. `index.html` 用 `type="module"` 加载（模块脚本延迟执行）；
2. `app.js` 每次用之前现取桥接层，并短暂等待。

第 2 条是兜底，也是这里测的东西 —— 第 1 条靠 test_page_markup.py 静态检查。
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "pages" / "artist" / "app.js"

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0,
    reason="没有 node，跳过 JS 行为测试",
)


def _run_harness(script_path: Path, bridge_delay_ms: int) -> subprocess.CompletedProcess:
    """在极简 DOM 桩里跑页面脚本，桥接层**故意晚到** bridge_delay_ms 毫秒。"""
    harness = textwrap.dedent(
        """
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[2], 'utf8');

        const nodes = {};
        function makeNode(id) {
          return {
            id, textContent: '', className: '', innerHTML: '', value: '', checked: false,
            disabled: false, files: [], dataset: {},
            classList: { add(){}, remove(){} },
            addEventListener(){}, appendChild(){}, remove(){},
            querySelectorAll(){ return []; }, prepend(){}, click(){},
            childElementCount: 0, lastChild: null,
          };
        }
        global.document = {
          getElementById(id) { return nodes[id] || (nodes[id] = makeNode(id)); },
          createElement() { return makeNode('x'); },
        };
        global.window = { setTimeout, clearTimeout };
        global.window.self = global.window;
        global.window.top = global.window;

        let calls = 0;
        setTimeout(() => {
          global.window.AstrBotPluginPage = {
            ready: async () => {},
            apiGet: async () => { calls++; return { status: 'ok', data: { slots: [] } }; },
            apiPost: async () => ({ status: 'ok', data: {} }),
            upload: async () => ({ status: 'ok', data: { slots: [] } }),
          };
        }, Number(process.argv[3]));

        eval(src);

        setTimeout(() => {
          console.log(JSON.stringify({ calls }));
          process.exit(0);
        }, 3000);
        """
    )
    return subprocess.run(
        ["node", "-e", harness, "harness", str(script_path), str(bridge_delay_ms)],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("delay", [0, 150, 400])
def test_page_recovers_when_bridge_arrives_late(delay):
    """桥接层晚到 0/150/400 毫秒，页面都必须照样把数据读出来。

    这就是真机那个场景：SDK 在 `</body>` 前注入，页面脚本先跑。
    """
    result = _run_harness(APP_JS, delay)
    assert result.returncode == 0, result.stderr[-800:]
    calls = json.loads(result.stdout.strip().splitlines()[-1])["calls"]
    assert calls > 0, f"桥接层晚到 {delay}ms 时页面没恢复（apiGet 一次都没调）"


def test_every_bridge_call_has_a_timeout():
    """桥接 SDK 的请求是「不 resolve 也不 reject」的 Promise。

    父页面不回消息时它就永远悬着 —— 真机症状是标题一直「正在读取…」、
    一个红字都没有。所以 apiGet / apiPost / ready 都必须包超时。
    """
    source = APP_JS.read_text(encoding="utf-8")
    assert "withTimeout(" in source, "没有超时包装函数"
    assert "bridge.apiGet(" in source and "withTimeout(bridge.apiGet(" in source
    assert "bridge.apiPost(" in source and "withTimeout(bridge.apiPost(" in source
    # ready() 同样没有超时，不能裸 await
    assert "await bridge.ready()" not in source, "ready() 必须包超时"
    assert "withTimeout(bridge.ready()" in source


def test_page_does_not_cache_bridge_at_load_time():
    """不能出现「加载时把桥接层存进常量」的写法 —— 那就是这个 bug 的根。

    只做静态检查：如果有人在顶层写了 `const bridge = window.AstrBotPluginPage`，
    前面那些行为测试会通过（因为 index.html 的 module 加载救了它），但兜底就没了，
    下次注入方式一变又会白屏。
    """
    source = APP_JS.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("const bridge =") or stripped.startswith("let bridge ="):
            if "waitForBridge" not in stripped:
                pytest.fail(f"顶层写死了桥接层：{stripped}")


def test_js_syntax_is_valid():
    result = subprocess.run(
        ["node", "--check", str(APP_JS)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

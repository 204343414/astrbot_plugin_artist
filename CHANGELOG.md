# 2.2.3 · 2026-10-25

## 修：`Permission denied to access property "AstrBotPluginPage" on cross-origin object`

2.2.2 之后页面不卡了，但每个请求都报上面这句。**这是我自己引入的**：

上一版为了「更稳」，我在找桥接层时顺手读了 `window.parent?.AstrBotPluginPage`。
但面板给 iframe 的 sandbox 是 `allow-scripts allow-forms allow-downloads`，
**没有 `allow-same-origin`** —— `window.parent` 是跨域对象，**读它的任何属性都会
直接抛 SecurityError**，而不是返回 undefined，可选链 `?.` 也挡不住。

桥接 SDK 是注入到**本窗口**的（它自己用 `window.parent.postMessage` 往外发，
那是允许的跨域通信），页面根本不需要看父窗口。

两处改动：

1. `findBridge()` 只看 `window.AstrBotPluginPage` / `AstrBotPluginView`，
   不再碰 `window.parent`；
2. 顺手去掉 `bridge.ready()` —— 它只在父页面发 `context` 时 resolve，
   等不到就永远悬着，而发请求并不需要它（meme 页面也是直接 `apiGet` 从不调它）。
   少一个「可能永远不回来」的等待，就少一种安静卡死的死法。

> 参考对象就是本仓库里已上线的 meme 页面：它从头到尾没碰过 `window.parent`，
> 这也是它一直没出这个问题的原因。

## 测试

行为测试的 DOM 桩里把 `window.parent` 换成了**读属性就抛 SecurityError 的 Proxy**，
原样复刻真实沙盒。写回 `window.parent?.AstrBotPluginPage` 会有 5 条测试变红。

# 2.2.2 · 2026-10-25

## 修：页面永远停在「正在读取…」，一个红字都不给

2.2.1 修好「桥接层不可用」之后，症状变成标题一直「正在读取…」。

原因是**桥接 SDK 的请求没有超时**：

```js
// plugin_page_bridge.js
pendingRequests.set(requestId, { resolve, reject });
// …父页面不回 `response` 消息，这个 Promise 就永远悬着
```

更绝的是 `bridge.ready()`：它只在父页面发 `context` 时 resolve，同样没有超时。
而上一版我在 `boot()` 里**裸 await** 了它 —— 只要那个 context 没来，整个启动流程
就停在那儿，`ui/state` 一次都不会发出去。于是页面什么都不报，就那么干等着。

三处加固：

1. 所有桥接调用（`apiGet` / `apiPost` / `ready`）**一律包超时**
   （请求 8 秒、ready 2.5 秒），宁可报错也不悬着；
2. `ready()` 超时不再致命，继续往下走 —— 请求自己也有超时兜着；
3. 新增**内联兜底守卫**：一段不依赖任何外部资源的内联脚本，4 秒后若发现入口
   脚本没启动，就在页面上写明「脚本没能启动 / 请硬刷新」或直接贴出报错行号。
   这样即使 `app.js` 整个加载失败（模块求值出错、资源被拦），也不会再出现
   「页面安静地卡住」这种没法排查的状态。

## 测试

新增 2 条：所有桥接调用必须带超时（裸 `await bridge.ready()` 会被判红）、
index.html 必须存在内联兜底守卫。

# 2.2.1 · 2026-10-25

## 修：后台页面能打开，但所有按钮都报「桥接层不可用」

真机症状：面板里打开「画图参考图」，页面显示正常、样式也正常，但每一处都是
**读取参考图失败：桥接层不可用（AstrBotPluginPage.apiGet 缺失）**。

原因不是接口没注册（日志里 `已注册 7 个页面接口` 是正常的），而是**脚本抢跑**：

- AstrBot 把桥接 SDK 注入在 `</body>` 之前，也就是页面自己的 `<script>` **之后**；
- 原来 `app.js` 用普通 `<script src>` 加载，会立刻执行，顶部那行
  `const bridge = window.AstrBotPluginPage` 取到的是 `undefined`，而且**永久**是它；
- CSS 和 HTML 不受影响，所以看起来像「页面好的、接口坏了」，特别容易误判。

两处一起修（缺一不可）：

1. `index.html` 改成 `<script type="module">` —— 模块脚本延迟到文档解析完才执行，
   正好排在桥接 SDK 之后；
2. `app.js` 不再在加载时缓存桥接层，改成**每次用之前现取**并最多等 3 秒
   （`waitForBridge`）。这样即使将来注入位置又变了，页面也能自己恢复。

顺带加了**人话报错**：真拿不到桥接层时，页面上直接写「刷新整个面板 / Ctrl+F5」
或「这个页面必须在面板里打开」，而不是甩一个英文 API 名。

## 测试

新增 12 条，专治「页面能开但一片失灵」这类静默故障：

- 在 Node 里用极简 DOM 桩跑 `app.js`，**故意让桥接层晚到 0/150/400 毫秒**，
  断言页面仍能读到数据（回滚修复会红 —— 实测老写法 apiGet 一次都不调）；
- 静态检查：`app.js` 必须是 `type="module"`、页面不能自己引 `bridge-sdk.js`、
  资源必须是相对路径、`el("id")` 用到的 id 在 HTML 里都必须存在。

# 2.2.0 · 2026-10-25

LLM 现在能自己决定画什么了，参考图也能在后台直接传。

## 新增

- **LLM 自主画图工具**（`@filter.llm_tool`）：
  - `draw_image(prompt, aspect_ratio, use_character, use_base, image_index, reference_bot)`
    —— 模型自己判断该不该画、画什么、什么比例、要不要带参考图，画完**直接发到会话里**；
  - `set_drawing_reference(action, target, image_index)` —— 记住/清除**临时**参考图
    （只影响当前会话，不写配置）；
  - `list_drawing_references()` —— 查当前用得着哪些参考图、立绘配好没有。
- **后台页面「画图参考图」**（插件面板里，`pages/artist/`）：
  - 拖/点上传**角色立绘**和**默认参考图**，带预览、尺寸、大小、更新时间；
  - 有「试画」按钮：用当前配置真调一次画图接口，报错原样显示，
    省得去群里猜是密钥问题还是模型名写错了；
  - 传完**立刻生效**，不用重启插件（立绘是每次画图都重读文件的）。

## 修复

- **`base_reference_image_path` / `enable_base_reference_image` 在配置界面里根本不存在** ——
  代码里有 `config.get`，`_conf_schema.json` 里没有，等于必须手改配置文件。
  现在两个都进了 schema，而且后台页面能直接传图。
- **「默认参考图」开关以前是摆设**：只有那个从来没被调用过的 `gemini_draw` 里认它。
  现在 `/draw` 和 LLM 工具在没有任何参考图时都会用它兜底。
- **参考图路径解析统一**：面板传的优先，其次才看配置里手填的路径。
  两套解析逻辑并存的状态下，症状是「面板里能看到图、画图时却说找不到」。
- **发送失败不再谎报成功**：图画出来了但没发出去时，明确告诉模型「没发出去」，
  不要让它对着一张对方没看到的图点评。

## 移除

- **删掉三个写好了却从没被注册的旧方法**：`gemini_draw`、`manage_drawing_reference`、
  `combine_images_draw`。它们 `yield` 一堆 `event.plain_result`，返回的是
  `AsyncGenerator`，而 `@filter.llm_tool` 要的是字符串 —— 这就是它们一直挂不上
  装饰器的原因（`grep llm_tool main.py` 曾经是 0 匹配）。留着等于两套画图逻辑
  并存，迟早各自漂移，而不一致的那一套会放过去不该放的东西。
  **`/draw` 命令照旧保留，行为不变。**

## 注意

- 升级后请重启一次 AstrBot，日志里应能看到：
  `Added llm tool: draw_image` / `set_drawing_reference` / `list_drawing_references`。
- 想让模型主动画图，需要 AstrBot 的会话 LLM 启用工具调用（函数调用）。

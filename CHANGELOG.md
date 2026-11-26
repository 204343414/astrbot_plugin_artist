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

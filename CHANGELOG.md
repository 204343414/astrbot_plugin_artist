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

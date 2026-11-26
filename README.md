# astrbot-plugin-helloworld

AstrBot 插件模板 / A template plugin for AstrBot plugin feature

> [!NOTE]
> This repo is just a template of [AstrBot](https://github.com/AstrBotDevs/AstrBot) Plugin.
> 
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) is an agentic assistant for both personal and group conversations. It can be deployed across dozens of mainstream instant messaging platforms, including QQ, Telegram, Feishu, DingTalk, Slack, LINE, Discord, Matrix, etc. In addition, it provides a reliable and extensible conversational AI infrastructure for individuals, developers, and teams. Whether you need a personal AI companion, an intelligent customer support agent, an automation assistant, or an enterprise knowledge base, AstrBot enables you to quickly build AI applications directly within your existing messaging workflows.

# Supports

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)

---

# 画图插件（本仓库实际内容）

## 能力

| 用法 | 说明 |
|---|---|
| `/draw 提示词` | 一次性画图，可附图或引用一条带图消息 |
| LLM 工具 `draw_image` | 模型自己决定画什么、什么宽高比、带哪些参考图，画完直接发到会话 |
| LLM 工具 `set_drawing_reference` | 记住/清除**临时**参考图（只影响当前会话，不写配置） |
| LLM 工具 `list_drawing_references` | 查当前参考图状态、立绘配好没有 |
| 后台页面「画图参考图」 | 上传/替换角色立绘与默认参考图，试画一张验证配置 |

宽高比：`auto` / `1:1` / `16:9` / `9:16` / `4:3` / `3:4`（命令与工具一致）。

## 参考图放哪儿

- 面板传的图存在 `data/plugin_data/astrbot_plugin_artist/references/`，
  文件名固定（`character.png` / `base.jpg`…），换图等于换内容不换名字；
- 旧图留在 `references/history/`（最多 10 张），换错了能捡回来；
- 面板传的图**优先于**配置项里手填的路径；
- 传完立刻生效，不需要重启插件。

## 升级后自检

```bash
docker logs --since 3m astrbot | grep -a "Added llm tool"
```

应当看到 `draw_image` / `set_drawing_reference` / `list_drawing_references` 三条。

# 画图参考图（插件后台页面）

AstrBot 4.29+ 的插件页面机制：`<插件目录>/(pages|views)/<名字>/index.html`
会被面板发现，页面前用 `window.AstrBotPluginPage` 桥接层调后端接口。

接口定义在 `web_api.py`，路径是 `/<插件名>/ui/<name>`；页面里写相对名
（`ui/state`）就行，桥接层负责拼前缀和带令牌。

## 两个必须记住的坑

1. **别想让 `<img src="接口路径">` 拿到图。** 桥接层的 `apiGet` 只会按 JSON
   解析响应，二进制拿不到；页面又是嵌在 iframe 里，自己带不了令牌。
   所以缩略图由后端压成 data URI 塞在 JSON 里（`reference_store.thumbnail`）。
2. **上传必须走 `bridge.upload(endpoint, file)`。** 自己拼 `FormData` + fetch
   会被面板的作用域令牌挡住。另外 `upload` 只收两个参数，槽位只能放在
   查询串上（`ui/upload?slot=character`）。

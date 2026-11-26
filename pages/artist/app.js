/* 画图参考图页面。
 *
 * 一切后端访问都走桥接层（window.AstrBotPluginPage）：页面是嵌在面板里的
 * iframe，直连后端会被令牌拦掉，桥接层帮我们把请求带上作用域令牌。
 *
 * 两个坑记在这里，省得下次再踩：
 * 1. 桥接层的 apiGet 只会把响应按 JSON 解析 —— 想让 <img> 直接指到接口是
 *    拿不到图的，所以缩略图由后端压成 data URI 塞在 JSON 里（见 reference_store.py
 *    的 thumbnail 方法）。
 * 2. 上传走 bridge.upload(endpoint, file)：原生 FormData + fetch 会被面板的
 *    作用域令牌挡住，桥接层才有权限。
 */
/* 桥接层**不能**在加载时就取好。
 *
 * AstrBot 把桥接 SDK 注入在 `</body>` 之前，而本文件是页面自己的脚本。
 * 如果这里写 `const bridge = window.AstrBotPluginPage`，取值那一刻 SDK 还没执行，
 * bridge 就永久是 undefined —— 症状正是面板里看到的那一幕：页面能开、样式正常，
 * 但每一处都报「桥接层不可用（AstrBotPluginPage.apiGet 缺失）」。
 *
 * index.html 那边已经改成 `type="module"`（模块脚本延迟执行，通常就没事了），
 * 但**不能只靠这一点**：注入位置将来可能变、SDK 也可能异步补上。所以这里改成
 * 每次用之前现取，并且会短暂等一等。
 */
const BRIDGE_WAIT_MS = 3000;
const BRIDGE_POLL_MS = 50;

/* 面板回调的超时。
 *
 * 桥接 SDK 里的 `makeRequest` 是个**既不 resolve 也不 reject** 的 Promise ——
 * 父页面要是不回 `response` 消息，它就永远悬着。实测症状就是标题一直
 * 「正在读取…」、没有任何红字：看着像卡死，其实是在等一个永远不来的回复。
 * `ready()` 更极端：它要等父页面发 `context`，同样没有超时。
 *
 * 所以这里**每一处等待都必须自带超时**，宁可报错也不要悬着 —— 悬着的时候
 * 用户完全不知道发生了什么，报错至少能给下一步。
 */
const REQUEST_TIMEOUT_MS = 8000;

function withTimeout(promise, ms, what) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${what} 超时（${ms / 1000} 秒没有回应）`)), ms)
    ),
  ]);
}

/* 只读**当前窗口**的全局对象，绝对不要碰 window.parent。
 *
 * 面板给 iframe 的 sandbox 是 `allow-scripts allow-forms allow-downloads`，
 * **没有 allow-same-origin**，所以 `window.parent` 是个跨域对象：连「读一下属性」
 * 都会直接抛 SecurityError，而不是返回 undefined。之前这里写了
 * `window.parent?.AstrBotPluginPage`，可选链也挡不住它，于是整页报
 *
 *   Permission denied to access property "AstrBotPluginPage" on cross-origin object
 *
 * 桥接 SDK 是注入到**本窗口**的（它自己用 window.parent.postMessage 往外发，
 * 那是允许的跨域通信），所以这里只需要看自己。
 */
function findBridge() {
  return window.AstrBotPluginPage || window.AstrBotPluginView || null;
}

/* 等到桥接层出现为止。超时就返回 null，交给调用方给一句能看懂的话。 */
async function waitForBridge() {
  const deadline = Date.now() + BRIDGE_WAIT_MS;
  for (;;) {
    const found = findBridge();
    if (found?.apiGet) return found;
    if (Date.now() >= deadline) return found;
    await new Promise((resolve) => setTimeout(resolve, BRIDGE_POLL_MS));
  }
}

const el = (id) => document.getElementById(id);

function on(id, type, handler) {
  const node = el(id);
  if (!node) {
    console.warn(`[artist page] 跳过 ${id} 的 ${type}：页面上没有这个元素`);
    return null;
  }
  node.addEventListener(type, handler);
  return node;
}

const state = { slots: [], ready: false };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/* 桥接层不同版本会直接返回 JSON、包一层 data，或包两层 —— 统一拆开。 */
function unwrap(response) {
  let value = response;
  for (let depth = 0; depth < 3; depth += 1) {
    if (!value || typeof value !== "object") break;
    if (value.status === "error") throw new Error(value.message || "接口返回错误");
    if (value.ok === false) throw new Error(value.message || "接口返回错误");
    if (Object.prototype.hasOwnProperty.call(value, "data") && Object.keys(value).length <= 2) {
      value = value.data;
      continue;
    }
    break;
  }
  return value;
}

async function apiGet(name, params) {
  const bridge = await waitForBridge();
  if (!bridge?.apiGet) throw new Error(bridgeHint());
  return unwrap(
    await withTimeout(bridge.apiGet(name, params || {}), REQUEST_TIMEOUT_MS, `读取 ${name}`)
  );
}

async function apiPost(name, body) {
  const bridge = await waitForBridge();
  if (!bridge?.apiPost) throw new Error(bridgeHint());
  return unwrap(
    await withTimeout(bridge.apiPost(name, body || {}), REQUEST_TIMEOUT_MS, `提交 ${name}`)
  );
}

/* 一句话说清「为什么不能用」以及「怎么自救」，别只丢一个英文 API 名。 */
function bridgeHint() {
  const inIframe = window.self !== window.top;
  return (
    "拿不到 AstrBot 的页面桥接层（SDK 没注入或被拦）。\n" +
    (inIframe
      ? "建议：刷新整个面板页面（不是只刷新这一块）；还不行就硬刷新 Ctrl+F5。"
      : "这个页面被单独打开了，它必须在 AstrBot 面板里打开才有权限。")
  );
}

function fmtSize(bytes) {
  const n = Number(bytes || 0);
  if (!n) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function banner(text, kind) {
  const node = el("banner");
  if (!node) return;
  if (!text) {
    node.classList.add("hidden");
    node.textContent = "";
    return;
  }
  node.className = `banner ${kind || ""}`.trim();
  node.textContent = text;
}

/* ---------------- 槽位渲染 ---------------- */

function slotCard(slot) {
  const hasImage = slot.exists && slot.thumb;
  const badge = slot.managed
    ? '<span class="badge ok">面板管理</span>'
    : slot.configuredPath
      ? '<span class="badge warn">配置指向别处</span>'
      : '<span class="badge">未设置</span>';

  const preview = hasImage
    ? `<img class="preview" src="${slot.thumb}" alt="${escapeHtml(slot.title)} 预览" />`
    : `<div class="preview empty"><span>还没有图</span></div>`;

  const meta = slot.exists
    ? `<dl class="meta">
         <div><dt>尺寸</dt><dd>${slot.width || "?"} × ${slot.height || "?"}</dd></div>
         <div><dt>大小</dt><dd>${fmtSize(slot.size)}</dd></div>
         <div><dt>更新于</dt><dd>${escapeHtml(slot.mtime || "—")}</dd></div>
         <div><dt>配置项</dt><dd><code>${escapeHtml(slot.configKey)}</code></dd></div>
       </dl>
       <p class="path" title="${escapeHtml(slot.path)}">${escapeHtml(slot.path)}</p>`
    : `<p class="msg">${escapeHtml(slot.hint)}</p>`;

  /* 配置指向别处时给一句解释性的提示。用户最容易在这里犯迷糊：
   * 「我明明传了图怎么没生效」——其实是配置里还指着老文件。 */
  const managedNote = !slot.managed && slot.configuredPath
    ? `<p class="msg warn">配置里现在指的是 <code>${escapeHtml(slot.configuredPath)}</code>。
       你在下面传一张，就会改用面板管理的那张。</p>`
    : "";

  return `
    <article class="card slot" data-slot="${escapeHtml(slot.slot)}">
      <div class="card-head">
        <h2>${escapeHtml(slot.title)} ${badge}</h2>
      </div>
      <p class="msg">${escapeHtml(slot.hint)}</p>
      ${managedNote}
      <div class="slot-body">
        ${preview}
        <div class="slot-info">
          ${meta}
          <div class="slot-actions">
            <input type="file" accept="image/png,image/jpeg,image/webp" hidden
                   id="file-${escapeHtml(slot.slot)}" data-slot="${escapeHtml(slot.slot)}" />
            <button class="primary" data-act="pick" data-slot="${escapeHtml(slot.slot)}">上传 / 替换</button>
            ${slot.managed ? `<button class="danger" data-act="clear" data-slot="${escapeHtml(slot.slot)}">清除</button>` : ""}
          </div>
          <p class="msg tiny">PNG / JPEG / WebP，12 MB 以内。传完立刻生效，不用重启插件。</p>
        </div>
      </div>
    </article>`;
}

function renderSlots(slots) {
  state.slots = slots || [];
  const host = el("slots");
  if (!host) return;
  host.innerHTML = state.slots.map(slotCard).join("");
  host.querySelectorAll('button[data-act="pick"]').forEach((node) => {
    node.addEventListener("click", () => {
      const input = el(`file-${node.dataset.slot}`);
      if (input) input.click();
    });
  });
  host.querySelectorAll('input[type="file"]').forEach((input) => {
    input.addEventListener("change", () => uploadSlot(input.dataset.slot, input));
  });
  host.querySelectorAll('button[data-act="clear"]').forEach((node) => {
    node.addEventListener("click", () => clearSlot(node.dataset.slot));
  });
}

/* ---------------- 数据加载 ---------------- */

async function loadState() {
  const subtitle = el("subtitle");
  try {
    const data = unwrap(await apiGet("ui/state", {}));
    renderSlots(data?.slots || []);
    if (subtitle) {
      const ready = (data?.slots || []).filter((s) => s.exists).length;
      subtitle.textContent = `2 个槽位，已有 ${ready} 张图。换图立刻生效，不用重启插件。`;
    }
    state.ready = true;
    banner("");
  } catch (error) {
    if (subtitle) subtitle.textContent = "读取失败";
    banner(`读取参考图失败：${error.message}`, "error");
  }
}

async function loadInfo() {
  const grid = el("infoGrid");
  const msg = el("infoMsg");
  try {
    const data = unwrap(await apiGet("ui/info", {}));
    const rows = [];
    rows.push(["画图接口", data.apiType || "—"]);
    rows.push(["模型", data.model || "—"]);
    rows.push(["密钥数量", data.apiKeys ?? 0]);
    rows.push(["今日额度", data.dailyLimit > 0 ? `${data.usedToday || 0} / ${data.dailyLimit}` : "不限制"]);
    rows.push(["群日预算", data.groupBudget > 0 ? `¥${data.groupBudget}` : "不限制"]);
    rows.push(["提示词审核", data.promptReview ? "开" : "关"]);
    rows.push(["角色立绘（配置项）", data.characterPath || "未填"]);
    rows.push(["默认参考图（配置项）", data.basePath || "未填"]);
    if (grid) {
      grid.innerHTML = rows
        .map(([k, v]) => `<div class="info-cell"><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd></div>`)
        .join("");
    }
    if (msg) {
      msg.textContent = data.apiKeys > 0
        ? "配置看起来是齐的。画不出来时先点下面的「试画」看具体报错。"
        : "没有配置画图 API 密钥 —— 群里和面板都画不出图。";
      msg.className = data.apiKeys > 0 ? "msg" : "msg warn";
    }
  } catch (error) {
    if (msg) {
      msg.textContent = `读取失败：${error.message}`;
      msg.className = "msg error";
    }
  }
}

/* ---------------- 上传 / 清除 ---------------- */

async function uploadSlot(slot, input) {
  const file = input?.files?.[0];
  if (!file) return;
  const limit = 12 * 1024 * 1024;
  if (file.size > limit) {
    banner(`这张图 ${fmtSize(file.size)}，超过 12 MB，先压一下再传。`, "error");
    input.value = "";
    return;
  }
  banner(`正在上传 ${file.name}…`);
  try {
    const bridge = await waitForBridge();
    if (!bridge?.upload) throw new Error(bridgeHint());
    /* endpoint 带查询串（?slot=xxx）：桥接层会把 query 原样拼到请求地址上，
     * 后端因此能从 query 里读到槽位。表单里再塞一份是为了兼容老版本。 */
    const endpoint = `ui/upload?slot=${encodeURIComponent(slot)}`;
    const result = unwrap(await bridge.upload(endpoint, file));
    renderSlots(result?.slots || []);
    banner(`已更新「${slot === "character" ? "角色立绘" : "默认参考图"}」。下一张图就用新的了。`, "ok");
    await loadInfo();
  } catch (error) {
    banner(`上传失败：${error.message}`, "error");
  } finally {
    if (input) input.value = "";
  }
}

async function clearSlot(slot) {
  const label = slot === "character" ? "角色立绘" : "默认参考图";
  if (!window.confirm(`清除面板上传的「${label}」？旧图还在 references/history 里。`)) return;
  try {
    const data = unwrap(await apiPost("ui/clear", { slot }));
    renderSlots(data?.state?.slots || []);
    banner(data?.removed ? `已清除「${label}」。` : `「${label}」本来就没上传过。`, "ok");
    await loadInfo();
  } catch (error) {
    banner(`清除失败：${error.message}`, "error");
  }
}

/* ---------------- 试画 ---------------- */

async function runTest() {
  const promptInput = el("testPrompt");
  const ratioInput = el("testRatio");
  const out = el("testOut");
  const msg = el("testMsg");
  const prompt = (promptInput?.value || "").trim();
  if (!prompt) {
    if (msg) {
      msg.textContent = "先写点什么再试。";
      msg.className = "msg warn";
    }
    return;
  }
  const button = el("testRun");
  if (button) button.disabled = true;
  if (msg) {
    msg.textContent = "正在画，通常十几秒到一分钟…";
    msg.className = "msg";
  }
  if (out) out.innerHTML = "";
  try {
    const data = unwrap(await apiPost("ui/test", {
      prompt,
      aspect_ratio: ratioInput?.value || "auto",
    }));
    if (out && data?.thumb) {
      out.innerHTML = `<img class="preview" src="${data.thumb}" alt="试画结果" />
        <p class="msg tiny">宽高比 ${escapeHtml(data.aspectRatio || "auto")} · 耗时 ${escapeHtml(data.elapsed || "?")} 秒</p>`;
    }
    if (msg) {
      msg.textContent = "画出来了。上面的图和群里拿到的完全一致（同一条生成链路）。";
      msg.className = "msg ok";
    }
  } catch (error) {
    if (msg) {
      msg.textContent = error.message;
      msg.className = "msg error";
    }
  } finally {
    if (button) button.disabled = false;
  }
}

/* ---------------- 启动 ---------------- */

on("refresh", "click", () => {
  loadState();
  loadInfo();
});
on("testRun", "click", runTest);
on("testPrompt", "keydown", (event) => {
  if (event.key === "Enter") runTest();
});

async function boot() {
  // 给内联守卫打个标记：它据此判断「脚本到底跑起来了没有」。
  window.__artistPageBooted = true;
  // 注意：**不调 bridge.ready()**。
  //
  // 它只等父页面那条 `context` 消息，来了才 resolve；等不到就永远悬着。
  // 而发请求根本不需要它（meme 页面也是直接 apiGet，从来不调 ready）。
  // 少一个“可能永远不回来”的等待，就少一种「页面安静卡住」的死法。
  const bridge = await waitForBridge();
  if (!bridge?.apiGet) {
    // 桥接层真的没有：把原因写在页面上，别让用户对着三个空盒子猜。
    banner(bridgeHint(), "error");
    const subtitle = el("subtitle");
    if (subtitle) subtitle.textContent = "读不到数据";
    const msg = el("infoMsg");
    if (msg) {
      msg.textContent = "桥接层未就绪，无法读取插件状态。";
      msg.className = "msg error";
    }
    return;
  }
  await loadState();
  await loadInfo();
}

boot();

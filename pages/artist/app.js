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
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
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
  if (!bridge?.apiGet) throw new Error("桥接层不可用（AstrBotPluginPage.apiGet 缺失）");
  return unwrap(await bridge.apiGet(name, params || {}));
}

async function apiPost(name, body) {
  if (!bridge?.apiPost) throw new Error("桥接层不可用（AstrBotPluginPage.apiPost 缺失）");
  return unwrap(await bridge.apiPost(name, body || {}));
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
    if (!bridge?.upload) throw new Error("桥接层不支持上传（AstrBotPluginPage.upload 缺失）");
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
  try {
    if (bridge?.ready) await bridge.ready();
  } catch (error) {
    console.warn("[artist page] 桥接层 ready 失败", error);
  }
  await loadState();
  await loadInfo();
}

boot();

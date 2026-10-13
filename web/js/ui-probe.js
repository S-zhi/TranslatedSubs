/* 下载测试页：独立探测单个 URL 是否可解析、可下载。
 *
 * 所有 probe 调用都会落库到 probe_records，本视图额外提供：
 * - 测试历史列表（按时间倒序，最近 50 条）
 * - 点击历史项把 URL 回填到输入框
 * - 单条删除 / 一键清空 / 手动刷新
 */

import { $, el, shortUrl } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";
import { LANG_LABEL } from "./constants.js";
import { setView } from "./store.js";

const URL_RE = /^https?:\/\/.+/i;

function isValidUrl(url) {
  // 判断输入是否是后端探针可接受的 http(s) 页面地址。
  return URL_RE.test(url);
}

function formatLang(code) {
  if (!code) return null;
  return LANG_LABEL[code] ? `${LANG_LABEL[code]} (${code})` : code;
}

function formatFilesize(bytes) {
  if (!bytes || !Number.isFinite(bytes)) return "";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function qualityLabel(q) {
  const map = {
    best: "原画最佳 (best)",
    "1080p": "1080P 高清",
    "720p": "720P 标清",
    "480p": "480P 省流",
    "360p": "360P 极速",
    audio_only: "纯音频流",
  };
  return map[q] || q;
}

function formatDuration(seconds) {
  // 把秒数格式化为紧凑的时长文案。
  if (!Number.isFinite(seconds)) return null;
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatTimestamp(ms) {
  // 把 epoch 毫秒格式化为本地时区的紧凑时间。
  if (!ms || !Number.isFinite(ms)) return "";
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  );
}

function setHint(bar, hint, message, isError = false) {
  // 同步输入栏提示文案和错误样式。
  bar.classList.toggle("is-error", isError);
  hint.classList.toggle("is-error", isError);
  hint.textContent = message;
}

function metaItem(label, value) {
  // 构造一个结果元信息项。
  const item = el("div", "probe-result__meta-item");
  const k = el("span");
  const v = el("strong");
  k.textContent = label;
  v.textContent = value;
  item.append(k, v);
  return item;
}

function renderChecking(resultEl) {
  // 渲染测试中的状态。
  resultEl.className = "probe-result is-checking";
  resultEl.innerHTML = `
    <div class="probe-result__icon"><i class="ph ph-spinner-gap" aria-hidden="true"></i></div>
    <div class="probe-result__body">
      <div class="probe-result__title">测试中</div>
      <div class="probe-result__desc">正在向后端探针确认链接。</div>
    </div>`;
}

function renderResult(resultEl, result, url) {
  // 渲染探针成功或失败的最终结果。
  resultEl.className = `probe-result ${result.ok ? "is-ok" : "is-fail"}`;
  resultEl.replaceChildren();

  const icon = el("div", "probe-result__icon");
  icon.innerHTML = `<i class="ph ${result.ok ? "ph-check-circle" : "ph-warning-circle"}" aria-hidden="true"></i>`;

  const body = el("div", "probe-result__body");

  // 封面预览（如有）
  if (result.thumbnail) {
    const thumb = el("div", "probe-result__thumb");
    const img = el("img");
    img.src = result.thumbnail;
    img.alt = result.title || "视频封面";
    img.loading = "lazy";
    thumb.append(img);
    body.append(thumb);
  }

  const title = el("div", "probe-result__title");
  title.textContent = result.ok ? "可以下载" : "暂时不可下载";
  const desc = el("div", "probe-result__desc");
  desc.textContent = result.ok
    ? (result.title || shortUrl(result.webpageUrl || url))
    : (result.reason || result.detail || "yt-dlp 未能确认这个链接");
  body.append(title, desc);

  const meta = el("div", "probe-result__meta");
  const duration = formatDuration(result.duration);
  const webpage = result.webpageUrl || url;
  meta.append(metaItem("链接", shortUrl(webpage)));
  if (result.uploader) meta.append(metaItem("作者 / 频道", result.uploader));
  if (result.extractor) meta.append(metaItem("站点解析器", result.extractor));
  if (duration) meta.append(metaItem("时长", duration));
  if (result.language) meta.append(metaItem("推测语言", formatLang(result.language)));
  if (result.ok) meta.append(metaItem("格式数量", String(result.formatsCount || 0)));
  if (!result.ok && result.detail && result.detail !== result.reason) {
    meta.append(metaItem("详情", result.detail));
  }
  body.append(meta);

  // 展现支持的清晰度标签（如 1080p, 720p, 480p, 纯音频）
  if (result.ok && Array.isArray(result.availableQualities) && result.availableQualities.length > 0) {
    const qualBox = el("div", "probe-result__qualities");
    const qualTitle = el("div", "probe-result__section-title");
    qualTitle.innerHTML = `<i class="ph ph-video" aria-hidden="true"></i><span>当前支持的画质策略</span>`;
    const qualTags = el("div", "probe-result__tags");
    result.availableQualities.forEach((q) => {
      const tag = el("span", `probe-tag ${q === "1080p" || q === "best" ? "is-highlight" : ""}`);
      tag.textContent = qualityLabel(q);
      qualTags.append(tag);
    });
    qualBox.append(qualTitle, qualTags);
    body.append(qualBox);
  }

  // 展现代表性音视频流明细
  if (result.ok && Array.isArray(result.formats) && result.formats.length > 0) {
    const formatsBox = el("div", "probe-result__formats");
    const fTitle = el("div", "probe-result__section-title");
    fTitle.innerHTML = `<i class="ph ph-list-dashes" aria-hidden="true"></i><span>可用流规格明细</span>`;
    const table = el("div", "probe-formats-table");
    result.formats.forEach((f) => {
      const row = el("div", "probe-format-row");
      const resCol = el("span", "probe-format-col--res");
      resCol.textContent = f.resolution || "视频";
      const extCol = el("span", "probe-format-col--ext");
      extCol.textContent = (f.ext || "").toUpperCase();
      const codecCol = el("span", "probe-format-col--codec");
      const codecs = [f.vcodec, f.acodec].filter(Boolean).join(" / ") || (f.note || "默认编码");
      codecCol.textContent = codecs;
      const sizeCol = el("span", "probe-format-col--size");
      const sizeText = f.filesize ? formatFilesize(f.filesize) : (f.tbr ? `${f.tbr} kbps` : "");
      sizeCol.textContent = sizeText;

      row.append(resCol, extCol, codecCol, sizeCol);
      table.append(row);
    });
    formatsBox.append(fTitle, table);
    body.append(formatsBox);
  }

  // 操作按钮：一键带入任务控制台
  if (result.ok) {
    const actionBox = el("div", "probe-result__actions");
    const createBtn = el("button", "btn btn--primary btn--sm");
    createBtn.type = "button";
    createBtn.innerHTML = `<i class="ph ph-plus-circle" aria-hidden="true"></i><span>以此链接创建任务</span>`;
    createBtn.addEventListener("click", () => {
      setView("tasks");
      setTimeout(() => {
        const urlEl = $("#url");
        if (urlEl) {
          urlEl.value = result.webpageUrl || url;
          urlEl.dispatchEvent(new Event("input", { bubbles: true }));
          urlEl.focus();
        }
      }, 50);
    });
    actionBox.append(createBtn);
    body.append(actionBox);
  }

  resultEl.append(icon, body);
}

async function runProbe(url, refs) {
  // 执行一次探针请求，并把结果同步到页面。
  refs.button.disabled = true;
  setHint(refs.bar, refs.hint, "正在测试链接...");
  renderChecking(refs.result);
  let ok = false;
  try {
    const result = await Api.probeVideo(url);
    ok = result.ok;
    setHint(
      refs.bar,
      refs.hint,
      result.ok ? "测试通过，可以加入任务队列" : "测试失败，请检查链接或 cookies",
      !result.ok,
    );
    renderResult(refs.result, result, url);
  } catch (err) {
    const result = {
      ok: false,
      reason: err.message || "测试失败",
      detail: null,
    };
    setHint(refs.bar, refs.hint, result.reason, true);
    renderResult(refs.result, result, url);
  } finally {
    refs.button.disabled = false;
  }
  // 不管成功失败都刷新历史：让"刚才测过什么"立刻可见
  if (refs.history) {
    refs.history.refresh().catch(() => {});
  }
  return ok;
}

/* ---------------- 历史记录渲染 ---------------- */

function renderHistoryItem(rec, refs) {
  // 单条历史项：左侧是状态徽标 + 元信息，右侧是回填 / 删除按钮。
  const li = el("li", `probe-history__item ${rec.ok ? "is-ok" : "is-fail"}`);
  li.dataset.id = rec.id;

  const head = el("div", "probe-history__item-head");
  const badge = el("span", `probe-history__badge ${rec.ok ? "is-ok" : "is-fail"}`);
  badge.textContent = rec.ok ? "可下载" : "不可用";
  const time = el("span", "probe-history__time");
  time.textContent = formatTimestamp(rec.createdAt);
  head.append(badge, time);

  const url = el("div", "probe-history__url");
  url.textContent = rec.webpageUrl || rec.url;
  url.title = rec.webpageUrl || rec.url;

  const summary = el("div", "probe-history__summary");
  if (rec.ok) {
    if (rec.title) summary.textContent = rec.title;
    const dur = formatDuration(rec.duration);
    const parts = [];
    if (dur) parts.push(`时长 ${dur}`);
    if (rec.extractor) parts.push(rec.extractor);
    if (rec.language) parts.push(`语言 ${LANG_LABEL[rec.language] || rec.language}`);
    if (rec.formatsCount) parts.push(`${rec.formatsCount} 种格式`);
    if (parts.length) summary.textContent = `${summary.textContent} · ${parts.join(" · ")}`;
  } else {
    summary.textContent = rec.reason || rec.detail || "yt-dlp 未能确认这个链接";
    if (rec.detail && rec.detail !== rec.reason) {
      summary.textContent = `${summary.textContent} · ${rec.detail}`;
    }
  }

  const actions = el("div", "probe-history__actions-row");
  const retry = el("button", "btn btn--ghost btn--sm");
  retry.type = "button";
  retry.innerHTML = `<i class="ph ph-arrow-u-up-left" aria-hidden="true"></i><span>回填</span>`;
  retry.addEventListener("click", () => {
    refs.input.value = rec.webpageUrl || rec.url;
    refs.input.focus();
    setHint(refs.bar, refs.hint, "按开始测试确认可下载性");
  });
  const remove = el("button", "btn btn--ghost btn--sm probe-history__remove");
  remove.type = "button";
  remove.title = "删除这条历史";
  remove.innerHTML = `<i class="ph ph-x" aria-hidden="true"></i>`;
  remove.addEventListener("click", async () => {
    if (!confirm("确认删除这条测试历史？")) return;
    try {
      await Api.deleteProbeRecord(rec.id);
      toast("已删除", "ph-trash");
      refs.refresh().catch(() => {});
    } catch (e) {
      toast(e?.message || "删除失败", "ph-warning-circle");
    }
  });
  actions.append(retry, remove);

  li.append(head, url, summary, actions);
  return li;
}

function createHistoryController(refs) {
  // 封装历史记录的加载 / 渲染，避免把状态散在闭包外。
  const list = refs.historyList;
  const hint = refs.historyHint;
  const refreshBtn = refs.historyRefresh;
  const clearBtn = refs.historyClear;

  let busy = false;

  async function refresh() {
    if (busy) return;
    busy = true;
    refreshBtn.disabled = true;
    try {
      const records = await Api.listProbeRecords(50);
      list.replaceChildren();
      if (!records || records.length === 0) {
        hint.textContent = "还没有测试记录，测试过的链接会自动出现在这里。";
        hint.classList.remove("is-error");
        return;
      }
      hint.textContent = `共 ${records.length} 条，按时间倒序`;
      hint.classList.remove("is-error");
      const frag = document.createDocumentFragment();
      records.forEach((r) => frag.appendChild(renderHistoryItem(r, refs)));
      list.appendChild(frag);
    } catch (e) {
      hint.textContent = e?.message || "加载历史失败";
      hint.classList.add("is-error");
    } finally {
      refreshBtn.disabled = false;
      busy = false;
    }
  }

  refreshBtn.addEventListener("click", () => { refresh().catch(() => {}); });

  clearBtn.addEventListener("click", async () => {
    if (!confirm("确认清空所有下载测试历史？此操作不可撤销。")) return;
    try {
      const res = await Api.clearProbeRecords();
      const n = res && Number.isFinite(res.deleted) ? res.deleted : 0;
      toast(`已清空 ${n} 条历史`, "ph-trash");
      await refresh();
    } catch (e) {
      toast(e?.message || "清空失败", "ph-warning-circle");
    }
  });

  return { refresh };
}

export function initProbe() {
  // 初始化下载测试页表单交互。
  const form = $("#probeForm");
  const input = $("#probeUrl");
  const bar = $("#probeBar");
  const hint = $("#probeHint");
  const button = $("#probeBtn");
  const result = $("#probeResult");

  // 历史记录相关的 DOM 节点
  const historyList = $("#probeHistoryList");
  const historyHint = $("#probeHistoryHint");
  const historyRefresh = $("#probeHistoryRefresh");
  const historyClear = $("#probeHistoryClear");

  const refs = { bar, hint, button, result, input,
                 historyList, historyHint, historyRefresh, historyClear };

  const history = createHistoryController(refs);
  refs.history = history;
  // 启动时尝试拉一次历史：失败（后端没启 / mock 关闭）也不阻塞测试功能
  history.refresh().catch(() => {});

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (!url || !isValidUrl(url)) {
      setHint(bar, hint, "请输入有效的视频链接（以 http(s):// 开头）", true);
      input.focus();
      return;
    }
    runProbe(url, refs);
  });

  input.addEventListener("input", () => {
    if (!input.value.trim()) {
      setHint(bar, hint, "等待输入链接");
    } else {
      setHint(bar, hint, "按开始测试确认可下载性");
    }
  });
}

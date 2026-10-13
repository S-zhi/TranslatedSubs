/* 字幕编辑：左侧任务选择 + 原文/译文双轨对照编辑。
 * 数据走 Api.getSubtitles / saveSubtitles / reburnSubtitles。
 */

import { LANG_LABEL } from "./constants.js";
import { $, el, escapeHtml } from "./utils.js";
import { state, subscribe } from "./store.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";

let listEl, hintEl, titleEl, subEl, actionsEl, versionInput;
let subsEl;
let addBtn, saveBtn, reburnBtn;
let railEl, railToggle;

const EDITOR_RAIL_COLLAPSED_KEY = "subtrans_editor_rail_collapsed_v1";

let currentTaskId = null;
let currentDoc = null;        // { taskId, title, burn, hasOriginal, hasTranslated, original, translated }
let dirty = false;

export function initEditor() {
  listEl = $("#editorList");
  hintEl = $("#editorHint");
  railEl = document.querySelector(".editor__rail");
  railToggle = $("#editorRailToggle");
  titleEl = $("#editorTitle");
  subEl = $("#editorSub");
  actionsEl = $("#editorActions");
  versionInput = $("#editorVersion");
  subsEl = $("#editorSubs");
  addBtn = $("#editorAdd");
  saveBtn = $("#editorSave");
  reburnBtn = $("#editorReburn");
  addBtn.addEventListener("click", () => onAdd());
  saveBtn.addEventListener("click", () => onSave());
  reburnBtn.addEventListener("click", () => onReburn());
  railToggle.addEventListener("click", toggleRail);
  setRailCollapsed(readRailCollapsed());

  subscribe(renderIfActive);
  renderIfActive();
}

function readRailCollapsed() {
  try { return window.localStorage.getItem(EDITOR_RAIL_COLLAPSED_KEY) === "1"; } catch (_) { return false; }
}

function setRailCollapsed(collapsed) {
  const editor = railEl?.closest(".editor");
  if (!editor || !railToggle) return;
  editor.classList.toggle("is-rail-collapsed", collapsed);
  railToggle.setAttribute("aria-expanded", String(!collapsed));
  railToggle.title = collapsed ? "展开任务面板" : "收起任务面板";
  railToggle.querySelector("i").className = `ph ${collapsed ? "ph-caret-right" : "ph-caret-left"}`;
  const label = collapsed ? "展开任务面板" : "收起任务面板";
  railToggle.querySelector(".sr-only").textContent = label;
}

function toggleRail() {
  const collapsed = !railEl.closest(".editor").classList.contains("is-rail-collapsed");
  setRailCollapsed(collapsed);
  try { window.localStorage.setItem(EDITOR_RAIL_COLLAPSED_KEY, collapsed ? "1" : "0"); } catch (_) {}
}

function renderIfActive() {
  if (state.view !== "editor") return;
  renderList();
  if (currentTaskId) {
    // 任务可能已被删/重试：始终以 state 为准同步一次
    const t = state.tasks.find((x) => x.id === currentTaskId);
    if (t) {
      titleEl.textContent = t.title || "字幕编辑";
    }
  }
}

/* ---------- 左侧任务列 ---------- */

function renderList() {
  const candidates = state.tasks.filter((t) => t.status === "SUCCESS" && t.needSubtitle !== false);
  listEl.replaceChildren();
  if (candidates.length === 0) {
    hintEl.textContent = "完成一个需要字幕的任务后会自动出现在这里";
    const e = el("div", "state");
    e.innerHTML = `
      <div class="state__icon"><i class="ph ph-text-aa" aria-hidden="true"></i></div>
      <div class="state__title">还没有可编辑的任务</div>
      <div class="state__desc">先在「任务」里跑一次完整流水线，生成 original.srt / translated.srt 后再回来。</div>`;
    listEl.append(e);
    actionsEl.hidden = true;
    renderSubsEmpty("未选择任务");
    return;
  }
  hintEl.textContent = "共 " + candidates.length + " 个可编辑任务";

  // 如果当前选中的任务已经不在列表里，重置
  if (currentTaskId && !candidates.some((t) => t.id === currentTaskId)) {
    currentTaskId = null;
    currentDoc = null;
  }
  if (!currentTaskId) currentTaskId = candidates[0].id;

  for (const t of candidates) {
    const item = el("button", "editem" + (t.id === currentTaskId ? " is-active" : ""));
    item.type = "button";
    item.dataset.id = t.id;
    item.innerHTML = `
      <span class="editem__thumb" aria-hidden="true"><i class="ph ph-text-aa"></i></span>
      <span class="editem__body">
        <span class="editem__title">${escapeHtml(t.title || "成品视频")}</span>
        <span class="editem__meta">${LANG_LABEL[t.targetLang] || t.targetLang} · ${t.burn === "hard" ? "硬字幕" : "软字幕（可开关）"}</span>
      </span>`;
    item.addEventListener("click", () => selectTask(t.id));
    listEl.append(item);
  }

  // 只在选中变化时拉数据。
  ensureLoaded();
}

function selectTask(id) {
  if (id === currentTaskId) return;
  if (dirty && !confirm("当前修改尚未保存，切换任务会丢失。确定继续？")) return;
  currentTaskId = id;
  dirty = false;
  currentDoc = null; // 强制重拉
  versionInput.value = "";
  renderList();
}

/* ---------- 数据加载 ---------- */

async function ensureLoaded() {
  if (!currentTaskId) return;
  if (currentDoc && currentDoc.taskId === currentTaskId) {
    renderMain();
    return;
  }
  try {
    const doc = await Api.getSubtitles(currentTaskId);
    currentDoc = doc;
    renderMain();
  } catch (e) {
    toast(e.message || "加载字幕失败", "ph-warning-circle");
    renderSubsEmpty(e.message || "无法加载字幕");
  }
}

function renderMain() {
  if (!currentDoc) return;
  const t = state.tasks.find((x) => x.id === currentTaskId);
  titleEl.textContent = t ? (t.title || "字幕编辑") : "字幕编辑";
  const burn = t ? (t.burn === "hard" ? "硬字幕" : "软字幕（可开关）") : "";
  subEl.textContent = `烧录方式：${burn} · 原文与译文对照编辑 · 共 ${entryCount()} 条`;
  actionsEl.hidden = false;
  renderSubs();
}

/* ---------- 字幕条目 ---------- */

function editableLocales() {
  if (!currentDoc) return [];
  return ["original", "translated"].filter((locale) => {
    const available = locale === "original" ? currentDoc.hasOriginal : currentDoc.hasTranslated;
    return available || entriesFor(locale).length > 0;
  });
}

function entriesFor(locale) {
  return currentDoc?.[locale] || [];
}

function entryCount() {
  return Math.max(...editableLocales().map((locale) => entriesFor(locale).length), 0);
}

function entryAt(locale, index) {
  return entriesFor(locale)[index] || null;
}

function renderSubsEmpty(msg) {
  subsEl.replaceChildren();
  const e = el("div", "state");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ph-text-aa" aria-hidden="true"></i></div>
    <div class="state__title">${escapeHtml(msg || "没有字幕")}</div>`;
  subsEl.append(e);
}

function renderSubs() {
  subsEl.replaceChildren();
  const count = entryCount();
  if (count === 0) {
    renderSubsEmpty("没有可编辑的字幕");
    return;
  }
  for (let i = 0; i < count; i++) {
    subsEl.append(buildRow(i));
  }
}

function buildRow(i) {
  const original = entryAt("original", i);
  const translated = entryAt("translated", i);
  const timing = translated || original;
  if (!timing) return el("div");

  const row = el("div", "subrow");
  row.dataset.index = String(i);
  row.innerHTML = `
    <div class="subrow__head">
      <span class="subrow__num num">${i + 1}</span>
      <div class="subrow__times">
        <input class="subrow__time num" data-field="start" type="text" inputmode="decimal" value="${fmtTime(timing.start)}" aria-label="第 ${i + 1} 条字幕开始时间" />
        <span class="subrow__arrow" aria-hidden="true">→</span>
        <input class="subrow__time num" data-field="end" type="text" inputmode="decimal" value="${fmtTime(timing.end)}" aria-label="第 ${i + 1} 条字幕结束时间" />
      </div>
      <div class="subrow__ops">
        <button class="iconbtn" type="button" data-op="merge-next" title="与下一条合并" aria-label="将第 ${i + 1} 条字幕与下一条合并"><i class="ph ph-arrows-in-line-vertical"></i></button>
        <button class="iconbtn" type="button" data-op="split" title="从中间拆成两条" aria-label="将第 ${i + 1} 条字幕拆成两条"><i class="ph ph-scissors"></i></button>
        <button class="iconbtn iconbtn--danger" type="button" data-op="delete" title="删除该条" aria-label="删除第 ${i + 1} 条字幕"><i class="ph ph-trash"></i></button>
      </div>
    </div>
    <div class="subrow__languages">
      ${languageField("original", "原文", i, !original)}
      ${languageField("translated", "译文", i, !translated)}
    </div>`;

  const startInput = row.querySelector('[data-field="start"]');
  const endInput = row.querySelector('[data-field="end"]');
  const originalText = row.querySelector('[data-locale="original"]');
  const translatedText = row.querySelector('[data-locale="translated"]');
  if (originalText) originalText.value = original?.text || "";
  if (translatedText) translatedText.value = translated?.text || "";
  row.querySelectorAll(".subrow__text").forEach((textArea) => {
    resizeTextarea(textArea);
    textArea.addEventListener("input", () => {
      resizeTextarea(textArea);
      const locale = textArea.dataset.locale;
      const entry = entryAt(locale, i);
      if (!entry) return;
      entriesFor(locale)[i] = { ...entry, text: textArea.value };
      markDirty();
    });
  });

  const updateTiming = () => {
    const start = parseTime(startInput.value);
    const end = parseTime(endInput.value);
    editableLocales().forEach((locale) => {
      const entry = entryAt(locale, i);
      if (entry) entriesFor(locale)[i] = { ...entry, start, end };
    });
    markDirty();
    if (startInput.classList.contains("is-invalid") || endInput.classList.contains("is-invalid")) {
      validateRow(row);
    }
  };

  startInput.addEventListener("change", () => {
    if (!validateRow(row)) return;
    updateTiming();
  });
  endInput.addEventListener("change", () => {
    if (!validateRow(row)) return;
    updateTiming();
  });

  row.querySelectorAll("[data-op]").forEach((btn) => {
    btn.addEventListener("click", () => handleOp(btn.dataset.op, i));
  });

  return row;
}

function languageField(locale, label, index, missing) {
  const disabled = missing ? " disabled" : "";
  const hint = missing ? `没有${label}字幕` : `编辑第 ${index + 1} 条${label}`;
  return `
    <label class="subrow__field subrow__field--${locale}">
      <span class="subrow__fieldlabel">${label}</span>
      <textarea class="subrow__text" data-locale="${locale}" rows="2" placeholder="${hint}" aria-label="${hint}" spellcheck="false"${disabled}></textarea>
    </label>`;
}

function resizeTextarea(textArea) {
  textArea.style.height = "auto";
  textArea.style.height = `${textArea.scrollHeight}px`;
}

function validateRow(row) {
  const startInput = row.querySelector('[data-field="start"]');
  const endInput = row.querySelector('[data-field="end"]');
  const start = parseTime(startInput.value);
  const end = parseTime(endInput.value);
  const startOk = Number.isFinite(start) && start >= 0;
  const endOk = Number.isFinite(end) && end >= 0;
  const orderOk = startOk && endOk && end >= start;
  startInput.classList.toggle("is-invalid", !startOk);
  endInput.classList.toggle("is-invalid", !endOk || !orderOk);
  return startOk && endOk && orderOk;
}

function handleOp(op, idx) {
  const total = entryCount();
  if (idx < 0 || idx >= total) return;
  if (op === "delete") {
    editableLocales().forEach((locale) => entriesFor(locale).splice(idx, 1));
    markDirty();
    renderSubs();
    return;
  }
  if (op === "merge-next") {
    if (idx >= total - 1) {
      toast("已经是最后一条，无法与下一条合并", "ph-info");
      return;
    }
    editableLocales().forEach((locale) => {
      const list = entriesFor(locale);
      const a = list[idx];
      const b = list[idx + 1];
      if (!a || !b) return;
      list.splice(idx, 2, {
        ...a,
        end: b.end,
        text: [a.text, b.text].filter(Boolean).join("\n"),
      });
    });
    markDirty();
    renderSubs();
    return;
  }
  if (op === "split") {
    const a = entryAt("translated", idx) || entryAt("original", idx);
    if (!a) return;
    if (a.end - a.start < 0.4) {
      toast("当前条目时长太短，无法再拆分", "ph-info");
      return;
    }
    const mid = (a.start + a.end) / 2;
    const nextId = "sub_" + Math.random().toString(36).slice(2, 10);
    editableLocales().forEach((locale) => {
      const list = entriesFor(locale);
      const entry = list[idx];
      if (!entry) return;
      const lines = (entry.text || "").split("\n");
      const half = Math.max(1, Math.ceil(lines.length / 2));
      const left = { ...entry, end: mid, text: lines.slice(0, half).join("\n") };
      const right = { ...entry, id: nextId, start: mid, text: lines.slice(half).join("\n") };
      list.splice(idx, 1, left, right);
    });
    markDirty();
    renderSubs();
    return;
  }
}

function onAdd() {
  if (!currentDoc) return;
  const last = entryAt("translated", entryCount() - 1) || entryAt("original", entryCount() - 1);
  const start = last ? round2(last.end) : 0;
  const end = round2(start + 2);
  const id = "sub_" + Math.random().toString(36).slice(2, 10);
  editableLocales().forEach((locale) => {
    const list = entriesFor(locale);
    list.push({ id, index: list.length + 1, start, end, text: "" });
  });
  markDirty();
  renderSubs();
  // 滚到新行
  subsEl.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "end" });
}

/* ---------- 工具：dirty / 保存 / 烧录 ---------- */

function markDirty() {
  dirty = true;
  saveBtn.classList.add("btn--accent");
  saveBtn.classList.remove("btn--ghost");
  document.title = "• 字幕编辑";
}

function clearDirty() {
  dirty = false;
  saveBtn.classList.remove("btn--accent");
  saveBtn.classList.add("btn--ghost");
  document.title = "字幕编辑";
}

async function onSave() {
  if (!currentDoc || !currentTaskId) return;
  // 提交前再做一次全表校验
  const rows = subsEl.querySelectorAll(".subrow");
  for (const r of rows) if (!validateRow(r)) {
    toast("存在非法时间，请修正后再保存", "ph-warning-circle");
    return;
  }
  const version = (versionInput.value || "").trim() || null;
  saveBtn.disabled = true;
  try {
    const results = [];
    for (const locale of editableLocales()) {
      const entries = entriesFor(locale).map((entry, i) => ({
        id: entry.id,
        index: i + 1,
        start: round2(entry.start),
        end: round2(entry.end),
        text: entry.text || "",
      }));
      results.push(await Api.saveSubtitles(currentTaskId, { locale, entries, version }));
    }
    clearDirty();
    const count = results.reduce((sum, result) => sum + result.count, 0);
    toast(
      version ? `已保存原文和译文版本（共 ${count} 条）` : `已保存原文和译文（共 ${count} 条）`,
      "ph-floppy-disk",
    );
  } catch (e) {
    toast(e.message || "保存失败", "ph-warning-circle");
  } finally {
    saveBtn.disabled = false;
  }
}

async function onReburn() {
  if (!currentTaskId) return;
  if (!confirm("将基于当前 translated.srt 重新烧录并生成带模式标识的成品视频。确定继续？")) return;
  reburnBtn.disabled = true;
  try {
    const res = await Api.reburnSubtitles(currentTaskId, {});
    toast(`已重新烧录 ${res.outputPath}（${res.mode}）`, "ph-fire");
  } catch (e) {
    toast(e.message || "重新烧录失败", "ph-warning-circle");
  } finally {
    reburnBtn.disabled = false;
  }
}

/* ---------- 工具：时间格式 ---------- */

function fmtTime(sec) {
  if (!Number.isFinite(sec)) return "00:00.000";
  const s = Math.max(0, sec);
  const m = Math.floor(s / 60);
  const ss = (s - m * 60);
  return `${pad2(m)}:${ss.toFixed(3).padStart(6, "0")}`;
}

function parseTime(str) {
  // 接受 1.234 / 1,234 / 1:23.456 / 01:23.456
  const t = String(str || "").trim().replace(",", ".");
  if (!t) return NaN;
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t);
  const m = t.match(/^(\d+):(\d+(?:\.\d+)?)$/);
  if (!m) return NaN;
  return Number(m[1]) * 60 + Number(m[2]);
}

function pad2(n) { return String(n).padStart(2, "0"); }
function round2(n) { return Math.round(n * 1000) / 1000; }

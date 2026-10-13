/* 本地资源治理 Tab：统计卡片 + 任务产物表格 + 安全清理预览 / 执行。
 * 复用 store.loadTasks() 在清理后刷新任务列表，保持任务 Tab 一致。
 */

import { $, $$, el, escapeHtml } from "./utils.js";
import { Api } from "./api.js";
import { state, loadTasks } from "./store.js";
import { toast } from "./toast.js";

const RUNNING = new Set([
  "PENDING", "DOWNLOADING", "EXTRACTING",
  "TRANSCRIBING", "TRANSLATING", "BURNING", "SYNTHESIZING", "DUBBING",
]);
const STORAGE_REFRESH_INTERVAL_MS = 8000;
const DEFAULT_RETENTION_DAYS = 30;

const KIND_LABEL = {
  source: "源视频",
  audio: "音频",
  original_srt: "原文字幕",
  translated_srt: "译文字幕",
  output: "成品视频",
  other: "其它",
};

const STATUS_LABEL = {
  PENDING: "排队中",
  DOWNLOADING: "下载中",
  EXTRACTING: "提取中",
  TRANSCRIBING: "识别中",
  TRANSLATING: "翻译中",
  BURNING: "烧录中",
  SYNTHESIZING: "生成配音中",
  DUBBING: "封装配音中",
  SUCCESS: "已完成",
  FAILED: "失败",
};

// 本模块状态：仅在 Tab 内有效
const local = {
  stats: null,
  selected: new Set(),   // 当前选中的 taskId
  preview: null,         // 最近一次预览结果
  retentionDays: DEFAULT_RETENTION_DAYS,
  kindFilter: "",
  loading: false,
  refreshTimer: null,
  visibilityRefreshRegistered: false,
  driveSync: null,
};

let els = {};

export function initStorage() {
  els = {
    cards: $("#storageCards"),
    total: $("#statTotal"),
    totalSub: $("#statTotalSub"),
    tasks: $("#statTasks"),
    cleanable: $("#statCleanable"),
    cleanableSub: $("#statCleanableSub"),
    retention: $("#statRetention"),
    selInfo: $("#storageSelInfo"),
    selectAll: $("#storageSelectAll"),
    apply: $("#storageApply"),
    table: $("#storageTable"),
    tbody: $("#storageTbody"),
    empty: $("#storageEmpty"),
    checkAll: $("#storageCheckAll"),
    refresh: $("#storageRefresh"),
    driveUpload: $("#storageDriveUpload"),
    driveSync: $("#storageDriveSync"),
    driveSyncTitle: $("#storageDriveSyncTitle"),
    driveSyncMeta: $("#storageDriveSyncMeta"),
    driveSyncBody: $("#storageDriveSyncBody"),
    driveSyncCancel: $("#storageDriveSyncCancel"),
  };

  els.refresh.addEventListener("click", () => refresh(true));
  els.selectAll.addEventListener("click", () => selectAll(true));
  els.apply.addEventListener("click", () => runApply());
  els.checkAll.addEventListener("change", (e) => selectAll(e.target.checked));
  els.driveUpload?.addEventListener("click", () => runDriveSync("UPLOAD"));
  els.driveSyncCancel?.addEventListener("click", () => cancelDriveSync());

  // 当切到本 Tab 时再加载一次，其它时候用定时轻量刷新
  if (state.view === "storage") {
    refresh();
  }
  document.addEventListener("viewchange", (e) => {
    if (e.detail && e.detail.view === "storage") refresh();
  });

  startAutoRefresh();
}

/* ---------- 自动刷新：Tab 可见时轮询，页面回到前台后立即重新扫描 ---------- */
function startAutoRefresh() {
  clearInterval(local.refreshTimer);
  local.refreshTimer = setInterval(() => {
    if (state.view === "storage" && !local.loading) refresh();
  }, STORAGE_REFRESH_INTERVAL_MS);

  // 浏览器标签页在后台时会降低定时器频率；重新回到前台立即刷新，
  // 避免外部文件变化后用户仍看到离开页面前的产物列表。
  if (!local.visibilityRefreshRegistered) {
    document.addEventListener("visibilitychange", () => {
      if (
        document.visibilityState === "visible"
        && state.view === "storage"
        && !local.loading
      ) {
        refresh();
      }
    });
    local.visibilityRefreshRegistered = true;
  }
}

/* ---------- 数据加载 ---------- */
async function refresh(showToast = false) {
  if (local.loading) return;
  local.loading = true;
  try {
    const [stats, retention] = await Promise.all([
      Api.getStorageStats().catch((e) => {
        if (showToast) toast(e.message, "ph-warning-circle");
        return null;
      }),
      Api.getRetention().catch(() => ({ days: DEFAULT_RETENTION_DAYS, updatedAt: null })),
    ]);
    if (stats) local.stats = stats;
    if (retention && retention.days !== undefined) {
      local.retentionDays = retention.days === null || retention.days === 0
        ? 0
        : (retention.days || DEFAULT_RETENTION_DAYS);
      // 仅在未初始化时同步 select
    }
    if (stats) {
      renderCards();
      renderTable();
    }
    if (showToast) toast("已刷新", "ph-arrow-clockwise");
  } finally {
    local.loading = false;
  }
}

async function saveRetention() {
  try {
    const v = local.retentionDays > 0 ? local.retentionDays : null;
    await Api.putRetention(v);
    renderCards();
    toast(
      v
        ? `产物保留期：${v} 天以上，任务记录保留`
        : "产物保留期：不限，任务记录保留",
      "ph-check",
    );
  } catch (e) {
    toast(e.message || "保存保留策略失败", "ph-warning-circle");
  }
}

/* ---------- 渲染 ---------- */
function renderCards() {
  if (!local.stats) return;
  const { totalBytes, byKind, byTask = [] } = local.stats;
  const completedTaskCount = byTask.filter((task) => task.status === "SUCCESS").length;
  const runningTaskCount = byTask.filter((task) => RUNNING.has(task.status)).length;
  els.total.textContent = formatBytes(totalBytes);
  els.totalSub.textContent = completedTaskCount
    ? `${(byKind.source || 0) > 0 ? "源视频 " + formatBytes(byKind.source) + " · " : ""}共 ${completedTaskCount} 个已完成任务`
    : "尚未生成任何产物";
  els.tasks.textContent = String(completedTaskCount);
  els.cleanable.textContent = String(runningTaskCount);
  els.cleanableSub.textContent = runningTaskCount > 0 ? "正在运行" : "暂无运行中任务";
}

function renderTable() {
  const tbody = els.tbody;
  const stats = local.stats;
  tbody.replaceChildren();

  if (!stats || stats.byTask.length === 0) {
    els.table.hidden = true;
    els.empty.hidden = false;
    updateActions();
    return;
  }
  els.table.hidden = false;
  els.empty.hidden = true;

  // 按创建时间倒序（在占用降序的基础上，让新任务也更靠前）
  const rows = [...stats.byTask].sort((a, b) => {
    if (a.size !== b.size) return b.size - a.size;
    return 0;
  });

  for (const t of rows) {
    const tr = el("tr", "");
    tr.dataset.id = t.taskId;
    if (local.selected.has(t.taskId)) tr.classList.add("is-selected");
    const isRunning = RUNNING.has(t.status);
    if (isRunning) tr.classList.add("is-running");

    // 复选框：运行中任务禁用
    const checkTd = el("td", "storage-table__check");
    const check = el("label", "storage-check");
    const cb = el("input", "storage-check__input");
    cb.type = "checkbox";
    cb.checked = local.selected.has(t.taskId);
    cb.disabled = isRunning;
    cb.title = isRunning ? "运行中任务会被自动跳过" : "选择此任务";
    cb.setAttribute("aria-label", cb.title);
    const checkBox = el("span", "storage-check__box");
    checkBox.setAttribute("aria-hidden", "true");
    cb.addEventListener("change", () => toggleSelect(t.taskId, cb.checked));
    check.append(cb, checkBox);
    checkTd.append(check);

    // 标题
    const titleTd = el("td", "storage-table__title");
    const titleMain = el("div", "storage-table__title-main");
    titleMain.textContent = t.title || "处理中的视频";
    const titleId = el("div", "storage-table__title-id");
    titleId.textContent = t.taskId;
    titleTd.append(titleMain, titleId);

    // 状态
    const statusTd = el("td", "storage-table__status");
    const badge = el("span", "storage-badge " + badgeClass(t.status));
    badge.innerHTML = `<i class="ph ${badgeIcon(t.status)}"></i>${STATUS_LABEL[t.status] || t.status}`;
    statusTd.append(badge);

    // 产物
    const artTd = el("td", "storage-table__artifacts");
    const list = el("div", "storage-table__artifacts-list");
    if (t.artifactCount === 0) {
      const chip = el("span", "storage-table__art-chip");
      chip.textContent = "无产物";
      list.append(chip);
    } else {
      // 聚合同类
      const grouped = {};
      for (const a of t.artifacts) {
        grouped[a.kind] = (grouped[a.kind] || 0) + 1;
      }
      for (const k of Object.keys(grouped)) {
        const chip = el("span", "storage-table__art-chip");
        const label = KIND_LABEL[k] || k;
        chip.textContent = grouped[k] > 1 ? `${label} ×${grouped[k]}` : label;
        chip.title = t.artifacts
          .filter((a) => a.kind === k)
          .map((a) => a.name)
          .join(", ");
        list.append(chip);
      }
    }
    artTd.append(list);

    // 占用
    const sizeTd = el("td", "storage-table__size num");
    sizeTd.textContent = formatBytes(t.size);

    // 创建时间：后端 stats 不带 createdAt，用本地 state 兜底
    const ageTd = el("td", "storage-table__age");
    ageTd.textContent = "—";

    const actionTd = el("td", "storage-table__action");
    const preview = el("button", "btn btn--ghost btn--sm");
    preview.type = "button";
    preview.innerHTML = `<i class="ph ph-folder-open"></i><span>打开文件夹</span>`;
    preview.title = "打开任务文件夹";
    preview.addEventListener("click", () => openTaskResource(t.taskId, t.title || t.taskId));
    actionTd.append(preview);
    tr.append(checkTd, titleTd, statusTd, artTd, sizeTd, ageTd, actionTd);
    tbody.append(tr);
  }

  // 用本地 store 的 tasks 兜底填入创建时间
  fillAges();
  updateActions();
}

async function openTaskResource(taskId, title) {
  try {
    const capability = await Api.folderCapability(taskId);
    if (capability.mode === "system") {
      await Api.openFolder(taskId);
      toast("已打开服务器文件夹", "ph-folder-open");
      return;
    }
    showTaskBrowser(taskId, title);
  } catch (error) {
    toast(error.message || "打开文件夹失败", "ph-warning-circle");
  }
}

function showTaskBrowser(taskId, title) {
  const overlay = el("div", "resource-browser");
  overlay.innerHTML = `<div class="resource-browser__panel" role="dialog" aria-modal="true">
    <div class="resource-browser__head"><strong>${escapeHtml(title)}</strong><button class="iconbtn" aria-label="关闭"><i class="ph ph-x"></i></button></div>
    <div class="resource-browser__crumb">任务产物 / <span></span></div>
    <div class="resource-browser__body"><span>正在读取…</span></div>
  </div>`;
  const body = overlay.querySelector(".resource-browser__body");
  const crumb = overlay.querySelector(".resource-browser__crumb span");
  let currentPath = "";
  const close = () => overlay.remove();
  overlay.querySelector(".iconbtn").addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  document.body.append(overlay);
  const load = async (path) => {
    currentPath = path;
    try {
      const data = await Api.listTaskFiles(taskId, path);
      crumb.textContent = path || "根目录";
      body.replaceChildren();
      if (path) {
        const up = el("button", "resource-browser__entry resource-browser__up");
        up.textContent = "↑ 返回上级";
        up.addEventListener("click", () => load(path.split("/").slice(0, -1).join("/")));
        body.append(up);
      }
      for (const entry of data.entries) {
        const button = el("button", "resource-browser__entry");
        button.innerHTML = `<i class="ph ${entry.directory ? "ph-folder" : "ph-file"}"></i><span>${escapeHtml(entry.name)}</span><small>${entry.directory ? "目录" : formatBytes(entry.size)}</small>`;
        button.addEventListener("click", () => entry.directory
          ? load(entry.path)
          : window.open(Api.taskFileUrl(taskId, entry.path), "_blank"));
        body.append(button);
      }
      if (!data.entries.length) body.textContent = "目录为空";
    } catch (error) { body.textContent = error.message || "读取失败"; }
  };
  void load(currentPath);
}

function fillAges() {
  const byId = new Map(state.tasks.map((t) => [t.id, t]));
  $$("tr[data-id]", els.tbody).forEach((tr) => {
    const t = byId.get(tr.dataset.id);
    const ageTd = tr.querySelector(".storage-table__age");
    if (!ageTd) return;
    ageTd.textContent = t && t.createdAt ? formatAgeFromMs(t.createdAt) : "—";
  });
}

function formatAgeFromMs(ms) {
  const days = Math.max(0, Math.floor((Date.now() - ms) / 86400000));
  if (days === 0) return "今天";
  if (days === 1) return "1 天前";
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  return months < 12 ? `${months} 个月前` : `${Math.floor(months / 12)} 年前`;
}

/* ---------- 选择 / 动作状态 ---------- */
function toggleSelect(id, on) {
  if (on) local.selected.add(id);
  else local.selected.delete(id);
  const tr = els.tbody.querySelector(`tr[data-id="${cssEscape(id)}"]`);
  if (tr) tr.classList.toggle("is-selected", on);
  updateActions();
}

function selectAll(on) {
  if (!local.stats) return;
  if (on) {
    // 跳过 RUNNING
    for (const t of local.stats.byTask) {
      if (!RUNNING.has(t.status)) local.selected.add(t.taskId);
    }
  } else {
    local.selected.clear();
  }
  // 同步所有复选框
  $$('input[type="checkbox"]', els.tbody).forEach((cb) => {
    const tr = cb.closest("tr");
    if (!tr) return;
    if (on) {
      if (!cb.disabled) cb.checked = true;
    } else {
      cb.checked = false;
    }
    tr.classList.toggle("is-selected", cb.checked);
  });
  updateActions();
}

function updateActions() {
  const n = local.selected.size;
  els.selInfo.textContent = n > 0 ? `已选 ${n} 项` : "未选择";
  const hasSelection = n > 0;
  const syncing = Boolean(local.driveSync?.active);
  els.apply.disabled = !hasSelection || syncing;
  if (els.driveUpload) els.driveUpload.disabled = !hasSelection || syncing;

  // 让表头选择器反映真实状态：全选、部分选择和无可选项分别可见。
  const selectable = local.stats
    ? local.stats.byTask.filter((t) => !RUNNING.has(t.status))
    : [];
  const selectedCount = selectable.filter((t) => local.selected.has(t.taskId)).length;
  els.checkAll.checked = selectable.length > 0 && selectedCount === selectable.length;
  els.checkAll.indeterminate = selectedCount > 0 && selectedCount < selectable.length;
  els.checkAll.disabled = selectable.length === 0;
}

/* ---------- Google Drive 任务级同步 ---------- */
async function runDriveSync(direction) {
  if (local.selected.size === 0 || local.driveSync?.active) return;
  const taskIds = [...local.selected];
  local.driveSync = {
    active: true,
    direction,
    taskIds,
    index: 0,
    batchId: "",
    batch: null,
    cancelled: false,
    failures: 0,
  };
  renderDriveSync();
  updateActions();
  try {
    for (let index = 0; index < taskIds.length; index += 1) {
      if (local.driveSync.cancelled) break;
      local.driveSync.index = index;
      const taskId = taskIds[index];
      const task = local.stats?.byTask.find((candidate) => candidate.taskId === taskId);
      const artifactNames = direction === "UPLOAD"
        ? (task?.artifacts || []).map((artifact) => artifact.name)
        : [];
      const started = direction === "UPLOAD"
        ? await Api.startDriveUpload(taskId, artifactNames)
        : await Api.startDriveDownload(taskId);
      local.driveSync.batchId = started.batchId;
      local.driveSync.batch = started;
      renderDriveSync();
      let current = started;
      while (!new Set(["SUCCESS", "FAILED", "CANCELLED"]).has(current.state)) {
        await new Promise((resolve) => setTimeout(resolve, 700));
        if (local.driveSync.cancelled) break;
        current = await Api.getDriveBatch(started.batchId);
        local.driveSync.batch = current;
        renderDriveSync();
      }
      if (current.state === "FAILED") local.driveSync.failures += 1;
      if (current.state === "CANCELLED") break;
    }
    if (local.driveSync.cancelled) {
      toast("Drive 同步已取消", "ph-x-circle");
    } else if (local.driveSync.failures > 0) {
      toast(`Drive 同步完成，${local.driveSync.failures} 个任务存在失败项`, "ph-warning");
    } else {
      toast(direction === "UPLOAD" ? "本地产物已逐项上传到 Drive" : "Drive 产物已逐项恢复到本地", "ph-check-circle");
    }
    await refresh();
    await loadTasks();
  } catch (error) {
    if (!local.driveSync.cancelled) toast(error.message || "Drive 同步失败", "ph-warning");
  } finally {
    if (local.driveSync) local.driveSync.active = false;
    renderDriveSync();
    updateActions();
  }
}

async function cancelDriveSync() {
  const sync = local.driveSync;
  if (!sync?.active) return;
  sync.cancelled = true;
  if (sync.batchId) {
    try {
      sync.batch = await Api.cancelDriveBatch(sync.batchId);
    } catch (error) {
      toast(error.message || "取消 Drive 同步失败", "ph-warning");
    }
  }
  renderDriveSync();
}

function renderDriveSync() {
  const sync = local.driveSync;
  if (!els.driveSync) return;
  if (!sync) {
    els.driveSync.hidden = true;
    return;
  }
  els.driveSync.hidden = false;
  const directionLabel = sync.direction === "UPLOAD" ? "上传到 Drive" : "从 Drive 下载";
  const taskId = sync.taskIds[sync.index] || "—";
  const batch = sync.batch || {};
  const entries = Array.isArray(batch.entries) ? batch.entries : [];
  const pending = entries.filter((entry) => entry.state !== "SUCCESS");
  const completed = Number(batch.completedEntries || 0);
  const total = Number(batch.totalEntries || entries.length || 0);
  els.driveSyncTitle.textContent = `${directionLabel} · ${taskId}`;
  els.driveSyncMeta.textContent = `${sync.index + 1}/${sync.taskIds.length} 个任务 · ${completed}/${total} 个产物已完成 · ${batch.state || "PENDING"}`;
  els.driveSyncCancel.hidden = !sync.active;
  els.driveSyncBody.replaceChildren();
  if (entries.length === 0 && !["SUCCESS", "FAILED", "CANCELLED"].includes(batch.state)) {
    const loading = el("span", "storage-sync__item storage-sync__item--active");
    loading.textContent = "正在读取 Drive 文件列表";
    els.driveSyncBody.append(loading);
    return;
  }
  if (pending.length === 0) {
    const done = el("span", "storage-sync__item storage-sync__item--success");
    done.textContent = batch.state === "FAILED" ? "存在失败项，可重试" : "待处理列表已清空";
    els.driveSyncBody.append(done);
    return;
  }
  for (const entry of pending) {
    const stateClass = entry.state === "FAILED"
      ? "storage-sync__item--failed"
      : (entry.state === "TRANSFERRING" ? "storage-sync__item--active" : "");
    const item = el("span", `storage-sync__item ${stateClass}`.trim());
    item.textContent = entry.state === "FAILED"
      ? `${entry.name} · 失败`
      : `${entry.name} · ${entry.state || "等待中"}`;
    if (entry.error) item.title = entry.error;
    els.driveSyncBody.append(item);
  }
}

/* ---------- 执行 ---------- */
function buildBody() {
  const body = {};
  if (local.selected.size > 0) body.taskIds = [...local.selected];
  if (local.kindFilter) body.kinds = [local.kindFilter];
  if (local.retentionDays > 0) body.olderThanDays = local.retentionDays;
  return body;
}

async function runApply() {
  if (local.selected.size === 0) return;
  if (!confirm(`确定要清理所选任务吗？\n\n运行中任务会被自动跳过。\n该操作不可撤销。`)) return;

  els.apply.disabled = true;
  try {
    const res = await Api.runCleanup(buildBody());
    const msg = res.note || (res.deletedTasks
      ? `已清理 ${res.deletedTasks} 个任务，释放 ${formatBytes(res.deletedBytes)}`
      : `已清理 ${formatBytes(res.deletedBytes)} 产物`);
    toast(msg, "ph-trash");
    local.selected.clear();
    await refresh();
    // 任务列表也需要同步
    await loadTasks();
  } catch (e) {
    toast(e.message || "执行清理失败", "ph-warning-circle");
  } finally {
    els.apply.disabled = false;
    updateActions();
  }
}

/* ---------- 工具 ---------- */
function formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const fixed = v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1);
  return `${fixed} ${units[i]}`;
}

function badgeClass(s) {
  if (s === "SUCCESS") return "storage-badge--success";
  if (s === "FAILED") return "storage-badge--failed";
  if (s === "PENDING") return "storage-badge--pending";
  return "storage-badge--active";
}
function badgeIcon(s) {
  if (s === "SUCCESS") return "ph-check";
  if (s === "FAILED") return "ph-warning";
  if (s === "PENDING") return "ph-clock";
  return "ph-spinner";
}

function cssEscape(s) {
  // 简易 CSS attr escape：taskId 形如 task_xxxxxxxx，安全字符
  return String(s).replace(/[^a-zA-Z0-9_-]/g, (c) => "\\" + c);
}

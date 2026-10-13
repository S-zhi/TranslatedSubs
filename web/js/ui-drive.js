/* Google Drive 视图：授权、文件列表、分片上传和后台传输控制。 */

import { $, escapeHtml } from "./utils.js";
import { state } from "./store.js";
import { toast } from "./toast.js";
import { DriveApi } from "./drive-api.js";

const CHUNK_SIZE = 16 * 1024 * 1024;
const FOLDER_UPLOAD_CONCURRENCY = 2;
const REFRESH_INTERVAL = 5000;
const DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder";
const ROOT_FOLDER_LABEL = "Subtitles AI";

const STATUS_LABELS = {
  PENDING: "等待中",
  TRANSFERRING: "传输中",
  RETRYING: "重试中",
  VERIFYING: "校验中",
  PAUSED: "已暂停",
  SUCCESS: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const KIND_LABELS = {
  DRIVE_UPLOAD: "上传 Drive",
  PYTHON_IMPORT: "导入字幕流水线",
};

const local = {
  auth: null,
  authError: "",
  files: [],
  filesError: "",
  filesLoading: false,
  filesRequestId: 0,
  rootFolderId: "",
  currentFolderId: "",
  currentFolder: null,
  breadcrumbs: [],
  pageToken: "",
  nextPageToken: "",
  pageHistory: [],
  transfers: [],
  transfersError: "",
  refreshing: false,
  upload: null,
  folderUpload: null,
  timer: null,
};

/** 目录上传和文件列表共用的稳定目录判断。 */
export function isDriveFolder(file) {
  return file?.mimeType === DRIVE_FOLDER_MIME || file?.mime_type === DRIVE_FOLDER_MIME || file?.kind === "folder";
}

/** 将浏览器提供的相对路径整理为安全、可复现的 POSIX 路径。 */
export function normalizeRelativePath(value, fallback = "未命名文件") {
  const parts = String(value || fallback)
    .replaceAll("\\", "/")
    .split("/")
    .map((part) => part.trim())
    .filter((part) => part && part !== "." && part !== "..");
  const safe = parts.map((part) => part.replace(/[<>:\"|?*\u0000-\u001f]/g, "_").trim()).filter(Boolean);
  return safe.join("/") || fallback;
}

/** 从 FileList 提取 Drive 文件夹批次所需的 manifest。 */
export function createFolderManifest(files) {
  return Array.from(files || []).map((file, index) => {
    const relativePath = normalizeRelativePath(file?.webkitRelativePath || file?.name, `file-${index + 1}`);
    const pathParts = relativePath.split("/");
    return {
      relativePath,
      name: pathParts.at(-1) || file?.name || `file-${index + 1}`,
      size: Math.max(0, Number(file?.size || 0)),
      mime: file?.type || "application/octet-stream",
    };
  });
}

// 便于其他前端模块和测试使用语义更直接的别名。
export const buildFolderManifest = createFolderManifest;

/** 为目录拖拽得到的 File 补充相对路径，同时保留分片上传所需的 File 接口。 */
function droppedFileWithRelativePath(file, relativePath) {
  return {
    name: file.name,
    size: file.size,
    type: file.type,
    lastModified: file.lastModified,
    webkitRelativePath: relativePath,
    slice: file.slice.bind(file),
  };
}

/** Chrome 的目录 reader 每次只返回一批条目，必须读到空批次才算结束。 */
async function readAllDirectoryEntries(reader) {
  const entries = [];
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) return entries;
    entries.push(...batch);
  }
}

async function collectDroppedEntry(entry, parentPath = "") {
  const relativePath = parentPath ? `${parentPath}/${entry.name}` : entry.name;
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    return [droppedFileWithRelativePath(file, relativePath)];
  }
  if (!entry.isDirectory) return [];
  const children = await readAllDirectoryEntries(entry.createReader());
  const nested = await Promise.all(children.map((child) => collectDroppedEntry(child, relativePath)));
  return nested.flat();
}

/**
 * 展开拖入的文件系统条目。普通文件保持原始 File；目录则递归生成带相对路径的文件列表。
 */
export async function collectDroppedFiles(dataTransfer) {
  const items = Array.from(dataTransfer?.items || []);
  const entries = items
    .filter((item) => item.kind === "file")
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean);
  const hasDirectory = entries.some((entry) => entry.isDirectory);
  if (!hasDirectory) {
    return { files: Array.from(dataTransfer?.files || []), hasDirectory: false };
  }
  const nested = await Promise.all(entries.map((entry) => collectDroppedEntry(entry)));
  return { files: nested.flat(), hasDirectory: true };
}

/** 目录上传进度的纯计算，浏览器和 Node 测试都可复用。 */
export function summarizeFolderProgress(upload) {
  const entries = Array.isArray(upload?.entries) ? upload.entries : [];
  const totalBytes = entries.reduce((sum, entry) => sum + Math.max(0, Number(entry.size || entry.file?.size || 0)), 0);
  const completedBytes = entries.reduce((sum, entry) => {
    const size = Math.max(0, Number(entry.size || entry.file?.size || 0));
    return sum + Math.min(size, Math.max(0, Number(entry.offset || 0)));
  }, 0);
  const completedEntries = entries.filter((entry) => entry.state === "SUCCESS" || (Number(entry.offset || 0) >= Number(entry.size || entry.file?.size || 0) && Number(entry.size || entry.file?.size || 0) > 0)).length;
  const failedEntries = entries.filter((entry) => entry.state === "FAILED").length;
  return {
    totalEntries: entries.length,
    completedEntries,
    failedEntries,
    totalBytes,
    completedBytes,
    percent: totalBytes > 0 ? Math.min(100, Math.round((completedBytes / totalBytes) * 100)) : 0,
  };
}

export const calculateFolderProgress = summarizeFolderProgress;

/** 为目录上传限制并发数；返回顺序与输入一致，便于稳定更新 UI。 */
export async function runWithConcurrency(items, limit, worker) {
  const list = Array.from(items || []);
  const width = Math.max(1, Math.floor(Number(limit) || 1));
  const results = new Array(list.length);
  let cursor = 0;
  async function consume() {
    while (cursor < list.length) {
      const index = cursor;
      cursor += 1;
      results[index] = await worker(list[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.min(width, list.length) }, consume));
  return results;
}

function batchFromResponse(value) {
  return value?.batch || value?.folderUpload || value || {};
}

function batchIDFromResponse(value) {
  const batch = batchFromResponse(value);
  return String(value?.batchId || value?.batch_id || batch.id || batch.batchId || batch.batch_id || "");
}

function entriesFromResponse(value) {
  const batch = batchFromResponse(value);
  return Array.isArray(value?.entries) ? value.entries : Array.isArray(batch.entries) ? batch.entries : [];
}

function entryPath(entry) {
  return String(entry?.relativePath || entry?.relative_path || entry?.path || "");
}

function entryID(entry) {
  return String(entry?.entryId || entry?.entry_id || entry?.id || "");
}

function uploadIDFromResponse(value) {
  return String(value?.uploadId || value?.upload_id || value?.upload?.id || value?.id || "");
}

function folderEntryStatus(entry) {
  return String(entry?.state || entry?.status || "PENDING").toUpperCase();
}

function hasActiveUpload() {
  return Boolean(local.upload || (local.folderUpload && !["SUCCESS", "CANCELLED"].includes(local.folderUpload.state)));
}

/**
 * 初始化 Google Drive 视图，并把按钮事件绑定到独立的 sidecar 状态流。
 */
export function initDrive() {
  const panel = $("#drivePanel");
  if (!panel) return;

  $("#driveRefresh")?.addEventListener("click", () => refresh(true));
  $("#driveAuthAction")?.addEventListener("click", openAuth);
  $("#driveDisconnect")?.addEventListener("click", disconnect);
  $("#driveFileInput")?.addEventListener("change", (event) => {
    const files = Array.from(event.target.files || []);
    if (files.length > 1 || files.some((file) => file.webkitRelativePath)) void startFolderUpload(files);
    else if (files[0]) void startUpload(files[0]);
    event.target.value = "";
  });
  $("#driveFolderInput")?.addEventListener("change", (event) => {
    void startFolderUpload(Array.from(event.target.files || []));
    event.target.value = "";
  });
  $("#driveChooseFile")?.addEventListener("click", () => $("#driveFileInput")?.click());
  $("#driveChooseFolder")?.addEventListener("click", () => $("#driveFolderInput")?.click());
  $("#driveUploadCancel")?.addEventListener("click", cancelUpload);
  bindDropzone();

  $("#driveFiles")?.addEventListener("click", handleFileAction);
  $("#driveFiles")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") handleFileAction(event);
  });
  $("#driveBreadcrumbs")?.addEventListener("click", handleBreadcrumbAction);
  $("#driveFilesPagination")?.addEventListener("click", handlePaginationAction);
  $("#driveFolderUploadProgress")?.addEventListener("click", handleFolderUploadAction);
  $("#driveTransfers")?.addEventListener("click", handleTransferAction);
  document.addEventListener("viewchange", handleViewChange);

  if (state.view === "other-settings" && state.settingsTab === "drive") void refresh();
}

/**
 * 让拖放区域同时支持鼠标拖放和键盘激活文件选择器。
 */
function bindDropzone() {
  const dropzone = $("#driveDropzone");
  const input = $("#driveFileInput");
  if (!dropzone || !input) return;

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-dragging");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    void collectDroppedFiles(event.dataTransfer).then(({ files, hasDirectory }) => {
      if (hasDirectory || files.length > 1 || files.some((file) => file.webkitRelativePath)) return startFolderUpload(files);
      if (files[0]) return startUpload(files[0]);
      return undefined;
    }).catch((error) => {
      toast(messageOf(error, "无法读取拖入的文件夹"), "ph-warning");
    });
  });
  dropzone.addEventListener("click", (event) => {
    if (!event.target.closest("button")) input.click();
  });
  dropzone.addEventListener("keydown", (event) => {
    if (event.target !== dropzone) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  });
}

/**
 * 当用户切换到 Drive 视图时加载数据，并在离开时停止轮询。
 */
function handleViewChange(event) {
  const view = event.detail?.view;
  const settingsTab = event.detail?.settingsTab;
  if (view === "other-settings" && settingsTab === "drive") {
    void refresh();
    startPolling();
  } else {
    stopPolling();
  }
}

/**
 * 轮询后台传输状态，让上传和导入在浏览器等待期间持续可见。
 */
function startPolling() {
  stopPolling();
  local.timer = setInterval(() => {
    if (state.view === "other-settings" && state.settingsTab === "drive" && !local.refreshing) void refresh();
  }, REFRESH_INTERVAL);
}

/**
 * 停止 Drive 视图的后台轮询。
 */
function stopPolling() {
  if (local.timer) clearInterval(local.timer);
  local.timer = null;
}

/** 并行读取 OAuth 和持久化传输状态，文件列表由独立分页函数负责。 */
async function refreshStatus() {
  const [authResult, transferResult] = await Promise.allSettled([
    DriveApi.status(),
    DriveApi.listTransfers(),
  ]);

  local.authError = authResult.status === "rejected" ? messageOf(authResult.reason, "无法连接 Drive sidecar") : "";
  local.auth = authResult.status === "fulfilled" ? authResult.value : null;
  if (transferResult.status === "fulfilled") {
    local.transfers = Array.isArray(transferResult.value) ? transferResult.value : [];
    local.transfersError = "";
  } else {
    local.transfersError = messageOf(transferResult.reason, "无法读取传输队列");
  }
}

/** 将服务端返回的文件页写入当前目录状态。 */
function applyFilePage(result, requestedFolderId, requestedPageToken) {
  local.files = Array.isArray(result?.files) ? result.files : [];
  local.rootFolderId = String(result?.rootFolderId || result?.root_folder_id || local.rootFolderId || "");
  local.currentFolder = result?.currentFolder || result?.current_folder || (requestedFolderId ? local.currentFolder : null);
  local.currentFolderId = requestedFolderId;
  local.pageToken = requestedPageToken;
  local.nextPageToken = String(result?.nextPageToken || result?.next_page_token || "");
  local.filesError = "";
}

/** 读取一个文件页；请求序号避免快速切换目录时旧响应覆盖新状态。 */
async function refreshFilesPage({ folderId = local.currentFolderId, pageToken = local.pageToken } = {}) {
  const requestId = ++local.filesRequestId;
  local.filesLoading = true;
  local.filesError = "";
  renderFiles();
  try {
    const result = await DriveApi.listFiles(folderId, pageToken, 100);
    if (requestId !== local.filesRequestId) return;
    applyFilePage(result, folderId, pageToken);
  } catch (error) {
    if (requestId !== local.filesRequestId) return;
    local.filesError = messageOf(error, "无法读取 Drive 文件");
  } finally {
    if (requestId === local.filesRequestId) {
      local.filesLoading = false;
      renderFiles();
    }
  }
}

/** 刷新 OAuth、文件页和传输队列；目录导航/分页不会触发完整状态重置。 */
async function refresh(showToast = false) {
  if (local.refreshing) return;
  local.refreshing = true;
  render();
  try {
    await refreshStatus();
    if (local.auth?.connected) {
      await refreshFilesPage();
      await refreshFolderUploadStatus();
    } else {
      local.files = [];
      local.filesError = "";
      local.currentFolderId = "";
      local.currentFolder = null;
      local.breadcrumbs = [];
      local.pageToken = "";
      local.nextPageToken = "";
      local.pageHistory = [];
    }
    if (showToast) toast("Google Drive 状态已刷新", "ph-check-circle");
  } finally {
    local.refreshing = false;
    render();
  }
}

/**
 * 在新窗口打开动态 loopback OAuth 页面，避免当前工作台被 Google 页面替换。
 */
function openAuth() {
  if (local.auth && !local.auth.configured) {
    toast(local.auth.configuration_error || "请先配置 Google OAuth ClientID 和 ClientSecret", "ph-warning");
    return;
  }
  const popup = window.open(DriveApi.authUrl(), "google-drive-oauth", "noopener,noreferrer");
  if (!popup) toast("授权窗口被浏览器拦截，请允许本地页面打开新窗口", "ph-warning");
  else toast("授权页面已打开，完成后回到这里刷新状态", "ph-arrow-square-out");
}

/**
 * 撤销并删除 sidecar 本地保存的 Refresh Token。
 */
async function disconnect() {
  if (!window.confirm("断开 Google Drive 后，下次操作需要重新授权。继续吗？")) return;
  const button = $("#driveDisconnect");
  if (button) button.disabled = true;
  try {
    await DriveApi.disconnect();
    toast("Google Drive 已断开", "ph-sign-out");
    await refresh();
  } catch (error) {
    toast(messageOf(error, "断开 Google Drive 失败"), "ph-warning");
  } finally {
    if (button) button.disabled = false;
  }
}

/** 通用断点分片循环；单文件和目录条目共用同一套 409 对齐逻辑。 */
async function uploadFileChunks(file, upload, onProgress = () => {}) {
  if (!file?.size) throw new Error("不能上传空文件");
  let offset = Math.max(0, Number(upload.offset || 0));
  let transferID = upload.transferID || "";
  while (offset < file.size) {
    const before = offset;
    const chunk = file.slice(before, Math.min(before + CHUNK_SIZE, file.size));
    try {
      const result = await DriveApi.uploadChunk(upload.uploadId, chunk, before, upload.signal);
      offset = Number(result.offset);
      transferID = result.transferID || transferID;
    } catch (error) {
      if (error?.status === 409 && !upload.cancelled) {
        offset = await DriveApi.uploadOffset(upload.uploadId);
        upload.offset = offset;
        onProgress(upload);
        continue;
      }
      throw error;
    }
    if (!Number.isFinite(offset) || offset <= before) throw new Error("sidecar 没有推进上传偏移");
    upload.offset = Math.min(file.size, offset);
    upload.transferID = transferID;
    onProgress(upload);
  }
  upload.offset = file.size;
  upload.transferID = transferID;
  onProgress(upload);
  return upload;
}

/**
 * 以 16 MiB 分片上传文件，并在网络短暂中断后用 HEAD 重新对齐服务端偏移。
 */
async function startUpload(file) {
  if (!file?.size) {
    toast("不能上传空文件", "ph-warning");
    return;
  }
  if (!local.auth?.connected) {
    toast("请先连接 Google Drive", "ph-cloud-slash");
    return;
  }
  if (hasActiveUpload()) {
    toast("当前已有文件正在上传", "ph-hourglass-simple");
    return;
  }

  const upload = {
    file,
    id: "",
    uploadId: "",
    offset: 0,
    transferID: "",
    controller: new AbortController(),
    cancelled: false,
    done: false,
    error: "",
  };
  local.upload = upload;
  render();

  try {
    const created = await DriveApi.createUpload(file, upload.controller.signal);
    upload.id = created.id;
    upload.uploadId = created.id;
    upload.offset = Number(created.offset || 0);
    if (upload.cancelled) {
      await DriveApi.deleteUpload(upload.id);
      return;
    }
    render();

    await uploadFileChunks(file, upload, () => render());

    upload.done = true;
    toast("文件已上传，Drive 后台任务已排队", "ph-check-circle");
    await refresh();
    window.setTimeout(() => {
      if (local.upload === upload) {
        local.upload = null;
        render();
      }
    }, 4000);
  } catch (error) {
    if (upload.cancelled || error?.name === "AbortError") return;
    upload.error = messageOf(error, "上传失败");
    toast(upload.error, "ph-warning");
    render();
  }
}

/** 将服务端批次响应与浏览器 File 按相对路径严格对齐。 */
export function mergeFolderEntries(manifest, files, remoteEntries) {
  const remoteByPath = new Map();
  for (const remote of remoteEntries) {
    const relativePath = entryPath(remote);
    if (!relativePath) throw new Error("sidecar 返回了缺少相对路径的文件夹条目");
    if (remoteByPath.has(relativePath)) throw new Error(`sidecar 返回了重复的文件夹条目：${relativePath}`);
    remoteByPath.set(relativePath, remote);
  }
  return manifest.map((item, index) => {
    const remote = remoteByPath.get(item.relativePath);
    if (!remote) throw new Error(`sidecar 未返回文件夹条目：${item.relativePath}`);
    return {
      ...item,
      file: files[index],
      id: entryID(remote),
      uploadId: String(remote.uploadId || remote.upload_id || remote.upload?.id || ""),
      transferID: String(remote.transferId || remote.transfer_id || ""),
      offset: Math.max(0, Number(remote.offset || remote.completedBytes || remote.completed_bytes || 0)),
      state: folderEntryStatus(remote),
      error: String(remote.error || ""),
    };
  });
}

/** 上传单个目录条目；失败留在批次中，允许用户只重试失败项。 */
async function uploadFolderEntry(batch, entry) {
  if (batch.cancelled || entry.state === "SUCCESS") return entry;
  if (!entry.file?.size) {
    entry.state = "FAILED";
    entry.error = "不能上传空文件";
    return entry;
  }
  entry.state = "TRANSFERRING";
  entry.error = "";
  render();
  try {
    if (!entry.id) throw new Error("sidecar 未返回文件夹条目 ID");
    if (!entry.uploadId) {
      const created = await DriveApi.createFolderEntryUpload(batch.id, entry.id, entry.file, batch.controller.signal);
      entry.uploadId = uploadIDFromResponse(created);
      entry.offset = Math.max(entry.offset, Number(created?.offset || 0));
      entry.transferID = String(created?.transferID || created?.transfer_id || entry.transferID || "");
    } else if (!entry.offset) {
      entry.offset = await DriveApi.uploadOffset(entry.uploadId);
    }
    await uploadFileChunks(entry.file, {
      uploadId: entry.uploadId,
      offset: entry.offset,
      transferID: entry.transferID,
      signal: batch.controller.signal,
      cancelled: batch.cancelled,
    }, (progress) => {
      entry.offset = progress.offset;
      entry.transferID = progress.transferID;
      render();
    });
    entry.offset = entry.file.size;
    entry.state = "SUCCESS";
    entry.error = "";
  } catch (error) {
    if (batch.cancelled || error?.name === "AbortError") {
      entry.state = "CANCELLED";
    } else {
      entry.state = "FAILED";
      entry.error = messageOf(error, "目录条目上传失败");
    }
  }
  render();
  return entry;
}

/** 创建目录批次并用两个并发 worker 提交条目。 */
async function startFolderUpload(files) {
  if (!local.auth?.connected) {
    toast("请先连接 Google Drive", "ph-cloud-slash");
    return;
  }
  if (hasActiveUpload()) {
    toast("当前已有文件正在上传", "ph-hourglass-simple");
    return;
  }
  const list = Array.from(files || []);
  const manifest = createFolderManifest(list);
  if (!manifest.length) {
    toast("选择的目录为空，Google Drive 不会创建空目录", "ph-folder-open");
    const hint = $("#driveFolderUploadHint");
    if (hint) hint.textContent = "空目录不会上传，请选择至少包含一个文件的目录。";
    return;
  }
  const controller = new AbortController();
  const batch = {
    id: "",
    controller,
    cancelled: false,
    state: "PENDING",
    error: "",
    entries: manifest.map((item, index) => ({ ...item, file: list[index], state: "PENDING", offset: 0, error: "", id: "", uploadId: "" })),
  };
  local.folderUpload = batch;
  render();
  try {
    const created = await DriveApi.createFolderUpload(manifest, controller.signal, `folder-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, local.currentFolderId);
    batch.id = batchIDFromResponse(created);
    if (!batch.id) throw new Error("sidecar 未返回文件夹批次 ID");
    let remoteEntries = entriesFromResponse(created);
    if (!remoteEntries.length) {
      const status = await DriveApi.folderUploadStatus(batch.id);
      remoteEntries = entriesFromResponse(status);
    }
    batch.entries = mergeFolderEntries(manifest, list, remoteEntries);
    batch.state = "TRANSFERRING";
    render();
    await runWithConcurrency(batch.entries, FOLDER_UPLOAD_CONCURRENCY, (entry) => uploadFolderEntry(batch, entry));
    if (batch.cancelled) {
      batch.state = "CANCELLED";
    } else if (batch.entries.some((entry) => entry.state === "FAILED")) {
      batch.state = "FAILED";
      batch.error = "部分文件上传失败，可重试失败条目";
    } else {
      batch.state = "SUCCESS";
      toast("文件夹已上传，Drive 后台任务已排队", "ph-check-circle");
      await refreshFolderUploadStatus();
      await refresh();
    }
    render();
  } catch (error) {
    if (batch.cancelled || error?.name === "AbortError") return;
    batch.state = "FAILED";
    batch.error = messageOf(error, "创建文件夹上传失败");
    toast(batch.error, "ph-warning");
    render();
  }
}

/** 将服务端批次状态合并到本地条目，不覆盖浏览器刚确认的更细粒度 offset。 */
function applyFolderUploadStatus(value) {
  const batch = local.folderUpload;
  if (!batch || !value) return;
  const remoteBatch = batchFromResponse(value);
  const remoteEntries = entriesFromResponse(value);
  if (remoteBatch.state || remoteBatch.status) {
    const remoteState = folderEntryStatus(remoteBatch);
    const hasLocalFailure = batch.entries.some((entry) => entry.state === "FAILED" && entry.offset < entry.size);
    if (!(hasLocalFailure && remoteState === "PENDING")) batch.state = remoteState;
  }
  if (remoteBatch.error) batch.error = String(remoteBatch.error);
  for (const remote of remoteEntries) {
    const path = entryPath(remote);
    const entry = batch.entries.find((candidate) => (path && candidate.relativePath === path) || entryID(remote) === candidate.id);
    if (!entry) continue;
    const offset = Number(remote.offset || remote.completedBytes || remote.completed_bytes || 0);
    if (Number.isFinite(offset)) entry.offset = Math.max(entry.offset, offset);
    const state = folderEntryStatus(remote);
    const localBrowserFailure = entry.state === "FAILED" && state === "PENDING" && entry.offset < entry.size;
    if (state && !localBrowserFailure) entry.state = state;
    if (remote.error) entry.error = String(remote.error);
    if (!entry.uploadId) entry.uploadId = String(remote.uploadId || remote.upload_id || remote.upload?.id || "");
    if (!entry.transferID) entry.transferID = String(remote.transferId || remote.transfer_id || "");
  }
}

/** 轮询批次状态；失败时保留浏览器本地进度，下一轮可继续。 */
async function refreshFolderUploadStatus() {
  if (!local.folderUpload?.id || local.folderUpload.cancelled) return;
  try {
    const result = await DriveApi.folderUploadStatus(local.folderUpload.id);
    applyFolderUploadStatus(result);
    renderFolderUpload();
  } catch (error) {
    // 批次状态不是浏览器上传的关键路径，状态接口暂时不可用时不清除本地进度。
    local.folderUpload.statusError = messageOf(error, "无法读取文件夹上传进度");
  }
}

/** 取消整个目录批次，同时中止浏览器中的最多两个活动分片请求。 */
async function cancelFolderUpload() {
  const batch = local.folderUpload;
  if (!batch) return;
  batch.cancelled = true;
  batch.state = "CANCELLED";
  batch.controller.abort();
  if (batch.id) {
    try {
      await DriveApi.folderUploadAction(batch.id, "cancel");
    } catch (error) {
      toast(messageOf(error, "取消文件夹上传失败"), "ph-warning");
    }
  }
  render();
  toast("文件夹上传已取消", "ph-x-circle");
}

/** 重试单个失败条目，已存在的 uploadId 会从 sidecar 当前 offset 继续。 */
async function retryFolderEntry(entryIDValue) {
  const batch = local.folderUpload;
  const entry = batch?.entries.find((candidate) => candidate.id === entryIDValue);
  if (!batch || !entry || entry.state !== "FAILED") return;
  if (batch.cancelled) {
    batch.cancelled = false;
    batch.controller = new AbortController();
  }
  batch.error = "";
  entry.state = "PENDING";
  entry.error = "";
  render();
  // 浏览器分片已经完成、但 Drive worker 失败时，必须让 sidecar 恢复原
  // transfer；仅重跑本地 while 循环会因为 offset 已到末尾而错误显示成功。
  if (entry.transferID && entry.offset >= entry.size) {
    try {
      const result = await DriveApi.folderEntryAction(batch.id, entry.id, "retry", batch.controller.signal);
      const remote = entriesFromResponse(result).find((candidate) => entryID(candidate) === entry.id);
      if (remote) {
        entry.state = folderEntryStatus(remote);
        entry.error = String(remote.error || "");
        entry.transferID = String(remote.transferId || remote.transfer_id || entry.transferID);
      }
      batch.state = "TRANSFERRING";
      render();
      return;
    } catch (error) {
      entry.state = "FAILED";
      entry.error = messageOf(error, "恢复 Drive 传输失败");
      batch.state = "FAILED";
      render();
      throw error;
    }
  }
  await uploadFolderEntry(batch, entry);
  const failed = batch.entries.some((candidate) => candidate.state === "FAILED");
  batch.state = failed ? "FAILED" : batch.entries.every((candidate) => candidate.state === "SUCCESS") ? "SUCCESS" : "TRANSFERRING";
  if (!failed && batch.state === "SUCCESS") toast("失败条目已重试完成", "ph-check-circle");
  render();
}

async function retryFailedFolderEntries() {
  const batch = local.folderUpload;
  if (!batch) return;
  const failed = batch.entries.filter((entry) => entry.state === "FAILED");
  await runWithConcurrency(failed, FOLDER_UPLOAD_CONCURRENCY, (entry) => retryFolderEntry(entry.id));
  await refreshFolderUploadStatus();
  render();
}

/** 处理文件夹上传进度卡片上的重试/取消动作。 */
async function handleFolderUploadAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (action === "cancel-folder") {
    button.disabled = true;
    await cancelFolderUpload();
    return;
  }
  if (action === "retry-folder") {
    button.disabled = true;
    try {
      await retryFailedFolderEntries();
    } catch (error) {
      toast(messageOf(error, "重试文件夹上传失败"), "ph-warning");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "retry-folder-entry") {
    button.disabled = true;
    try {
      await retryFolderEntry(button.dataset.entryId);
    } catch (error) {
      toast(messageOf(error, "重试文件夹条目失败"), "ph-warning");
    } finally {
      button.disabled = false;
    }
  }
}

/**
 * 中止浏览器分片请求，并清理尚未进入 Drive worker 的本地暂存文件。
 */
async function cancelUpload() {
  const upload = local.upload;
  if (!upload) return;
  upload.cancelled = true;
  upload.controller.abort();
  if (upload.id && !upload.done) {
    try {
      await DriveApi.deleteUpload(upload.id);
    } catch (error) {
      toast(messageOf(error, "本地暂存清理失败"), "ph-warning");
    }
  }
  local.upload = null;
  render();
  toast("上传已取消", "ph-x-circle");
}

/** 进入 Drive 子目录；目录行只导航，不提供下载/导入动作。 */
async function openFolder(fileID, name) {
  if (!fileID) return;
  local.currentFolderId = fileID;
  local.currentFolder = { id: fileID, name: name || "未命名文件夹", mimeType: DRIVE_FOLDER_MIME };
  local.breadcrumbs = [...local.breadcrumbs, { id: fileID, name: name || "未命名文件夹" }];
  local.pageToken = "";
  local.nextPageToken = "";
  local.pageHistory = [];
  await refreshFilesPage({ folderId: fileID, pageToken: "" });
}

/** 处理文件列表中的目录导航、下载、导入和软删除按钮。 */
async function handleFileAction(event) {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const fileID = button.dataset.id;
  const action = button.dataset.action;
  if (!fileID) return;

  if (action === "open-folder") {
    await openFolder(fileID, button.dataset.name);
    return;
  }

  if (action === "download") {
    window.open(DriveApi.downloadUrl(fileID), "_blank", "noopener,noreferrer");
    return;
  }
  if (action === "import") {
    button.disabled = true;
    try {
      await DriveApi.importFile(fileID);
      toast("已加入字幕处理流水线", "ph-arrow-line-down");
      await refresh();
    } catch (error) {
      toast(messageOf(error, "创建导入任务失败"), "ph-warning");
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (action === "trash") {
    const name = button.dataset.name || "该文件";
    if (!window.confirm(`将“${name}”移入 Google Drive 回收站？`)) return;
    button.disabled = true;
    try {
      await DriveApi.trashFile(fileID);
      toast("文件已移入回收站", "ph-trash");
      await refresh();
    } catch (error) {
      toast(messageOf(error, "移入回收站失败"), "ph-warning");
    } finally {
      button.disabled = false;
    }
  }
}

/** 返回根目录或面包屑中指定的目录。 */
async function navigateToBreadcrumb(index) {
  const target = Number(index);
  if (!Number.isInteger(target) || target < 0) return;
  if (target === 0) {
    local.currentFolderId = "";
    local.currentFolder = null;
    local.breadcrumbs = [];
  } else {
    const crumb = local.breadcrumbs[target - 1];
    if (!crumb) return;
    local.currentFolderId = crumb.id;
    local.currentFolder = { ...crumb, mimeType: DRIVE_FOLDER_MIME };
    local.breadcrumbs = local.breadcrumbs.slice(0, target);
  }
  local.pageToken = "";
  local.nextPageToken = "";
  local.pageHistory = [];
  await refreshFilesPage({ folderId: local.currentFolderId, pageToken: "" });
}

async function handleBreadcrumbAction(event) {
  const button = event.target.closest("button[data-breadcrumb-index]");
  if (!button) return;
  button.disabled = true;
  try {
    await navigateToBreadcrumb(button.dataset.breadcrumbIndex);
  } finally {
    button.disabled = false;
  }
}

/** 以 pageToken 历史实现上一页/下一页切换，切换目录时历史会重置。 */
async function handlePaginationAction(event) {
  const button = event.target.closest("button[data-page-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.pageAction;
  let token = "";
  if (action === "next" && local.nextPageToken) {
    local.pageHistory.push(local.pageToken);
    token = local.nextPageToken;
  } else if (action === "previous" && local.pageHistory.length) {
    token = local.pageHistory.pop() || "";
  } else {
    return;
  }
  local.pageToken = token;
  await refreshFilesPage({ folderId: local.currentFolderId, pageToken: token });
}

/**
 * 处理后台传输的暂停、恢复和取消操作。
 */
async function handleTransferAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.id;
  const action = button.dataset.action;
  if (!id || !action) return;
  button.disabled = true;
  try {
    await DriveApi.transferAction(id, action);
    toast(`传输${action === "pause" ? "已暂停" : action === "resume" ? "已恢复" : "已取消"}`, "ph-check-circle");
    await refresh();
  } catch (error) {
    toast(messageOf(error, "更新传输状态失败"), "ph-warning");
  } finally {
    button.disabled = false;
  }
}

/**
 * 渲染整个 Drive 视图，保持 DOM 结构集中，减少各异步操作之间的竞态。
 */
function render() {
  renderAuth();
  renderUpload();
  renderFiles();
  renderTransfers();
  const refreshButton = $("#driveRefresh");
  if (refreshButton) refreshButton.disabled = local.refreshing;
}

/**
 * 渲染顶部连接状态、授权按钮和配置错误提示。
 */
function renderAuth() {
  const status = $("#driveStatus");
  const statusText = $("#driveStatusText");
  const hint = $("#driveAuthHint");
  const action = $("#driveAuthAction");
  const disconnectButton = $("#driveDisconnect");
  if (!status || !statusText || !hint || !action || !disconnectButton) return;

  const auth = local.auth;
  let tone = "offline";
  let label = "未连接";
  if (local.authError) {
    label = "sidecar 离线";
  } else if (auth?.connected) {
    tone = "online";
    label = "已连接";
  } else if (auth?.configured) {
    tone = "pending";
    label = auth.reauthorize ? "需要重新授权" : "等待授权";
  } else if (auth) {
    label = "待配置 OAuth";
  }
  status.className = `drive-status drive-status--${tone}`;
  statusText.textContent = label;

  if (auth?.connected) {
    action.hidden = true;
    disconnectButton.hidden = false;
    const expiry = auth.token_expiry ? ` · Token 到期 ${formatDate(auth.token_expiry)}` : "";
    hint.textContent = `已连接到本地 OAuth 账户${expiry}。文件会进入 sidecar 管理的 Subtitles AI 文件夹。`;
  } else {
    action.hidden = false;
    disconnectButton.hidden = true;
    action.disabled = Boolean(auth && !auth.configured) || Boolean(local.authError);
    action.querySelector("span").textContent = auth?.reauthorize ? "重新授权" : "连接 Google Drive";
    hint.textContent = local.authError
      || auth?.configuration_error
      || (auth?.configured ? "点击后会打开 Google 授权页面；完成后回到此处刷新状态。" : "请在 drive-service/config.local.json 配置 Google OAuth ClientID 和 ClientSecret。");
  }
}

/**
 * 渲染浏览器到 sidecar 的本地分片上传进度。
 */
function renderUpload() {
  renderSingleUpload();
  renderFolderUpload();
}

function renderSingleUpload() {
  const box = $("#driveUploadProgress");
  const name = $("#driveUploadName");
  const value = $("#driveUploadValue");
  const bar = $("#driveUploadBar");
  const bytes = $("#driveUploadBytes");
  const cancel = $("#driveUploadCancel");
  if (!box || !name || !value || !bar || !bytes || !cancel) return;

  const upload = local.upload;
  box.hidden = !upload;
  if (!upload) return;
  const percent = upload.file.size ? Math.min(100, Math.round((upload.offset / upload.file.size) * 100)) : 0;
  name.textContent = upload.file.name;
  value.textContent = upload.error || (upload.done ? "已排队" : `${percent}%`);
  bar.style.width = `${upload.done ? 100 : percent}%`;
  bytes.textContent = upload.done
    ? "本地分片已全部提交，Drive worker 正在继续"
    : `${formatBytes(upload.offset)} / ${formatBytes(upload.file.size)}`;
  cancel.disabled = upload.done;
  cancel.textContent = upload.done ? "已提交" : "取消";
}

/** 渲染目录批次总进度、失败条目重试和整批取消。 */
function renderFolderUpload() {
  const box = $("#driveFolderUploadProgress");
  const name = $("#driveFolderUploadName");
  const value = $("#driveFolderUploadValue");
  const bar = $("#driveFolderUploadBar");
  const bytes = $("#driveFolderUploadBytes");
  const entriesRoot = $("#driveFolderUploadEntries");
  const cancel = $("#driveFolderUploadCancel");
  if (!box || !name || !value || !bar || !bytes || !entriesRoot || !cancel) return;
  const upload = local.folderUpload;
  box.hidden = !upload;
  if (!upload) return;
  const summary = summarizeFolderProgress(upload);
  name.textContent = upload.entries[0]?.relativePath?.split("/")[0] || "文件夹上传";
  value.textContent = upload.error || (upload.state === "SUCCESS" ? "已排队" : upload.state === "CANCELLED" ? "已取消" : `${summary.percent}%`);
  bar.style.width = `${summary.percent}%`;
  bytes.textContent = `${summary.completedEntries}/${summary.totalEntries} 个文件 · ${formatBytes(summary.completedBytes)} / ${formatBytes(summary.totalBytes)}`;
  cancel.disabled = ["SUCCESS", "CANCELLED"].includes(upload.state);
  cancel.textContent = upload.state === "CANCELLED" ? "已取消" : upload.state === "SUCCESS" ? "已提交" : "取消";
  cancel.parentElement?.querySelectorAll("button[data-action=\"retry-folder\"]").forEach((node) => node.remove());
  const retryAll = upload.state === "FAILED"
    ? `<button class="btn btn--ghost btn--sm" type="button" data-action="retry-folder">重试失败</button>`
    : "";
  cancel.insertAdjacentHTML("beforebegin", retryAll);
  entriesRoot.innerHTML = upload.entries.map((entry) => {
    const entryName = escapeHtml(entry.relativePath || entry.name || "未命名文件");
    const state = escapeHtml(entry.state || "PENDING");
    const detail = entry.error
      ? `<span class="drive-folder-upload-entry__error">${escapeHtml(entry.error)}</span>`
      : `<span>${formatBytes(entry.offset)} / ${formatBytes(entry.size)}</span>`;
    const retry = entry.state === "FAILED"
      ? `<button class="btn btn--ghost btn--sm" type="button" data-action="retry-folder-entry" data-entry-id="${escapeHtml(entry.id)}">重试</button>`
      : "";
    return `<div class="drive-folder-upload-entry">
      <div class="drive-folder-upload-entry__main"><strong title="${entryName}">${entryName}</strong><span class="drive-folder-upload-entry__state drive-folder-upload-entry__state--${state.toLowerCase()}">${state}</span><small>${detail}</small></div>
      ${retry}
    </div>`;
  }).join("");
}

/** 面包屑状态独立渲染，避免文件行刷新时重建导航节点。 */
function renderBreadcrumbs() {
  const root = $("#driveBreadcrumbs");
  if (!root) return;
  const items = [{ id: "", name: ROOT_FOLDER_LABEL }, ...local.breadcrumbs];
  root.innerHTML = items.map((item, index) => {
    const label = escapeHtml(item.name || ROOT_FOLDER_LABEL);
    const separator = index ? `<span class="drive-breadcrumbs__separator" aria-hidden="true">/</span>` : "";
    return `${separator}<button class="drive-breadcrumbs__item${index === items.length - 1 ? " is-current" : ""}" type="button" data-breadcrumb-index="${index}" ${index === items.length - 1 ? "aria-current=\"page\"" : ""}>${label}</button>`;
  }).join("");
}

/** 分页控件独立渲染，能在列表请求期间保留当前页并禁用重复请求。 */
function renderPagination() {
  const root = $("#driveFilesPagination");
  if (!root) return;
  const hasPrevious = local.pageHistory.length > 0;
  const hasNext = Boolean(local.nextPageToken);
  root.innerHTML = `<button class="btn btn--ghost btn--sm" type="button" data-page-action="previous" ${hasPrevious && !local.filesLoading ? "" : "disabled"}>上一页</button><span>${local.filesLoading ? "读取中…" : (hasPrevious ? "分页浏览" : "第一页")}</span><button class="btn btn--ghost btn--sm" type="button" data-page-action="next" ${hasNext && !local.filesLoading ? "" : "disabled"}>下一页</button>`;
}

function renderFileRow(file) {
  const name = escapeHtml(file.name || "未命名文件");
  const id = escapeHtml(file.id);
  const mime = escapeHtml(shortMime(file.mimeType));
  if (isDriveFolder(file)) {
    return `<article class="drive-file drive-file--folder" data-action="open-folder" data-id="${id}" data-name="${name}" role="button" tabindex="0" aria-label="打开文件夹 ${name}">
      <div class="drive-file__icon" aria-hidden="true"><i class="ph ph-folder-open"></i></div>
      <div class="drive-file__main"><strong class="drive-file__name" title="${name}">${name}</strong><span class="drive-file__meta">文件夹 · 点击打开</span></div>
      <div class="drive-file__actions"><i class="ph ph-caret-right" aria-hidden="true"></i></div>
    </article>`;
  }
  const canDownload = file.capabilities?.canDownload !== false;
  return `<article class="drive-file">
    <div class="drive-file__icon" aria-hidden="true"><i class="ph ${fileIcon(file.mimeType)}"></i></div>
    <div class="drive-file__main"><strong class="drive-file__name" title="${name}">${name}</strong><span class="drive-file__meta">${mime} · ${formatBytes(file.size)} · ${formatDate(file.modifiedTime)}</span></div>
    <div class="drive-file__actions">
      <button class="iconbtn iconbtn--accent" type="button" data-action="download" data-id="${id}" title="下载" aria-label="下载 ${name}" ${canDownload ? "" : "disabled"}><i class="ph ph-download-simple" aria-hidden="true"></i></button>
      <button class="iconbtn" type="button" data-action="import" data-id="${id}" title="导入字幕流水线" aria-label="导入 ${name}" ${canDownload ? "" : "disabled"}><i class="ph ph-arrow-line-down" aria-hidden="true"></i></button>
      <button class="iconbtn iconbtn--danger" type="button" data-action="trash" data-id="${id}" data-name="${name}" title="移入回收站" aria-label="删除 ${name}"><i class="ph ph-trash" aria-hidden="true"></i></button>
    </div>
  </article>`;
}

/** 渲染 Drive 文件表格；空态、目录导航和文件动作彼此独立。 */
function renderFiles() {
  const root = $("#driveFiles");
  const count = $("#driveFileCount");
  if (!root || !count) return;
  renderBreadcrumbs();
  renderPagination();
  count.textContent = local.auth?.connected ? `${local.files.length} 项` : "未连接";

  if (local.authError) {
    root.innerHTML = emptyState("ph-plugs-connected", "无法连接 sidecar", local.authError);
    return;
  }
  if (!local.auth?.connected) {
    root.innerHTML = emptyState("ph-lock-key", "连接后查看文件", "Google Drive 授权完成后，这里会显示 Subtitles AI 文件夹内容。");
    return;
  }
  if (local.filesLoading && !local.files.length) {
    root.innerHTML = emptyState("ph-spinner-gap ph-spin", "正在读取文件", "正在从 Google Drive 读取当前目录。");
    return;
  }
  if (local.filesError) {
    root.innerHTML = emptyState("ph-warning", "文件列表读取失败", local.filesError);
    return;
  }
  if (!local.files.length) {
    root.innerHTML = emptyState("ph-folder-open", "还没有文件", "上传一个视频或字幕文件，完成后会出现在这里。");
    return;
  }

  root.innerHTML = local.files.map(renderFileRow).join("");
}

/**
 * 渲染上传到 Drive 和导入 Python 的持久化传输队列。
 */
function renderTransfers() {
  const root = $("#driveTransfers");
  const count = $("#driveTransferCount");
  if (!root || !count) return;
  count.textContent = local.transfers.length ? `${local.transfers.length} 项` : "空闲";
  if (local.transfersError) {
    root.innerHTML = emptyState("ph-warning", "队列读取失败", local.transfersError);
    return;
  }
  if (!local.transfers.length) {
    root.innerHTML = emptyState("ph-check-circle", "暂无传输任务", "上传或导入 Drive 文件后，后台进度会显示在这里。");
    return;
  }

  root.innerHTML = local.transfers.map((transfer) => {
    const total = Number(transfer.total_bytes || 0);
    const transferred = Math.max(0, Number(transfer.transferred_bytes || 0));
    const complete = transfer.state === "SUCCESS";
    const percent = complete ? 100 : total > 0 ? Math.min(100, Math.round((transferred / total) * 100)) : 0;
    const state = escapeHtml(transfer.state || "PENDING");
    const stateLabel = escapeHtml(STATUS_LABELS[transfer.state] || transfer.state || "等待中");
    const fileName = escapeHtml(transfer.file_name || transfer.python_task_id || "未命名任务");
    const canPause = ["PENDING", "TRANSFERRING", "RETRYING", "VERIFYING"].includes(transfer.state);
    const canResume = transfer.state === "PAUSED";
    const canCancel = !["SUCCESS", "FAILED", "CANCELLED"].includes(transfer.state);
    const action = canPause
      ? `<button class="btn btn--ghost btn--sm" type="button" data-action="pause" data-id="${escapeHtml(transfer.id)}">暂停</button>`
      : canResume
        ? `<button class="btn btn--ghost btn--sm" type="button" data-action="resume" data-id="${escapeHtml(transfer.id)}">恢复</button>`
        : "";
    const cancel = canCancel
      ? `<button class="btn btn--ghost btn--sm drive-transfer__cancel" type="button" data-action="cancel" data-id="${escapeHtml(transfer.id)}">取消</button>`
      : "";
    const error = transfer.error ? `<p class="drive-transfer__error">${escapeHtml(transfer.error)}</p>` : "";
    return `<article class="drive-transfer">
      <div class="drive-transfer__top">
        <div class="drive-transfer__identity">
          <span class="drive-transfer__kind">${escapeHtml(KIND_LABELS[transfer.kind] || transfer.kind || "传输")}</span>
          <strong title="${fileName}">${fileName}</strong>
        </div>
        <span class="drive-transfer__state drive-transfer__state--${state.toLowerCase()}">${stateLabel}</span>
      </div>
      <div class="drive-progress"><span style="width:${percent}%"></span></div>
      <div class="drive-transfer__bottom">
        <span>${formatBytes(transferred)} / ${formatBytes(total)} <b>${percent}%</b></span>
        <div class="drive-transfer__actions">${action}${cancel}</div>
      </div>
      ${error}
    </article>`;
  }).join("");
}

/**
 * 生成列表空状态，统一文件列表和队列的视觉节奏。
 */
function emptyState(icon, title, detail) {
  return `<div class="drive-empty"><i class="ph ${icon}" aria-hidden="true"></i><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span></div>`;
}

/**
 * 将异常转换成短文本，避免把网络对象直接展示到页面。
 */
function messageOf(error, fallback) {
  return String(error?.message || error || fallback);
}

/**
 * 按文件 MIME 类型选择一个稳定的 Phosphor 图标。
 */
function fileIcon(mime = "") {
  if (mime.startsWith("video/")) return "ph-film-strip";
  if (mime.startsWith("audio/")) return "ph-waveform";
  if (mime.includes("subtitle") || mime.includes("text")) return "ph-text-aa";
  if (mime.includes("zip") || mime.includes("compressed")) return "ph-file-zip";
  return "ph-file";
}

/**
 * 把过长 MIME 类型压缩成适合列表展示的标签。
 */
function shortMime(mime = "") {
  if (!mime) return "未知类型";
  return mime.replace("application/", "").replace("video/", "video/").replace("audio/", "audio/");
}

/**
 * 格式化字节数，统一使用二进制单位以匹配上传分片和 Drive 元数据。
 */
function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const amount = bytes / 1024 ** index;
  return `${amount >= 100 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

/**
 * 格式化 Drive 的 RFC3339 时间戳；异常值回退为“未知时间”。
 */
function formatDate(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知时间";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

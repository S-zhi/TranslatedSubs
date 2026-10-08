/* 视频审片室：剧院感舞台 + 已完成清单。仅在选中变化时重建播放器，避免打断播放。 */

import { LANG_LABEL } from "./constants.js";
import { $, el, escapeHtml } from "./utils.js";
import { state, subscribe, completedTasks, setPreviewId } from "./store.js";
import { Api, USE_MOCK } from "./api.js";
import { toast } from "./toast.js";

let stageEl, listEl;
let lastStageId = undefined;
let selectedTrack = "subtitled";
const VIDEO_AVAILABILITY_TIMEOUT_MS = 5000;
export const VIDEO_MISSING_TITLE = "视频找不到了";

export function isVideoKnownMissing(task, videoKind) {
  return task?.resourceStatus === "MISSING" && (
    videoKind === "video" || task.needSubtitle === false
  );
}

export async function checkVideoAvailability(
  videoUrl,
  fetchImpl = fetch,
  timeoutMs = VIDEO_AVAILABILITY_TIMEOUT_MS,
) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(videoUrl, {
      method: "HEAD",
      cache: "no-store",
      signal: controller.signal,
    });
    return response.ok;
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

export function initPreview() {
  stageEl = $("#previewStage");
  listEl = $("#previewList");
  subscribe(renderPreview);
}

function renderPreview() {
  if (state.view !== "preview") return;
  const completed = completedTasks();
  renderList(completed);

  const sel = completed.find((t) => t.id === state.previewId) || completed[0] || null;
  const selId = sel ? sel.id : null;
  if (selId !== lastStageId) {
    renderStage(sel);
    lastStageId = selId;
  }
}

function renderList(completed) {
  listEl.replaceChildren();
  if (completed.length === 0) {
    const e = el("div", "state");
    e.innerHTML = `
      <div class="state__icon"><i class="ph ph-film-slate" aria-hidden="true"></i></div>
      <div class="state__title">还没有成品</div>
      <div class="state__desc">完成一个任务后会出现在这里。</div>`;
    listEl.append(e);
    return;
  }
  const selId = state.previewId || (completed[0] && completed[0].id);
  completed.forEach((t) => {
    const item = el("button", "pvitem" + (t.id === selId ? " is-active" : ""));
    item.type = "button";
    item.dataset.id = t.id;
    item.innerHTML = `
      <span class="pvitem__thumb" aria-hidden="true"><i class="ph ph-film-strip"></i></span>
      <span class="pvitem__body">
        <span class="pvitem__title">${escapeHtml(t.title || "成品视频")}</span>
        <span class="pvitem__meta">${LANG_LABEL[t.targetLang] || t.targetLang} · ${
      t.burn === "hard" ? "硬字幕" : "软字幕（可开关）"
    }</span>
      </span>`;
    item.addEventListener("click", () => setPreviewId(t.id));
    listEl.append(item);
  });
}

function renderStage(sel) {
  stageEl.replaceChildren();

  if (!sel) {
    stageEl.append(stageEmpty("ph-monitor-play", "选择一个成品", "从右侧已完成列表挑一个开始播放。"));
    return;
  }
  if (USE_MOCK) {
    stageEl.append(stageEmpty("ph-flask", "示例模式", "当前为前端示例数据，没有真实视频可播放。"));
    return;
  }

  const hasSubtitledVideo = sel.needSubtitle !== false;
  if (selectedTrack === "dubbed" && sel.ttsStatus !== "SUCCESS") {
    selectedTrack = hasSubtitledVideo ? "subtitled" : "source";
  }
  const track = hasSubtitledVideo ? selectedTrack : "source";
  const videoKind = track === "source" ? "source" : track === "dubbed" ? "dubbed" : "video";
  const trackLabel = track === "source" ? "源视频" : track === "dubbed" ? "配音视频" : "带字幕视频";
  const stage = el("div", "stage");

  const screen = el("div", "stage__screen");

  const bar = el("div", "stage__bar");
  const title = el("div", "stage__title");
  title.textContent = sel.title || "成品视频";
  const tags = el("div", "stage__tags");
  const tracks = el("div", "stage__tracks");
  const sourceTrack = stageTrack("source", "源视频", track === "source");
  const subtitledTrack = stageTrack("subtitled", "带字幕视频", track === "subtitled");
  const dubbedTrack = stageTrack("dubbed", "配音视频", track === "dubbed");
  if (!hasSubtitledVideo) {
    subtitledTrack.disabled = true;
    subtitledTrack.title = "该任务未生成字幕成品";
  }
  if (sel.ttsStatus !== "SUCCESS") {
    dubbedTrack.disabled = true;
    dubbedTrack.title = sel.ttsStatus === "FAILED" ? (sel.ttsError || "配音生成失败") : "配音尚未生成";
  }
  tracks.append(sourceTrack, subtitledTrack, dubbedTrack);
  tracks.addEventListener("click", (event) => {
    const next = event.target.closest("[data-track]")?.dataset.track;
    if (!next || next === track || (next === "subtitled" && !hasSubtitledVideo) || (next === "dubbed" && sel.ttsStatus !== "SUCCESS")) return;
    selectedTrack = next;
    renderStage(sel);
  });
  tags.append(
    tracks,
    stageTag(`${LANG_LABEL[sel.sourceLang] || sel.sourceLang} → ${LANG_LABEL[sel.targetLang] || sel.targetLang}`),
    stageTag(sel.mode === "bilingual" ? "双语" : "单语"),
    stageTag(sel.burn === "hard" ? "硬字幕" : "软字幕（可开关）")
  );
  const actions = el("div", "stage__actions");
  const folder = el("button", "btn btn--ghost btn--sm");
  folder.innerHTML = `<i class="ph ph-folder-open"></i><span>打开文件夹</span>`;
  folder.addEventListener("click", () => openFolder(sel));
  const dlVideo = el("button", "btn btn--primary btn--sm");
  dlVideo.disabled = true;
  dlVideo.innerHTML = `<i class="ph ph-download-simple"></i><span>下载${trackLabel}</span>`;
  dlVideo.addEventListener("click", () => open(sel, videoKind));
  const dlSub = el("button", "btn btn--ghost btn--sm");
  dlSub.innerHTML = `<i class="ph ph-closed-captioning"></i><span>下载字幕</span>`;
  dlSub.addEventListener("click", () => open(sel, "subtitle"));
  if (!hasSubtitledVideo) {
    dlSub.disabled = true;
    dlSub.title = "该任务未生成字幕";
  }
  actions.append(folder, dlVideo, dlSub);

  bar.append(title, tags, actions);
  stage.append(screen, bar);
  stageEl.append(stage);
  if (isVideoKnownMissing(sel, videoKind)) {
    showStageVideoMissing(screen, dlVideo, trackLabel);
    return;
  }

  screen.append(stageVideoChecking(trackLabel));
  // 先确认资源可用，再创建 <video>。这样 409 不会暴露原生播放器的加载转圈。
  void prepareStageVideo(stage, screen, dlVideo, Api.downloadUrl(sel.id, videoKind), trackLabel);
}

async function prepareStageVideo(stage, screen, dlVideo, videoUrl, trackLabel) {
  const available = await checkVideoAvailability(videoUrl);
  if (!stageEl.contains(stage)) return;
  if (!available) {
    showStageVideoMissing(screen, dlVideo, trackLabel);
    return;
  }

  const video = el("video");
  video.controls = true;
  video.preload = "metadata";
  video.addEventListener("error", () => {
    if (stageEl.contains(stage)) showStageVideoMissing(screen, dlVideo, trackLabel);
  }, { once: true });
  screen.replaceChildren(video);
  dlVideo.disabled = false;
  video.src = videoUrl;
}

function showStageVideoMissing(screen, dlVideo, trackLabel) {
  screen.classList.add("is-unavailable");
  screen.replaceChildren(stageVideoMissing(trackLabel));
  dlVideo.className = "btn btn--ghost btn--sm";
  dlVideo.disabled = true;
  dlVideo.innerHTML = `<i class="ph ph-video-camera-slash"></i><span>${trackLabel}不可用</span>`;
}

function open(sel, kind) {
  if (USE_MOCK) {
    toast("示例模式：下载占位");
    return;
  }
  window.open(Api.downloadUrl(sel.id, kind), "_blank");
}

// 打开当前预览视频对应的本地产物文件夹。
async function openFolder(sel) {
  try {
    await Api.openFolder(sel.id);
    toast(USE_MOCK ? "示例模式：文件夹打开占位" : "已打开任务文件夹", "ph-folder-open");
  } catch (e) {
    toast(e.message || "打开文件夹失败", "ph-warning-circle");
  }
}

function stageTag(text) {
  const s = el("span", "stage__tag");
  s.textContent = text;
  return s;
}

function stageTrack(id, label, active) {
  const button = el("button", "stage__track" + (active ? " is-active" : ""));
  button.type = "button";
  button.dataset.track = id;
  button.setAttribute("aria-pressed", String(active));
  button.innerHTML = `<i class="ph ${id === "source" ? "ph-video-camera" : "ph-closed-captioning"}" aria-hidden="true"></i><span>${label}</span>`;
  return button;
}

function stageEmpty(icon, title, desc) {
  const e = el("div", "stage__empty");
  e.innerHTML = `
    <div class="state__icon"><i class="ph ${icon}" aria-hidden="true"></i></div>
    <div class="state__title">${title}</div>
    <div class="state__desc">${desc}</div>`;
  return e;
}

function stageVideoMissing(trackLabel) {
  const e = el("div", "stage__missing");
  e.innerHTML = `
    <span class="stage__missing-kicker">PREVIEW UNAVAILABLE</span>
    <div class="stage__missing-title">${VIDEO_MISSING_TITLE}</div>
    <p class="stage__missing-desc">${trackLabel}文件可能已被清理或移动。</p>
    <p class="stage__missing-hint">请返回任务页重新处理后，再回来预览。</p>`;
  return e;
}

function stageVideoChecking(trackLabel) {
  const e = el("div", "stage__checking");
  e.innerHTML = `
    <span class="stage__checking-kicker">CHECKING AVAILABILITY</span>
    <div class="stage__checking-title">正在确认${trackLabel}</div>
    <p>资源确认后会自动开始加载。</p>`;
  return e;
}

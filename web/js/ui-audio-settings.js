/* 音频增强设置：读取并保存后端运行时配置。 */

import { $ } from "./utils.js";
import { Api } from "./api.js";
import { toast } from "./toast.js";

function setStatus(kind, text) {
  const node = $("#audioSettingsStatus");
  if (!node) return;
  node.className = `engine-status engine-status--${kind}`;
  node.textContent = text;
}

function fill(data) {
  $("#audioSeparationEnabled").checked = data.enabled === true;
  $("#audioSeparationModel").value = data.model || "htdemucs";
  $("#audioSeparationThreads").value = data.threads || 1;
  $("#audioSeparationTimeout").value = data.timeout || 1800;
  const notice = $("#audioSettingsNotice");
  if (notice) {
    notice.hidden = !data.message;
    notice.textContent = data.message || "";
  }
  const meta = $("#audioSettingsMeta");
  if (meta) meta.textContent = data.ready ? "Demucs 与 FFmpeg 已就绪" : "运行环境尚未就绪";
  setStatus(data.ready ? "available" : "unknown", data.ready ? "已就绪" : "待安装");
}

async function refresh() {
  setStatus("checking", "正在读取");
  try {
    fill(await Api.getAudioSettings());
  } catch (error) {
    setStatus("unavailable", "读取失败");
    toast(error.message || "读取音频设置失败", "ph-warning-circle");
  }
}

async function save(event) {
  event.preventDefault();
  const button = $("#audioSettingsSave");
  button.disabled = true;
  try {
    const data = await Api.updateAudioSettings({
      enabled: $("#audioSeparationEnabled").checked,
      model: $("#audioSeparationModel").value,
      threads: Number($("#audioSeparationThreads").value),
      timeout: Number($("#audioSeparationTimeout").value),
    });
    fill(data);
    toast(data.enabled && !data.ready ? "设置已保存，但当前环境未安装 Demucs" : "音频设置已保存", "ph-check-circle");
  } catch (error) {
    toast(error.message || "保存音频设置失败", "ph-warning-circle");
  } finally {
    button.disabled = false;
  }
}

export function initAudioSettings() {
  const form = $("#audioSettingsForm");
  if (!form) return;
  form.addEventListener("submit", save);
  document.addEventListener("viewchange", (event) => {
    if (event.detail?.view === "other-settings" && event.detail?.settingsTab === "audio") refresh();
  });
  refresh();
}

/* Optional per-task instance choices, with explicit provenance for bound controls. */

import { Api } from "./api.js";
import { escapeHtml } from "./utils.js";
import { normalizeWhisperModel } from "./task-whisper.js";
import { PLUGIN_STAGES, resolveTaskPluginBindings, selectedPluginInstances, taskPluginStages } from "./plugin-settings.js";

export function taskPluginContext(form) {
  return {
    sourceType: form.querySelector?.("#url")?.dataset?.sourceType === "upload" || form.elements.videoFile?.files?.length ? "upload" : "url",
    needSubtitle: form.elements.needSubtitle.value === "on",
    ttsEnabled: form.elements.ttsEnabled.checked,
  };
}

export function collectTaskPluginInstances(form) {
  const choices = Object.fromEntries(Array.from(form.querySelectorAll?.("[data-task-plugin-stage]") || []).map((select) => [select.dataset.taskPluginStage, select.value]));
  return selectedPluginInstances(choices, taskPluginContext(form));
}

export function createTaskPluginController({ root, form, api = Api, getWhisperState = () => null, onChange = () => {} }) {
  if (!root) return null;
  const list = root.querySelector("#taskPluginStages");
  const feedback = root.querySelector("#taskPluginFeedback");
  const refreshButton = root.querySelector("#taskPluginRefresh");
  let snapshot = { instances: [], defaults: {} };
  let settings = {};
  let engines = [];
  let selections = {};
  let resolution = { resolved: {}, errors: [] };
  let loaded = false;
  let loading = false;
  let loadError = "";
  let pending = null;
  let sequence = 0;
  const savedValues = new Map();

  const control = (selector) => form.querySelector(selector);
  const boundHint = (binding) => `来源：${binding.explicit ? "任务指定" : "阶段默认"}插件「${binding.instance.name}」`;

  function setControlValue(node, value, label) {
    if (!Array.from(node.options || []).some((option) => option.value === value)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label || value;
      option.dataset.pluginBoundOption = "true";
      node.append(option);
    }
    node.value = value;
  }

  function bindControl(node, value, label, active, normalDisabled, normalValue) {
    if (!node) return;
    if (active) {
      if (!savedValues.has(node)) savedValues.set(node, node.value);
      setControlValue(node, value, label);
      node.disabled = true;
      node.dataset.pluginBound = "true";
    } else {
      if (savedValues.has(node)) {
        const restore = normalValue || savedValues.get(node);
        savedValues.delete(node);
        Array.from(node.options || []).filter((option) => option.dataset?.pluginBoundOption).forEach((option) => option.remove());
        if (Array.from(node.options || []).some((option) => option.value === restore)) node.value = restore;
      }
      delete node.dataset.pluginBound;
      node.disabled = normalDisabled;
    }
  }

  function applyControls() {
    const task = taskPluginContext(form);
    const whisper = getWhisperState();
    const transcribe = resolution.resolved.transcribe;
    const translate = resolution.resolved.translate;
    const tts = resolution.resolved.tts;
    const modelBound = task.needSubtitle && Boolean(transcribe?.instance.model_ref);
    const engineBound = task.needSubtitle && Boolean(translate?.instance.engine_ref || translate?.instance.model_ref);
    const voiceBound = task.needSubtitle && task.ttsEnabled && Boolean(tts?.instance.model_ref);
    bindControl(control("#model"), normalizeWhisperModel(transcribe?.model?.model), transcribe?.model?.name || "绑定模型不可用", modelBound,
      !task.needSubtitle || !whisper?.ok || whisper?.locked, whisper?.locked ? whisper.model : null);
    const llm = Boolean(translate?.instance.model_ref);
    bindControl(control("#engine"), llm ? "plugin-bound-llm" : translate?.engine?.id || "", llm ? `插件绑定 LLM · ${translate?.model?.name || "不可用"}` : translate?.engine?.name || "绑定引擎不可用", engineBound, !task.needSubtitle);
    bindControl(form.elements.ttsVoice, tts?.model?.voice || "auto", tts?.model?.voice || "自动", voiceBound, !task.needSubtitle);
    const modelHint = control("#whisperModelHint");
    if (modelHint) {
      modelHint.textContent = modelBound ? `${boundHint(transcribe)} → Whisper · ${transcribe.model?.name || transcribe.instance.model_ref.card_id}` : whisper?.message || "沿用任务 / 当前 Whisper 模型";
      modelHint.classList.toggle("is-error", modelBound ? !transcribe.model || transcribe.model.enabled === false || (transcribe.model.status && transcribe.model.status !== "READY") : !whisper?.ok);
    }
    const engineHint = control("#taskEngineBindingHint");
    if (engineHint) engineHint.textContent = engineBound ? `${boundHint(translate)} → ${llm ? "LLM" : "引擎"} · ${translate.model?.name || translate.engine?.name || "绑定不可用"}` : "沿用任务选择的翻译引擎";
    const voiceHint = control("#taskTtsBindingHint");
    if (voiceHint) voiceHint.textContent = voiceBound ? `${boundHint(tts)} → TTS · ${tts.model?.name || "绑定不可用"}` : "沿用任务 / 当前 TTS 模型";
  }

  function render() {
    const task = taskPluginContext(form);
    const visible = new Set(taskPluginStages(task));
    list.innerHTML = PLUGIN_STAGES.map((stage) => {
      const instances = snapshot.instances.filter((item) => item.stage === stage.id && item.enabled);
      const chosen = selections[stage.id] || "";
      const defaultInstance = snapshot.instances.find((item) => item.id === snapshot.defaults[stage.id]);
      const defaultText = defaultInstance ? `跟随默认 · ${defaultInstance.name}` : "跟随默认";
      const missing = chosen && !instances.some((item) => item.id === chosen);
      const bound = resolution.resolved[stage.id];
      const hint = bound?.instance.engine_ref ? `引擎：${bound.engine?.name || bound.instance.engine_ref.id}`
        : bound?.instance.model_ref ? `${bound.instance.model_ref.kind.toUpperCase()}：${bound.model?.name || bound.instance.model_ref.card_id}`
          : "沿用任务 / 当前模型";
      return `<label class="param"${visible.has(stage.id) ? "" : " hidden"}><span class="param__label">${stage.label}</span><select class="select" data-task-plugin-stage="${stage.id}" aria-label="${stage.label}插件实例"${!loaded || !visible.has(stage.id) || loading ? " disabled" : ""}><option value="">${escapeHtml(defaultText)}</option>${instances.map((instance) => `<option value="${escapeHtml(instance.id)}"${chosen === instance.id ? " selected" : ""}>${escapeHtml(instance.name)}</option>`).join("")}${missing ? `<option value="${escapeHtml(chosen)}" disabled selected>${escapeHtml(chosen)}（已删除或停用）</option>` : ""}</select><small class="param__hint">${escapeHtml(hint)}</small></label>`;
    }).join("");
    const errors = loadError ? [loadError] : resolution.errors;
    feedback.textContent = loading ? "正在读取插件设置…" : errors.length ? errors.join(" ") : "未选择的阶段在提交时跟随默认实例。人声分离仅在音频增强或配音需要时运行。";
    feedback.classList.toggle("is-error", !loading && errors.length > 0);
    feedback.setAttribute("role", errors.length ? "alert" : "status");
    refreshButton.disabled = loading;
  }

  function sync() {
    resolution = loaded ? resolveTaskPluginBindings({ snapshot, selections, task: taskPluginContext(form), settings, engines }) : { resolved: {}, errors: [] };
    render();
    applyControls();
    onChange();
  }

  async function refresh({ force = false } = {}) {
    if (pending) return force ? pending.then(() => refresh()) : pending;
    const requestSequence = ++sequence;
    loading = true;
    sync();
    pending = (async () => {
      try {
        const [instances, models, engineList] = await Promise.all([api.listPluginInstances(), api.getModelSettings(), api.listTranslationEngines()]);
        if (requestSequence !== sequence) return false;
        snapshot = instances;
        settings = models;
        engines = engineList;
        loaded = true;
        loadError = "";
        return true;
      } catch (error) {
        loadError = `无法确认插件设置，请刷新后重试。${error.message || ""}`;
        return false;
      } finally {
        loading = false;
        pending = null;
        sync();
      }
    })();
    return pending;
  }

  function externalTranscriber() {
    const instance = resolution.resolved.transcribe?.instance;
    if (!instance) return false;
    if (instance.plugin_id && instance.plugin_id !== "builtin.whisper") return true;
    if (instance.model_ref) return false;
    return ["http", "custom", "custom_http"].includes(String(instance.config?.backend || "").trim().toLowerCase());
  }

  function requiresWhisperInventory() {
    return !resolution.resolved.transcribe?.instance.model_ref && !externalTranscriber();
  }

  function whisperReady() {
    const bound = resolution.resolved.transcribe;
    // Remote/custom plugins validate their own runtime dependencies during preflight.
    if (externalTranscriber()) return true;
    if (!bound?.instance.model_ref) return Boolean(getWhisperState()?.ok);
    return Boolean(bound.model && bound.model.enabled !== false && (!bound.model.status || bound.model.status === "READY"));
  }

  function applyPayload(payload) {
    const result = { ...payload, pluginInstances: selectedPluginInstances(selections, taskPluginContext(form)) };
    const { transcribe, translate, tts } = resolution.resolved;
    if (transcribe?.model) result.model = normalizeWhisperModel(transcribe.model.model);
    if (translate?.instance.model_ref) result.engine = "deepseek";
    else if (translate?.engine) result.engine = translate.engine.id;
    if (tts?.model) result.ttsVoice = tts.model.voice || "auto";
    return result;
  }

  list.addEventListener("change", (event) => {
    const stage = event.target.dataset?.taskPluginStage;
    if (!stage) return;
    selections = { ...selections, [stage]: event.target.value };
    sync();
  });
  refreshButton.addEventListener("click", () => refresh({ force: true }));
  form.addEventListener("change", (event) => { if (["needSubtitle", "ttsEnabled", "videoFile"].includes(event.target.name)) sync(); });
  document.addEventListener("plugin-settings-change", () => refresh({ force: true }));
  document.addEventListener("model-settings-change", () => refresh({ force: true }));
  document.addEventListener("translation-engines-change", () => refresh({ force: true }));
  document.addEventListener("viewchange", (event) => { if (event.detail?.view === "tasks") refresh({ force: true }); });
  return {
    refresh, sync, applyControls, applyPayload, whisperReady, requiresWhisperInventory,
    downloadFingerprint: () => JSON.stringify(resolution.resolved.download?.instance || null),
    isReady: () => loaded && !loading && !loadError && !resolution.errors.length,
    whisperBound: () => Boolean(resolution.resolved.transcribe?.instance.model_ref),
    llmBound: () => Boolean(resolution.resolved.translate?.instance.model_ref),
    error: () => loadError || resolution.errors.join(" "),
  };
}

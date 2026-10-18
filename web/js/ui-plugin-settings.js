/* Plugin instances share the existing settings styles and model/engine credentials. */

import { Api } from "./api.js";
import { $, escapeHtml } from "./utils.js";
import { state, subscribe } from "./store.js";
import {
  PLUGIN_STAGES, bindingValue, editablePluginFields, parsePluginConfig,
  pluginBindingOptions, pluginErrorMessage, pluginForInstance, pluginKey, refsForBinding, stageLabel,
} from "./plugin-settings.js";

const clone = (value) => JSON.parse(JSON.stringify(value));

export function pluginConfigFields(schema, config = {}) {
  return editablePluginFields(schema).map(([key, spec]) => {
    const value = config[key];
    const label = escapeHtml(spec.label || key);
    const common = `data-plugin-config="${escapeHtml(key)}" aria-label="${label}"`;
    let input;
    if (spec.enum || spec.type === "boolean") {
      const choices = spec.enum || [true, false];
      const defaultLabel = spec.default !== undefined ? `默认：${spec.default}` : "沿用默认";
      input = `<select ${common}><option value="">${escapeHtml(defaultLabel)}</option>${choices.map((item) => {
        const text = item === null ? "无" : item === true ? "开启" : item === false ? "关闭" : item;
        return `<option value="${escapeHtml(JSON.stringify(item))}"${value !== undefined && value === item ? " selected" : ""}>${escapeHtml(text)}</option>`;
      }).join("")}</select>`;
    } else {
      const numeric = ["number", "integer"].includes(spec.type);
      input = `<input ${common} type="${numeric ? "number" : "text"}" value="${escapeHtml(value ?? "")}"${numeric ? ` step="${spec.type === "integer" ? "1" : "any"}"` : ""}${spec.minimum !== undefined ? ` min="${spec.minimum}"` : ""}${spec.maximum !== undefined ? ` max="${spec.maximum}"` : ""} placeholder="${escapeHtml(spec.default === undefined ? "留空沿用默认" : `默认：${spec.default}`)}" />`;
    }
    return `<label class="engine-field"><span>${label}</span>${input}<small class="plugin-field-error" data-plugin-error="${escapeHtml(key)}"></small></label>`;
  }).join("") || '<p class="muted">此插件无需额外配置，使用任务参数与系统设置。</p>';
}

export function pluginInstanceCard(instance, plugin, isDefault, candidates) {
  const bound = bindingValue(instance);
  const binding = bound ? candidates.find((item) => item.value === bound)?.label || `绑定已失效：${bound}` : "沿用任务 / 当前模型";
  return `<article class="engine-card plugin-instance" data-plugin-instance="${escapeHtml(instance.id)}">
    <div class="engine-card__top"><div class="engine-card__identity"><span class="engine-card__icon"><i class="ph ph-plugs-connected" aria-hidden="true"></i></span><div><h4 class="engine-card__name">${escapeHtml(instance.name)}</h4><div class="engine-card__type">${escapeHtml(instance.id)}</div></div></div><span class="engine-status engine-status--${instance.enabled ? "available" : "unconfigured"}">${isDefault ? "阶段默认" : instance.enabled ? "已启用" : "已停用"}</span></div>
    <p class="plugin-instance__description">${escapeHtml(plugin?.name || instance.plugin_id)} · ${escapeHtml(instance.implementation_version)}</p>
    <p class="plugin-instance__binding">${escapeHtml(binding)}</p>
    <div class="plugin-instance__actions"><button class="btn btn--ghost btn--sm" type="button" data-plugin-action="edit" data-id="${escapeHtml(instance.id)}">编辑</button><button class="btn btn--ghost btn--sm" type="button" data-plugin-action="default" data-id="${escapeHtml(instance.id)}"${!instance.enabled || isDefault ? " disabled" : ""}>${isDefault ? "当前默认" : "设为默认"}</button><button class="btn btn--ghost btn--sm" type="button" data-plugin-action="toggle" data-id="${escapeHtml(instance.id)}"${isDefault ? ' disabled title="请先切换阶段默认实例"' : ""}>${instance.enabled ? "停用" : "启用"}</button><button class="btn btn--ghost btn--sm plugin-instance__delete" type="button" data-plugin-action="delete" data-id="${escapeHtml(instance.id)}"${isDefault ? ' disabled title="请先切换阶段默认实例"' : ""}>删除</button></div>
  </article>`;
}

export function createPluginSettingsController({ root, api = Api, notify = () => {} }) {
  const list = root.querySelector("#pluginStageList");
  const status = root.querySelector("#pluginSettingsFeedback");
  const revisionNode = root.querySelector("#pluginSettingsRevision");
  const editor = root.querySelector("#pluginInstanceEditor");
  const form = root.querySelector("#pluginInstanceForm");
  const editorFeedback = root.querySelector("#pluginEditorFeedback");
  const title = root.querySelector("#pluginEditorTitle");
  const fields = root.querySelector("#pluginConfigFields");
  const binding = root.querySelector("#pluginBinding");
  const bindingHint = root.querySelector("#pluginBindingHint");
  const submit = root.querySelector("#pluginSave");
  const removeConfirm = root.querySelector("#pluginDeleteConfirm");
  let catalog = [];
  let snapshot = { revision: null, instances: [], defaults: {} };
  let models = {};
  let engines = [];
  let draft = null;
  let pendingDelete = null;
  let busy = false;
  let sequence = 0;
  let conflicted = false;
  let draftRevision = null;

  function feedback(message = "", isError = false, target = status) {
    target.textContent = message;
    target.hidden = !message;
    target.classList.toggle("is-error", isError);
    target.setAttribute("role", isError ? "alert" : "status");
  }

  function setBusy(value) {
    busy = value;
    root.setAttribute("aria-busy", String(value));
    root.querySelectorAll("[data-plugin-action], #pluginAdd, #pluginRefresh, #pluginDeleteAccept").forEach((button) => { button.disabled = value; });
    submit.disabled = value || conflicted;
    if (!value) render();
  }

  function render() {
    revisionNode.textContent = snapshot.revision === null ? "尚未加载" : `版本 ${snapshot.revision}`;
    list.innerHTML = PLUGIN_STAGES.map((stage) => {
      const plugins = catalog.filter((plugin) => plugin.stage === stage.id);
      const instances = snapshot.instances.filter((instance) => instance.stage === stage.id);
      const descriptions = plugins.map((plugin) => `<details class="plugin-catalog"><summary>${escapeHtml(plugin.name)} · ${escapeHtml(plugin.implementation_version)}</summary><p>${escapeHtml(plugin.description || "")}</p><p class="muted">操作：${escapeHtml((plugin.operations || []).join("、") || "—")} · 能力：${escapeHtml((plugin.capabilities || []).join("、") || "通用处理")}</p></details>`).join("");
      return `<section class="plugin-stage" data-plugin-stage="${stage.id}"><header class="plugin-stage__head"><h3>${stage.label}</h3><button class="btn btn--ghost btn--sm" type="button" data-plugin-action="add" data-stage="${stage.id}"${!plugins.length || busy ? " disabled" : ""}>新增实例</button></header>${descriptions}<div class="engine-list">${instances.map((instance) => {
        const plugin = pluginForInstance(catalog, instance);
        return pluginInstanceCard(instance, plugin, snapshot.defaults[stage.id] === instance.id, pluginBindingOptions(plugin, models, engines));
      }).join("") || '<p class="muted">暂无实例</p>'}</div></section>`;
    }).join("");
    root.querySelector("#pluginAdd").disabled = busy || snapshot.revision === null;
    if (busy || snapshot.revision === null) list.querySelectorAll("button").forEach((button) => { button.disabled = true; });
  }

  async function refresh({ preserveDraft = true } = {}) {
    if (busy) return false;
    const requestSequence = ++sequence;
    setBusy(true);
    feedback("正在读取插件与绑定候选…");
    try {
      const [plugins, instances, modelSettings, engineSettings] = await Promise.all([
        api.getPluginCatalog(), api.listPluginInstances(), api.getModelSettings(), api.listTranslationEngines(),
      ]);
      if (requestSequence !== sequence) return false;
      catalog = plugins.plugins || [];
      snapshot = instances;
      models = modelSettings;
      engines = engineSettings;
      conflicted = false;
      feedback("");
      if (draft && preserveDraft) {
        draftRevision = snapshot.revision;
        updateBindingOptions(binding.value);
        feedback("已刷新版本，保留了你的输入。请核对配置后再次保存。", false, editorFeedback);
      }
      notify(snapshot);
      return true;
    } catch (error) {
      feedback(pluginErrorMessage(error), true);
      return false;
    } finally {
      if (requestSequence === sequence) setBusy(false);
    }
  }

  function selectedPlugin() {
    return catalog.find((plugin) => pluginKey(plugin) === form.elements.plugin.value);
  }

  function updateBindingOptions(selected = "") {
    const plugin = selectedPlugin();
    const options = pluginBindingOptions(plugin, models, engines);
    binding.innerHTML = '<option value="">沿用任务 / 当前模型（不绑定）</option>' + options.map((option) => `<option value="${escapeHtml(option.value)}"${option.disabled ? " disabled" : ""}>${escapeHtml(option.label)}</option>`).join("");
    if (selected && !options.some((option) => option.value === selected)) {
      binding.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(selected)}" disabled>绑定已失效：${escapeHtml(selected)}</option>`);
    }
    binding.value = selected;
    binding.disabled = !plugin?.bindings?.length;
    syncBindingSource();
  }

  function syncBindingSource() {
    const plugin = selectedPlugin();
    const whisperBound = binding.value.startsWith("whisper:");
    fields.querySelectorAll("[data-plugin-config]").forEach((input) => {
      if (!["backend", "service_url"].includes(input.dataset.pluginConfig) || plugin?.stage !== "transcribe") return;
      input.disabled = whisperBound;
      if (whisperBound) input.value = "";
    });
    bindingHint.textContent = whisperBound
      ? "转写后端、服务地址与模型由绑定的 Whisper 卡提供，不能在实例中重复配置；凭据仍在模型设置中维护。"
      : plugin?.bindings?.length
        ? "只引用模型卡或引擎的稳定 ID，凭据仍在原设置中维护。没有可选模型卡时，请先到模型设置保存模型。"
        : "此插件无需模型 / 引擎绑定。";
  }

  function renderEditorFields(config = {}, selectedBinding = "") {
    const plugin = selectedPlugin();
    fields.innerHTML = pluginConfigFields(plugin?.config_schema || {}, config);
    updateBindingOptions(selectedBinding);
    root.querySelector("#pluginVersionHint").textContent = plugin ? `实现版本 ${plugin.implementation_version} · 配置版本 ${plugin.config_schema_version}` : "请选择插件实现";
  }

  function updatePluginOptions(stage, instance = null) {
    const plugins = catalog.filter((plugin) => plugin.stage === stage);
    form.elements.plugin.innerHTML = plugins.map((plugin) => `<option value="${escapeHtml(pluginKey(plugin))}">${escapeHtml(plugin.name)} · ${escapeHtml(plugin.implementation_version)}</option>`).join("");
    if (instance && pluginForInstance(catalog, instance)) form.elements.plugin.value = pluginKey(instance);
    renderEditorFields(instance?.config || {}, bindingValue(instance));
  }

  function openDraft(instance = null, stage = "download") {
    draft = instance ? clone(instance) : { id: "", stage, config: {} };
    draftRevision = snapshot.revision;
    conflicted = false;
    pendingDelete = null;
    removeConfirm.hidden = true;
    editor.hidden = false;
    title.textContent = instance ? `编辑 ${instance.name}` : "新增插件实例";
    form.elements.id.value = instance?.id || "";
    form.elements.id.disabled = Boolean(instance);
    form.elements.name.value = instance?.name || "";
    form.elements.stage.innerHTML = PLUGIN_STAGES.map((item) => `<option value="${item.id}">${item.label}</option>`).join("");
    form.elements.stage.value = instance?.stage || stage;
    form.elements.stage.disabled = Boolean(instance);
    form.elements.enabled.checked = instance?.enabled !== false;
    form.elements.enabled.disabled = Boolean(instance && snapshot.defaults[instance.stage] === instance.id);
    updatePluginOptions(instance?.stage || stage, instance);
    form.elements.plugin.disabled = Boolean(instance);
    feedback("", false, editorFeedback);
    submit.disabled = busy;
    form.elements.name.focus();
  }

  async function editInstance(id) {
    if (busy) return;
    setBusy(true);
    try {
      const detail = await api.getPluginInstance(id);
      // A newer detail must not silently advance the revision of the stale list/default controls.
      openDraft(detail.instance);
      draftRevision = detail.revision;
    } catch (error) { feedback(pluginErrorMessage(error), true); }
    finally { setBusy(false); }
  }

  function collectDraft() {
    const plugin = selectedPlugin();
    if (!plugin) throw new Error("请选择有效的插件实现");
    const name = form.elements.name.value.trim();
    const id = form.elements.id.value.trim();
    if (!name) throw new Error("请输入实例名称");
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(id)) throw new Error("实例 ID 需为 1–64 位字母、数字、点、下划线或连字符，以字母或数字开头");
    const values = Object.fromEntries([...fields.querySelectorAll("[data-plugin-config]")].filter((input) => !input.disabled).map((input) => [input.dataset.pluginConfig, input.value]));
    const parsed = parsePluginConfig(plugin.config_schema, values);
    fields.querySelectorAll("[data-plugin-error]").forEach((node) => { node.textContent = parsed.errors[node.dataset.pluginError] || ""; });
    fields.querySelectorAll("[data-plugin-config]").forEach((node) => { node.setAttribute("aria-invalid", String(Boolean(parsed.errors[node.dataset.pluginConfig]))); });
    if (Object.keys(parsed.errors).length) throw new Error(Object.values(parsed.errors).join("；"));
    if (binding.value && !pluginBindingOptions(plugin, models, engines).some((option) => option.value === binding.value && !option.disabled)) throw new Error("绑定已不存在或不可用，请重新选择");
    return {
      id, name, stage: plugin.stage, plugin_id: plugin.plugin_id,
      implementation_version: plugin.implementation_version, config_schema_version: plugin.config_schema_version,
      enabled: form.elements.enabled.checked, config: parsed.config, ...refsForBinding(binding.value),
    };
  }

  async function mutate(operation, { editorAction = false, success = "插件设置已保存" } = {}) {
    if (busy || (editorAction && conflicted)) return false;
    setBusy(true);
    const target = editorAction ? editorFeedback : status;
    feedback("正在保存…", false, target);
    try {
      const result = await operation();
      if (editorAction) { editor.hidden = true; draft = null; }
      removeConfirm.hidden = true;
      pendingDelete = null;
      try {
        snapshot = await api.listPluginInstances();
        feedback(success);
        notify(snapshot);
      } catch (refreshError) {
        // The write succeeded; never invite a duplicate POST when only the read failed.
        snapshot.revision = null;
        feedback(`${success}（版本 ${result.revision}）。列表刷新失败，请刷新后继续。${refreshError.message || ""}`, true);
        notify();
      }
      return true;
    } catch (error) {
      if (error.status === 409 && editorAction) conflicted = true;
      feedback(pluginErrorMessage(error), true, target);
      return false;
    } finally { setBusy(false); }
  }

  async function save() {
    if (!draft || busy || conflicted) return false;
    let record;
    try { record = collectDraft(); }
    catch (error) { feedback(error.message, true, editorFeedback); return false; }
    const updating = Boolean(draft.id);
    return mutate(() => updating
      ? api.updatePluginInstance(draft.id, record, draftRevision)
      : api.createPluginInstance(record, draftRevision), { editorAction: true });
  }

  function closeEditor() {
    if (busy) return;
    draft = null;
    editor.hidden = true;
    conflicted = false;
  }

  root.querySelector("#pluginRefresh").addEventListener("click", () => refresh());
  root.querySelector("#pluginEditorRefresh").addEventListener("click", () => refresh());
  root.querySelector("#pluginAdd").addEventListener("click", () => { if (!busy && snapshot.revision !== null) openDraft(); });
  root.querySelector("#pluginEditorCancel").addEventListener("click", closeEditor);
  root.querySelector("#pluginDeleteCancel").addEventListener("click", () => { pendingDelete = null; removeConfirm.hidden = true; });
  root.querySelector("#pluginDeleteAccept").addEventListener("click", () => {
    if (pendingDelete) mutate(() => api.deletePluginInstance(pendingDelete, snapshot.revision), { success: "插件实例已删除" });
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); save(); });
  form.elements.stage.addEventListener("change", () => updatePluginOptions(form.elements.stage.value));
  form.elements.plugin.addEventListener("change", () => renderEditorFields());
  binding.addEventListener("change", syncBindingSource);
  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-plugin-action]");
    if (!button || button.disabled || busy) return;
    const { pluginAction: action, id, stage } = button.dataset;
    if (action === "add") { openDraft(null, stage); return; }
    const instance = snapshot.instances.find((item) => item.id === id);
    if (!instance) return;
    if (action === "edit") editInstance(id);
    else if (action === "default") mutate(() => api.updatePluginDefaults({ [instance.stage]: id }, snapshot.revision), { success: `${stageLabel(instance.stage)}默认插件已更新` });
    else if (action === "toggle") mutate(() => api.updatePluginInstance(id, { ...instance, enabled: !instance.enabled }, snapshot.revision));
    else if (action === "delete") {
      pendingDelete = id;
      removeConfirm.hidden = false;
      root.querySelector("#pluginDeleteMessage").textContent = `删除实例“${instance.name}”？已有任务保留其创建时的配置。`;
      root.querySelector("#pluginDeleteAccept").focus();
    }
  });

  return { refresh, save, openDraft, editInstance, getSnapshot: () => clone(snapshot) };
}

export function initPluginSettings() {
  const root = $("#pluginSettings");
  if (!root) return null;
  const controller = createPluginSettingsController({ root, notify: (snapshot) => document.dispatchEvent(new CustomEvent("plugin-settings-change", { detail: { snapshot } })) });
  const visible = () => state.view === "other-settings" && state.settingsTab === "plugins";
  subscribe(({ type }) => { if (["view", "settings-tab"].includes(type) && visible()) controller.refresh(); });
  for (const name of ["model-settings-change", "translation-engines-change"]) document.addEventListener(name, () => { if (visible()) controller.refresh(); });
  if (visible()) controller.refresh();
  return controller;
}

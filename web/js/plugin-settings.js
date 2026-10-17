/* Shared plugin metadata and scalar form policy; credentials stay in model/engine settings. */

export const PLUGIN_STAGES = Object.freeze([
  { id: "download", label: "下载" },
  { id: "audio_extract", label: "音频提取" },
  { id: "vocal_separate", label: "人声分离" },
  { id: "transcribe", label: "转写" },
  { id: "translate", label: "翻译" },
  { id: "tts", label: "配音" },
  { id: "render", label: "字幕 / 配音封装" },
]);

export const stageLabel = (stage) => PLUGIN_STAGES.find((item) => item.id === stage)?.label || stage;
export const pluginKey = (plugin) => JSON.stringify([plugin.plugin_id, plugin.implementation_version]);
export const pluginForInstance = (plugins, instance) => plugins.find((plugin) => plugin.plugin_id === instance?.plugin_id
  && plugin.implementation_version === instance?.implementation_version);

export function editablePluginFields(schema = {}) {
  return Object.entries(schema).filter(([key]) => !/(api_?key|secret|password|access_?token|credential)/i.test(key));
}

export function parsePluginConfig(schema, values = {}) {
  const config = {};
  const errors = {};
  for (const [key, spec] of editablePluginFields(schema)) {
    let value = values[key];
    const label = spec.label || key;
    if (value === undefined || value === "") {
      if (spec.required) errors[key] = `${label}不能为空`;
      continue;
    }
    if (spec.enum) {
      const selected = spec.enum.find((item) => JSON.stringify(item) === value || item === value);
      if (selected === undefined) { errors[key] = `${label}请选择有效选项`; continue; }
      value = selected;
    } else if (spec.type === "boolean") {
      if (![true, false, "true", "false"].includes(value)) { errors[key] = `${label}必须为开或关`; continue; }
      value = value === true || value === "true";
    } else if (["number", "integer"].includes(spec.type)) {
      value = Number(value);
      if (!Number.isFinite(value) || (spec.type === "integer" && !Number.isInteger(value))) {
        errors[key] = `${label}需要有效${spec.type === "integer" ? "整数" : "数字"}`;
        continue;
      }
      if (value < (spec.minimum ?? -Infinity) || value > (spec.maximum ?? Infinity)) {
        errors[key] = `${label}应在 ${spec.minimum ?? "不限"} 至 ${spec.maximum ?? "不限"} 之间`;
        continue;
      }
    } else {
      value = String(value).trim();
      if (value.length > (spec.maxLength || 4096)) { errors[key] = `${label}内容过长`; continue; }
    }
    config[key] = value;
  }
  return { config, errors };
}

export function pluginBindingOptions(plugin, settings = {}, engines = []) {
  const options = [];
  for (const kind of plugin?.bindings || []) {
    const items = kind === "engine" ? engines : settings[kind]?.models || [];
    for (const item of items) {
      // A display fallback is not a persisted model card and cannot be bound.
      if (!item.id) continue;
      const unavailable = item.enabled === false || (kind === "whisper" && item.status && item.status !== "READY");
      options.push({
        value: `${kind}:${item.id}`,
        label: `${kind === "engine" ? "翻译引擎" : kind.toUpperCase()} · ${item.name || item.model || item.id}${unavailable ? "（未启用 / 未就绪）" : ""}`,
        disabled: unavailable,
      });
    }
  }
  return options;
}

export function bindingValue(instance) {
  if (instance?.engine_ref) return `engine:${instance.engine_ref.id}`;
  if (instance?.model_ref) return `${instance.model_ref.kind}:${instance.model_ref.card_id}`;
  return "";
}

export function refsForBinding(value = "") {
  if (!value) return { model_ref: null, engine_ref: null };
  const split = value.indexOf(":");
  const kind = value.slice(0, split);
  const id = value.slice(split + 1);
  if (split < 0 || !id || !["engine", "llm", "whisper", "tts"].includes(kind)) throw new Error("请选择有效的模型或引擎绑定");
  return kind === "engine" ? { engine_ref: { id }, model_ref: null } : { model_ref: { kind, card_id: id }, engine_ref: null };
}

export function pluginErrorMessage(error) {
  if (error?.status === 409) return `配置已发生变化或操作冲突。输入已保留，请刷新后核对再保存。${error.message ? ` ${error.message}` : ""}`;
  if (error?.status === 503) return `插件服务暂不可用，输入已保留，可重试。${error.message || ""}`;
  if (error?.status === 422) return `配置校验失败：${error.message || "请检查配置字段与绑定"}`;
  return error?.message || "插件设置操作失败，请重试";
}

export function taskPluginStages({ sourceType = "url", needSubtitle = true, ttsEnabled = false } = {}) {
  const stages = sourceType === "upload" ? [] : ["download"];
  if (needSubtitle) stages.push("audio_extract", "vocal_separate", "transcribe", "translate", ...(ttsEnabled ? ["tts"] : []), "render");
  return stages;
}

export function selectedPluginInstances(selections = {}, task = {}) {
  return Object.fromEntries(taskPluginStages(task).filter((stage) => selections[stage]).map((stage) => [stage, selections[stage]]));
}

export function resolveTaskPluginBindings({ snapshot, selections = {}, task = {}, settings = {}, engines = [] }) {
  const resolved = {};
  const errors = [];
  for (const stage of taskPluginStages(task)) {
    const id = selections[stage] || snapshot.defaults?.[stage];
    const instance = snapshot.instances?.find((item) => item.id === id && item.stage === stage);
    if (!instance || !instance.enabled) {
      errors.push(`${stageLabel(stage)}插件${id ? ` ${id}` : "默认实例"}不可用，请刷新或重新选择。`);
      continue;
    }
    const binding = { instance, explicit: Boolean(selections[stage]), model: null, engine: null };
    if (instance.model_ref) {
      const ref = instance.model_ref;
      binding.model = settings[ref.kind]?.models?.find((item) => item.id === ref.card_id) || null;
      if (!binding.model || binding.model.enabled === false) errors.push(`${instance.name}绑定的 ${ref.kind.toUpperCase()} 模型已不存在或未启用，请到插件设置修复。`);
      else if (ref.kind === "whisper" && binding.model.status && binding.model.status !== "READY") errors.push(`${instance.name}绑定的 Whisper 模型未就绪，请到模型设置检查。`);
    }
    if (instance.engine_ref) {
      binding.engine = engines.find((item) => item.id === instance.engine_ref.id) || null;
      if (!binding.engine || binding.engine.enabled === false) errors.push(`${instance.name}绑定的翻译引擎已不存在或未启用，请到插件设置修复。`);
    }
    resolved[stage] = binding;
  }
  return { resolved, errors };
}

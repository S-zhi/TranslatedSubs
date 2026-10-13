/* Pure policy for the task page's Whisper model selector. */

export function normalizeWhisperModel(value) {
  const model = String(value || "").trim();
  return model.includes(":") ? model : (model ? `local:${model}` : "");
}

export function resolveTaskWhisperState({ items, settings, readiness }) {
  const configuredWhisper = settings?.whisper;
  const readinessWhisper = readiness?.modelSettings?.whisper || readiness?.model_settings?.whisper;
  const whisper = typeof configuredWhisper?.runtimeManaged === "boolean"
    ? configuredWhisper
    : readinessWhisper;
  if (!Array.isArray(items) || !whisper || typeof whisper.runtimeManaged !== "boolean") {
    return { ok: false, options: [], message: "无法确认 Whisper 模型状态，请检查模型设置与服务连接。" };
  }
  const ready = [...new Map(items
    .filter((item) => String(item.status || "").toUpperCase() === "READY")
    .map((item) => [normalizeWhisperModel(item.name), {
      value: normalizeWhisperModel(item.name),
      label: item.label || `Whisper ${item.name}`,
      status: "READY",
    }])).values()];
  const effectiveModel = normalizeWhisperModel(whisper.model);
  if (whisper.runtimeManaged && whisper.enabled !== false) {
    const selected = ready.find((item) => item.value === effectiveModel);
    if (!selected) {
      return {
        ok: false, options: ready, model: effectiveModel, locked: true,
        message: `${effectiveModel || "当前模型"} 未就绪或已不存在，请在模型设置中修复。`,
      };
    }
    return { ok: true, options: ready, model: effectiveModel, locked: true, message: "由模型设置控制" };
  }
  if (!ready.length) return { ok: false, options: [], locked: false, message: "没有已就绪的 Whisper 模型，请先从模型商店下载或导入。" };
  const settingsModel = normalizeWhisperModel(settings?.whisper?.model);
  const selected = ready.some((item) => item.value === settingsModel)
    ? settingsModel
    : (ready.some((item) => item.value === "local:tiny") ? "local:tiny" : ready[0].value);
  return { ok: true, options: ready, model: selected, locked: false, message: "" };
}

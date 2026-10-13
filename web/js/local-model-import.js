const SIDECARS = new Set([
  "config.json", "generation_config.json", "preprocessor_config.json",
  "tokenizer_config.json", "tokenizer.json", "vocab.json", "merges.txt",
  "normalizer.json", "special_tokens_map.json", "added_tokens.json",
  "pytorch_model.bin.index.json", "model.safetensors.index.json",
]);

const WEIGHT = /^(pytorch_model(?:-\d{5}-of-\d{5})?\.bin|model(?:-\d{5}-of-\d{5})?\.safetensors)$/;

const REQUIRED_HINTS = [
  { key: "weights", label: "至少一个 .bin 或 .safetensors 权重" },
  { key: "config", label: "config.json" },
  { key: "preprocessor", label: "preprocessor_config.json" },
  { key: "tokenizer", label: "tokenizer.json，或 vocab.json + merges.txt" },
];

export function filePath(file) {
  return String(file?.webkitRelativePath || file?.relativePath || file?.name || "");
}

function pathParts(file) {
  return filePath(file).split("/").filter(Boolean);
}

function isAllowedFile(file) {
  return SIDECARS.has(file?.name) || WEIGHT.test(file?.name || "");
}

export function filterModelFiles(files = []) {
  const accepted = [];
  const ignored = [];

  for (const file of Array.from(files)) {
    const parts = pathParts(file);
    const isAtModelRoot = parts.length <= 2;
    if (isAtModelRoot && isAllowedFile(file)) accepted.push(file);
    else ignored.push(file);
  }

  return { accepted, ignored };
}

function getDirectoryLabel(files) {
  for (const file of files) {
    const parts = pathParts(file);
    if (parts.length > 1) return parts[0];
  }
  return files.length ? "已选择的模型文件" : "尚未选择模型目录";
}

export function analyzeModelFiles(files = []) {
  const source = Array.from(files);
  const { accepted, ignored } = filterModelFiles(source);
  const names = new Set(accepted.map((file) => file.name));
  const weights = accepted.filter((file) => WEIGHT.test(file.name || ""));
  const sidecars = {
    config: names.has("config.json"),
    preprocessor: names.has("preprocessor_config.json"),
    tokenizer: names.has("tokenizer.json"),
    vocabulary: names.has("vocab.json"),
    merges: names.has("merges.txt"),
  };
  const checks = {
    weights: weights.length > 0,
    config: sidecars.config,
    preprocessor: sidecars.preprocessor,
    tokenizer: sidecars.tokenizer || (sidecars.vocabulary && sidecars.merges),
  };
  const missing = REQUIRED_HINTS.filter(({ key }) => !checks[key]).map(({ label }) => label);

  return {
    files: accepted,
    ignored,
    directory: getDirectoryLabel(source),
    acceptedCount: accepted.length,
    ignoredCount: ignored.length,
    weightCount: weights.length,
    sidecars,
    checks,
    missing,
    valid: accepted.length > 0 && missing.length === 0,
  };
}

export function suggestModelId(label, existingNames = []) {
  const base = String(label || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64)
    .replace(/-+$/g, "") || "local-model";
  const taken = new Set(Array.from(existingNames, (name) => String(name).toLowerCase()));
  let candidate = base;
  let suffix = 2;
  while (taken.has(candidate.toLowerCase())) {
    const suffixText = `-${suffix}`;
    candidate = `${base.slice(0, 64 - suffixText.length)}${suffixText}`;
    suffix += 1;
  }
  return candidate;
}

export { SIDECARS, WEIGHT };

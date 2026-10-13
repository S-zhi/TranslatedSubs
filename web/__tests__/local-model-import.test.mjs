import { test } from "node:test";
import assert from "node:assert/strict";

import { analyzeModelFiles, filterModelFiles, suggestModelId } from "../js/local-model-import.js";

const file = (name, relativePath = name) => ({ name, webkitRelativePath: relativePath });

const validFiles = [
  file("pytorch_model.bin", "whisper-custom/pytorch_model.bin"),
  file("config.json", "whisper-custom/config.json"),
  file("preprocessor_config.json", "whisper-custom/preprocessor_config.json"),
  file("tokenizer.json", "whisper-custom/tokenizer.json"),
];

test("filterModelFiles keeps supported model-root files and reports ignored files", () => {
  const result = filterModelFiles([
    ...validFiles,
    file("README.md", "whisper-custom/README.md"),
    file("nested.bin", "whisper-custom/checkpoints/nested.bin"),
  ]);

  assert.deepEqual(result.accepted.map(({ name }) => name), [
    "pytorch_model.bin",
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
  ]);
  assert.deepEqual(result.ignored.map(({ name }) => name), ["README.md", "nested.bin"]);
});

test("analyzeModelFiles reports structural readiness and file details", () => {
  const result = analyzeModelFiles(validFiles);

  assert.equal(result.directory, "whisper-custom");
  assert.equal(result.acceptedCount, 4);
  assert.equal(result.ignoredCount, 0);
  assert.equal(result.weightCount, 1);
  assert.deepEqual(result.missing, []);
  assert.equal(result.valid, true);
});

test("analyzeModelFiles recognizes sharded safetensors and vocabulary tokenizer files", () => {
  const result = analyzeModelFiles([
    file("model-00001-of-00002.safetensors", "model/model-00001-of-00002.safetensors"),
    file("config.json", "model/config.json"),
    file("preprocessor_config.json", "model/preprocessor_config.json"),
    file("vocab.json", "model/vocab.json"),
    file("merges.txt", "model/merges.txt"),
  ]);

  assert.equal(result.valid, true);
  assert.equal(result.checks.tokenizer, true);
});

test("analyzeModelFiles lists the missing required structure", () => {
  const result = analyzeModelFiles([file("README.md", "model/README.md")]);

  assert.equal(result.valid, false);
  assert.deepEqual(result.missing, [
    "至少一个 .bin 或 .safetensors 权重",
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json，或 vocab.json + merges.txt",
  ]);
});

test("suggestModelId creates editable safe IDs and avoids collisions", () => {
  assert.equal(suggestModelId("English Small Model"), "english-small-model");
  assert.equal(suggestModelId("中文模型"), "local-model");
  assert.equal(suggestModelId("English Small Model", ["english-small-model"]), "english-small-model-2");
  assert.match(suggestModelId("!!!"), /^[a-z0-9][a-z0-9_-]*$/);
});

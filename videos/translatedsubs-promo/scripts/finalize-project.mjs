#!/usr/bin/env node
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const voiceFlag = args.findIndex((arg) => arg === "--voice");
const voiceArg = args.find((arg) => arg.startsWith("--voice="));
const voiceLabel = voiceArg
  ? voiceArg.slice("--voice=".length)
  : voiceFlag >= 0
    ? args[voiceFlag + 1]
    : "Kokoro Heart";
const skipIndex = args.includes("--skip-index");

function updateFile(path, transform) {
  const original = readFileSync(path, "utf8");
  const updated = transform(original);
  if (updated !== original) writeFileSync(path, updated);
  return updated !== original;
}

const storyboardPath = resolve(projectRoot, "STORYBOARD.md");
let updatedFrames = 0;
updateFile(storyboardPath, (source) => {
  const blocks = source.split(/(?=^## Frame \d+ —)/m);
  const result = blocks.map((block) => {
    const srcMatch = block.match(/^- src: (.+)$/m);
    if (!srcMatch || !existsSync(resolve(projectRoot, srcMatch[1].trim()))) return block;
    const statusMatch = block.match(/^- status: ([^\r\n]+)$/m);
    if (!statusMatch || statusMatch[1] === "animated") return block;
    updatedFrames += 1;
    return block.replace(/^- status: [^\r\n]+$/m, "- status: animated");
  });
  return result.join("");
});

const scriptPath = resolve(projectRoot, "SCRIPT.md");
updateFile(scriptPath, (source) => {
  let result = source.replace(
    /^\*\*Voice:\*\*[^\r\n]*$/m,
    `**Voice:** ${voiceLabel} (English default)`,
  );
  result = result.replace(
    /^\*\*Voice settings:\*\*[^\r\n]*$/m,
    "**Voice settings:** provider local Kokoro · language en · speed 1.0",
  );
  const captionNote = "**Captions:** Skipped for the full-frame narration; the interface already demonstrates subtitles and the English headlines carry the story.";
  if (/^\*\*Captions:\*\*/m.test(result)) {
    result = result.replace(/^\*\*Captions:\*\*[^\r\n]*$/m, captionNote);
  } else {
    result = result.replace(/^(\*\*Voice direction:\*\*[^\r\n]*\r?\n)/m, `$1${captionNote}\n`);
  }
  return result;
});

let indexLocalized = false;
if (!skipIndex) {
  const indexPath = resolve(projectRoot, "index.html");
  indexLocalized = updateFile(indexPath, (source) =>
    source.replace(
      /https:\/\/cdn\.jsdelivr\.net\/npm\/gsap@[^"'\s]+\/dist\/gsap\.min\.js/g,
      "assets/vendor/gsap.min.js",
    ),
  );
}

console.log(`Marked ${updatedFrames} existing frame source${updatedFrames === 1 ? "" : "s"} animated.`);
console.log(`Voice label: ${voiceLabel}. Narration text was left unchanged.`);
console.log("Narration captions are marked skipped in SCRIPT.md.");
console.log(
  skipIndex
    ? "index.html left unchanged (--skip-index)."
    : indexLocalized
      ? "Localized the GSAP script path in index.html."
      : "index.html already uses the local GSAP asset.",
);

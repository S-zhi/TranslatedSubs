import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PROJECT_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CLI = "/Users/wenzhengfeng/.npm/_npx/702923228c2ce1e6/node_modules/hyperframes/bin/hyperframes.mjs";
const PYTHON = "/private/tmp/translatedsubs-promo-audio/bin/python";
const VOICE = "af_heart";
const SPEED = 1.0;
const LANG = "en-us";
const SEGMENT_DURATIONS = [5, 6, 8, 8, 8, 5];
const PREPEND_SILENCE_S = 0.2;
const OUTPUT_SAMPLE_RATE = 48_000;
const MAX_ATEMPO = 1.2;
const MAX_BUFFER = 8 * 1024 * 1024;

const SFX_PLAN = [
  {
    frame: 2,
    source: "assets/sfx/subdued-ui-click.mp3",
    sourceStart: 0,
    duration: 0.576,
    offset: 5.19,
    volume: 0.12,
    suffix: "ui-click",
  },
  {
    frame: 3,
    source: "assets/sfx/two-soft-process-ticks.mp3",
    sourceStart: 0,
    duration: 0.34,
    offset: 2.02,
    volume: 0.11,
    suffix: "process-tick-a",
  },
  {
    frame: 3,
    source: "assets/sfx/two-soft-process-ticks.mp3",
    sourceStart: 1.03,
    duration: 0.32,
    offset: 4.52,
    volume: 0.11,
    suffix: "process-tick-b",
  },
  {
    frame: 4,
    source: "assets/sfx/soft-edit-confirmation.mp3",
    sourceStart: 0,
    duration: 0.24,
    offset: 2.02,
    volume: 0.1,
    suffix: "edit-confirmation",
  },
  {
    frame: 5,
    source: "assets/sfx/gentle-export-handoff.mp3",
    sourceStart: 0,
    duration: 0.9,
    offset: 4.4,
    volume: 0.11,
    suffix: "export-handoff",
  },
  {
    frame: 6,
    source: "assets/sfx/quiet-brand-resolve.mp3",
    sourceStart: 0,
    duration: 1,
    offset: 3.24,
    volume: 0.1,
    suffix: "brand-resolve",
  },
];

function projectPath(relativePath) {
  return resolve(PROJECT_DIR, relativePath);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: PROJECT_DIR,
    encoding: "utf8",
    env: options.env ?? process.env,
    maxBuffer: MAX_BUFFER,
    stdio: ["ignore", "pipe", "pipe"],
    timeout: options.timeout ?? 600_000,
  });
  if (result.error) {
    throw new Error(`${command} failed to start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim().slice(-1400);
    throw new Error(`${command} exited with status ${result.status}${detail ? `:\n${detail}` : "."}`);
  }
  return { stdout: result.stdout || "", stderr: result.stderr || "" };
}

function probeDuration(path) {
  const { stdout } = run("ffprobe", [
    "-v", "error",
    "-show_entries", "format=duration",
    "-of", "default=noprint_wrappers=1:nokey=1",
    path,
  ], { timeout: 30_000 });
  const duration = Number(stdout.trim());
  if (!Number.isFinite(duration) || duration <= 0) {
    throw new Error(`ffprobe returned an invalid duration for ${path}: ${stdout.trim()}`);
  }
  return duration;
}

function readNarrationLines() {
  const scriptText = readFileSync(projectPath("SCRIPT.md"), "utf8");
  const headers = [...scriptText.matchAll(/^## Line (\d+)\b[^\r\n]*$/gm)];
  const lines = headers.map((header, index) => {
    const blockStart = header.index + header[0].length;
    const blockEnd = headers[index + 1]?.index ?? scriptText.length;
    const block = scriptText.slice(blockStart, blockEnd);
    const narration = block.match(/^ {4}(.+?)\s*$/m)?.[1];
    if (!narration) {
      throw new Error(`Could not read narration text for SCRIPT.md line ${header[1]}.`);
    }
    return { frame: Number(header[1]), text: narration };
  });
  if (lines.length !== SEGMENT_DURATIONS.length || lines.some((line, index) => line.frame !== index + 1)) {
    throw new Error(`SCRIPT.md must contain the six ordered narration lines for frames 1–6; found ${lines.length}.`);
  }
  return lines;
}

function ensureSourceVoice(line, ttsText) {
  const relativePath = `assets/audio/voice/source/frame-${String(line.frame).padStart(2, "0")}.wav`;
  const outputPath = projectPath(relativePath);
  mkdirSync(dirname(outputPath), { recursive: true });
  if (existsSync(outputPath)) {
    try {
      const duration = probeDuration(outputPath);
      if (duration > 0.05) {
        console.log(`[voice] frame ${line.frame}: reusing cached source (${duration.toFixed(2)}s)`);
        return { path: outputPath, duration };
      }
    } catch {
      // An invalid or incomplete cache is regenerated below.
    }
  }

  console.log(`[voice] frame ${line.frame}: synthesizing ${VOICE}`);
  const env = { ...process.env, HYPERFRAMES_PYTHON: PYTHON };
  const result = run(process.execPath, [
    CLI,
    "tts",
    ttsText,
    "--voice", VOICE,
    "--speed", String(SPEED),
    "--lang", LANG,
    "--output", outputPath,
    "--json",
  ], { env });
  const lastLine = result.stdout.trim().split(/\r?\n/).at(-1);
  let response;
  try {
    response = JSON.parse(lastLine);
  } catch {
    throw new Error(`TTS did not return JSON for frame ${line.frame}: ${lastLine || "no output"}`);
  }
  if (!response.ok || !existsSync(outputPath)) {
    throw new Error(`TTS did not produce frame ${line.frame} source audio: ${response.error || "output missing"}`);
  }
  return { path: outputPath, duration: probeDuration(outputPath) };
}

function loudnormStats(sourcePath, atempo) {
  const tempoFilter = `atempo=${atempo.toFixed(6)}`;
  const result = run("ffmpeg", [
    "-hide_banner", "-loglevel", "info", "-nostats",
    "-i", sourcePath,
    "-af", `${tempoFilter},adelay=200:all=1,loudnorm=I=-16:TP=-2:LRA=11:print_format=json`,
    "-f", "null", "-",
  ], { timeout: 180_000 });
  const match = result.stderr.match(/\{\s*"input_i"\s*:[\s\S]*?\}/);
  if (!match) {
    throw new Error(`Could not read loudnorm analysis for ${sourcePath}.`);
  }
  const stats = JSON.parse(match[0]);
  for (const key of ["input_i", "input_tp", "input_lra", "input_thresh", "target_offset"]) {
    if (!Number.isFinite(Number(stats[key]))) {
      throw new Error(`loudnorm analysis returned invalid ${key} for ${sourcePath}.`);
    }
  }
  return stats;
}

function renderVoice(sourcePath, outputPath, sourceDuration, targetDuration) {
  const spokenRoom = targetDuration - PREPEND_SILENCE_S;
  if (spokenRoom <= 0) {
    throw new Error(`Scene duration ${targetDuration}s cannot fit the required leading silence.`);
  }
  const requiredTempo = sourceDuration / spokenRoom;
  if (requiredTempo > MAX_ATEMPO + 1e-6) {
    throw new Error(
      `Narration is too long for a ${targetDuration}s scene: ${sourceDuration.toFixed(2)}s requires atempo ${requiredTempo.toFixed(3)}, above the ${MAX_ATEMPO} cap.`,
    );
  }
  const atempo = Math.max(1, requiredTempo);
  const stats = loudnormStats(sourcePath, atempo);
  const loudnorm = [
    "loudnorm=I=-16:TP=-2:LRA=11",
    `measured_I=${Number(stats.input_i).toFixed(2)}`,
    `measured_TP=${Number(stats.input_tp).toFixed(2)}`,
    `measured_LRA=${Number(stats.input_lra).toFixed(2)}`,
    `measured_thresh=${Number(stats.input_thresh).toFixed(2)}`,
    `offset=${Number(stats.target_offset).toFixed(2)}`,
    "linear=true",
    "print_format=summary",
  ].join(":");
  const filter = [
    `atempo=${atempo.toFixed(6)}`,
    "adelay=200:all=1",
    loudnorm,
    "alimiter=limit=0.794328:level=disabled",
    `aresample=${OUTPUT_SAMPLE_RATE}`,
    `apad=whole_dur=${targetDuration}`,
    `atrim=end_sample=${Math.round(targetDuration * OUTPUT_SAMPLE_RATE)}`,
    "asetpts=PTS-STARTPTS",
  ].join(",");
  const temporaryPath = `${outputPath}.tmp.wav`;
  run("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-nostats",
    "-y", "-i", sourcePath,
    "-af", filter,
    "-ar", String(OUTPUT_SAMPLE_RATE), "-ac", "1",
    "-c:a", "pcm_s16le",
    temporaryPath,
  ], { timeout: 180_000 });
  const actualDuration = probeDuration(temporaryPath);
  if (Math.abs(actualDuration - targetDuration) > 0.01) {
    throw new Error(`Processed voice duration mismatch: expected ${targetDuration}s, got ${actualDuration.toFixed(3)}s.`);
  }
  renameSync(temporaryPath, outputPath);
}

function renderSfxCue(cue) {
  const sourcePath = projectPath(cue.source);
  if (!existsSync(sourcePath)) {
    throw new Error(`SFX source is missing: ${cue.source}`);
  }
  const sourceDuration = probeDuration(sourcePath);
  if (cue.duration > 1.2 || cue.offset < 0 || cue.offset + cue.duration > SEGMENT_DURATIONS[cue.frame - 1]) {
    throw new Error(`SFX cue for frame ${cue.frame} is too long or falls outside its scene.`);
  }
  if (cue.sourceStart < 0 || cue.sourceStart + cue.duration > sourceDuration + 0.01) {
    throw new Error(`SFX source trim exceeds ${cue.source} (${sourceDuration.toFixed(3)}s).`);
  }
  const relativePath = `assets/sfx/prepared/frame-${String(cue.frame).padStart(2, "0")}-${cue.suffix}.wav`;
  const outputPath = projectPath(relativePath);
  mkdirSync(dirname(outputPath), { recursive: true });
  const temporaryPath = `${outputPath}.tmp.wav`;
  run("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-nostats",
    "-y", "-ss", cue.sourceStart.toFixed(3), "-i", sourcePath,
    "-t", cue.duration.toFixed(3),
    "-af", "asetpts=PTS-STARTPTS",
    "-ar", "48000", "-ac", "2",
    "-c:a", "pcm_s16le",
    temporaryPath,
  ], { timeout: 60_000 });
  const actualDuration = probeDuration(temporaryPath);
  if (actualDuration > 1.2 || Math.abs(actualDuration - cue.duration) > 0.03) {
    throw new Error(`Trimmed SFX duration mismatch for ${cue.source}: expected ${cue.duration}s, got ${actualDuration.toFixed(3)}s.`);
  }
  renameSync(temporaryPath, outputPath);
  return { frame: cue.frame, file: relativePath, offset_s: cue.offset, duration_s: cue.duration, volume: cue.volume };
}

function renderBgm() {
  const sourcePath = projectPath("assets/bgm/track.mp3");
  const outputPath = projectPath("assets/bgm/promo-bed.mp3");
  const temporaryPath = projectPath("assets/bgm/promo-bed.tmp.mp3");
  if (!existsSync(sourcePath)) {
    throw new Error("BGM source is missing: assets/bgm/track.mp3");
  }
  const sourceDuration = probeDuration(sourcePath);
  if (sourceDuration < 40) {
    throw new Error(`BGM source is only ${sourceDuration.toFixed(2)}s; 40s is required.`);
  }
  const filter = [
    "atrim=start=0:duration=40",
    "asetpts=PTS-STARTPTS",
    "loudnorm=I=-29:TP=-6:LRA=11",
    "afade=t=in:st=0:d=0.6",
    "afade=t=out:st=38:d=2",
    "atrim=start=0:duration=40",
    "asetpts=PTS-STARTPTS",
  ].join(",");
  run("ffmpeg", [
    "-hide_banner", "-loglevel", "error", "-nostats",
    "-y", "-i", sourcePath,
    "-af", filter,
    "-t", "40",
    "-c:a", "libmp3lame", "-q:a", "3",
    temporaryPath,
  ], { timeout: 180_000 });
  const actualDuration = probeDuration(temporaryPath);
  if (Math.abs(actualDuration - 40) > 0.08) {
    throw new Error(`Prepared BGM must be 40s; ffprobe reports ${actualDuration.toFixed(3)}s.`);
  }
  renameSync(temporaryPath, outputPath);
  return { path: "assets/bgm/promo-bed.mp3", volume: 1, duration_s: 40 };
}

function formatSrtTime(seconds) {
  const totalMilliseconds = Math.round(seconds * 1000);
  const hours = Math.floor(totalMilliseconds / 3_600_000);
  const minutes = Math.floor((totalMilliseconds % 3_600_000) / 60_000);
  const wholeSeconds = Math.floor((totalMilliseconds % 60_000) / 1000);
  const milliseconds = totalMilliseconds % 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")},${String(milliseconds).padStart(3, "0")}`;
}

function atomicWrite(path, text) {
  const temporaryPath = `${path}.tmp`;
  writeFileSync(temporaryPath, text, "utf8");
  renameSync(temporaryPath, path);
}

function main() {
  const narration = readNarrationLines();
  const voices = narration.map((line, index) => {
    const ttsText = line.text.replaceAll("TranslatedSubs", "Translated Subs");
    const source = ensureSourceVoice(line, ttsText);
    const relativePath = `assets/audio/voice/frame-${String(line.frame).padStart(2, "0")}.wav`;
    const outputPath = projectPath(relativePath);
    mkdirSync(dirname(outputPath), { recursive: true });
    renderVoice(source.path, outputPath, source.duration, SEGMENT_DURATIONS[index]);
    return {
      frame: line.frame,
      path: relativePath,
      duration_s: SEGMENT_DURATIONS[index],
      text: line.text,
    };
  });

  const bgm = renderBgm();
  const sfx = SFX_PLAN.map(renderSfxCue);
  const audioMeta = { bgm, voices, sfx };
  atomicWrite(projectPath("audio_meta.json"), `${JSON.stringify(audioMeta, null, 2)}\n`);

  let sceneStart = 0;
  const srt = narration.map((line, index) => {
    const start = sceneStart + PREPEND_SILENCE_S;
    sceneStart += SEGMENT_DURATIONS[index];
    return `${index + 1}\n${formatSrtTime(start)} --> ${formatSrtTime(sceneStart)}\n${line.text}`;
  }).join("\n\n");
  atomicWrite(projectPath("narration.srt"), `${srt}\n`);
  console.log("Audio prep complete: six scene voices, 40s music bed, short SFX cues, metadata, and narration.srt.");
}

try {
  main();
} catch (error) {
  console.error(`[audio-prep] ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
}

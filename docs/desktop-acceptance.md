# macOS desktop phase-one acceptance

Status: **NOT RUN** for all real-device, media, quality, network and release checks below.
The benchmark runner and its fake-backed unit tests are tooling, not product acceptance.
Scope and gates come from [the product brief](macos-desktop-v1-product-brief.md) and
[the implementation contract](macos-desktop-v1-implementation.md).

## Local-video benchmark

`scripts/desktop/benchmark.py` submits one deliberately selected local video to an
**already-running, authenticated desktop API**. It uses the shared Python environment
and existing `httpx` dependency. It does not start a server, prepare models, change
settings, fetch remote media, cancel jobs, or download result files. Uploading creates
a real task and consumes processing resources. Do not run without an approved sample.

Use a new output directory and keep both samples and evidence outside the repository.
The operator needs the current desktop loopback port and session token from the
authorized launcher/test harness. The session descriptor intentionally does **not**
contain the token. Do not search logs/configuration for secrets or weaken authentication.
The script prompts with hidden input if `SUBTRANS_DESKTOP_SESSION_TOKEN` is not already
in its environment. There is deliberately no token command-line argument.

From the repository root, after selecting the actual sample and output locations:

```sh
.venv/bin/python scripts/desktop/benchmark.py \
  --api http://127.0.0.1:PORT \
  --input /absolute/path/outside-repository/course.mp4 \
  --output-dir /absolute/path/outside-repository/new-benchmark-run \
  --ffprobe /absolute/path/to/ffprobe \
  --acceptance
```

Replace `PORT` and the placeholder paths before executing. `ffprobe` must already
exist; point to the packaged binary when checking the packaged application. The
script never installs it. For a development run, omit `--acceptance`. This permits
other hosts/samples but produces **DEVELOPMENT_ONLY**, never target-device acceptance.
The physical target is **Apple M1, arm64, 8 GiB RAM**, not M1 Pro/Max, a later M-series
chip or a 16GB development Mac. Host data comes from `platform` and macOS `sysctl`;
no hostname, serial number, account name or full system profile is collected.

Preflight requires ready desktop dependencies and both models, no active tasks,
and the contract defaults: English → Simplified Chinese, bilingual, hard burn,
`local:base.en`, `local-opus-en-zh`, subtitles enabled, TTS disabled. The multipart
upload uses the generic filename `benchmark-input.<extension>`, avoiding the user's
original filename in task history. The file is streamed, not loaded wholesale into
memory. Only literal `http://127.0.0.1:<port>` origins are allowed; redirects and
environment proxies are disabled. ffprobe is restricted to local file/pipe protocols.

The input must be **1920×1080 and 3600 seconds**, with an explicitly declared ±1 second
mux/timebase tolerance. No broader tolerance is silently accepted. ffprobe supplies
duration, dimensions and file size. English speech and course content remain a manual
check. `--acceptance` refuses the upload if the hardware or numeric sample requirements
do not match. Keep the machine on power, awake, and free of competing tasks for the
performance run; test sleep behavior in a separate run.

### Timing and interpretation

The upload API persists the input, validates it, and enqueues the job before returning.
Its `createdAt` can precede the final local copy; `updatedAt` changes on progress updates.
Neither is a reliable stage-start timestamp. The runner therefore polls
`GET /api/tasks/{id}` after the upload transaction completes and records each request
start/response end with `time.monotonic()`. Server epoch-millisecond timestamps are
retained as diagnostic evidence only, never subtracted to invent stage durations.

For a transition seen between consecutive polls, its earliest time is the preceding
request start and its latest time is the current response end. Subtracting these
intervals gives conservative lower/upper duration bounds, including request latency.
The total runs from entry to **TRANSCRIBING** through **SUCCESS**; the individual
intervals cover **TRANSCRIBING → TRANSLATING → BURNING → SUCCESS**. Hard encode is
included. Queueing, extraction before transcription, the complete upload request,
model preparation, and remote media downloads are excluded. Extraction timing is
visible in the observation log but is not mislabelled as transcription. Stage bounds
overlap in uncertainty, so their summed bounds need not equal the total bounds.

If transcription has already begun before the first post-upload GET, an earlier phase
was skipped between polls, an unexpected download occurred, or phases repeat, the
runner returns **INCONCLUSIVE**. It does not undercount by starting a stopwatch after
processing already began. A terminal failure/cancellation or missing video/subtitle
output references yields **FAILED**. Output references are evidence of API availability,
not proof that files play correctly or have visible hard subtitles.

| Result | Meaning |
| --- | --- |
| `PASS_TIMING_ONLY` | Exact target hardware/sample, app/model version evidence, all boundaries, and total upper bound ≤7200 seconds. Manual quality/release acceptance still NOT RUN. |
| `FAIL_OVER_7200S` | Total lower bound exceeds 7200 seconds. |
| `INCONCLUSIVE_AT_7200S` | The measured interval straddles the limit; do not round down to pass. |
| `INCONCLUSIVE` | Missing timing boundaries or clock/suspend discontinuity; repeat with valid evidence. |
| `INCONCLUSIVE_VERSION_EVIDENCE` | Completed target run lacks an app version or installed model revision. |
| `DEVELOPMENT_ONLY` | Timing is within the limit, but hardware or input is outside the target. |
| `FAILED` | Preflight/API/task/timeout/output failure. Inspect the local application for detailed errors. |

`--poll-interval` defaults to 0.5 seconds. `--timeout` defaults to 10800 seconds after
upload, deliberately longer than the 7200-second acceptance limit to measure overruns.
The deadline is checked around each GET; its HTTP timeout is bounded by the remaining
budget. `--upload-timeout` defaults to 600 seconds per HTTP operation, **not** a total
upload deadline. An upload error may occur after server-side creation; inspect the app
before repeating, because the script never automatically retries a POST. A polling
timeout, request failure or Ctrl-C retains known task ID/evidence and may leave the job
running. Stop it through the existing app controls if desired. Timeout does not imply
successful cancellation or resumability. A wall-clock/monotonic discrepancy >5 seconds
invalidates the timing run; clock changes and sleep require separate investigation.

The runner writes exclusive, owner-readable `benchmark.json` and `benchmark.md` files
in a new directory. Exit code 0 means a completed timing-only/development measurement;
it **never** means full product acceptance. Other results exit 1. JSON contains host and
media metadata, app/model revisions when exposed, task ID, sanitized observations,
upload elapsed time excluded from the target, phase/total intervals, and explicit error
codes. It omits media paths/names, titles, URLs, API error text, response bodies and
credentials. The token and selected media path/name are also redacted before writing.
Do not commit reports, videos, subtitles, credentials or manually captured private logs.

### Current integration limitations

The current task API has no immutable per-stage start/end timestamps or monotonic
duration history. Polling yields honest bounds; short stages missed between polls
remain unmeasured. A future optional server phase-history API would allow exact durations
and eliminate the upload/first-observation race; no such API is assumed by this runner.

The implementation contract's `/api/desktop/status` model rows currently do not require
`revision`, and the model coordinator currently omits it. For full version evidence,
the models owner should expose each **verified installed revision** in those rows
(`models.models[].revision`). The runner already consumes that additive field and
otherwise records null. It must not infer an installed revision from source constants
or label an expected revision as observed. No external-owned code was changed here.

There is no backend resource telemetry in this API. The script records total host RAM,
not peak inference RSS or a claim about memory pressure. For target acceptance, retain
Activity Monitor/OS evidence for backend and application peak memory, memory pressure,
swap, and sustained responsiveness, with timestamps and the matching task ID. Avoid
reporting the benchmark Python process's memory as the backend's memory.

## Manual evidence and quality review

Create a private evidence directory next to the benchmark report. Record the selected
sample's opaque ID, SHA-256, legal source/usage permission, English-course confirmation,
input duration/resolution, app build/version, installed model revisions, macOS/chip/RAM,
power/sleep settings, operator/date, task ID and region/network context. Keep paths,
personal data and credentials out of shareable copies. Hash locally; do not upload media
or publish evidence without separate authorization.

Export the MP4 and subtitle file through the application. Probe the exported video's
duration/resolution and independently play it with subtitle tracks disabled to confirm
both Chinese and English text are burned into the picture. Inspect audio, seekability,
end-of-file playback, subtitle encoding and standalone subtitle usability. Record any
duration loss, dropped ending, missing audio, font/tofu rendering, clipped or unreadable
subtitles. The benchmark does not download outputs or automate quality approval.

For the 60-minute course, retain exact reviewed time ranges and screenshots/notes at
**00:30–01:30**, **29:30–30:30**, and **58:30–59:30**, plus the actual first and last spoken
sentences. If a window is silent, record the silence and add the nearest spoken segment
instead of counting silence as a quality pass. At each point compare original English
speech, English transcript, Chinese meaning, subtitle timing and visible bilingual layout.
Record sustained offsets, large untranslated passages, and meaning-changing errors;
minor awkward phrasing/terminology does not by itself imply failure. Confirm the UI or
export workflow clearly labels the subtitles as machine generated.

**Start/middle/end spot checks alone cannot prove there are no missing spans.** Review
the entire speech-bearing timeline against the transcript/subtitles (full playback or
audited speech/transcript alignment). Keep a coverage ledger with start/end times for
every reviewed range and every gap. Explain each gap as silence or record the missing
speech as a defect; overlap/duplicate cues and a dropped final segment also count.
Only mark coverage PASS when all speech-bearing spans have evidence. Record any tooling
used for alignment as an aid, never as an automatic translation-quality verdict.

| Evidence item | Status | Required record |
| --- | --- | --- |
| M1/8GB, 60-minute 1080p English course, ≤7200 seconds | NOT RUN | JSON/Markdown, sample identity, versions, phase intervals, resource evidence |
| Exported MP4 + subtitle integrity | NOT RUN | Output probe, playback/seek/audio notes, subtitle file check |
| Start / middle / end meaning and timing | NOT RUN | Exact timestamps, comparison notes, screenshots |
| Whole-timeline coverage, no missing speech spans | NOT RUN | Complete coverage/gap ledger and resolutions |
| No sustained offset or large untranslated passages | NOT RUN | Reviewed ranges and defects with timestamps |
| Machine-generated disclosure | NOT RUN | UI/export evidence |

## Manual release matrix

Every row starts **NOT RUN**. Replace a status only with actual evidence and build/task
identifiers; a unit-test result does not satisfy these rows. Keep development-host runs
separate from M1/8GB acceptance. Perform destructive/low-disk/network-interruption cases
only in controlled test data/environments.

| Area | Required scenarios / pass evidence | Status |
| --- | --- | --- |
| Clean install | Download candidate DMG on a Mac without Python/Redis/FFmpeg/dev tools; drag/install/open; no shell, API key or configuration required; record macOS compatibility and bundle size. | NOT RUN |
| Signing / notarization | Validate Developer ID signature, notarization and stapled ticket on final downloaded artifact; Gatekeeper first-open/quarantine experience; record identity and ticket evidence without credentials. Unsigned builds do not pass public release. | NOT RUN |
| First model preparation | Cold launch automatically prepares both pinned models, shows real size/progress/unknown-size state and download/conversion/checking phases; ready only after offline load and dependencies succeed. | NOT RUN |
| Model retry / failure | Interrupted transfer, unavailable source, insufficient disk, corrupt cached model; actionable error and retry; no duplicate jobs; preparation failure cannot silently permit tasks. | NOT RUN |
| Warm / offline local run | Prepared models reused without redownload; network disconnected local English→Chinese hard-subtitle job succeeds without cloud calls; local-only source remains usable. | NOT RUN |
| Local input / errors | Real 60-minute 1080p course on M1/8GB; supported local formats; corrupt/empty/oversized/over-duration file; missing tool/dependency error; actionable stage and retry behavior. | NOT RUN |
| YouTube public source | On a network able to access the chosen public video: actual download, processing, preview/export. Separately simulate inaccessible/deleted video and interrupted download; explain network boundary, offer retry/local file. Webpage access alone is not success. | NOT RUN |
| Processing failure / retry | Induce controlled transcription, translation and encode failures; stage/error visible; retry creates valid work/results; do not claim resume from failed position or phase. | NOT RUN |
| Background / reopen | Close last window during each long phase; task continues; menu bar reopens window with current progress; completed result remains accessible. | NOT RUN |
| Explicit quit | Active-task warning appears; cancelling quit preserves work; confirming quit stops owned task/process resources; unrelated processes survive; idle quit is clean. | NOT RUN |
| Restart / crash recovery | Reopen after normal exit and controlled abnormal termination; history and completed artifacts survive; interrupted jobs marked retryable failure, not auto-requeued or falsely successful. | NOT RUN |
| Sleep / wake | Sleep during preparation/transcription/translation/encode, then wake; document actual progress, errors and retry/recovery. Do not promise processing during sleep or uninterrupted resume. Keep out of performance run. | NOT RUN |
| Preview / export | Visible bilingual hard subtitles, audio/seek/end playback; system save dialog for MP4 and subtitle; cancel, repeated export, invalid/unwritable destination, missing result; no arbitrary path/URL handling. | NOT RUN |
| Updates | Disabled/no channel, current version, available version with notes, failed/offline/malformed manifest; valid HTTPS DMG destination; no interruption, auto-install, restart or replacement during tasks. | NOT RUN |
| Domestic network | Independently download final app, cold-prepare both models, check update and obtain candidate newer DMG, complete local video; record ISP/region/date/source and failures. No claim that the app enables YouTube access. | NOT RUN |
| Overseas network | Repeat app download, cold-model preparation, update detection/download and local pipeline with independent network evidence. | NOT RUN |
| Long-run resource behavior | M1/8GB peak app/backend memory, pressure/swap and responsiveness; 16GB observations retained separately; repeated tasks and lifecycle cycles leave no owned orphan processes. | NOT RUN |
| Session security | No token in URL/log/output/config; authenticated web/API/export; cross-origin mutations and remote navigation rejected; reopening session uses fresh token without breaking local file selection. | NOT RUN |
| Installed upgrade | Manually install approved newer candidate using download flow; existing data/results survive; displayed version matches bundle; never substitute update-check unit tests for install evidence. | NOT RUN |

Unresolved release gates include the selected full-length sample, M1/8GB measurements
and quality evidence, clean-device compatibility, domestic/overseas distribution,
actual signing/notarization, and a deployed update channel. None is waived by having
the runner available. No measurements are fabricated or included with this document.

## Tooling validation

Run only the offline tests from the repository root:

```sh
.venv/bin/python -m pytest -q tests/test_desktop_benchmark.py
```

Tests use HTTPX MockTransport, an injected clock, temporary dummy files and mocked
metadata. They exercise request fields, authentication, timing bounds, failures,
timeouts, target discrimination and report redaction. They never decode real media,
load models, download remote content or start services. HTTPX client/multipart/mock
transport usage was checked through Context7 against the official HTTPX documentation.

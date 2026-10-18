# macOS arm64 packaging

The frozen backend has been built in this worktree and passed its offline native
import/CPU self-test. A complete standalone app/DMG has **not** been verified.
The pipeline does not publish anything. Python 3.12, Xcode/Swift and an
Apple Silicon build host are required. The tentative minimum deployment target
is 13.0; a clean macOS 13 machine must still validate all shipped dependencies.

`pyproject.toml` owns the application name, executable product, bundle identifier,
minimum target and version. The Swift menu must read `CFBundleDisplayName` /
`CFBundleName` to keep its labels on the same source (native owner's integration).

## Prepare dependencies and licensed inputs

Only the dependency owner should synchronize the shared environment:

```sh
uv lock
uv sync --frozen --group desktop-build --extra local-translation
.venv/bin/python -m pytest -q tests/test_desktop_packaging.py
```

No script installs dependencies implicitly. Do not run concurrent `uv sync`.
The coordinator completed `uv lock` (166 packages) and the frozen desktop sync;
the earlier lock-resolution blocker is resolved. Dependency changes remain with
the coordinator.

Supply two extracted, redistributable upstream distributions plus their original
source archives. Each extracted directory has an `inventory.json` sidecar. The
sidecar is created **outside** the archive (it cannot hash itself). It records:

```json
{
  "schema": 1,
  "kind": "ffmpeg",
  "source_url": "ACTUAL_HTTPS_ARCHIVE_URL",
  "source_sha256": "ACTUAL_64_CHARACTER_LOWERCASE_SHA256",
  "license": "ACTUAL_LICENSE_IDENTIFIER",
  "files": [
    {"path": "bin/ffmpeg", "role": "ffmpeg", "sha256": "ACTUAL_FILE_SHA256"},
    {"path": "bin/ffprobe", "role": "ffprobe", "sha256": "ACTUAL_FILE_SHA256"},
    {"path": "licenses/COPYING", "role": "license", "sha256": "ACTUAL_FILE_SHA256"}
  ]
}
```

FFmpeg needs arm64, libass `subtitles`, and the exact configure argument
`--enable-libfontconfig` (not `--enable-fontconfig`). Include every
non-system dylib explicitly as `role: library` below `lib/`, already relocated
with `@loader_path` / `@rpath`; include the actual distribution's license notices
and corresponding-source obligations. The builder never harvests Homebrew or
arbitrary host libraries. Explicit `--dev-ffmpeg` and `--dev-ffprobe` paths are
allowed only for a development build, and must still pass dependency auditing.

The font inventory uses `kind: fonts`, `languages: ["en", "zh-CN"]`, one or more
`.otf`, `.ttf`, or `.ttc` files with `role: font`, and actual license files with
`role: license`. Suggested upstream to evaluate is
[Noto CJK](https://github.com/notofonts/noto-cjk), which supplies Simplified Chinese
fonts and OFL notices. No unverified font binary, invented source hash, or host
system font is checked into this repository. Font coverage declarations and
source licensing must be reviewed; real CJK rendering is a release acceptance
gate. The runtime hook points libass/fontconfig to bundled `Resources/fonts`,
with cache outside the app. It does not write user configuration.

`source-inventory.json` includes FFmpeg/fonts provenance and installed Python
distribution license metadata (UNKNOWN means review is still needed). The locked
Python resolution and project license are included too. PyInstaller collects
dependency metadata/license assets, torch native libraries, Transformers Marian
conversion assets, CTranslate2, SentencePiece, Sacremoses, and Whisper. The
inventory is evidence for a license review, not an assertion of legal clearance.

## Build locally

```sh
.venv/bin/python scripts/desktop/build.py --mode dev \
  --ffmpeg-dir /absolute/verified-ffmpeg --ffmpeg-archive /absolute/ffmpeg-source.zip \
  --font-dir /absolute/verified-fonts --font-archive /absolute/fonts-source.zip \
  --output dist/desktop-candidate
```

Use a fresh output directory. An optional `--native-executable` uses the native
owner's completed release build; otherwise Swift compiles into a private scratch
directory, without touching `desktop/macos/.build`.
The scratch `.ci/astra/native-check/TranslateSub` from the direct CLT check is
not a distributable override: `otool` shows an absolute dependency on its sibling
`libDesktopCore.dylib` and a scratch-directory rpath. Dependency auditing rejects
it. Supply a relocatable release executable; do not bypass that audit.

Resource layout:

```text
TranslateSub.app/Contents/
  Info.plist
  MacOS/TranslateSub
  Resources/
    backend/translatedsubs-backend
    backend/_internal/web/index.html
    backend/_internal/<Python, torch, libraries and metadata>
    bin/ffmpeg
    bin/ffprobe
    lib/<explicitly inventoried FFmpeg dylibs, if any>
    fonts/<licensed CJK font files, notices and fonts.conf>
    source-inventory.json
    dependency-audit.json
    python-dependencies.lock
```

The backend retains `--session-file ABSOLUTE_PATH` and requires the native
launcher's session environment. Web collection agrees with the runtime's
`sys._MEIPASS/web` lookup. The CLI is not the server `src.cli` entrypoint.
The frozen entry also has a packaging-only `--packaging-self-test`: it loads
torch/Whisper/Marian/converter native dependencies, performs a tiny CPU tensor
operation and checks Web assets, without models or a web server. Every build
runs it with offline model settings after signing. No runtime CLI changes are
required in `src.desktop`.

## Signed release and manual CI

Add `--mode release`, `--identity 'Developer ID Application: …'`,
`--notary-profile PROFILE`, `--release-notes FILE`, `--update-url ACTUAL_HTTPS_URL`,
and `--download-url ACTUAL_HTTPS_DMG_URL`. The existing keychain profile must be
usable. Release forbids development FFmpeg paths. There is no ad-hoc fallback.

The pipeline audits each Mach-O with `lipo`/`otool`, rejects unresolved external
dependencies, higher deployment targets and escaping symlinks, signs nested code inside-out with hardened
runtime (without `codesign --deep` signing or disabled library validation), then
verifies the app. Release submits the app ZIP to Apple, requires `Accepted`,
staples/validates the app, creates/signs/submits/staples/validates the DMG, checks
Gatekeeper and `hdiutil verify`, and only then writes the release manifest. Thus
the manifest SHA-256 describes the final stapled DMG. Development builds are
explicitly ad-hoc and generate no public update manifest.

`macos-desktop.yml` runs only on `workflow_dispatch`, using an arm64
[`macos-15` runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
The official [runner image label table](https://github.com/actions/runner-images#available-images)
lists `macos-15` for ARM64 and `macos-15-intel` for Intel; `macos-15-arm64`
is an image name, not a listed workflow label. The workflow retains `macos-15`
and checks the host architecture before dependency installation.
Its `assets_json` input is a JSON object with `ffmpeg` and `fonts` entries, each
containing `url`, `sha256`, and the full `inventory` object above. The archive
layout must match inventory paths directly, with no top-level wrapper directory.
CI downloads only these explicit archives, verifies hashes before extraction,
rejects unsafe members, and rechecks file hashes. It never downloads models.

Development dispatches need no signing secrets. Release dispatches require all
`DESKTOP_CERTIFICATE_BASE64`, `DESKTOP_CERTIFICATE_PASSWORD`,
`DESKTOP_SIGNING_IDENTITY`, `DESKTOP_APPLE_ID`, `DESKTOP_APPLE_PASSWORD` (an
app-specific password), and `DESKTOP_TEAM_ID` secrets, plus real update/download
URLs and notes. Credentials are imported into a temporary runner keychain and
removed on exit. No Developer ID signing or notarization/upload has been performed
for this candidate; PyInstaller's local ad-hoc signing is not release signing.
Future release-mode dispatch authorizes Apple's notarization upload; workflow
artifacts are uploaded only to that workflow run. There is no GitHub Release
creation, website publication, or automatic dispatch.

### Observed local verification and remaining gates

The coordinator's `.ci/astra/frozen-build.log` records a successful PyInstaller
build. Running `.ci/astra/frozen-dist/translatedsubs-backend/translatedsubs-backend
--packaging-self-test` again from outside the repository with model downloads
disabled passed. No missing package was observed in that smoke test, so the spec
was not expanded speculatively. The build warnings about Linux/CUDA libraries,
Emscripten `js`, and TensorBoard concern paths not exercised by the macOS CPU
smoke. Optional `curl_cffi` and `yt_dlp_ejs` were also absent; successful YouTube
extraction is still a separate unverified acceptance gate.

A read-only scan of the actual frozen tree found 116 Mach-O files and no absolute
non-system dylib dependencies. **92 files declare minimum macOS 14.0**, including
Torch, PyAV (and its FFmpeg libraries), ONNX Runtime and NumPy, while the current
project deployment target is 13.0. The bundle audit correctly rejects this
mismatch. The coordinator must select compatible dependency versions or agree
and propagate a revised minimum OS; the packaging code does not lower the audit
or silently change the product target. PyAV's collected FFmpeg libraries also
belong in the distribution license review; they do not replace the separately
required CLI FFmpeg/ffprobe distribution.

Offline tests exercise exact FFmpeg configure tokens, real ZIP/TAR extraction
with mocked downloads, archive provenance/hash mismatches, path traversal and
link rejection, native scratch rpath rejection, and version/resource/manifest
consistency. Apple signing/notarization commands remain mocked in those tests.

Remaining release gates: resolve the macOS deployment-target mismatch; supply
reviewed FFmpeg/font distributions and a relocatable native release executable;
validate clean-machine rendering, real model conversion, lifecycle, minimum OS,
regions/network and M1 8GB performance; provision the separately authorized
signing/notarization credentials and actual distribution channel. Passing mocked
tests does not satisfy those gates.

#!/bin/bash
set -euo pipefail
# Invoked only by workflow_dispatch. Never publish to Releases or another host.
: "${DESKTOP_MODE:?}"
: "${DESKTOP_ASSETS_JSON:?}"
: "${RUNNER_TEMP:?}"
printf '%s' "$DESKTOP_ASSETS_JSON" > "$RUNNER_TEMP/desktop-assets.json"
python scripts/desktop/prepare_assets.py "$RUNNER_TEMP/desktop-assets.json" "$RUNNER_TEMP/desktop-assets"
args=(--mode "$DESKTOP_MODE" --ffmpeg-dir "$RUNNER_TEMP/desktop-assets/ffmpeg"
      --ffmpeg-archive "$RUNNER_TEMP/desktop-assets/ffmpeg.archive"
      --font-dir "$RUNNER_TEMP/desktop-assets/fonts"
      --font-archive "$RUNNER_TEMP/desktop-assets/fonts.archive")
if [[ "$DESKTOP_MODE" == release ]]; then
  : "${DESKTOP_CERTIFICATE_BASE64:?Release requires signing certificate}"
  : "${DESKTOP_CERTIFICATE_PASSWORD:?Release requires certificate password}"
  : "${DESKTOP_SIGNING_IDENTITY:?Release requires Developer ID identity}"
  : "${DESKTOP_APPLE_ID:?Release requires notarization account}"
  : "${DESKTOP_APPLE_PASSWORD:?Release requires app-specific password}"
  : "${DESKTOP_TEAM_ID:?Release requires team ID}"
  : "${DESKTOP_UPDATE_URL:?Release requires actual update URL}"
  : "${DESKTOP_DOWNLOAD_URL:?Release requires actual DMG URL}"
  : "${DESKTOP_RELEASE_NOTES:?Release requires release notes}"
  keychain="$RUNNER_TEMP/desktop-signing.keychain-db"
  certificate="$RUNNER_TEMP/desktop-signing.p12"
  trap 'security delete-keychain "$keychain" >/dev/null 2>&1 || true; rm -f "$certificate"' EXIT
  python -c 'import base64, os; from pathlib import Path; Path(os.environ["RUNNER_TEMP"], "desktop-signing.p12").write_bytes(base64.b64decode(os.environ["DESKTOP_CERTIFICATE_BASE64"], validate=True))'
  keychain_password="$(openssl rand -hex 24)"
  security create-keychain -p "$keychain_password" "$keychain"
  security set-keychain-settings -lut 21600 "$keychain"
  security unlock-keychain -p "$keychain_password" "$keychain"
  security import "$certificate" -P "$DESKTOP_CERTIFICATE_PASSWORD" -k "$keychain" -T /usr/bin/codesign -T /usr/bin/security
  security set-key-partition-list -S apple-tool:,apple:,codesign: -k "$keychain_password" "$keychain" >/dev/null
  security list-keychains -d user -s "$keychain"
  security default-keychain -d user -s "$keychain"
  xcrun notarytool store-credentials desktop-ci --apple-id "$DESKTOP_APPLE_ID" --password "$DESKTOP_APPLE_PASSWORD" --team-id "$DESKTOP_TEAM_ID" --keychain "$keychain" >/dev/null
  printf '%s' "$DESKTOP_RELEASE_NOTES" > "$RUNNER_TEMP/desktop-release-notes.txt"
  args+=(--identity "$DESKTOP_SIGNING_IDENTITY" --notary-profile desktop-ci
         --update-url "$DESKTOP_UPDATE_URL" --download-url "$DESKTOP_DOWNLOAD_URL"
         --release-notes "$RUNNER_TEMP/desktop-release-notes.txt")
elif [[ "$DESKTOP_MODE" != dev ]]; then
  exit 2
fi
python scripts/desktop/build.py "${args[@]}"

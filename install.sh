#!/usr/bin/env bash
# voice-stream installer: venv, model, systemd units.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${VOICE_STREAM_HOME:-$HOME/.local/share/voice-stream}"
DEVICE="${VOICE_STREAM_DEVICE_NAME:-DJI}"
WINDOW="${VOICE_STREAM_WINDOW:-dsh-cdp}"
MODEL_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2"

command -v pw-record >/dev/null || { echo "error: pw-record not found (PipeWire tools required)"; exit 1; }
command -v wl-copy   >/dev/null || { echo "error: wl-copy not found (wl-clipboard required)"; exit 1; }
command -v wtype     >/dev/null || { echo "error: wtype not found"; exit 1; }

mkdir -p "$APP" "$HOME/.config/systemd/user"

echo "== python environment =="
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" -q install --upgrade pip
"$APP/venv/bin/pip" -q install sherpa-onnx numpy

echo "== speech model =="
if [ ! -f "$APP/sensevoice-small/model.int8.onnx" ]; then
  tmp="$(mktemp -d)"
  curl -L --fail -o "$tmp/model.tar.bz2" "$MODEL_URL"
  tar xjf "$tmp/model.tar.bz2" -C "$tmp"
  rm -rf "$APP/sensevoice-small"
  mv "$tmp"/sherpa-onnx-sense-voice-* "$APP/sensevoice-small"
  rm -rf "$tmp"
fi

echo "== systemd =="
for u in voice-stream.service voice-overlay.service voice-mic-watch.service voice-mic-watch.timer; do
  sed -e "s|__APP__|$APP|g" -e "s|__REPO__|$REPO|g" \
      -e "s|VOICE_STREAM_DEVICE_NAME=DJI|VOICE_STREAM_DEVICE_NAME=$DEVICE|" \
      -e "s|VOICE_STREAM_WINDOW=dsh-cdp|VOICE_STREAM_WINDOW=$WINDOW|" \
      "$REPO/systemd/$u" > "$HOME/.config/systemd/user/$u"
done
systemctl --user daemon-reload
systemctl --user enable --now voice-overlay.service voice-mic-watch.timer
systemctl --user start voice-mic-watch.service || true

echo
echo "installed. check with:  systemctl --user status voice-stream"
echo "logs: $APP/stream.log"

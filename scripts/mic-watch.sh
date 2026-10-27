#!/usr/bin/env bash
# Start/stop voice-stream depending on whether the bound microphone is present.
set -u
MATCH="${VOICE_STREAM_DEVICE_NAME:-DJI}"
export MATCH
present=$(python3 - <<PY
import json, os, subprocess
try:
    data = json.loads(subprocess.run(["pactl","-f","json","list","sources"],capture_output=True,text=True).stdout or "[]")
except Exception:
    data = []
m = os.environ.get("MATCH","DJI").lower()
hit = any(m in (s.get("description","") + " " + s.get("name","")).lower() for s in data)
print("1" if hit else "0")
PY
)
if [ "$present" = "1" ]; then
  systemctl --user is-active --quiet voice-stream || systemctl --user start voice-stream
else
  systemctl --user is-active --quiet voice-stream && systemctl --user stop voice-stream
fi

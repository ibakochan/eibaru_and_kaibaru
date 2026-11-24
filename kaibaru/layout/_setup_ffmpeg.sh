#!/bin/bash
set -euo pipefail
VENV="/mnt/c/Users/user/code/kaibaru_backend/venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv /tmp/kaibaru-ffmpeg-venv
  VENV=/tmp/kaibaru-ffmpeg-venv
fi
"$VENV/bin/pip" install -q imageio-ffmpeg
"$VENV/bin/python" - <<'PY'
import imageio_ffmpeg
print(imageio_ffmpeg.get_ffmpeg_exe())
PY

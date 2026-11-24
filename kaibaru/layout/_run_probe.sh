#!/bin/bash
set -euo pipefail
python3 /mnt/c/Users/user/code/kaibaru_backend/kaibaru/layout/_mp4_durations.py
echo "---"
command -v ffmpeg || true
command -v ffprobe || true
python3 - <<'PY'
import shutil
print("which ffmpeg", shutil.which("ffmpeg"))
PY

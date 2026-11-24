#!/bin/bash
set -euo pipefail
PY=/mnt/c/Users/user/code/kaibaru_backend/venv/bin/python
SCRIPT=/mnt/c/Users/user/code/kaibaru_backend/kaibaru/layout/_concat_hero_clips.py
ASSETS=/mnt/c/Users/user/.cursor/projects/c-Users-user-code/assets
for folder in "$@"; do
  "$PY" "$SCRIPT" "$folder" \
    "$ASSETS/${folder}-clip1.mp4" \
    "$ASSETS/${folder}-clip2.mp4" \
    "$ASSETS/${folder}-clip3.mp4"
done

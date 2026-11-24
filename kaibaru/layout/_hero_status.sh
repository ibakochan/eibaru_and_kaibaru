#!/bin/bash
set -euo pipefail
PY=/mnt/c/Users/user/code/kaibaru_backend/venv/bin/python
ROOT=/mnt/c/Users/user/code/kaibaru_backend/tmp_layout_images/layouts
ASSETS=/mnt/c/Users/user/.cursor/projects/c-Users-user-code/assets
echo "=== ready heroes ==="
for folder in karate fitness gym default cram eikaiwa dance swimming sports hair_salon nail_salon massage_salon; do
  f="$ROOT/$folder/hero.mp4"
  if [ -f "$f" ]; then
    dur=$("$PY" - <<PY
from pathlib import Path
import struct
p=Path("$f")
data=p.read_bytes(); i=data.find(b"mvhd");
v=data[i+4]
if v==1:
 t=struct.unpack(">I", data[i+20:i+24])[0]; d=struct.unpack(">Q", data[i+24:i+32])[0]
else:
 t=struct.unpack(">I", data[i+16:i+20])[0]; d=struct.unpack(">I", data[i+20:i+24])[0]
print(round(d/t,3) if t else "?")
PY
)
    echo "OK $folder ${dur}s $(wc -c < "$f")b"
  else
    clips=0
    for n in 1 2 3; do
      [ -f "$ASSETS/${folder}-clip${n}.mp4" ] && clips=$((clips+1))
    done
    echo "WAIT $folder clips=$clips/3"
  fi
done

#!/bin/bash
set -euo pipefail
KEY="${HOME}/.ssh/ibacho.pem"
SRC="/mnt/c/Users/user/code/kaibaru_backend/tmp_layout_images/layouts"
STAGE="/tmp/kaibaru-hero-upload-stage"
REMOTE="bitnami@13.158.230.76"
REMOTE_DIR="/tmp/kaibaru-hero-upload-stage"

FOLDERS=(
  karate
  fitness
  gym
  default
  cram
  eikaiwa
  dance
  swimming
  sports
  hair_salon
  nail_salon
  massage_salon
)

rm -rf "$STAGE"
mkdir -p "$STAGE/layouts"
for folder in "${FOLDERS[@]}"; do
  if [ -f "$SRC/$folder/hero.mp4" ]; then
    mkdir -p "$STAGE/layouts/$folder"
    cp -f "$SRC/$folder/hero.mp4" "$STAGE/layouts/$folder/hero.mp4"
    echo "stage $folder $(wc -c < "$STAGE/layouts/$folder/hero.mp4")b"
  else
    echo "SKIP missing $folder"
    exit 1
  fi
done

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$REMOTE" "rm -rf $REMOTE_DIR"
scp -i "$KEY" -o StrictHostKeyChecking=accept-new -r "$STAGE" "$REMOTE:$REMOTE_DIR"

ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$REMOTE" bash -s <<'EOF'
set -euo pipefail
export GOOGLE_APPLICATION_CREDENTIALS=/home/bitnami/dp/service-account.json
/home/bitnami/dp/mysite/venv/bin/python - <<'PY'
import os
from pathlib import Path
from google.cloud import storage

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/bitnami/dp/service-account.json"
root = Path("/tmp/kaibaru-hero-upload-stage")
client = storage.Client()
bucket = client.bucket("sitepicsforkaibaru")
paths = sorted(root.rglob("hero.mp4"))
if not paths:
    raise SystemExit("no hero.mp4 files found on remote stage")
for path in paths:
    name = path.relative_to(root).as_posix()
    if not name.startswith("layouts/"):
        raise SystemExit(f"unexpected object path: {name}")
    blob = bucket.blob(name)
    blob.cache_control = "public, max-age=3600"
    blob.upload_from_filename(str(path), content_type="video/mp4")
    print(f"uploaded {name} ({path.stat().st_size} bytes)")
    print(f"https://storage.googleapis.com/sitepicsforkaibaru/{name}")
PY
EOF

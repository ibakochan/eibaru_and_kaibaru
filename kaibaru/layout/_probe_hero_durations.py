#!/usr/bin/env python3
"""Probe local + GCS hero.mp4 durations."""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

ASSETS = Path("/mnt/c/Users/user/.cursor/projects/c-Users-user-code/assets")
TMP = Path("/tmp/hero_probe")
TMP.mkdir(parents=True, exist_ok=True)

FOLDERS = [
    ("default", "default"),
    ("yoga", "yoga"),
    ("striking", "striking"),
    ("bjj", "bjj"),
    ("karate", "karate"),
    ("fitness", "fitness"),
    ("gym", "gym"),
    ("cram", "cram"),
    ("eikaiwa", "eikaiwa"),
    ("dance", "dance"),
    ("swimming", "swimming"),
    ("music", "music"),
    ("sports", "sports"),
    ("hair_salon", "hair_salon"),
    ("nail_salon", "nail_salon"),
    ("massage_salon", "massage_salon"),
]


def duration(path: Path) -> str:
    if not shutil.which("ffprobe"):
        return f"size={path.stat().st_size}"
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        text=True,
    ).strip()
    return out


def main() -> None:
    print("=== local assets ===")
    for key, _folder in FOLDERS:
        local = ASSETS / f"{key}-hero.mp4"
        if local.exists():
            print(f"{key:16} local {duration(local)}s {local.stat().st_size}b")
        else:
            print(f"{key:16} local MISSING")

    print("=== gcs ===")
    for key, folder in FOLDERS:
        url = f"https://storage.googleapis.com/sitepicsforkaibaru/layouts/{folder}/hero.mp4"
        dest = TMP / f"{folder}.mp4"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            dest.write_bytes(data)
            print(f"{folder:16} gcs {duration(dest)}s {len(data)}b")
        except Exception as exc:
            print(f"{folder:16} gcs FAIL {exc}")

    print("=== json video_url ===")
    layout_dir = Path("/mnt/c/Users/user/code/kaibaru_backend/kaibaru/layout")
    for path in sorted(layout_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for section in (data.get("sections") or {}).values():
            if isinstance(section, dict) and section.get("type") == "video":
                print(f"{path.stem:20} {section.get('video_url')}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Concatenate three 5s clips into a 15s hero.mp4."""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

ASSETS = Path("/mnt/c/Users/user/.cursor/projects/c-Users-user-code/assets")
OUT_ROOT = Path("/mnt/c/Users/user/code/kaibaru_backend/tmp_layout_images/layouts")


def mp4_duration(path: Path) -> float | None:
    data = path.read_bytes()
    i = data.find(b"mvhd")
    if i < 0:
        return None
    version = data[i + 4]
    if version == 1:
        timescale = struct.unpack(">I", data[i + 20 : i + 24])[0]
        duration = struct.unpack(">Q", data[i + 24 : i + 32])[0]
    else:
        timescale = struct.unpack(">I", data[i + 16 : i + 20])[0]
        duration = struct.unpack(">I", data[i + 20 : i + 24])[0]
    return duration / timescale if timescale else None


def concat(folder: str, clips: list[Path], out_name: str | None = None) -> Path:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    dest_dir = OUT_ROOT / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / (out_name or "hero.mp4")
    list_file = dest_dir / "concat.txt"
    lines = []
    for clip in clips:
        if not clip.exists():
            raise SystemExit(f"missing clip: {clip}")
        # ffmpeg concat demuxer wants escaped single quotes on Windows paths via WSL
        lines.append(f"file '{clip.as_posix()}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(out),
    ]
    print(" ".join(cmd))
    subprocess.check_call(cmd)
    print(f"wrote {out} duration={mp4_duration(out)} size={out.stat().st_size}")
    # also copy into assets for local preview naming
    assets_copy = ASSETS / f"{folder}-hero.mp4"
    assets_copy.write_bytes(out.read_bytes())
    print(f"copied {assets_copy}")
    return out


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        raise SystemExit("usage: _concat_hero_clips.py <folder> [clip1 clip2 clip3]")
    folder = argv[1]
    if len(argv) >= 5:
        clips = [Path(p) for p in argv[2:5]]
    else:
        clips = [
            ASSETS / f"{folder}-clip1.mp4",
            ASSETS / f"{folder}-clip2.mp4",
            ASSETS / f"{folder}-clip3.mp4",
        ]
    concat(folder, clips)


if __name__ == "__main__":
    main(sys.argv)

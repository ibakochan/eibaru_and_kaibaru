#!/usr/bin/env python3
"""Read MP4 durations from the mvhd box without ffprobe."""
from __future__ import annotations

import struct
from pathlib import Path


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
    if not timescale:
        return None
    return duration / timescale


def main() -> None:
    assets = Path("/mnt/c/Users/user/.cursor/projects/c-Users-user-code/assets")
    for path in sorted(assets.glob("*-hero.mp4")):
        dur = mp4_duration(path)
        print(f"{path.name:22} {dur}s  {path.stat().st_size}b")


if __name__ == "__main__":
    main()

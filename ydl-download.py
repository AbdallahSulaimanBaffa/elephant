#!/usr/bin/env python3
"""Local yt-dlp downloader with per-toggle embed options.

Toggles are read from ydl-config.txt in the same folder (key = true/false).
Run:  python3 ydl-download.py <URL>
"""

import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "ydl-config.txt")
YTDLP = os.path.join(HERE, "plugin", "yt-dlp", "yt_dlp", "__main__.py")


def load_config(path):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip().lower()] = v.strip().lower() in ("true", "1", "yes", "on")
    return cfg


def build_args(cfg, url):
    args = [sys.executable, YTDLP]

    if cfg.get("embed_chapters"):
        args += ["--embed-chapters"]
    if cfg.get("embed_metadata"):
        args += ["--embed-metadata"]
    if cfg.get("embed_thumbnail"):
        args += ["--embed-thumbnail", "--write-thumbnail", "--convert-thumbnails", "jpg"]
    if cfg.get("embed_subs"):
        args += ["--embed-subs"]

    # sane defaults
    args += ["--no-playlist"]

    args += [url]
    return args


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 ydl-download.py <URL>")
        sys.exit(2)

    cfg = load_config(CONFIG)
    args = build_args(cfg, sys.argv[1])

    print("Running yt-dlp with embed toggles:", {k: v for k, v in cfg.items() if v})
    subprocess.run(args, cwd=HERE, check=True)


if __name__ == "__main__":
    main()

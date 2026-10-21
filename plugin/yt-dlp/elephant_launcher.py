#!/usr/bin/env python3
"""Elephant launcher for FDM.

Runs yt-dlp to capture the info JSON, writes a sidecar per video
(title, chapters, description, channel, tags, thumbnail, subtitles, ...),
then spawns a detached watcher that embeds them into the finished
download. Prints yt-dlp's JSON to stdout so FDM's parse flow is unchanged.
"""

import subprocess
import sys
import os
import json
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
YTDLP_MAIN = os.path.join(HERE, "yt_dlp", "__main__.py")
WATCHER = os.path.join(PLUGIN_ROOT, "watcher.py")
CONFIG = os.path.join(PLUGIN_ROOT, "ydl-config.txt")
SIDECAR_DIR = os.path.join(PLUGIN_ROOT, "sidecars")
LOG = os.path.join(PLUGIN_ROOT, "launcher.log")

# subtitle formats in order of preference (json3 converts to clean srt)
SUB_FORMATS = ["json3", "srt", "vtt"]

# info fields NOT copied into the sidecar's "info" block: bulky lists, duplicates,
# and format details that describe yt-dlp's pick rather than the file FDM saved
INFO_SKIP = {
    "description", "chapters", "duration", "display_id", "fulltitle", "original_url",
    "webpage_url_basename", "webpage_url_domain", "extractor", "epoch",
    "playlist", "playlist_index", "format", "format_id", "format_note", "ext",
    "protocol", "filesize", "filesize_approx", "tbr", "vbr", "abr", "asr", "width",
    "height", "resolution", "fps", "dynamic_range", "vcodec", "acodec",
    "stretched_ratio", "aspect_ratio", "audio_channels",
}


def info_block(info):
    """Every simple top-level field (str/number/bool or list of those)."""
    out = {}
    for k, v in info.items():
        if k.startswith("_") or k in INFO_SKIP or v in (None, "", []):
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            out[k] = v
    return out


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def load_config():
    cfg = {"embed_subs": False, "auto_subs": True, "sub_langs": "en"}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = (s.strip() for s in line.split("=", 1))
                k = k.lower()
                if k in ("embed_subs", "auto_subs"):
                    cfg[k] = v.lower() in ("true", "1", "yes", "on")
                elif k == "sub_langs":
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def pick_format(tracks):
    for fmt in SUB_FORMATS:
        for t in tracks:
            if t.get("ext") == fmt and t.get("url"):
                return t
    return None


def pick_subtitles(info, cfg):
    """Choose one track per wanted language: manual first, then auto."""
    if not cfg["embed_subs"]:
        return []
    manual = info.get("subtitles") or {}
    auto = (info.get("automatic_captions") or {}) if cfg["auto_subs"] else {}
    langs = [l.strip() for l in cfg["sub_langs"].split(",") if l.strip()]
    if "all" in langs:
        langs = [l for l in manual if l != "live_chat"]

    picked = []
    for lang in langs:
        for source, is_auto in ((manual, False), (auto, True)):
            # exact key first, then the original-language auto track
            keys = [lang, lang + "-orig"] + [k for k in source if k.startswith(lang + "-")]
            track = None
            for key in keys:
                if key in source:
                    track = pick_format(source[key] or [])
                    if track:
                        break
            if track:
                picked.append({"lang": lang, "auto": is_auto,
                               "ext": track["ext"], "url": track["url"],
                               "name": track.get("name") or lang})
                break
    return picked


def spawn_watcher():
    """Start watcher.py detached so it outlives this launcher."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, WATCHER],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         cwd=PLUGIN_ROOT, **kw)
        log("watcher spawned")
    except Exception as e:
        log("watcher spawn failed: " + repr(e))


def write_sidecar(info, cfg):
    chapters = info.get("chapters")
    sidecar = {
        "request_time": time.time(),
        "id": info.get("id"),
        "webpage_url": info.get("webpage_url"),
        "title": info.get("title"),
        "ext": info.get("ext"),
        "chapters": chapters if isinstance(chapters, list) else None,
        "description": info.get("description"),
        "channel": info.get("channel"),
        "uploader": info.get("uploader"),
        "uploader_id": info.get("uploader_id"),
        "channel_url": info.get("channel_url"),
        "tags": info.get("tags"),
        "categories": info.get("categories"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "subtitles": pick_subtitles(info, cfg),
        "info": info_block(info),
    }
    key = "".join(c if c.isalnum() or c in "-_" else "_"
                  for c in str(info.get("extractor_key") or "") + "_" + str(info.get("id")))
    path = os.path.join(SIDECAR_DIR, key + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=1)
    log("sidecar: %s (%d chapters, %d subs)" % (
        os.path.basename(path), len(sidecar["chapters"] or []), len(sidecar["subtitles"])))


def main():
    os.makedirs(SIDECAR_DIR, exist_ok=True)
    args = sys.argv[1:]

    result = subprocess.run([sys.executable, YTDLP_MAIN] + args,
                            capture_output=True, text=True)
    out = result.stdout

    try:
        info = json.loads(out)
    except Exception:
        info = None

    # single videos only; playlist entries get their own parse when FDM adds them
    if isinstance(info, dict) and info.get("_type", "video") == "video" and info.get("id"):
        try:
            write_sidecar(info, load_config())
            spawn_watcher()
        except Exception as e:
            log("sidecar write failed: " + repr(e))

    # hand yt-dlp's JSON straight back to FDM (unchanged behaviour)
    sys.stdout.write(out)
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Elephant watcher (detached).

Watches the download folder(s) for finished videos that match a sidecar,
then embeds chapters / metadata / thumbnail / subtitles into the file with
ffmpeg, per the toggles in ydl-config.txt. Single-instance via a file lock
(works across Flatpak sandboxes, unlike a pid check). Exits as soon as
nothing is pending, when FDM closes, or when watcher.py itself is updated,
so stale code never lingers; the launcher respawns it on the next parse.
"""

import os
import re
import sys
import json
import time
import fcntl
import shutil
import tempfile
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "ydl-config.txt")
SIDECAR_DIR = os.path.join(HERE, "sidecars")
DONE_DIR = os.path.join(SIDECAR_DIR, "done")
FAILED_DIR = os.path.join(SIDECAR_DIR, "failed")
LOG = os.path.join(HERE, "watcher.log")
LOCKFILE = os.path.join(HERE, "watcher.lock")

POLL_SECONDS = 3
STABLE_SECONDS = 8
SIDECAR_TTL = 24 * 3600      # give up on a sidecar after a day
MAX_ATTEMPTS = 3
RETRY_SECONDS = 30

MEDIA_EXTS = {".mp4", ".m4a", ".mov", ".mkv", ".webm", ".mka"}
MP4_EXTS = {".mp4", ".m4a", ".mov"}
SKIP_MARKERS = (".fdmdownload", "(audio part)", "(video part)", ".elephant-tmp")

ISO639_2 = {"en": "eng", "ar": "ara", "fr": "fra", "de": "deu", "es": "spa",
            "it": "ita", "pt": "por", "ru": "rus", "ja": "jpn", "ko": "kor",
            "zh": "zho", "tr": "tur", "nl": "nld", "pl": "pol", "hi": "hin",
            "id": "ind", "ur": "urd", "fa": "fas", "sv": "swe", "uk": "ukr"}


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def load_config():
    cfg = {
        "embed_chapters": True,
        "embed_metadata": True,
        "embed_thumbnail": True,
        "embed_subs": False,
        "notify": True,
        "watch_folder": "",
        "ffmpeg": "ffmpeg",
    }
    try:
        with open(CONFIG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = (s.strip() for s in line.split("=", 1))
                k = k.lower()
                if k in ("watch_folder", "ffmpeg"):
                    cfg[k] = v          # paths keep their case
                elif k in cfg:
                    cfg[k] = v.lower() in ("true", "1", "yes", "on")
    except Exception:
        pass
    return cfg


def acquire_lock():
    """Hold an exclusive lock for our lifetime; None if another watcher has it."""
    fd = open(LOCKFILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    fd.write(str(os.getpid()))
    fd.flush()
    return fd


def norm(s):
    return re.sub(r"[^0-9a-z]+", "", (s or "").lower())


def watch_folders(cfg):
    folders = [os.path.expanduser(p.strip()) for p in cfg["watch_folder"].split(",") if p.strip()]
    folders.append(os.path.expanduser("~/Downloads"))
    seen = []
    for d in folders:
        if os.path.isdir(d) and d not in seen:
            seen.append(d)
    return seen


def find_file(sidecar, folders):
    """Return (path, busy). busy=True means FDM is still writing parts for it."""
    want = norm(sidecar.get("title"))
    if not want:
        return None, False
    since = sidecar.get("request_time", 0) - 60
    best, busy = None, False
    for d in folders:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            stem, ext = os.path.splitext(name)
            n = norm(stem)
            if any(m in name for m in SKIP_MARKERS):
                # FDM's in-progress parts / temp files for this video
                if want in norm(name):
                    busy = True
                continue
            # FDM may append " (1)" for duplicates or truncate long titles
            if not (n == want or (n.startswith(want) and n[len(want):].isdigit())
                    or (len(n) >= 20 and want.startswith(n))):
                continue
            p = os.path.join(d, name)
            if ext.lower() not in MEDIA_EXTS or not os.path.isfile(p):
                continue
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if mtime < since:
                continue        # older file with the same title
            if best is None or mtime > os.path.getmtime(best):
                best = p
    return best, busy


def size_stable(path):
    try:
        s1 = os.path.getsize(path)
        time.sleep(STABLE_SECONDS)
        return os.path.getsize(path) == s1 and s1 > 0
    except OSError:
        return False


def fetch(url, out_path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(out_path, "wb") as f:
        shutil.copyfileobj(r, f)
    return out_path


def ffescape(s):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(s))


def write_chapters(chapters, duration, out_path):
    lines = [";FFMETADATA1"]
    for i, ch in enumerate(chapters):
        start = float(ch.get("start_time") or 0)
        end = ch.get("end_time")
        if end is None:
            end = chapters[i + 1].get("start_time") if i + 1 < len(chapters) else duration
        end = float(end or start)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  "START=%d" % round(start * 1000), "END=%d" % round(end * 1000),
                  "title=" + ffescape(ch.get("title") or "Chapter %d" % (i + 1))]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


def srt_time(ms):
    ms = max(0, int(ms))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def json3_to_srt(src, dst):
    """YouTube json3 -> srt; clips overlaps so rolling auto-captions don't stack."""
    with open(src, encoding="utf-8") as f:
        events = json.load(f).get("events") or []
    cues = []
    for ev in events:
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).strip()
        if not text:
            continue
        start = ev.get("tStartMs", 0)
        cues.append([start, start + ev.get("dDurationMs", 0), text])
    for i in range(len(cues) - 1):
        cues[i][1] = min(cues[i][1], cues[i + 1][0])
    with open(dst, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(c for c in cues if c[1] > c[0]):
            f.write("%d\n%s --> %s\n%s\n\n" % (i + 1, srt_time(a), srt_time(b), t))
    return dst


INFO_LABELS = {
    "id": "Video ID", "webpage_url": "URL", "channel_follower_count": "Subscribers",
    "channel_is_verified": "Verified channel", "timestamp": "Uploaded at",
    "extractor_key": "Site", "duration_string": "Duration", "view_count": "Views",
    "like_count": "Likes", "comment_count": "Comments", "channel_url": "Channel URL",
    "channel_id": "Channel ID", "uploader_url": "Uploader URL", "uploader_id": "Uploader ID",
    "thumbnail": "Thumbnail URL",
}


def fmt_value(key, v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    if key.endswith("_date") and isinstance(v, str) and len(v) == 8 and v.isdigit():
        return "%s-%s-%s" % (v[:4], v[4:6], v[6:])
    if key.endswith("timestamp") and isinstance(v, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(v))
    if key.endswith(("_count", "_follower_count")) and isinstance(v, int):
        return "{:,}".format(v)
    return str(v)


def info_text(info, chapters):
    """Readable 'Label: value' block of every info field, plus the chapter list."""
    lines = ["%s: %s" % (INFO_LABELS.get(k) or k.replace("_", " ").capitalize(), fmt_value(k, v))
             for k, v in info.items()]
    if chapters:
        lines += ["", "Chapters:"]
        for ch in chapters:
            s = int(ch.get("start_time") or 0)
            stamp = "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60) if s >= 3600 else "%02d:%02d" % (s // 60, s % 60)
            lines.append("%s %s" % (stamp, ch.get("title") or ""))
    return "\n".join(lines)


def count_video_streams(cfg, path):
    ffprobe = os.path.join(os.path.dirname(cfg["ffmpeg"]), "ffprobe") if os.sep in cfg["ffmpeg"] else "ffprobe"
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v",
                              "-show_entries", "stream=index", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout
        return len(out.split())
    except Exception:
        return 1


def embed(sidecar, filepath, cfg):
    """Embed enabled extras into filepath via a temp file; True on success."""
    ext = os.path.splitext(filepath)[1].lower()
    is_mp4, is_mkv, is_webm = ext in MP4_EXTS, ext in (".mkv", ".mka"), ext == ".webm"
    work = tempfile.mkdtemp(prefix="elephant-", dir=HERE)
    out = os.path.join(os.path.dirname(filepath),
                       os.path.basename(filepath) + ".elephant-tmp" + ext)
    inputs = [filepath]
    maps = ["-map", "0", "-dn"]
    opts = ["-c", "copy", "-map_metadata", "0"]
    done = []

    try:
        if cfg["embed_chapters"] and sidecar.get("chapters"):
            inputs.append(write_chapters(sidecar["chapters"], sidecar.get("duration"),
                                         os.path.join(work, "chapters.txt")))
            opts += ["-map_chapters", str(len(inputs) - 1)]
            done.append("%d chapters" % len(sidecar["chapters"]))

        if cfg["embed_subs"] and ext not in (".m4a", ".mka"):
            sub_codec = "mov_text" if is_mp4 else "webvtt" if is_webm else "srt"
            n = 0
            for sub in sidecar.get("subtitles") or []:
                try:
                    raw = fetch(sub["url"], os.path.join(work, "sub%d.%s" % (n, sub["ext"])))
                    if sub["ext"] == "json3":
                        raw = json3_to_srt(raw, os.path.join(work, "sub%d.srt" % n))
                except Exception as e:
                    log("subtitle %s download failed: %r" % (sub.get("lang"), e))
                    continue
                inputs.append(raw)
                maps += ["-map", "%d:s" % (len(inputs) - 1)]
                lang = ISO639_2.get(sub["lang"].split("-")[0], sub["lang"])
                opts += ["-metadata:s:s:%d" % n, "language=" + lang,
                         "-metadata:s:s:%d" % n, "title=" + sub.get("name", lang)]
                n += 1
            if n:
                opts += ["-c:s", sub_codec]
                if is_mp4:
                    # without this the mp4 muxer corrupts chapter 0 when mov_text is present
                    opts += ["-time_base:s", "1/1000"]
                done.append("%d subs" % n)

        if cfg["embed_metadata"]:
            date = sidecar.get("upload_date") or ""
            if len(date) == 8:
                date = "%s-%s-%s" % (date[:4], date[4:6], date[6:])
            meta = {
                "title": sidecar.get("title"),
                "artist": sidecar.get("channel") or sidecar.get("uploader"),
                "date": date,
                "description": sidecar.get("description"),
                "synopsis": sidecar.get("description"),
                # VLC shows the comment tag as the file's Description
                "comment": sidecar.get("description"),
                "purl": sidecar.get("webpage_url"),
                "genre": ", ".join(sidecar.get("categories") or []),
                "keywords": ", ".join(sidecar.get("tags") or []),
            }
            info = sidecar.get("info") or {k: sidecar.get(k) for k in (
                "id", "channel", "channel_url", "uploader_id", "view_count",
                "like_count", "upload_date", "webpage_url") if sidecar.get(k)}
            if is_mp4:
                # mp4 has no free-form tags VLC reads; these three are the only
                # extras it shows (as Writer, Grouping and Lyrics) on its Metadata tab
                meta.update({
                    "composer": sidecar.get("channel") or sidecar.get("uploader"),
                    "grouping": sidecar.get("webpage_url"),
                    "lyrics": info_text(info, sidecar.get("chapters")),
                    "episode_id": sidecar.get("id"),
                })
            else:
                # Matroska keeps every tag and VLC lists them all under Metadata
                for k, v in info.items():
                    if k not in meta:
                        meta[k] = fmt_value(k, v)
            for k, v in meta.items():
                if v:
                    opts += ["-metadata", "%s=%s" % (k, v)]
            done.append("metadata")

        if cfg["embed_thumbnail"] and sidecar.get("thumbnail"):
            if is_webm:
                log("cover skipped: webm can't hold cover art")
            else:
                try:
                    raw = fetch(sidecar["thumbnail"], os.path.join(work, "thumb_raw"))
                    jpg = os.path.join(work, "cover.jpg")
                    subprocess.run([cfg["ffmpeg"], "-y", "-v", "error", "-i", raw, "-frames:v", "1", jpg],
                                   check=True, capture_output=True, timeout=60)
                    if is_mp4:
                        vidx = count_video_streams(cfg, filepath)
                        inputs.append(jpg)
                        maps += ["-map", "%d:v" % (len(inputs) - 1)]
                        opts += ["-disposition:v:%d" % vidx, "attached_pic"]
                    elif is_mkv:
                        opts += ["-attach", jpg, "-metadata:s:t", "mimetype=image/jpeg",
                                 "-metadata:s:t", "filename=cover.jpg"]
                    done.append("cover")
                except Exception as e:
                    log("thumbnail failed: %r" % e)

        if not done:
            log("nothing to embed for " + os.path.basename(filepath))
            return True

        args = [cfg["ffmpeg"], "-y", "-v", "error"]
        for p in inputs:
            args += ["-i", p]
        args += maps + opts
        if is_mp4:
            args += ["-movflags", "+faststart"]
        args.append(out)

        log("embed start: %s [%s]" % (os.path.basename(filepath), ", ".join(done)))
        res = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        if res.returncode != 0 or not os.path.exists(out):
            log("embed FAILED rc=%d: %s" % (res.returncode, (res.stderr or "").strip()[-800:]))
            log("cmd: " + " ".join(repr(a) for a in args))
            return False
        os.replace(out, filepath)
        log("embedded OK: " + filepath)
        if cfg["notify"]:
            notify("Added " + ", ".join(done), os.path.basename(filepath))
        return True
    except Exception as e:
        log("embed exception: %r" % e)
        return False
    finally:
        if os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)


def notify(summary, body):
    """Desktop notification; FDM's Flatpak is allowed to talk to the notification service."""
    try:
        subprocess.run(["notify-send", "--app-name=Free Download Manager",
                        "--icon=org.freedownloadmanager.Manager", "Elephant: " + summary, body],
                       capture_output=True, timeout=10)
    except Exception as e:
        log("notify failed: %r" % e)


def retire(spath, folder):
    try:
        os.makedirs(folder, exist_ok=True)
        os.replace(spath, os.path.join(folder, os.path.basename(spath)))
    except OSError:
        pass


def pending():
    try:
        return sorted(n for n in os.listdir(SIDECAR_DIR) if n.endswith(".json"))
    except OSError:
        return []


def fdm_running():
    """Inside FDM's Flatpak sandbox, check the fdm process is still alive."""
    if not os.path.exists("/.flatpak-info"):
        return True     # outside the sandbox (manual runs/tests): can't tell
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open("/proc/%s/comm" % pid) as f:
                    if f.read().strip() == "fdm":
                        return True
            except OSError:
                pass
    return False


def main():
    lock = acquire_lock()
    if lock is None:
        return
    cfg = load_config()
    log("watcher started pid=%d (ffmpeg=%s, folders=%s)" % (os.getpid(), cfg["ffmpeg"], watch_folders(cfg)))
    attempts = {}
    my_mtime = os.path.getmtime(os.path.abspath(__file__))
    while True:
        cfg = load_config()  # toggles are re-read every pass
        folders = watch_folders(cfg)
        names = pending()
        if not names:
            log("nothing pending, exiting")
            lock.close()
            # a parse may have queued a sidecar while we were deciding to quit
            if pending():
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
            return
        if not fdm_running():
            log("FDM closed, exiting (pending sidecars are picked up next time)")
            return
        try:
            if os.path.getmtime(os.path.abspath(__file__)) != my_mtime:
                log("watcher.py was updated, exiting so the new code takes over")
                lock.close()
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
        except OSError:
            return

        for name in names:
            spath = os.path.join(SIDECAR_DIR, name)
            try:
                with open(spath, encoding="utf-8") as f:
                    sidecar = json.load(f)
            except Exception:
                continue
            if time.time() - sidecar.get("request_time", 0) > SIDECAR_TTL:
                log("expired (never saw the file): " + name)
                retire(spath, FAILED_DIR)
                continue
            count, last_try = attempts.get(name, (0, 0))
            if time.time() - last_try < RETRY_SECONDS:
                continue
            path, busy = find_file(sidecar, folders)
            if busy or not path or not size_stable(path):
                continue
            # re-check: FDM may have started merging parts during the wait
            if find_file(sidecar, folders)[1]:
                continue
            if embed(sidecar, path, cfg):
                retire(spath, DONE_DIR)
                attempts.pop(name, None)
            else:
                attempts[name] = (count + 1, time.time())
                if count + 1 >= MAX_ATTEMPTS:
                    log("giving up after %d attempts: %s" % (MAX_ATTEMPTS, name))
                    if cfg["notify"]:
                        notify("Couldn't add chapters/metadata", "%s\nSee watcher.log in the Elephant plugin folder."
                               % (sidecar.get("title") or name))
                    retire(spath, FAILED_DIR)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

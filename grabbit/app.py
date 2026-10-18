#!/usr/bin/env python3
"""
Grabbit - a local video & chapter downloader.

A small Flask web app that wraps yt-dlp. It runs entirely on your own
machine and can fetch video/audio from Internet Archive, YouTube, Vimeo,
Wikimedia Commons and ~1,800 other sites that yt-dlp supports, including
splitting a video into its individual chapters.

Usage (requires Python 3.10+):
    pip install flask "yt-dlp[default]"
    # Also install, on your PATH:
    #   - ffmpeg + ffprobe  (the BINARIES, not the PyPI package called "ffmpeg")
    #   - a JS runtime (deno / node / bun) for full YouTube support
    python app.py
    # then open http://127.0.0.1:8080

Tip: sites change often and the monthly "stable" yt-dlp can go stale. If a
download breaks, update first:  pip install -U --pre "yt-dlp[default]"

Use responsibly: download only content you own, that is public domain /
openly licensed (e.g. most Internet Archive items, Creative Commons,
Wikimedia Commons), or that you otherwise have the right to download.
Respect each site's Terms of Service and applicable copyright law.
"""

import io
import mimetypes
import os
import platform
import posixpath
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template_string,
    request,
    send_file,
)

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    raise SystemExit("yt-dlp is not installed. Run:  pip install yt-dlp")

APP_DIR = Path(__file__).resolve().parent
DOWNLOAD_ROOT = APP_DIR / "downloads"
DOWNLOAD_ROOT.mkdir(exist_ok=True)

# Self-contained tools live here, inside the app folder.
BIN_DIR = APP_DIR / "bin"
FFMPEG_DIR = None  # set at startup: folder holding ffmpeg/ffprobe, or None (use system)

app = Flask(__name__)

# In-memory job registry. Each job tracks the progress of one download.
JOBS = {}
JOBS_LOCK = threading.Lock()

CHAPTER_RE = re.compile(r"^\d{3,4} - ")  # matches our chapter filename prefix

# Build a format selector that strongly prefers the widely-playable combo
# (H.264 / AVC video + AAC audio in MP4), so files open in Windows Media Player,
# QuickTime, phones, etc. — instead of YouTube's default AV1 + Opus, which many
# players can't decode. Falls back to other codecs only if nothing else exists.
def _compat_format(height=None):
    h = f"[height<={height}]" if height else ""
    return (
        f"bv*[vcodec^=avc1]{h}+ba[acodec^=mp4a]/"  # ideal: H.264 + AAC
        f"bv*[vcodec^=avc1]{h}+ba/"                 # H.264 video + any audio
        f"b[vcodec^=avc1]{h}/"                      # progressive H.264
        f"bv*{h}+ba/b{h}"                           # last resort: whatever exists
    )


# Map the UI's quality choice to a yt-dlp format selector.
QUALITY_FORMATS = {
    "best": _compat_format(None),
    "1080": _compat_format(1080),
    "720": _compat_format(720),
    "480": _compat_format(480),
    "360": _compat_format(360),
}


# "Highest quality" selectors: best stream at the chosen resolution regardless
# of codec (may be AV1/VP9/4K — great quality, but some players need VLC).
def _quality_format(height=None):
    h = f"[height<={height}]" if height else ""
    return f"bv*{h}+ba/b{h}"


QUALITY_RAW = {
    "best": _quality_format(None),
    "1080": _quality_format(1080),
    "720": _quality_format(720),
    "480": _quality_format(480),
    "360": _quality_format(360),
}


# A browser-like User-Agent — some servers reject the default urllib agent.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def human_duration(seconds):
    if not seconds:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(num):
    if not num:
        return None
    num = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def _best_thumb(d):
    if not d:
        return None
    if d.get("thumbnail"):
        return d["thumbnail"]
    thumbs = d.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


# --------------------------------------------------------------------------- #
#  Metadata lookup
# --------------------------------------------------------------------------- #
@app.post("/api/info")
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Please paste a link."), 400
    if not re.match(r"^https?://", url, re.I):
        return jsonify(error="Links must start with http:// or https://"), 400

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,            # let playlists be recognised
        "extract_flat": "in_playlist",  # fast: don't resolve every entry
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    # yt-dlp's generic extractor scans arbitrary pages for embedded video,
    # so this also works on sites without a dedicated extractor.
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        # Not a recognised media page — maybe it's a direct file link.
        direct = _probe_direct(url)
        if direct:
            return jsonify(kind="direct", **direct)
        return jsonify(error=f"Could not read that link: {_clean_err(exc)}"), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify(error=f"Unexpected error: {exc}"), 500

    # --- a playlist / channel ---
    if info.get("_type") == "playlist":
        entries = _flatten_entries(info.get("entries"))
        if not entries:
            return jsonify(error="That playlist looks empty or private."), 400
        first = entries[0]
        # A specific video is also available when the link points at one.
        single_available = bool(
            re.search(r"[?&]v=", url) or "/watch" in url or "youtu.be/" in url
        )
        return jsonify(
            kind="media",
            is_playlist=True,
            playlist={"title": info.get("title") or "Playlist", "count": len(entries)},
            title=info.get("title") or "Playlist",
            uploader=info.get("uploader") or info.get("channel") or info.get("extractor_key"),
            source=info.get("extractor_key"),
            thumbnail=_best_thumb(first) or _best_thumb(info),
            first_title=first.get("title"),
            single_available=single_available,
            entries=[_entry_summary(e, i) for i, e in enumerate(entries, 1)],
            has_chapters=False,
            chapters=[],
        )

    # --- a single video ---
    chapters = info.get("chapters") or []
    chapter_list = [
        {
            "title": c.get("title") or f"Chapter {i + 1}",
            "start": human_duration(c.get("start_time")),
        }
        for i, c in enumerate(chapters)
    ]

    return jsonify(
        kind="media",
        is_playlist=False,
        title=info.get("title") or "Untitled",
        uploader=info.get("uploader") or info.get("channel") or info.get("extractor_key"),
        source=info.get("extractor_key"),
        duration=human_duration(info.get("duration")),
        thumbnail=info.get("thumbnail"),
        chapters=chapter_list,
        has_chapters=bool(chapter_list),
    )


def _flatten_entries(entries):
    """List every video in a playlist/channel, opening nested lists (e.g. a
    channel's Videos / Shorts / Live tabs) so they read as one list."""
    out = []
    for e in entries or []:
        if not e:
            continue
        if e.get("_type") == "playlist" or isinstance(e.get("entries"), list):
            out.extend(_flatten_entries(e.get("entries")))
        else:
            out.append(e)
    return out


def _small_thumb(e):
    """A thumbnail small enough for a list row."""
    thumbs = [t for t in (e.get("thumbnails") or []) if t.get("url")]
    sized = [t for t in thumbs if (t.get("width") or 0) >= 160]
    if sized:
        return min(sized, key=lambda t: t["width"])["url"]
    return thumbs[-1]["url"] if thumbs else e.get("thumbnail")


def _entry_summary(e, position):
    url = e.get("webpage_url") or e.get("url") or ""
    return {
        # Number by place in the combined list; a channel's tabs each count from 1.
        "index": position,
        "url": url if re.match(r"^https?://", url, re.I) else "",
        "title": e.get("title") or f"Video {position}",
        "duration": human_duration(e.get("duration")),
        "thumbnail": _small_thumb(e),
    }


# --------------------------------------------------------------------------- #
#  Download
# --------------------------------------------------------------------------- #
@app.post("/api/download")
def api_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    mode = data.get("mode", "video")  # video | chapters | audio | direct
    quality = data.get("quality", "best")
    codec = data.get("codec", "compatible")  # compatible | quality | force_h264
    scope = data.get("scope", "single")  # single | playlist
    playlist_count = int(data.get("playlist_count") or 0)
    filename = data.get("filename")  # only used for direct downloads
    if not url:
        return jsonify(error="Missing URL."), 400
    items = []
    if scope == "selected":
        items = _clean_items(data.get("items"))
        if not items:
            return jsonify(error="Pick at least one video."), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "speed": None,
            "eta": None,
            "title": None,
            "phase": "Preparing…",
            "error": None,
            "result_path": None,
            "download_name": None,
            "warning": None,
        }
        if scope == "selected" and data.get("title"):
            JOBS[job_id]["title"] = str(data["title"])

    if mode == "direct":
        target = _run_direct
        args = (job_id, url, filename)
    elif scope == "selected":
        target = _run_selected
        args = (job_id, url, items, mode, quality, codec)
    else:
        target = _run_download
        args = (job_id, url, mode, quality, scope, playlist_count, codec)

    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return jsonify(job_id=job_id)


def _run_download(job_id, url, mode, quality, scope="single", playlist_count=0, codec="compatible"):
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    is_playlist = scope == "playlist"

    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                frac = done / total if total else 0
                spd = d.get("speed")
                job["speed"] = f"{spd / 1_048_576:.1f} MB/s" if spd else None
                eta = d.get("eta")
                job["eta"] = f"{int(eta)}s" if eta else None
                job["status"] = "downloading"
                if is_playlist:
                    pinfo = d.get("info_dict") or {}
                    count = pinfo.get("playlist_count") or playlist_count or 1
                    idx = pinfo.get("playlist_index") or 1
                    job["progress"] = round(min(((idx - 1) + frac) / max(count, 1) * 100, 99.0), 1)
                    job["phase"] = f"Downloading video {idx} of {count}…"
                else:
                    job["progress"] = round(frac * 100, 1)
                    job["phase"] = "Downloading…"
            elif d["status"] == "finished":
                if is_playlist:
                    job["phase"] = "Processing…"  # more videos may follow
                else:
                    job["progress"] = 100
                    job["status"] = "processing"
                    job["phase"] = "Converting / merging…"

    outtmpl_default = (
        "%(playlist_index)03d - %(title).180B.%(ext)s" if is_playlist
        else "%(title).200B.%(ext)s"
    )
    opts = {
        "noplaylist": not is_playlist,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "paths": {"home": str(job_dir)},
        "outtmpl": {"default": outtmpl_default},
        "restrictfilenames": False,
        "windowsfilenames": True,  # keep names portable across OSes
    }
    if is_playlist:
        opts["ignoreerrors"] = True  # one bad entry shouldn't sink the whole run
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR

    # Chapter-splitting only applies to a single video.
    _apply_media_opts(opts, mode, quality, codec, split_chapters=not is_playlist)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = info.get("title") or ("playlist" if is_playlist else "video")
        with JOBS_LOCK:
            JOBS[job_id]["title"] = title

        if codec == "force_h264" and mode != "audio":
            _force_h264_dir(job_id, job_dir)

        _finalize(job_id, job_dir, mode, title, scope)
    except yt_dlp.utils.DownloadError as exc:
        _fail(job_id, _clean_err(exc))
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, str(exc))


def _apply_media_opts(opts, mode, quality, codec, split_chapters=False):
    """Set yt-dlp's format and post-processing for the chosen mode."""
    if mode == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
        return
    fmt_map = QUALITY_RAW if codec == "quality" else QUALITY_FORMATS
    opts["format"] = fmt_map.get(quality, fmt_map["best"])
    opts["merge_output_format"] = "mp4"
    if mode == "chapters" and split_chapters:
        opts["outtmpl"]["chapter"] = "%(section_number)03d - %(section_title).150B.%(ext)s"
        opts["postprocessors"] = [{"key": "FFmpegSplitChapters", "force_keyframes": False}]
    else:  # plain video: embed chapter markers if present
        opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_chapters": True}]


# --------------------------------------------------------------------------- #
#  Download only the videos the user picked from a playlist / channel
# --------------------------------------------------------------------------- #
def _clean_items(raw):
    """Keep well-formed picks: each needs a web link or a playlist position."""
    items = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        link = str(it.get("url") or "").strip()
        try:
            index = int(it.get("index") or 0)
        except (TypeError, ValueError):
            index = 0
        if not re.match(r"^https?://", link, re.I):
            link = ""
        if link or index > 0:
            items.append({"url": link, "index": index, "title": str(it.get("title") or "")})
    return items


def _run_selected(job_id, list_url, items, mode, quality, codec):
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    total = len(items)
    state = {"n": 0}  # which pick (1-based) is downloading now

    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            n = state["n"]
            if d["status"] == "downloading":
                size = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                frac = done / size if size else 0
                spd = d.get("speed")
                job["speed"] = f"{spd / 1_048_576:.1f} MB/s" if spd else None
                eta = d.get("eta")
                job["eta"] = f"{int(eta)}s" if eta else None
                job["status"] = "downloading"
                job["progress"] = round(min(((n - 1) + frac) / total * 100, 99.0), 1)
                job["phase"] = f"Downloading video {n} of {total}…"
            elif d["status"] == "finished":
                job["phase"] = f"Processing video {n} of {total}…"

    failed = []
    for n, item in enumerate(items, 1):
        state["n"] = n
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            job["status"] = "downloading"
            job["phase"] = f"Starting video {n} of {total}…"
            job["progress"] = round((n - 1) / total * 100, 1)

        # Number files by their place in the channel so the zip keeps its order.
        prefix = f"{item['index']:03d}" if item["index"] else f"{n:03d}"
        opts = {
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
            "paths": {"home": str(job_dir)},
            "outtmpl": {"default": f"{prefix} - %(title).180B.%(ext)s"},
            "restrictfilenames": False,
            "windowsfilenames": True,
        }
        if item["url"]:
            target, opts["noplaylist"] = item["url"], True
        else:  # no direct link for this entry: fetch it by its playlist position
            target, opts["noplaylist"] = list_url, False
            opts["playlist_items"] = str(item["index"])
        if FFMPEG_DIR:
            opts["ffmpeg_location"] = FFMPEG_DIR
        _apply_media_opts(opts, mode, quality, codec)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(target, download=True)
        except Exception as exc:  # noqa: BLE001 — one bad video shouldn't stop the rest
            failed.append(item["title"] or f"video {n}")
            print(f"  ⚠  Skipped {item['title'] or target}: {_clean_err(exc)}")

    if len(failed) == total:
        _fail(job_id, "None of the chosen videos could be downloaded.")
        return
    if failed:
        with JOBS_LOCK:
            JOBS[job_id]["warning"] = (
                f"{len(failed)} of {total} videos could not be downloaded and were skipped."
            )

    if codec == "force_h264" and mode != "audio":
        _force_h264_dir(job_id, job_dir)

    title = "selected videos"
    with JOBS_LOCK:
        title = JOBS[job_id].get("title") or title
    _finalize(job_id, job_dir, mode, title, "playlist")


def _finalize(job_id, job_dir, mode, title, scope="single"):
    """Decide what single artifact the user downloads."""
    media = [
        p for p in job_dir.iterdir()
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]

    # Whole playlist -> bundle every downloaded file into one zip.
    if scope == "playlist":
        if not media:
            _fail(job_id, "No files were produced.")
            return
        if len(media) == 1:
            _succeed(job_id, media[0], media[0].name)
            return
        zip_path = job_dir / "playlist.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in sorted(media):
                zf.write(p, arcname=p.name)
        _succeed(job_id, zip_path, f"{_safe(title)}.zip")
        return

    if mode == "chapters":
        chapter_files = [p for p in media if CHAPTER_RE.match(p.name)]
        if chapter_files:
            # Remove the un-split original so the zip only holds chapters.
            for p in media:
                if p not in chapter_files:
                    p.unlink(missing_ok=True)
            zip_path = job_dir / "chapters.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for p in sorted(chapter_files):
                    zf.write(p, arcname=p.name)
            _succeed(job_id, zip_path, f"{_safe(title)} - chapters.zip")
            return
        # No chapters existed -> fall back to the single file we got.

    if not media:
        _fail(job_id, "No file was produced.")
        return

    result = max(media, key=lambda p: p.stat().st_size)
    _succeed(job_id, result, result.name)


def _succeed(job_id, path, download_name):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(
                status="done",
                progress=100,
                phase="Ready",
                result_path=str(path),
                download_name=download_name,
            )


def _fail(job_id, message):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(status="error", error=message, phase="Failed")


# --------------------------------------------------------------------------- #
#  "Force H.264" — re-encode only the files that aren't already compatible
# --------------------------------------------------------------------------- #
def _tool(name):
    """Path to ffmpeg/ffprobe, preferring the bundled copy in ./bin."""
    exe = name + (".exe" if os.name == "nt" else "")
    if FFMPEG_DIR and (Path(FFMPEG_DIR) / exe).exists():
        return str(Path(FFMPEG_DIR) / exe)
    return name  # fall back to PATH


def _stream_codec(path, stream):
    try:
        out = subprocess.run(
            [_tool("ffprobe"), "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=codec_name", "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        lines = (out.stdout or "").strip().splitlines()
        return lines[0].strip() if lines else ""
    except Exception:  # noqa: BLE001
        return ""


def _force_h264_dir(job_id, job_dir):
    if not (FFMPEG_DIR or shutil.which("ffmpeg")):
        return  # can't transcode without ffmpeg
    vids = [
        p for p in job_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".m4v")
    ]
    for path in vids:
        vcodec = _stream_codec(path, "v:0")
        acodec = _stream_codec(path, "a:0")
        if vcodec == "h264" and acodec in ("aac", "mp4a", ""):
            continue  # already plays everywhere — no re-encode
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["phase"] = "Re-encoding to H.264…"
                job["status"] = "processing"
        out = path.with_name(path.stem + ".__h264__.mp4")
        try:
            r = subprocess.run(
                [_tool("ffmpeg"), "-y", "-i", str(path),
                 "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
                capture_output=True, text=True, timeout=7200,
            )
        except Exception:  # noqa: BLE001
            out.unlink(missing_ok=True)
            continue
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            final = path.with_suffix(".mp4")
            path.unlink(missing_ok=True)
            out.rename(final)
        else:
            out.unlink(missing_ok=True)


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Unknown job."), 404
        return jsonify({k: v for k, v in job.items() if k != "result_path"})


@app.get("/api/file/<job_id>")
def api_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("result_path"):
        abort(404)
    path = Path(job["result_path"])
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=job["download_name"])


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _clean_err(exc):
    msg = str(exc)
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)        # strip ANSI colour codes
    msg = re.sub(r"^ERROR:\s*", "", msg).strip()
    return msg[:300]


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name)[:120]


# --------------------------------------------------------------------------- #
#  Direct (non-yt-dlp) web downloads — any file behind a plain URL
# --------------------------------------------------------------------------- #
def _filename_from(url, disposition, ctype):
    """Work out a sensible filename for a direct download."""
    if disposition:
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, re.I)
        if m:
            return _safe(urllib.parse.unquote(m.group(1)))
    path = urllib.parse.urlparse(url).path
    base = posixpath.basename(path)
    if base and "." in base:
        return _safe(urllib.parse.unquote(base))
    ext = mimetypes.guess_extension((ctype or "").split(";")[0].strip()) or ""
    return _safe((base or "download") + ext)


def _probe_direct(url):
    """HEAD/Range probe to see if a URL points straight at a downloadable file."""
    def _read_headers(req):
        with urllib.request.urlopen(req, timeout=15) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            clen = r.headers.get("Content-Length")
            disp = r.headers.get("Content-Disposition") or ""
            return ctype, clen, disp

    try:
        ctype, clen, disp = _read_headers(
            urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        )
    except Exception:  # noqa: BLE001 — some servers reject HEAD; try a tiny GET
        try:
            ctype, clen, disp = _read_headers(
                urllib.request.Request(
                    url, headers={"User-Agent": UA, "Range": "bytes=0-0"}
                )
            )
            clen = None  # a 206 reports only the 1-byte range length
        except Exception:  # noqa: BLE001
            return None

    # Reject HTML pages — those weren't real media and aren't a useful file.
    if not ctype or ctype.startswith("text/html"):
        return None

    return {
        "filename": _filename_from(url, disp, ctype),
        "content_type": ctype,
        "size_bytes": int(clen) if clen and clen.isdigit() else None,
        "size": human_size(int(clen)) if clen and clen.isdigit() else None,
    }


def _run_direct(job_id, url, filename):
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    with JOBS_LOCK:
        JOBS[job_id].update(status="downloading", phase="Downloading…")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            if not filename:
                filename = _filename_from(
                    url,
                    resp.headers.get("Content-Disposition") or "",
                    resp.headers.get("Content-Type") or "",
                )
            total = resp.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else 0
            dest = job_dir / _safe(filename or "download")

            done = 0
            started = time.monotonic()
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(262144)  # 256 KB
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    elapsed = time.monotonic() - started
                    speed = done / elapsed if elapsed > 0 else 0
                    with JOBS_LOCK:
                        job = JOBS.get(job_id)
                        if not job:
                            return
                        job["progress"] = round(done / total * 100, 1) if total else 0
                        job["speed"] = f"{speed / 1_048_576:.1f} MB/s" if speed else None
                        if total and speed:
                            job["eta"] = f"{int((total - done) / speed)}s"

        _succeed(job_id, dest, dest.name)
    except Exception as exc:  # noqa: BLE001
        _fail(job_id, f"Direct download failed: {exc}")


# --------------------------------------------------------------------------- #
#  Self-contained ffmpeg — kept inside ./bin so nothing touches the system
# --------------------------------------------------------------------------- #
_FFMPEG_BASE = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"


def _download_to(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)


def _extract_binaries(archive, kind, names):
    """Pull just the named binaries out of a downloaded archive into BIN_DIR."""
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                if posixpath.basename(member) in names and not member.endswith("/"):
                    with z.open(member) as src, open(BIN_DIR / posixpath.basename(member), "wb") as out:
                        shutil.copyfileobj(src, out)
    else:  # tar (.xz / .gz)
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                if member.isfile() and os.path.basename(member.name) in names:
                    src = t.extractfile(member)
                    if src:
                        with open(BIN_DIR / os.path.basename(member.name), "wb") as out:
                            shutil.copyfileobj(src, out)


def _fetch_ffmpeg():
    """Download a static ffmpeg+ffprobe for this platform into ./bin."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        # No yt-dlp/BtbN macOS build; use evermeet's static binaries.
        for name in ("ffmpeg", "ffprobe"):
            tmp = BIN_DIR / f"{name}.zip"
            _download_to(f"https://evermeet.cx/ffmpeg/getrelease/{name}/zip", tmp)
            _extract_binaries(tmp, "zip", {name})
            tmp.unlink(missing_ok=True)
        return

    if system == "Windows":
        url, kind, names = _FFMPEG_BASE + "ffmpeg-master-latest-win64-gpl.zip", "zip", {"ffmpeg.exe", "ffprobe.exe"}
    elif system == "Linux" and ("aarch64" in machine or "arm64" in machine):
        url, kind, names = _FFMPEG_BASE + "ffmpeg-master-latest-linuxarm64-gpl.tar.xz", "tar", {"ffmpeg", "ffprobe"}
    elif system == "Linux":
        url, kind, names = _FFMPEG_BASE + "ffmpeg-master-latest-linux64-gpl.tar.xz", "tar", {"ffmpeg", "ffprobe"}
    else:
        raise RuntimeError(f"no bundled ffmpeg build for {system}/{machine}")

    ext = ".zip" if kind == "zip" else ".tar.xz"
    tmp = BIN_DIR / f"ffmpeg-download{ext}"
    _download_to(url, tmp)
    _extract_binaries(tmp, kind, names)
    tmp.unlink(missing_ok=True)


def ensure_ffmpeg():
    """Prefer a copy of ffmpeg in ./bin; download one there if none is available."""
    global FFMPEG_DIR
    suffix = ".exe" if os.name == "nt" else ""
    local = BIN_DIR / f"ffmpeg{suffix}"

    if local.exists():
        FFMPEG_DIR = str(BIN_DIR)
    elif shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return  # a system ffmpeg is already available; leave it
    else:
        BIN_DIR.mkdir(exist_ok=True)
        print("  Setting up ffmpeg inside ./bin (one-time download, ~40 MB)...")
        try:
            _fetch_ffmpeg()
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠  Could not bundle ffmpeg ({exc}).")
            print("     Grabbit still runs; some quality/audio/chapter features need ffmpeg.\n")
            return
        if not local.exists():
            print("  ⚠  ffmpeg download did not produce the expected files.\n")
            return
        for f in BIN_DIR.iterdir():
            if f.is_file():
                try:
                    f.chmod(0o755)
                except OSError:
                    pass
        FFMPEG_DIR = str(BIN_DIR)
        print("  ffmpeg is ready in ./bin.\n")

    # Make the bundled tools discoverable to any subprocess too.
    if FFMPEG_DIR:
        os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grabbit · video & chapter downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700;12..96,800&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0e0f0d; --panel:#16181400; --card:#1a1c18; --line:#2b2e27;
    --ink:#ece9df; --muted:#8d9183; --accent:#e9c46a; --accent2:#f4a261;
    --good:#a7c957; --bad:#e76f51; --radius:14px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:
      radial-gradient(900px 500px at 85% -10%, rgba(233,196,106,.10), transparent 60%),
      radial-gradient(700px 500px at -10% 110%, rgba(244,162,97,.08), transparent 55%),
      var(--bg);
    color:var(--ink);
    font-family:"IBM Plex Sans",system-ui,sans-serif;
    line-height:1.5;
    background-attachment:fixed;
  }
  .wrap{max-width:760px;margin:0 auto;padding:48px 22px 80px}
  header{display:flex;align-items:baseline;gap:14px;margin-bottom:6px}
  .logo{
    font-family:"Bricolage Grotesque",sans-serif;font-weight:800;
    font-size:38px;letter-spacing:-.02em;
    background:linear-gradient(95deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;background-clip:text;color:transparent;
  }
  .logo .paw{ -webkit-text-fill-color:initial }
  .tag{font-family:"IBM Plex Mono",monospace;color:var(--muted);font-size:12.5px;letter-spacing:.04em}
  .sub{color:var(--muted);font-size:14px;margin:0 0 28px}
  .sub b{color:var(--ink);font-weight:600}

  .bar{display:flex;gap:10px}
  input[type=url]{
    flex:1;min-width:0;background:var(--card);border:1px solid var(--line);
    color:var(--ink);border-radius:12px;padding:15px 16px;font-size:15px;
    font-family:"IBM Plex Mono",monospace;outline:none;transition:border-color .15s,box-shadow .15s;
  }
  input[type=url]:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(233,196,106,.14)}
  button{
    font-family:"IBM Plex Sans",sans-serif;font-weight:600;font-size:15px;cursor:pointer;
    border:none;border-radius:12px;transition:transform .08s ease,filter .15s,background .15s;
  }
  button:active{transform:translateY(1px)}
  .go{
    padding:0 26px;color:#1b1c18;background:linear-gradient(95deg,var(--accent),var(--accent2));
    white-space:nowrap;
  }
  .go:hover{filter:brightness(1.06)}
  button:disabled{opacity:.5;cursor:default}

  .msg{margin-top:16px;font-family:"IBM Plex Mono",monospace;font-size:13px}
  .msg.err{color:var(--bad)}

  .card{
    margin-top:22px;background:var(--card);border:1px solid var(--line);
    border-radius:var(--radius);overflow:hidden;
    animation:rise .35s ease both;
  }
  @keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .meta{display:flex;gap:16px;padding:16px}
  .thumb{width:168px;height:96px;flex:none;border-radius:9px;object-fit:cover;background:#0c0d0b;border:1px solid var(--line)}
  .filebox{width:96px;height:96px;flex:none;border-radius:9px;background:#13150f;border:1px solid var(--line);
    display:flex;align-items:center;justify-content:center;font-size:34px;color:var(--accent);
    font-family:"IBM Plex Mono",monospace}
  .meta h2{font-family:"Bricolage Grotesque",sans-serif;font-size:18px;margin:2px 0 8px;line-height:1.25}
  .pills{display:flex;flex-wrap:wrap;gap:7px}
  .pill{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);
    border:1px solid var(--line);border-radius:999px;padding:3px 9px;background:#13150f}
  .pill.src{color:var(--accent);border-color:rgba(233,196,106,.4)}

  .opts{border-top:1px solid var(--line);padding:16px;display:grid;gap:14px}
  .label{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.08em;
    text-transform:uppercase;color:var(--muted);margin-bottom:8px}
  .modes{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
  .mode{position:relative}
  .mode input{position:absolute;opacity:0;inset:0;cursor:pointer}
  .mode span{
    display:block;text-align:center;padding:12px 8px;border:1px solid var(--line);
    border-radius:11px;font-size:13.5px;color:var(--muted);transition:.15s;background:#13150f
  }
  .mode small{display:block;font-family:"IBM Plex Mono",monospace;font-size:10.5px;opacity:.8;margin-top:3px}
  .mode input:checked + span{border-color:var(--accent);color:var(--ink);background:rgba(233,196,106,.08)}
  .mode input:disabled + span{opacity:.4;cursor:not-allowed}

  .quality{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap}
  .qfield{display:flex;flex-direction:column;gap:6px}
  .qfield .label{margin:0}
  select{
    background:#13150f;border:1px solid var(--line);color:var(--ink);
    border-radius:10px;padding:9px 12px;font-family:"IBM Plex Mono",monospace;font-size:13px;outline:none
  }
  select:focus{border-color:var(--accent)}
  .quality.hide{display:none}

  details.ch{border-top:1px solid var(--line)}
  details.ch summary{padding:13px 16px;cursor:pointer;font-size:13px;color:var(--muted);
    font-family:"IBM Plex Mono",monospace;list-style:none}
  details.ch summary::-webkit-details-marker{display:none}
  details.ch summary::before{content:"▸ ";color:var(--accent)}
  details.ch[open] summary::before{content:"▾ "}
  .chlist{padding:0 16px 14px;display:grid;gap:5px;max-height:230px;overflow:auto}
  .chrow{display:flex;justify-content:space-between;font-size:12.5px;padding:6px 10px;
    background:#13150f;border-radius:8px;border:1px solid var(--line)}
  .chrow .t{color:var(--muted);font-family:"IBM Plex Mono",monospace}

  .picker{display:grid;gap:9px}
  .picker.hide{display:none}
  .ptools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .ptools input[type=search]{
    flex:1;min-width:150px;background:#13150f;border:1px solid var(--line);color:var(--ink);
    border-radius:10px;padding:8px 12px;font-family:"IBM Plex Mono",monospace;font-size:12.5px;outline:none
  }
  .ptools input[type=search]:focus{border-color:var(--accent)}
  .ghost{padding:8px 12px;font-size:12.5px;color:var(--ink);background:#13150f;border:1px solid var(--line)}
  .ghost:hover{border-color:var(--accent)}
  .pcount{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted);margin-left:auto}
  .vlist{display:grid;gap:5px;max-height:380px;overflow:auto;padding-right:2px}
  .vrow{display:flex;align-items:center;gap:10px;padding:6px 10px;background:#13150f;
    border:1px solid var(--line);border-radius:9px;cursor:pointer;font-size:13px;user-select:none}
  .vrow:hover{border-color:#3d4136}
  .vrow.on{border-color:rgba(233,196,106,.55);background:rgba(233,196,106,.06)}
  .vrow input{accent-color:var(--accent);width:16px;height:16px;flex:none;margin:0;cursor:pointer}
  .vrow img,.vrow .noimg{width:64px;height:36px;flex:none;border-radius:5px;object-fit:cover;background:#0c0d0b}
  .vrow .vt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .vrow .vn,.vrow .vd{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);flex:none}
  .vrow .vn{width:34px}
  .vempty{padding:12px;text-align:center;color:var(--muted);font-size:12.5px}
  .warn{color:var(--accent2)}

  .actions{border-top:1px solid var(--line);padding:16px;display:flex;gap:12px;align-items:center}
  .dl{padding:13px 24px;color:#1b1c18;background:linear-gradient(95deg,var(--accent),var(--accent2))}
  .dl:hover{filter:brightness(1.06)}

  .prog{flex:1;min-width:0}
  .prog.hide{display:none}
  .track{height:9px;background:#13150f;border:1px solid var(--line);border-radius:999px;overflow:hidden}
  .fill{height:100%;width:0;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .3s}
  .fill.done{background:var(--good)}
  .fill.err{background:var(--bad)}
  .pstat{display:flex;justify-content:space-between;margin-top:7px;
    font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted)}

  footer{margin-top:40px;color:var(--muted);font-size:12px;line-height:1.7;
    border-top:1px solid var(--line);padding-top:18px}
  footer b{color:var(--ink)}
  a{color:var(--accent)}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
    border-top-color:var(--accent);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
  @keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><span class="paw">🐰</span> Grabbit</div>
    <div class="tag">// yt-dlp · 1,800+ sites</div>
  </header>
  <p class="sub">Paste a link from <b>Internet Archive</b>, <b>YouTube</b>, <b>Vimeo</b>, <b>Wikimedia</b> — a single video, a whole <b>playlist</b> or <b>channel</b> (pick just the videos you want), any page with video, or a <b>direct file link</b>. Grab full videos, split into <b>chapters</b>, or pull <b>audio</b> only.</p>

  <div class="bar">
    <input id="url" type="url" placeholder="https://archive.org/details/…   ·   any video page   ·   https://site.com/clip.mp4" autocomplete="off" spellcheck="false">
    <button class="go" id="fetch">Fetch</button>
  </div>
  <div class="msg" id="msg"></div>

  <div id="result"></div>

  <footer>
    <b>Runs locally on your machine.</b> Nothing is uploaded; files download straight to you.<br>
    Please download only content you own, that is public domain / openly licensed (most Internet Archive
    items, Creative Commons, Wikimedia Commons), or that you have permission to take. Respect each site's
    Terms of Service and copyright law.
  </footer>
</div>

<script>
const $ = s => document.querySelector(s);
const urlEl = $("#url"), fetchBtn = $("#fetch"), msg = $("#msg"), result = $("#result");
let poll = null;

function setMsg(t, err){ msg.className = "msg" + (err ? " err" : ""); msg.innerHTML = t; }

fetchBtn.onclick = getInfo;
urlEl.addEventListener("keydown", e => { if (e.key === "Enter") getInfo(); });

async function getInfo(){
  const url = urlEl.value.trim();
  if (!url){ setMsg("Paste a video URL first.", true); return; }
  if (poll){ clearInterval(poll); poll = null; }
  result.innerHTML = "";
  fetchBtn.disabled = true;
  setMsg('<span class="spin"></span> Reading link…');
  try{
    const r = await fetch("/api/info", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({url})});
    const d = await r.json();
    if (!r.ok){ setMsg(d.error || "Could not read that link.", true); return; }
    setMsg("");
    if (d.kind === "direct") renderDirect(url, d);
    else if (d.is_playlist) renderPlaylist(url, d);
    else render(url, d);
  }catch(e){ setMsg("Network error: " + e.message, true); }
  finally{ fetchBtn.disabled = false; }
}

function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function render(url, d){
  const chRows = d.chapters.map(c =>
    `<div class="chrow"><span>${esc(c.title)}</span><span class="t">${c.start||""}</span></div>`).join("");
  const chBlock = d.has_chapters ? `
    <details class="ch"><summary>${d.chapters.length} chapters detected</summary>
      <div class="chlist">${chRows}</div></details>` : "";

  result.innerHTML = `
  <div class="card">
    <div class="meta">
      ${d.thumbnail ? `<img class="thumb" src="${esc(d.thumbnail)}" alt="" referrerpolicy="no-referrer">` : ""}
      <div>
        <h2>${esc(d.title)}</h2>
        <div class="pills">
          ${d.source ? `<span class="pill src">${esc(d.source)}</span>`:""}
          ${d.uploader ? `<span class="pill">${esc(d.uploader)}</span>`:""}
          ${d.duration ? `<span class="pill">⏱ ${d.duration}</span>`:""}
          ${d.has_chapters ? `<span class="pill">${d.chapters.length} chapters</span>`:""}
        </div>
      </div>
    </div>
    <div class="opts">
      <div>
        <div class="label">What to grab</div>
        <div class="modes">
          <label class="mode"><input type="radio" name="mode" value="video" checked>
            <span>Full video<small>mp4</small></span></label>
          <label class="mode"><input type="radio" name="mode" value="chapters" ${d.has_chapters?"":"disabled"}>
            <span>Split chapters<small>${d.has_chapters?"zip":"none found"}</small></span></label>
          <label class="mode"><input type="radio" name="mode" value="audio">
            <span>Audio only<small>mp3</small></span></label>
        </div>
      </div>
      <div class="quality" id="qrow">
        <div class="qfield">
          <div class="label">Quality</div>
          <select id="quality">
            <option value="best">Best available</option>
            <option value="1080">1080p</option>
            <option value="720">720p</option>
            <option value="480">480p</option>
            <option value="360">360p</option>
          </select>
        </div>
        <div class="qfield">
          <div class="label">Format</div>
          <select id="codec">
            <option value="compatible">Most compatible (H.264)</option>
            <option value="quality">Highest quality</option>
            <option value="force_h264">Force H.264 (re-encode)</option>
          </select>
        </div>
      </div>
    </div>
    ${chBlock}
    <div class="actions">
      <button class="dl" id="dl">Download</button>
      <div class="prog hide" id="prog">
        <div class="track"><div class="fill" id="fill"></div></div>
        <div class="pstat"><span id="phase"></span><span id="rate"></span></div>
      </div>
    </div>
  </div>`;

  const qrow = $("#qrow");
  document.querySelectorAll('input[name=mode]').forEach(r =>
    r.addEventListener("change", () => {
      qrow.classList.toggle("hide", document.querySelector('input[name=mode]:checked').value === "audio");
    }));
  $("#dl").onclick = () => startDownload(url, {
    mode: document.querySelector('input[name=mode]:checked').value,
    quality: $("#quality").value,
    codec: $("#codec").value,
  });
}

function renderPlaylist(url, d){
  const n = d.playlist.count;
  const entries = d.entries || [];
  // Small lists start fully ticked; big channels start empty so nothing huge
  // downloads by accident.
  const startAll = entries.length <= 50;
  const picked = new Set(startAll ? entries.map((_, i) => i) : []);

  const scopeBlock = d.single_available ? `
      <div>
        <div class="label">How much</div>
        <div class="modes">
          <label class="mode"><input type="radio" name="scope" value="pick" checked>
            <span>Choose videos<small>${n} in list</small></span></label>
          <label class="mode"><input type="radio" name="scope" value="single">
            <span>This video only<small>1 video</small></span></label>
        </div>
      </div>` : "";

  const rows = entries.map((e, i) => `
      <label class="vrow" data-i="${i}">
        <input type="checkbox" data-i="${i}">
        <span class="vn">${e.index}</span>
        ${e.thumbnail ? `<img src="${esc(e.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="noimg"></span>`}
        <span class="vt" title="${esc(e.title)}">${esc(e.title)}</span>
        <span class="vd">${e.duration || ""}</span>
      </label>`).join("");

  result.innerHTML = `
  <div class="card">
    <div class="meta">
      ${d.thumbnail
        ? `<img class="thumb" src="${esc(d.thumbnail)}" alt="" referrerpolicy="no-referrer">`
        : `<div class="filebox">&#8801;</div>`}
      <div>
        <h2>${esc(d.title)}</h2>
        <div class="pills">
          ${d.source ? `<span class="pill src">${esc(d.source)}</span>`:""}
          <span class="pill">&#9654; ${n} videos</span>
          ${d.uploader ? `<span class="pill">${esc(d.uploader)}</span>`:""}
        </div>
      </div>
    </div>
    <div class="opts">
      ${scopeBlock}
      <div class="picker" id="picker">
        <div class="label" style="margin:0">Pick the videos you want</div>
        <div class="ptools">
          <input type="search" id="vfilter" placeholder="Search titles…">
          <button class="ghost" id="vall">Select all</button>
          <button class="ghost" id="vnone">Clear</button>
          <span class="pcount" id="pcount"></span>
        </div>
        <div class="vlist" id="vlist">${rows || `<div class="vempty">No videos listed.</div>`}</div>
      </div>
      <div>
        <div class="label">What to grab</div>
        <div class="modes">
          <label class="mode"><input type="radio" name="mode" value="video" checked>
            <span>Full video<small>mp4</small></span></label>
          <label class="mode"><input type="radio" name="mode" value="audio">
            <span>Audio only<small>mp3</small></span></label>
        </div>
      </div>
      <div class="quality" id="qrow">
        <div class="qfield">
          <div class="label">Quality</div>
          <select id="quality">
            <option value="best">Best available</option>
            <option value="1080">1080p</option>
            <option value="720">720p</option>
            <option value="480">480p</option>
            <option value="360">360p</option>
          </select>
        </div>
        <div class="qfield">
          <div class="label">Format</div>
          <select id="codec">
            <option value="compatible">Most compatible (H.264)</option>
            <option value="quality">Highest quality</option>
            <option value="force_h264">Force H.264 (re-encode)</option>
          </select>
        </div>
      </div>
    </div>
    <div class="actions">
      <button class="dl" id="dl">Download</button>
      <div class="prog hide" id="prog">
        <div class="track"><div class="fill" id="fill"></div></div>
        <div class="pstat"><span id="phase"></span><span id="rate"></span></div>
      </div>
    </div>
  </div>`;

  const list = $("#vlist"), dl = $("#dl"), picker = $("#picker"), filter = $("#vfilter");
  const boxes = [...list.querySelectorAll("input[type=checkbox]")];
  const rowsEl = [...list.querySelectorAll(".vrow")];
  let last = null;

  const scopeNow = () => {
    const c = document.querySelector('input[name=scope]:checked');
    return c ? c.value : "pick";
  };
  const visible = () => rowsEl.filter(r => r.style.display !== "none").map(r => +r.dataset.i);

  function refresh(){
    boxes.forEach((b, i) => { b.checked = picked.has(i); rowsEl[i].classList.toggle("on", b.checked); });
    const k = picked.size;
    $("#pcount").textContent = `${k} of ${entries.length} selected`;
    const single = scopeNow() === "single";
    picker.classList.toggle("hide", single);
    if (!dl.dataset.busy){
      dl.disabled = !single && k === 0;
      dl.textContent = single ? "Download" : k === 0 ? "Pick videos" : `Download ${k} video${k === 1 ? "" : "s"}`;
    }
  }

  // Shift-click ticks (or unticks) every visible row between the two clicks.
  list.addEventListener("click", ev => {
    const row = ev.target.closest(".vrow");
    if (!row) return;
    ev.preventDefault();
    const i = +row.dataset.i;
    const turnOn = !picked.has(i);
    if (ev.shiftKey && last !== null){
      const vis = visible();
      const a = vis.indexOf(last), b = vis.indexOf(i);
      if (a !== -1 && b !== -1){
        vis.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(j => turnOn ? picked.add(j) : picked.delete(j));
      }
    } else {
      turnOn ? picked.add(i) : picked.delete(i);
    }
    last = i;
    refresh();
  });

  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    rowsEl.forEach((r, i) => { r.style.display = !q || entries[i].title.toLowerCase().includes(q) ? "" : "none"; });
  });
  // "Select all" / "Clear" act on what the search is currently showing.
  $("#vall").onclick = () => { visible().forEach(i => picked.add(i)); refresh(); };
  $("#vnone").onclick = () => { visible().forEach(i => picked.delete(i)); refresh(); };
  document.querySelectorAll('input[name=scope]').forEach(r => r.addEventListener("change", refresh));

  const qrow = $("#qrow");
  document.querySelectorAll('input[name=mode]').forEach(r =>
    r.addEventListener("change", () => {
      qrow.classList.toggle("hide", document.querySelector('input[name=mode]:checked').value === "audio");
    }));

  dl.onclick = () => {
    const base = {
      mode: document.querySelector('input[name=mode]:checked').value,
      quality: $("#quality").value,
      codec: $("#codec").value,
    };
    if (scopeNow() === "single") return startDownload(url, {...base, scope: "single"});
    const chosen = [...picked].sort((a, b) => a - b).map(i => entries[i]);
    if (!chosen.length) return;
    // Everything ticked: grab the list in one go, as before.
    if (chosen.length === entries.length && entries.length === n)
      return startDownload(url, {...base, scope: "playlist", playlist_count: n});
    startDownload(url, {
      ...base, scope: "selected", title: d.title,
      items: chosen.map(e => ({url: e.url, index: e.index, title: e.title})),
    });
  };

  refresh();
}

function renderDirect(url, d){
  result.innerHTML = `
  <div class="card">
    <div class="meta">
      <div class="filebox">↓</div>
      <div>
        <h2>${esc(d.filename)}</h2>
        <div class="pills">
          <span class="pill src">Direct file</span>
          ${d.content_type ? `<span class="pill">${esc(d.content_type)}</span>`:""}
          ${d.size ? `<span class="pill">${esc(d.size)}</span>`:""}
        </div>
      </div>
    </div>
    <div class="actions">
      <button class="dl" id="dl">Download file</button>
      <div class="prog hide" id="prog">
        <div class="track"><div class="fill" id="fill"></div></div>
        <div class="pstat"><span id="phase"></span><span id="rate"></span></div>
      </div>
    </div>
  </div>`;
  $("#dl").onclick = () => startDownload(url, {mode:"direct", filename:d.filename});
}

async function startDownload(url, extra){
  const dl = $("#dl"), prog = $("#prog"), fill = $("#fill"), phase = $("#phase"), rate = $("#rate");
  dl.disabled = true; dl.textContent = "Working…"; dl.dataset.busy = "1";
  prog.classList.remove("hide"); fill.className = "fill"; fill.style.width = "0%";

  let job;
  try{
    const r = await fetch("/api/download", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({url, ...extra})});
    const d = await r.json();
    if (!r.ok){ throw new Error(d.error || "Download failed to start."); }
    job = d.job_id;
  }catch(e){ fail(e.message); return; }

  poll = setInterval(async () => {
    try{
      const r = await fetch("/api/progress/" + job);
      const p = await r.json();
      fill.style.width = (p.progress||0) + "%";
      phase.textContent = p.phase || "";
      rate.textContent = [p.speed, p.eta ? "ETA "+p.eta : ""].filter(Boolean).join("  ·  ");
      if (p.status === "done"){
        clearInterval(poll); poll = null;
        fill.classList.add("done"); fill.style.width = "100%";
        phase.textContent = "Done"; rate.textContent = "";
        if (p.warning) setMsg('<span class="warn">⚠ ' + esc(p.warning) + '</span>');
        window.location = "/api/file/" + job;
        delete dl.dataset.busy;
        dl.disabled = false; dl.textContent = "Download again";
      } else if (p.status === "error"){
        clearInterval(poll); poll = null; fail(p.error || "Download failed.");
      }
    }catch(e){ clearInterval(poll); poll = null; fail(e.message); }
  }, 700);

  function fail(m){
    fill.classList.add("err"); fill.style.width = "100%";
    phase.textContent = "Failed"; rate.textContent = "";
    delete dl.dataset.busy;
    dl.disabled = false; dl.textContent = "Try again";
    setMsg("✗ " + m, true);
  }
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import sys
    import webbrowser

    URL = "http://127.0.0.1:8080"
    print(f"\n  Grabbit is running →  {URL}\n")

    if sys.version_info < (3, 10):
        print("  ⚠  yt-dlp requires Python 3.10 or newer; you are on "
              f"{sys.version_info.major}.{sys.version_info.minor}.\n")

    ensure_ffmpeg()

    if not FFMPEG_DIR and not shutil.which("ffmpeg"):
        print("  ⚠  ffmpeg is unavailable. Direct downloads still work, but")
        print("     quality selection, audio extraction and chapter splitting need it.\n")

    if not any(shutil.which(rt) for rt in ("deno", "node", "bun", "qjs")):
        print("  ⚠  No JavaScript runtime found (deno / node / bun). Recent")
        print("     yt-dlp needs one for full YouTube support; downloads from")
        print("     YouTube may fail without it. Other sites are unaffected.\n")

    # Pop open the browser shortly after the server starts listening.
    if os.environ.get("GRABBIT_NO_BROWSER") != "1":
        def _open():
            try:
                webbrowser.open(URL)
            except Exception:  # noqa: BLE001 — headless / no browser is fine
                pass
        threading.Timer(1.5, _open).start()

    app.run(host="127.0.0.1", port=8080, threaded=True, debug=False)

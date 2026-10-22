# Grabbit — local video & chapter downloader

A small web app that runs on your own machine. Paste a link from
**Internet Archive, YouTube, Vimeo, Wikimedia Commons** and ~1,800 other sites
[supported by yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md),
then grab the full video, split it into its **chapters**, or pull **audio only**.

It also handles the wider web:

- **Whole playlists & channels** — paste a playlist link and download every
  video (or every audio track) in one go, bundled into a single `.zip`. If the
  link is a video that happens to be in a playlist, you can choose *this video*
  or *the whole playlist*.
- **Pick videos from a channel** — paste a channel link (e.g.
  `https://www.youtube.com/@name`) or a playlist link and Grabbit lists every
  video in it. Tick the ones you want (search by title, *Select all* / *Clear*,
  or shift-click to tick a range), then download them all at once as one `.zip`.
- **Never the same video twice** — Grabbit remembers every video it has
  downloaded. Next time you open that channel, those videos are marked
  **✓ Downloaded** and left unticked, and they're skipped if you try again.
  You always get the choice: tick *Also allow videos I've already downloaded*
  (or *Download it again anyway* for a single video), or press the
  **Download again** button Grabbit shows when it skips something.
- **Any page with an embedded video** — even sites without a dedicated
  extractor are scanned by yt-dlp's generic detector.
- **Direct media links** — paste a raw `https://site.com/clip.mp4` (or `.webm`,
  `.mkv`, `.m3u8` …) and it downloads straight away.
- **Any other direct file** — if a link points at a plain file rather than a
  media page, Grabbit streams it down for you with a progress bar.

Under the hood it's a thin, friendly front-end over
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and `ffmpeg` — the same tools the
command line uses, with a nicer interface and one-click chapter splitting.

## Quick start (no terminal needed)

Just open the launcher for your system:

| System  | Double-click        |
|---------|---------------------|
| Windows | **`start-windows.bat`** |
| macOS   | **`start-mac.command`** |
| Linux   | right-click **`start-linux.sh`** → **Run as a Program** (or run `./start-linux.sh` in a terminal) |

On the first run the launcher checks for everything it needs and sets it up:

- **Python** — if it's missing, the launcher tries to install it for you
  (via `winget` on Windows, Homebrew on macOS, your package manager on Linux).
  If it can't, it opens python.org so you can grab it. On Windows, after Python
  installs you'll be asked to run the launcher once more so it picks up the new
  install.
- **Flask** and **yt-dlp** — installed automatically into a private environment
  (`.venv`) inside the `grabbit` folder.
- **ffmpeg** — downloaded automatically into `grabbit/bin/` on first run and
  used from there, so it never touches your system. (On macOS, if that download
  isn't available, install ffmpeg with `brew install ffmpeg` as a fallback.)

Then it starts Grabbit and opens it in your browser. Later runs skip the setup
and start in seconds. Leave the launcher window open while you use it; close it
(or press Ctrl+C) to stop.

### Where everything lives

Grabbit keeps itself self-contained in its own folder:

| Inside `grabbit/` | What it is |
|-------------------|------------|
| `.venv/`     | Flask, yt-dlp and their Python dependencies |
| `bin/`       | the bundled `ffmpeg` / `ffprobe` |
| `downloads/` | finished files (also delivered to your browser) |
| `download-history.txt` | every video you've downloaded, so it isn't fetched twice — delete it to start fresh |

**One folder, several systems.** The `grabbit` folder can live on a drive
shared by Windows, Linux and macOS. Each system keeps its own environment and
tools there and leaves the others' alone: if `.venv` was made on Windows, Linux
uses `.venv-linux` (macOS `.venv-darwin`, Windows `.venv-windows` for one made
elsewhere), and if `bin/ffmpeg` belongs to another system this one fetches its
own into `bin/linux` (or `bin/darwin`). Grabbit tells a binary's system by its
first bytes, so another system's file is never run — that would only fail with
`Exec format error`.

Deleting these folders resets it; deleting the whole `grabbit` folder removes
it entirely. The one thing kept outside the folder is the base **Python**
interpreter itself — it's what runs the launcher in the first place, so it lives
where you (or the launcher) installed it. Everything Grabbit pulls in afterward
stays in the folder.

> **Linux note:** double-clicking `start-linux.sh` in the file manager opens it
> in a text editor — use right-click → **Run as a Program**; it opens a
> terminal window to run in. If a terminal says `Permission denied` (unzipping
> can drop the executable bit), run `bash start-linux.sh`, or
> `chmod +x start-linux.sh` once. On Debian and Ubuntu the first run also needs
> `sudo apt-get install -y python3-venv`; the launcher says so when it's
> missing, and picks up where it stopped once it's installed.

> **macOS note:** the first time you open `start-mac.command`, macOS may block
> it. Right-click it → **Open** → **Open**, and it'll remember your choice. If
> double-clicking does nothing, open Terminal and run `chmod +x start-mac.command`
> once.

The manual steps below are only if you'd rather drive it yourself.

## Requirements

- **Python 3.10+** (required by current yt-dlp)
- **ffmpeg + ffprobe** on your PATH — the actual binaries, **not** the PyPI
  package called `ffmpeg`. Needed for quality selection, merging, audio
  extraction and chapter splitting.
  - macOS: `brew install ffmpeg`
  - Windows: `winget install Gyan.FFmpeg`  (or download from ffmpeg.org)
  - Debian/Ubuntu: `sudo apt install ffmpeg`
- **A JavaScript runtime — for YouTube.** Recent yt-dlp needs one (plus the
  bundled `yt-dlp-ejs`) for full YouTube support; without it, YouTube
  downloads may fail. Other sites are unaffected. Install any one of:
  - [Deno](https://deno.land) (recommended), [Node.js](https://nodejs.org),
    [Bun](https://bun.sh), or [QuickJS](https://bellard.org/quickjs/)

## Setup

```bash
pip install flask "yt-dlp[default]"
python app.py
```

The `[default]` extra pulls yt-dlp's recommended networking dependencies
(certifi, brotli, websockets, requests). Then open
**http://127.0.0.1:8080** in your browser.

> Grabbit prints a warning at startup if ffmpeg or a JS runtime is missing,
> so you'll know before a download fails.

## Using it

1. Paste a video URL and press **Fetch**. You'll see the title, source,
   duration, thumbnail, and any chapters the video has.
2. Choose what to grab:
   - **Full video** — a single `.mp4` (chapter markers embedded if present)
   - **Split chapters** — one file per chapter, delivered as a `.zip`
   - **Audio only** — an `.mp3`
   For a **channel or playlist**, a list of its videos appears — tick the ones
   you want. Short lists (50 or fewer) start with everything ticked; bigger
   channels start with nothing ticked so you don't grab thousands by accident.
   If a video fails, the rest still download and you'll see a note saying how
   many were skipped.
3. Pick a **quality** and a **format**, then press **Download**. The file saves
   straight to your browser's downloads folder.

The **Format** choice controls codecs:

- **Most compatible (H.264)** — default. Picks H.264 video + AAC audio so the
  file plays in Windows Media Player, QuickTime, phones, etc. Tops out at 1080p
  (the highest resolution sites like YouTube offer in H.264).
- **Highest quality** — the best stream at your chosen resolution regardless of
  codec (may be AV1/VP9, up to 4K). Best picture, but some built-in players
  can't open it — use [VLC](https://www.videolan.org/) if so.
- **Force H.264 (re-encode)** — like "most compatible", but if a video only
  exists in AV1/VP9, it's re-encoded to H.264 so it plays anywhere. This is
  slower (it transcodes), and only kicks in when a file isn't already H.264.

Finished files are also kept under `downloads/<job-id>/` next to `app.py`,
so you can clear that folder anytime.

## Tests

```bash
python -m unittest discover -s tests
```

## Notes

- The server binds to `127.0.0.1` only — it is **not** exposed to your network.
  Don't put it behind a public address; it's a single-user local tool.
- Change the port at the bottom of `app.py` if `8080` is taken.
- If a site changes its layout and a download breaks, update the engine first.
  The monthly "stable" build often lags behind site changes, so updating to
  the latest (nightly) release usually fixes it:
  `pip install -U --pre "yt-dlp[default]"`.

## Responsible use

This tool can download from any site yt-dlp supports, but **you** decide what's
appropriate to take. Use it for content you own, that is public domain or
openly licensed (most Internet Archive items, Creative Commons material,
Wikimedia Commons), or that you otherwise have permission to download. Some
sites — YouTube among them — restrict downloading in their Terms of Service.
Respect those terms and applicable copyright law.

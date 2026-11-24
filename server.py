#!/usr/bin/env python3
"""
OmniDownloader - Universal Video Downloader & Clipper Web Server
Designed for desktop, home servers, and Android (Termux) hosting.
Preserves existing yt_downloader.py and provides full format parity with it.
"""

import os
import re
import sys
import time
import uuid
import shutil
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

# Ensure Termux bin directories are in PATH when executed via root / su
termux_bin = "/data/data/com.termux/files/usr/bin"
if os.path.isdir(termux_bin) and termux_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{termux_bin}:{os.environ.get('PATH', '')}"

from flask import Flask, request, jsonify, render_template, send_file, Response
import yt_dlp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("OmniDownloader")

app = Flask(__name__, template_folder="templates", static_folder="static")

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "web_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job tracker
JOBS = {}
JOBS_LOCK = threading.Lock()

# Supported platform patterns
PLATFORM_PATTERNS = [
    (r"(youtube\.com|youtu\.be)", "YouTube"),
    (r"music\.youtube\.com", "YouTube Music"),
    (r"instagram\.com", "Instagram"),
    (r"pinterest\.(com|cl|co|de|es|fr|it|jp|ru)", "Pinterest"),
    (r"pin\.it", "Pinterest"),
    (r"tiktok\.com", "TikTok"),
    (r"(twitter\.com|x\.com)", "X / Twitter"),
    (r"reddit\.com", "Reddit"),
    (r"facebook\.com", "Facebook"),
    (r"vimeo\.com", "Vimeo"),
    (r"soundcloud\.com", "SoundCloud"),
    (r"twitch\.tv", "Twitch"),
]

def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for pattern, name in PLATFORM_PATTERNS:
        if re.search(pattern, url_lower):
            return name
    return "Universal (yt-dlp)"

def safe_filename(name: str, maxlen: int = 100) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', '_', name)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:maxlen] if cleaned else "download"

def parse_time_seconds(time_val) -> float:
    if time_val is None:
        return None
    val_str = str(time_val).strip()
    if not val_str:
        return None
    parts = val_str.split(':')
    try:
        if len(parts) == 1:
            return float(parts[0])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None

def format_seconds(seconds: float) -> str:
    if seconds is None:
        return "Unknown"
    sec = int(round(seconds))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def format_bytes(num_bytes) -> str:
    if not num_bytes:
        return "?"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"

def codec_short(vcodec: str) -> str:
    if not vcodec or vcodec == "none": return "?"
    vc = vcodec.lower()
    if "av01" in vc: return "AV1"
    if "vp9"  in vc: return "VP9"
    if "vp8"  in vc: return "VP8"
    if "avc"  in vc or "h264" in vc: return "H.264"
    if "hev"  in vc or "h265" in vc: return "H.265"
    return vcodec.split(".")[0].upper()[:6]

def dr_priority(f) -> int:
    dr = (f.get("dynamic_range") or "sdr").lower()
    if "dv"  in dr: return 4
    if "hdr" in dr: return 3
    if "hlg" in dr: return 2
    return 1


# ── Spatial 8D / 16D Audio Engine ───────────────────────────────
def build_spatial_audio_filter(
    mode="8d",
    intensity=7,
    speed_sec=8.0,
    bass_boost=True,
    reverb=True,
    clarity_boost=False,
    pitch_semitones=0,
    tempo=1.0,
    preset="none",
    stem_filter="none"
):
    """
    Constructs a comprehensive, hardware-grade FFmpeg audio filtergraph.
    Supports 8D / 16D spatial panning, stem filters (vocal remover / karaoke, vocal isolate, bass remover, sub-bass focus),
    pitch & tempo adjustment, acoustic enhancements (deep bass shelf, air clarity), and viral presets (slowed+reverb, nightcore, 432Hz).
    """
    filters = []

    # 1. Stem Filtering (Karaoke vocal cancel / Vocal isolation / Bass cut / Bass only)
    if stem_filter == "vocal_remover":
        # Center-channel vocal cancellation in stereo mix with kick recovery
        filters.append("pan=stereo|c0=c0-c1|c1=c1-c0,bass=g=3.5:f=120")
    elif stem_filter == "vocal_isolate":
        # Formant bandpass isolating human vocal presence
        filters.append("highpass=f=200,lowpass=f=4200")
    elif stem_filter == "bass_remove":
        # Complete low-end bass & sub cut
        filters.append("highpass=f=250,bass=g=-24:f=180")
    elif stem_filter == "bass_only":
        # Sub-bass extraction
        filters.append("lowpass=f=220,bass=g=5:f=100")

    # 2. Pitch Shifting (± Semitones using rubberband)
    try:
        pitch = float(pitch_semitones)
    except (ValueError, TypeError):
        pitch = 0.0

    if abs(pitch) >= 0.1:
        pitch_factor = 2.0 ** (pitch / 12.0)
        filters.append(f"rubberband=pitch={pitch_factor:.5f}")

    # 3. Special Sound Signatures / Presets
    if preset == "slowed_reverb":
        filters.append("atempo=0.88,asetrate=44100*0.92,aresample=44100")
    elif preset == "nightcore":
        filters.append("asetrate=44100*1.25,aresample=44100")
    elif preset == "432hz":
        ratio = 432.0 / 440.0
        filters.append(f"asetrate=44100*{ratio:.6f},atempo={1.0/ratio:.6f},aresample=44100")
    elif tempo != 1.0:
        try:
            t_val = float(tempo)
            if 0.5 <= t_val <= 2.0 and abs(t_val - 1.0) > 0.02:
                filters.append(f"atempo={t_val:.2f}")
        except (ValueError, TypeError):
            pass

    # 4. Acoustic Enhancements: Bass Warmth & Air Clarity
    if bass_boost and stem_filter != "bass_remove":
        filters.append("bass=g=4.5:f=110:w=0.6")

    if clarity_boost and stem_filter != "bass_only":
        filters.append("treble=g=3.5:f=8000,crystalizer=i=1.5")

    # 5. Spatial Orbit: 8D or 16D Binaural Panning
    mode_lower = str(mode).lower() if mode else "none"
    if mode_lower in ("8d", "16d"):
        try:
            if isinstance(speed_sec, str):
                s_map = {"fast": 5.0, "normal": 8.0, "slow": 14.0}
                sec = s_map.get(speed_sec.lower(), 8.0)
            else:
                sec = float(speed_sec)
            sec = max(1.0, min(60.0, sec))
        except (ValueError, TypeError):
            sec = 8.0

        hz = 1.0 / sec

        try:
            val = float(intensity)
            val = max(1.0, min(10.0, val))
            amount = 0.5 + (val / 10.0) * 0.5
        except (ValueError, TypeError):
            amount = 0.85

        if mode_lower == "16d":
            hz_16d = hz * 1.5
            filters.append("stereowiden=delay=25:feedback=0.38:crossfeed=0.32:drymix=0.75")
            filters.append(f"apulsator=mode=sine:hz={hz_16d:.4f}:amount={amount:.2f}:offset_l=0:offset_r=0.5")
        else:
            filters.append("stereowiden=delay=18:feedback=0.25:crossfeed=0.25:drymix=0.8")
            filters.append(f"apulsator=mode=sine:hz={hz:.4f}:amount={amount:.2f}:offset_l=0:offset_r=0.5")

    # 6. Psychoacoustic Reverb
    if reverb and stem_filter not in ("bass_only", "vocal_isolate"):
        if mode_lower == "16d":
            filters.append("aecho=0.8:0.88:45|90:0.3|0.2")
        elif preset == "slowed_reverb":
            filters.append("aecho=0.8:0.85:80|1200:0.35|0.18")
        else:
            filters.append("aecho=0.8:0.85:50|80:0.25|0.15")

    return ",".join(filters) if filters else "anull"

def embed_artwork_into_mp3(mp3_path: Path, thumb_path: Path):
    """Embeds thumbnail artwork into an MP3 file using mutagen."""
    try:
        from mutagen.id3 import ID3, APIC
        from mutagen.mp3 import MP3
        try:
            audio = MP3(str(mp3_path), ID3=ID3)
        except Exception:
            return
        if audio.tags is None:
            audio.add_tags()
        mime = "image/jpeg"
        ext = thumb_path.suffix.lower()
        if "png" in ext: mime = "image/png"
        elif "webp" in ext: mime = "image/webp"

        with open(thumb_path, "rb") as f:
            audio.tags.add(APIC(
                encoding=3,
                mime=mime,
                type=3, # Cover front
                desc="Cover",
                data=f.read()
            ))
        audio.save()
        logger.info(f"Embedded cover artwork into {mp3_path.name}")
    except Exception as e:
        logger.warning(f"Could not embed ID3 artwork into {mp3_path.name}: {e}")


# ── Background Storage Auto-Cleaner ───────────────────────────────
def cleanup_old_files():
    """Periodically purges files older than 30 minutes to save phone storage."""
    while True:
        try:
            time.sleep(300)  # Check every 5 minutes
            cutoff = datetime.now() - timedelta(minutes=30)
            with JOBS_LOCK:
                expired_jobs = []
                for job_id, job in JOBS.items():
                    if job.get("created_at") and job["created_at"] < cutoff:
                        expired_jobs.append(job_id)
                        job_folder = DOWNLOAD_DIR / job_id
                        if job_folder.exists():
                            try:
                                shutil.rmtree(job_folder, ignore_errors=True)
                                logger.info(f"Cleaned up expired job folder: {job_id}")
                            except Exception as e:
                                logger.warning(f"Failed to remove {job_folder}: {e}")
                for job_id in expired_jobs:
                    del JOBS[job_id]

            # Cleanup expired uploaded audio files (> 1 hour old)
            upload_cutoff = datetime.now() - timedelta(hours=1)
            for f in UPLOAD_DIR.iterdir():
                if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < upload_cutoff:
                    try: f.unlink()
                    except: pass
        except Exception as e:
            logger.error(f"Error in cleanup worker: {e}")

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()


# ── Routes ────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/8d")
def spatial_studio():
    return render_template("8d.html")

@app.route("/api/upload", methods=["POST"])
def upload_audio():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"success": False, "error": "Empty filename"}), 400

    ext = Path(file.filename).suffix.lower()
    allowed = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".wma"}
    if ext not in allowed:
        return jsonify({"success": False, "error": f"Unsupported format '{ext}'. Supported: MP3, WAV, FLAC, M4A, AAC, OGG."}), 400

    upload_id = uuid.uuid4().hex[:10]
    clean_stem = safe_filename(Path(file.filename).stem, 80)
    saved_path = UPLOAD_DIR / f"{upload_id}_{clean_stem}{ext}"
    file.save(str(saved_path))

    duration = 0
    title = clean_stem
    artist = "Local Audio File"

    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:format_tags=title,artist",
            "-of", "default=noprint_wrappers=1:nokey=0",
            str(saved_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if line.startswith("duration="):
                try: duration = float(line.split("=")[1])
                except: pass
            elif line.startswith("TAG:title="):
                val = line.split("=", 1)[1].strip()
                if val: title = val
            elif line.startswith("TAG:artist="):
                val = line.split("=", 1)[1].strip()
                if val: artist = val
    except Exception as e:
        logger.warning(f"Metadata probe failed for upload: {e}")

    filesize = saved_path.stat().st_size

    return jsonify({
        "success": True,
        "upload_id": upload_id,
        "filename": file.filename,
        "title": title,
        "author": artist,
        "duration": round(duration),
        "duration_formatted": format_seconds(round(duration)) if duration else "--:--",
        "platform": "Local Audio File",
        "filesize": format_bytes(filesize),
        "stream_url": f"/api/raw_audio/{upload_id}"
    })

@app.route("/api/raw_audio/<upload_id>")
def serve_raw_audio(upload_id):
    matches = list(UPLOAD_DIR.glob(f"{upload_id}_*"))
    if not matches:
        return "Audio file not found", 404
    file_path = matches[0]
    ext = file_path.suffix.lower()
    mime = "audio/mpeg"
    if ext == ".wav": mime = "audio/wav"
    elif ext == ".flac": mime = "audio/flac"
    elif ext in (".m4a", ".aac"): mime = "audio/mp4"
    elif ext in (".ogg", ".opus"): mime = "audio/ogg"
    return send_file(str(file_path), mimetype=mime, conditional=True)

@app.route("/api/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return Response(status=204)
    ffmpeg_available = bool(shutil.which("ffmpeg"))
    return jsonify({
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "ffmpeg": ffmpeg_available,
        "platform": sys.platform
    })

@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "error": "Please provide a valid URL"}), 400

    platform = detect_platform(url)
    logger.info(f"Fetching metadata for [{platform}]: {url}")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as e:
        logger.error(f"Metadata extraction failed: {e}")
        return jsonify({
            "success": False,
            "error": f"Failed to fetch media information: {str(e)}"
        }), 400

    if not meta:
        return jsonify({"success": False, "error": "No media found at this URL"}), 404

    # Playlist or multi-item detection
    entries = meta.get("entries")
    if entries:
        is_playlist = True
        title = meta.get("title") or "Playlist / Collection"
        thumbnail = meta.get("thumbnail") or (entries[0].get("thumbnail") if len(entries) > 0 else "")
        duration = sum((e.get("duration") or 0) for e in entries if isinstance(e, dict))
        author = meta.get("uploader") or meta.get("channel") or "Various"
        total_items = len(entries)
    else:
        is_playlist = False
        title = meta.get("title") or "Untitled Media"
        thumbnail = meta.get("thumbnail") or ""
        duration = meta.get("duration")
        author = meta.get("uploader") or meta.get("channel") or meta.get("artist") or "Unknown"
        total_items = 1

    # Extract all video formats with full metadata (matching yt_downloader.py parity)
    raw_formats = meta.get("formats", [])
    video_fmts = [
        f for f in raw_formats
        if f.get("vcodec") not in (None, "none", "") and f.get("height")
    ]

    seen = set()
    formats_list = []

    for f in sorted(video_fmts,
                    key=lambda x: (x.get("height") or 0,
                                   x.get("fps")    or 0,
                                   dr_priority(x)),
                    reverse=True):
        dr = (f.get("dynamic_range") or "SDR").upper()
        codec = codec_short(f.get("vcodec", ""))
        height = f.get("height")
        fps = round(f.get("fps") or 0)
        key = (height, fps, dr, codec)
        if key in seen:
            continue
        seen.add(key)

        filesize = f.get("filesize") or f.get("filesize_approx")
        ext = f.get("ext", "mp4")

        # Label resolution
        res_label = f"{height}p"
        if height >= 2160: res_label += " (4K Ultra HD)"
        elif height >= 1440: res_label += " (2K QHD)"
        elif height >= 1080: res_label += " (Full HD)"
        elif height >= 720: res_label += " (HD)"

        formats_list.append({
            "format_id": f.get("format_id"),
            "height": height,
            "resolution": res_label,
            "fps": fps,
            "dynamic_range": dr,
            "codec": codec,
            "ext": ext,
            "filesize": format_bytes(filesize),
            "filesize_bytes": filesize or 0,
            "has_audio": f.get("acodec") not in (None, "none", ""),
            "is_hdr": "HDR" in dr or "DV" in dr or "HLG" in dr
        })

    # Audio format options
    audio_formats = [
        {"id": "mp3_320", "label": "MP3 (320 kbps Premium + Album Art)", "format": "mp3", "quality": "320"},
        {"id": "m4a_aac", "label": "M4A / AAC (Native Stream, Apple & Android)", "format": "m4a", "quality": "best"},
        {"id": "flac", "label": "FLAC (Lossless Studio Quality)", "format": "flac", "quality": "lossless"},
    ]

    return jsonify({
        "success": True,
        "title": title,
        "author": author,
        "thumbnail": thumbnail,
        "duration": duration,
        "duration_formatted": format_seconds(duration) if duration else "--:--",
        "platform": platform,
        "is_playlist": is_playlist,
        "item_count": total_items,
        "formats": formats_list,
        "audio_formats": audio_formats,
        "webpage_url": meta.get("webpage_url", url)
    })

def run_download_task(job_id: str, url: str, options: dict):
    """Worker function executed in a background thread."""
    job_folder = DOWNLOAD_DIR / job_id
    job_folder.mkdir(parents=True, exist_ok=True)

    download_mode = options.get("mode", "video")  # 'video', 'audio', 'thumbnail', '8d', '16d'
    spatial_audio = options.get("spatial_audio", "none").lower()
    if download_mode in ("8d", "16d"):
        spatial_audio = download_mode
        download_mode = "audio"
        audio_codec = "mp3"

    spatial_intensity = options.get("spatial_intensity", 7)
    spatial_speed_sec = options.get("spatial_speed_sec") or options.get("spatial_speed", 8.0)
    spatial_bass = options.get("spatial_bass", True)
    spatial_reverb = options.get("spatial_reverb", True)
    spatial_clarity = options.get("spatial_clarity", False)
    spatial_pitch = options.get("spatial_pitch", 0)
    spatial_tempo = options.get("spatial_tempo", 1.0)
    spatial_preset = options.get("spatial_preset", "none")
    stem_filter = options.get("stem_filter", "none")

    format_id = options.get("format_id")
    has_audio = options.get("has_audio", False)
    audio_codec = options.get("audio_codec", "mp3")
    container = options.get("container", "mp4").lower()
    is_trim = options.get("trim", False)
    start_sec = parse_time_seconds(options.get("start_time"))
    end_sec = parse_time_seconds(options.get("end_time"))

    ffmpeg_available = bool(shutil.which("ffmpeg"))

    # Progress hook callback
    def progress_hook(d):
        status = d.get("status")
        with JOBS_LOCK:
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                percent = round((downloaded / total * 100), 1) if total > 0 else 0

                JOBS[job_id]["status"] = "downloading"
                JOBS[job_id]["progress"] = percent
                JOBS[job_id]["speed"] = f"{speed / (1024*1024):.2f} MB/s" if speed else "Calculating..."
                JOBS[job_id]["eta"] = f"{int(eta)}s" if eta else "..."
            elif status == "finished":
                JOBS[job_id]["status"] = "processing"
                JOBS[job_id]["progress"] = 99
                JOBS[job_id]["message"] = "Processing / converting final media..."

    ydl_opts = {
        "outtmpl": str(job_folder / "%(title).80s.%(ext)s"),
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    # 1. THUMBNAIL MODE
    if download_mode == "thumbnail":
        try:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "processing"
                JOBS[job_id]["progress"] = 50
                JOBS[job_id]["message"] = "Fetching high-resolution thumbnail..."

            with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                thumb_url = info.get("thumbnail")
                title = safe_filename(info.get("title", "thumbnail"))

            if not thumb_url:
                raise Exception("No thumbnail URL found for this media.")

            import requests
            resp = requests.get(thumb_url, timeout=15)
            resp.raise_for_status()

            ext = "jpg"
            if "png" in thumb_url.lower(): ext = "png"
            elif "webp" in thumb_url.lower(): ext = "webp"

            out_file = job_folder / f"{title}.{ext}"
            out_file.write_bytes(resp.content)

            with JOBS_LOCK:
                JOBS[job_id]["status"] = "finished"
                JOBS[job_id]["progress"] = 100
                JOBS[job_id]["filename"] = out_file.name
                JOBS[job_id]["file_path"] = str(out_file)
                JOBS[job_id]["file_size"] = format_bytes(out_file.stat().st_size)
            return
        except Exception as e:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = str(e)
            return

    # 2. AUDIO ONLY MODE
    elif download_mode == "audio":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["writethumbnail"] = True
        if ffmpeg_available:
            codec_to_use = "mp3"
            quality_to_use = "320"
            if audio_codec == "m4a":
                codec_to_use = "m4a"
                quality_to_use = "0"
            elif audio_codec == "flac":
                codec_to_use = "flac"
                quality_to_use = "0"

            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": codec_to_use,
                    "preferredquality": quality_to_use
                },
                {"key": "FFmpegMetadata", "add_metadata": True},
                {"key": "EmbedThumbnail"}
            ]

    # 3. VIDEO MODE (Matches yt_downloader.py quality and merger options)
    else:
        if format_id and format_id != "best":
            # If user picked an exact format ID:
            fmt_str = format_id if has_audio else f"{format_id}+bestaudio/best"
        else:
            # Best available video + audio
            fmt_str = "bestvideo+bestaudio/best"

        ydl_opts["format"] = fmt_str

        if ffmpeg_available:
            ydl_opts["merge_output_format"] = container
            if container == "mp4":
                ydl_opts["postprocessor_args"] = {
                    "merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
                }
            else:
                ydl_opts["postprocessor_args"] = {
                    "merger": ["-c:v", "copy", "-c:a", "copy"]
                }
            ydl_opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]

    try:
        upload_id = options.get("upload_id")
        is_preview = bool(options.get("is_preview", False))
        if is_preview:
            preview_dur = options.get("preview_duration", 30)
            if str(preview_dur).lower() in ("full", "0", "all", "none"):
                is_trim = False
            else:
                try:
                    dur = max(5, int(preview_dur))
                except (ValueError, TypeError):
                    dur = 30
                is_trim = True
                if start_sec is None:
                    start_sec = 0.0
                end_sec = start_sec + dur

        if upload_id:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "processing"
                JOBS[job_id]["progress"] = 25
                JOBS[job_id]["message"] = "Loading uploaded audio track..."

            matches = list(UPLOAD_DIR.glob(f"{upload_id}_*"))
            if not matches:
                raise Exception("Uploaded audio file was not found on server.")
            source_file = matches[0]
            clean_name = source_file.name[len(upload_id)+1:]
            final_file = job_folder / clean_name
            shutil.copy2(source_file, final_file)
            logger.info(f"Loaded uploaded audio: {final_file.name}")
        else:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            files = [f for f in job_folder.iterdir() if f.is_file() and not f.name.endswith(".part") and not f.name.endswith(".temp")]
            if not files:
                raise Exception("Download succeeded but no output file was generated.")

            final_file = files[0]

        # Fast post-download trimming (Instant, 0% CPU re-encoding overhead, works across all platforms)
        if is_trim and start_sec is not None and end_sec is not None and end_sec > start_sec:
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "processing"
                JOBS[job_id]["message"] = f"Trimming clip ({format_seconds(start_sec)} -> {format_seconds(end_sec)})..."

            duration = end_sec - start_sec
            trimmed_file = job_folder / f"clip_{final_file.name}"
            trim_cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_sec),
                "-i", str(final_file),
                "-t", str(duration),
                "-c", "copy",
                "-avoid_negative_ts", "1",
                str(trimmed_file)
            ]
            logger.info(f"Executing fast stream trim: {' '.join(trim_cmd)}")
            res = subprocess.run(trim_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0 and trimmed_file.exists() and trimmed_file.stat().st_size > 0:
                final_file.unlink(missing_ok=True)
                final_file = trimmed_file
                logger.info(f"Fast trim succeeded: {final_file.name} ({format_bytes(final_file.stat().st_size)})")
            else:
                logger.warning(f"Fast copy trim returned {res.returncode}. Falling back to safe re-encode trim...")
                trim_cmd_fallback = [
                    "ffmpeg", "-y",
                    "-ss", str(start_sec),
                    "-i", str(final_file),
                    "-t", str(duration),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k",
                    "-avoid_negative_ts", "1",
                    str(trimmed_file)
                ]
                res_fallback = subprocess.run(trim_cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if res_fallback.returncode == 0 and trimmed_file.exists() and trimmed_file.stat().st_size > 0:
                    final_file.unlink(missing_ok=True)
                    final_file = trimmed_file

        # Comprehensive Audio Effects & Spatial Mastering
        spatial_mode = "16d" if (spatial_audio == "16d") else ("8d" if (spatial_audio == "8d") else None)
        has_effects = (
            bool(spatial_mode)
            or (stem_filter not in (None, "", "none"))
            or (spatial_preset not in (None, "", "none"))
            or (abs(float(spatial_pitch or 0)) >= 0.1)
            or bool(spatial_clarity)
            or bool(spatial_bass)
            or bool(spatial_reverb)
        )

        if has_effects and ffmpeg_available:
            tag_parts = []
            if spatial_mode:
                tag_parts.append(spatial_mode.upper())
            if stem_filter == "vocal_remover":
                tag_parts.append("KARAOKE")
            elif stem_filter == "vocal_isolate":
                tag_parts.append("ACAPELLA")
            elif stem_filter == "bass_remove":
                tag_parts.append("NO-BASS")
            elif stem_filter == "bass_only":
                tag_parts.append("BASS-ONLY")
            if spatial_preset == "slowed_reverb":
                tag_parts.append("SLOWED+REVERB")
            elif spatial_preset == "nightcore":
                tag_parts.append("NIGHTCORE")
            elif spatial_preset == "432hz":
                tag_parts.append("432Hz")
            if abs(float(spatial_pitch or 0)) >= 0.1:
                tag_parts.append(f"PITCH_{int(spatial_pitch):+d}")

            tag_str = f"[{'+'.join(tag_parts)}]" if tag_parts else "[MASTERED]"

            with JOBS_LOCK:
                JOBS[job_id]["status"] = "processing"
                JOBS[job_id]["message"] = f"Mastering {tag_str} Audio (Speed: {spatial_speed_sec}s, Intensity: {spatial_intensity}/10)..."

            spatial_filter = build_spatial_audio_filter(
                mode=spatial_mode or "none",
                intensity=spatial_intensity,
                speed_sec=spatial_speed_sec,
                bass_boost=spatial_bass,
                reverb=spatial_reverb,
                clarity_boost=spatial_clarity,
                pitch_semitones=spatial_pitch,
                tempo=spatial_tempo,
                preset=spatial_preset,
                stem_filter=stem_filter
            )
            clean_stem = safe_filename(final_file.stem, 70)
            spatial_out = job_folder / f"{tag_str} {clean_stem}.mp3"
            spatial_cmd = [
                "ffmpeg", "-y",
                "-i", str(final_file),
                "-af", spatial_filter,
                "-acodec", "libmp3lame",
                "-b:a", "320k",
                "-movflags", "+faststart",
                str(spatial_out)
            ]
            logger.info(f"Rendering {tag_str} audio: {' '.join(spatial_cmd)}")
            res_spatial = subprocess.run(spatial_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res_spatial.returncode == 0 and spatial_out.exists() and spatial_out.stat().st_size > 0:
                thumbs = [f for f in job_folder.iterdir() if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') and f != final_file and f != spatial_out]
                if thumbs:
                    embed_artwork_into_mp3(spatial_out, thumbs[0])
                final_file.unlink(missing_ok=True)
                final_file = spatial_out
                logger.info(f"{tag_str} audio render complete: {final_file.name} ({format_bytes(final_file.stat().st_size)})")
            else:
                err_msg = res_spatial.stderr[-400:] if res_spatial.stderr else "FFmpeg render error"
                logger.error(f"Audio mastering failed: {err_msg}")
                if is_preview:
                    raise Exception(f"Audio processing error: {err_msg}")

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "finished"
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["filename"] = final_file.name
            JOBS[job_id]["file_path"] = str(final_file)
            JOBS[job_id]["file_size"] = format_bytes(final_file.stat().st_size)
            logger.info(f"Job {job_id} completed: {final_file.name} ({JOBS[job_id]['file_size']})")

    except Exception as e:
        logger.error(f"Download failed for job {job_id}: {e}")
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    upload_id = data.get("upload_id")
    if not url and not upload_id:
        return jsonify({"success": False, "error": "URL or uploaded audio file is required"}), 400

    job_id = uuid.uuid4().hex[:10]

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "speed": "",
            "eta": "",
            "message": "Initializing worker...",
            "filename": None,
            "file_path": None,
            "file_size": None,
            "error": None,
            "created_at": datetime.now()
        }

    thread = threading.Thread(
        target=run_download_task,
        args=(job_id, url, data),
        daemon=True
    )
    thread.start()

    return jsonify({"success": True, "job_id": job_id})

@app.route("/api/preview", methods=["POST"])
def generate_preview():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    upload_id = data.get("upload_id")
    if not url and not upload_id:
        return jsonify({"success": False, "error": "URL or uploaded audio file is required"}), 400

    job_id = "prev_" + uuid.uuid4().hex[:8]
    data["is_preview"] = True

    preview_dur = data.get("preview_duration", 30)
    start_raw = data.get("start_time", "00:00")
    start_sec = parse_time_seconds(start_raw) or 0

    if str(preview_dur).lower() in ("full", "0", "all", "none"):
        data["trim"] = False
        duration_label = "Full track"
    else:
        try:
            dur = max(5, int(preview_dur))
        except (ValueError, TypeError):
            dur = 30
        data["trim"] = True
        data["start_time"] = format_seconds(start_sec)
        data["end_time"] = format_seconds(start_sec + dur)
        duration_label = f"{dur}s"

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "speed": "",
            "eta": "",
            "message": f"Rendering {duration_label} audio audition preview...",
            "filename": None,
            "file_path": None,
            "file_size": None,
            "error": None,
            "created_at": datetime.now()
        }

    thread = threading.Thread(
        target=run_download_task,
        args=(job_id, url, data),
        daemon=True
    )
    thread.start()

    return jsonify({"success": True, "job_id": job_id})

@app.route("/api/status/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404
        return jsonify({"success": True, "job": job})

@app.route("/api/file/<job_id>")
def download_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("status") != "finished":
            return "File is not ready or does not exist", 404
        file_path = job.get("file_path")
        filename = job.get("filename")

    if not file_path or not os.path.exists(file_path):
        return "File not found on server", 404

    # Trigger browser file download
    response = send_file(
        file_path,
        as_attachment=True,
        download_name=filename
    )

    # Optional: Schedule deletion 5 minutes after being served
    def delayed_remove():
        time.sleep(300)
        try:
            p = Path(file_path)
            if p.parent.exists():
                shutil.rmtree(p.parent, ignore_errors=True)
                logger.info(f"Cleaned up served download {job_id}")
        except Exception:
            pass

    threading.Thread(target=delayed_remove, daemon=True).start()

    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "="*60)
    print("  🚀 OmniDownloader Server Started")
    print(f"  📡 Local Access   : http://localhost:{port}")
    print(f"  🌐 Network Access : http://0.0.0.0:{port}")
    print("="*60 + "\n")
    app.run(host="0.0.0.0", port=port, threaded=True)

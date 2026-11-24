import os
import sys
import shutil
import uuid
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Set
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("demucs_service")

# Directories
BASE_DIR = Path(__file__).resolve().parent
TEMP_JOBS_DIR = BASE_DIR / "temp_jobs"
TEMP_JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Configuration
ALLOWED_EXTENSIONS: Set[str] = {".mp3", ".wav", ".m4a", ".flac"}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB limit
CLEANUP_INTERVAL_SECONDS = 900     # Check every 15 minutes
MAX_FILE_AGE_SECONDS = 3600         # 1 hour retention

def find_ffmpeg_executable() -> Optional[str]:
    """Checks for ffmpeg on standard PATH and platform-specific locations."""
    path = shutil.which("ffmpeg")
    if path:
        return path

    # Common alternate paths
    common_candidates = [
        "/data/data/com.termux/files/usr/bin/ffmpeg",  # Android Termux
        "C:\\ffmpeg\\bin\\ffmpeg.exe",                 # Windows default
        "C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe",
        "/usr/bin/ffmpeg",                             # Linux standard
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg"                     # macOS Homebrew
    ]
    for candidate in common_candidates:
        if Path(candidate).is_file():
            return candidate

    return None

def find_demucs_command() -> list[str]:
    """Returns the executable command list for Demucs."""
    demucs_bin = shutil.which("demucs")
    if demucs_bin:
        return [demucs_bin]
    # Fallback to python module execution
    return [sys.executable, "-m", "demucs.separate"]

async def cleanup_old_jobs():
    """Background task that periodically purges job directories older than 1 hour."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            now = time.time()
            deleted_count = 0
            if TEMP_JOBS_DIR.exists():
                for job_path in TEMP_JOBS_DIR.iterdir():
                    if job_path.is_dir():
                        mtime = job_path.stat().st_mtime
                        if now - mtime > MAX_FILE_AGE_SECONDS:
                            shutil.rmtree(job_path, ignore_errors=True)
                            deleted_count += 1
            if deleted_count > 0:
                logger.info(f"Purged {deleted_count} temporary job directory(ies) older than 1 hour.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Error during job cleanup: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: verify ffmpeg and start cleanup background worker
    ffmpeg_bin = find_ffmpeg_executable()
    if ffmpeg_bin:
        logger.info(f"✔ FFmpeg detected at: {ffmpeg_bin}")
    else:
        logger.warning("⚠ FFmpeg was not detected in PATH. Audio format conversions may fail.")

    cleanup_task = asyncio.create_task(cleanup_old_jobs())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

# Initialize FastAPI App
app = FastAPI(
    title="Demucs AI Vocal Remover Service",
    description="Studio-Grade Vocal and Instrumental Separation using Meta's HTDemucs neural network.",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for full frontend compatibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health_check():
    """System health and dependency readiness check."""
    ffmpeg_bin = find_ffmpeg_executable()
    return {
        "status": "online",
        "ffmpeg_available": ffmpeg_bin is not None,
        "ffmpeg_path": ffmpeg_bin,
        "supported_formats": list(ALLOWED_EXTENSIONS),
        "max_upload_size_mb": MAX_FILE_SIZE // (1024 * 1024),
        "timestamp": time.time()
    }

@app.post("/api/separate")
async def separate_audio(file: UploadFile = File(...)):
    """
    Accepts an uploaded audio file (.mp3, .wav, .m4a, .flac), runs Meta's HTDemucs 
    with --two-stems=vocals asynchronously, and returns download URLs for Vocals and Instrumental.
    """
    # 1. Validate filename and extension
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing uploaded file or filename."
        )

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported audio format '{ext}'. Allowed formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    # 2. Check FFmpeg availability
    ffmpeg_bin = find_ffmpeg_executable()
    if not ffmpeg_bin:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="FFmpeg is not installed or not available on the server system. Please install FFmpeg."
        )

    # 3. Create unique isolated job folder
    job_id = uuid.uuid4().hex[:12]
    job_dir = TEMP_JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    
    input_file_path = job_dir / f"input{ext}"
    output_dir = job_dir / "separated"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 4. Save uploaded file while streaming to prevent memory exhaustion and enforce size limit
    total_bytes = 0
    try:
        with open(input_file_path, "wb") as dest:
            while chunk := await file.read(1024 * 1024):  # 1MB buffer
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Uploaded file exceeds maximum limit of {MAX_FILE_SIZE // (1024 * 1024)}MB."
                    )
                dest.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded audio: {str(e)}"
        )

    if total_bytes == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty (0 bytes)."
        )

    logger.info(f"[{job_id}] Received '{file.filename}' ({total_bytes / (1024 * 1024):.2f} MB). Starting Demucs...")

    # 5. Build and execute Demucs command asynchronously
    demucs_cmd = find_demucs_command() + [
        "-n", "htdemucs",
        "--two-stems=vocals",
        "-o", str(output_dir),
        str(input_file_path)
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *demucs_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            logger.error(f"[{job_id}] Demucs execution failed (code {proc.returncode}): {err_msg}")
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Demucs AI separation failed: {err_msg or 'Non-zero exit code'}"
            )
    except FileNotFoundError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Demucs command line runner is not installed in the active Python environment. Run 'pip install demucs'."
        )
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Demucs processing error: {str(e)}"
        )

    # 6. Locate generated stem files
    # Demucs outputs to: <output_dir>/htdemucs/<input_stem>/vocals.wav and no_vocals.wav
    stem_folder = output_dir / "htdemucs" / input_file_path.stem
    vocals_file = stem_folder / "vocals.wav"
    instrumental_file = stem_folder / "no_vocals.wav"

    if not vocals_file.exists() or not instrumental_file.exists():
        logger.error(f"[{job_id}] Demucs output stems missing in {stem_folder}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Demucs completed but output stem files (vocals.wav, no_vocals.wav) were not found."
        )

    logger.info(f"[{job_id}] Demucs separation succeeded for '{file.filename}'!")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "success",
            "job_id": job_id,
            "filename": file.filename,
            "vocals_url": f"/api/download/{job_id}/vocals",
            "instrumental_url": f"/api/download/{job_id}/no_vocals"
        }
    )

@app.get("/api/download/{job_id}/{stem}")
async def download_stem(job_id: str, stem: str):
    """
    Serves the resulting separated .wav file for the specified job_id and stem.
    Supported stems: 'vocals', 'no_vocals', or 'instrumental'.
    """
    job_dir = TEMP_JOBS_DIR / job_id
    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or temporary files have expired."
        )

    # Map alias 'instrumental' to Demucs's 'no_vocals'
    stem_name = "no_vocals" if stem.lower() in ("no_vocals", "instrumental") else stem.lower()
    if stem_name not in ("vocals", "no_vocals"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid stem '{stem}'. Available stems: 'vocals', 'no_vocals', 'instrumental'."
        )

    # Search for the .wav file in the job's directory
    target_filename = f"{stem_name}.wav"
    found_file: Optional[Path] = None
    for candidate in job_dir.rglob(target_filename):
        if candidate.is_file():
            found_file = candidate
            break

    if not found_file or not found_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Requested stem '{stem}' is not ready or was deleted."
        )

    client_download_name = f"{job_id}_{'instrumental' if stem_name == 'no_vocals' else 'vocals'}.wav"

    return FileResponse(
        path=str(found_file),
        media_type="audio/wav",
        filename=client_download_name
    )

# Dedicated Modern Neumorphic UI for Demucs AI Separation
@app.get("/", response_class=HTMLResponse)
@app.get("/demucs", response_class=HTMLResponse)
async def serve_demucs_ui():
    """Serves a sleek, Neumorphic web interface for Demucs AI vocal removal."""
    html_content = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Meta Demucs AI — Studio Vocal Remover</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root[data-theme="dark"] {
      --nm-bg: #151517;
      --nm-surface: #1e1e22;
      --nm-surface-dark: #121214;
      --nm-border: rgba(255, 255, 255, 0.07);
      --nm-shadow-light: rgba(255, 255, 255, 0.05);
      --nm-shadow-dark: rgba(0, 0, 0, 0.65);
      --color-accent: #f05a24;
      --color-text-primary: #f5f5f4;
      --color-text-muted: #8e8e93;
      --nm-raised: -6px -6px 14px var(--nm-shadow-light), 6px 6px 14px var(--nm-shadow-dark);
      --nm-inset: inset -3px -3px 8px var(--nm-shadow-light), inset 3px 3px 8px var(--nm-shadow-dark);
    }
    :root[data-theme="light"] {
      --nm-bg: #e0e5ec;
      --nm-surface: #e0e5ec;
      --nm-surface-dark: #d1d9e6;
      --nm-border: rgba(0, 0, 0, 0.08);
      --nm-shadow-light: #ffffff;
      --nm-shadow-dark: #a3b1c6;
      --color-accent: #ea580c;
      --color-text-primary: #1e293b;
      --color-text-muted: #64748b;
      --nm-raised: -6px -6px 14px var(--nm-shadow-light), 6px 6px 14px var(--nm-shadow-dark);
      --nm-inset: inset -3px -3px 8px var(--nm-shadow-light), inset 3px 3px 8px var(--nm-shadow-dark);
    }
    body {
      background-color: var(--nm-bg);
      color: var(--color-text-primary);
      font-family: 'Plus Jakarta Sans', sans-serif;
      min-height: 100vh;
      transition: background 0.25s ease;
    }
    .nm-card {
      background-color: var(--nm-surface);
      border: 1px solid var(--nm-border);
      border-radius: 1.25rem;
      box-shadow: var(--nm-raised);
    }
    .nm-inset {
      background-color: var(--nm-surface);
      border: 1px solid var(--nm-border);
      border-radius: 0.875rem;
      box-shadow: var(--nm-inset);
    }
    .nm-btn {
      background-color: var(--nm-surface);
      border: 1px solid var(--nm-border);
      border-radius: 0.75rem;
      box-shadow: var(--nm-raised);
      transition: all 0.15s ease;
      cursor: pointer;
    }
    .nm-btn:hover { filter: brightness(1.05); }
    .nm-btn:active { box-shadow: var(--nm-inset); transform: translateY(1px); }
    .nm-btn-primary {
      background: linear-gradient(135deg, #f05a24, #d03e0c);
      color: #ffffff;
      border: none;
      border-radius: 0.75rem;
      box-shadow: 0 4px 14px rgba(240, 90, 36, 0.4);
      transition: all 0.2s ease;
      cursor: pointer;
    }
    .nm-btn-primary:hover {
      filter: brightness(1.1);
      box-shadow: 0 6px 20px rgba(240, 90, 36, 0.5);
    }
  </style>
</head>
<body class="p-4 sm:p-8 flex items-center justify-center">

  <div class="max-w-2xl w-full space-y-6">

    <!-- Header Card -->
    <header class="nm-card p-6 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-[var(--color-accent)]/10 text-[var(--color-accent)] flex items-center justify-center border border-[var(--color-accent)]/20">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"/></svg>
        </div>
        <div>
          <h1 class="text-lg font-bold">Meta Demucs AI Vocal Remover</h1>
          <p class="text-xs text-[var(--color-text-muted)] font-mono">HTDemucs Transformer • FastAPI Backend</p>
        </div>
      </div>
      <button onclick="toggleTheme()" class="nm-btn p-2.5 text-xs font-mono">🌓 Theme</button>
    </header>

    <!-- Upload Card -->
    <section class="nm-card p-6 space-y-5">
      <div>
        <h2 class="text-sm font-bold flex items-center gap-2">
          <span>1. Upload Song / Track</span>
          <span class="text-[10px] font-mono bg-cyan-500/10 text-cyan-400 px-2 py-0.5 rounded-full border border-cyan-500/20">MP3 / WAV / M4A / FLAC</span>
        </h2>
        <p class="text-xs text-[var(--color-text-muted)] mt-1">Upload any track (up to 100MB). Demucs will separate it into pure Vocals and Backing Music.</p>
      </div>

      <!-- Drag & Drop Zone -->
      <div id="drop-zone" class="nm-inset p-8 text-center border-2 border-dashed border-[var(--nm-border)] rounded-xl cursor-pointer hover:border-[var(--color-accent)] transition-all">
        <input type="file" id="file-input" accept=".mp3,.wav,.m4a,.flac" class="hidden">
        <div class="space-y-2">
          <div class="w-12 h-12 mx-auto rounded-full bg-[var(--color-accent)]/10 text-[var(--color-accent)] flex items-center justify-center">
            <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>
          </div>
          <p id="file-label" class="text-sm font-medium">Click to select audio file or drag & drop here</p>
          <p class="text-[11px] text-[var(--color-text-muted)] font-mono">Max size: 100MB</p>
        </div>
      </div>

      <!-- Separate Button -->
      <button id="btn-submit" onclick="submitSeparation()" class="nm-btn-primary w-full py-4 text-sm font-bold flex items-center justify-center gap-2">
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>
        <span id="btn-text">Separate Vocals & Instrumental (Demucs AI)</span>
        <svg id="btn-spinner" class="animate-spin h-4 w-4 hidden" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
      </button>

      <!-- Status Indicator -->
      <div id="status-card" class="nm-inset p-4 hidden space-y-2 text-center">
        <div class="flex items-center justify-center gap-2 text-xs font-mono text-cyan-400">
          <svg class="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path></svg>
          <span id="status-msg">Demucs neural network is isolating audio stems...</span>
        </div>
        <p class="text-[11px] text-[var(--color-text-muted)]">This uses deep spectrogram masking. Processing typically takes 15–45 seconds depending on file length.</p>
      </div>
    </section>

    <!-- Results Section (Dual Players) -->
    <section id="results-card" class="nm-card p-6 space-y-6 hidden">
      <div class="flex items-center justify-between border-b border-[var(--nm-border)] pb-3">
        <div>
          <h2 class="text-sm font-bold text-emerald-400 flex items-center gap-1.5">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
            Separation Complete!
          </h2>
          <p id="result-filename" class="text-xs font-mono text-[var(--color-text-muted)] mt-0.5">track.mp3</p>
        </div>
        <span class="text-[10px] font-mono px-2.5 py-1 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20">HTDemucs 2-Stem</span>
      </div>

      <!-- Player 1: Vocals -->
      <div class="nm-card p-4 space-y-3 border-l-4 border-cyan-400">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-lg">🗣️</span>
            <div>
              <h3 class="text-xs font-bold text-cyan-400">Vocals Only (Acapella)</h3>
              <p class="text-[10px] text-[var(--color-text-muted)]">Isolated lead & backing singing voice</p>
            </div>
          </div>
          <a id="download-vocals-btn" href="#" download class="nm-btn px-3 py-1.5 text-xs font-bold text-cyan-400 flex items-center gap-1">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
            Download WAV
          </a>
        </div>
        <div class="nm-inset p-2">
          <audio id="player-vocals" controls class="w-full h-8"></audio>
        </div>
      </div>

      <!-- Player 2: Instrumental -->
      <div class="nm-card p-4 space-y-3 border-l-4 border-[var(--color-accent)]">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="text-lg">🎸</span>
            <div>
              <h3 class="text-xs font-bold text-[var(--color-accent)]">Instrumental / Backing Track</h3>
              <p class="text-[10px] text-[var(--color-text-muted)]">Full drums, bass, synths, and accompaniment</p>
            </div>
          </div>
          <a id="download-inst-btn" href="#" download class="nm-btn px-3 py-1.5 text-xs font-bold text-[var(--color-accent)] flex items-center gap-1">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
            Download WAV
          </a>
        </div>
        <div class="nm-inset p-2">
          <audio id="player-inst" controls class="w-full h-8"></audio>
        </div>
      </div>
    </section>

  </div>

  <script>
    let selectedFile = null;

    function toggleTheme() {
      const doc = document.documentElement;
      const cur = doc.getAttribute('data-theme') || 'dark';
      doc.setAttribute('data-theme', cur === 'dark' ? 'light' : 'dark');
    }

    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const fileLabel = document.getElementById('file-label');

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.style.borderColor = 'var(--color-accent)'; });
    dropZone.addEventListener('dragleave', () => { dropZone.style.borderColor = ''; });
    dropZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropZone.style.borderColor = '';
      if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener('change', () => {
      if (fileInput.files.length) handleFile(fileInput.files[0]);
    });

    function handleFile(file) {
      selectedFile = file;
      fileLabel.innerHTML = `<span class="font-bold text-cyan-400">${file.name}</span> (${(file.size / (1024 * 1024)).toFixed(2)} MB)`;
    }

    async function submitSeparation() {
      if (!selectedFile) {
        alert('Please choose an audio file (.mp3, .wav, .m4a, .flac) first.');
        return;
      }

      const btn = document.getElementById('btn-submit');
      const btnText = document.getElementById('btn-text');
      const spinner = document.getElementById('btn-spinner');
      const statusCard = document.getElementById('status-card');
      const resultsCard = document.getElementById('results-card');

      btn.disabled = true;
      btnText.textContent = 'Processing with Demucs AI…';
      spinner.classList.remove('hidden');
      statusCard.classList.remove('hidden');
      resultsCard.classList.add('hidden');

      const formData = new FormData();
      formData.append('file', selectedFile);

      try {
        const res = await fetch('/api/separate', {
          method: 'POST',
          body: formData
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: 'HTTP ' + res.status }));
          throw new Error(err.detail || 'Separation failed');
        }

        const data = await res.json();

        // Update Results UI
        document.getElementById('result-filename').textContent = data.filename;

        const vocalsPlayer = document.getElementById('player-vocals');
        vocalsPlayer.src = data.vocals_url;
        document.getElementById('download-vocals-btn').href = data.vocals_url;

        const instPlayer = document.getElementById('player-inst');
        instPlayer.src = data.instrumental_url;
        document.getElementById('download-inst-btn').href = data.instrumental_url;

        statusCard.classList.add('hidden');
        resultsCard.classList.remove('hidden');
        resultsCard.scrollIntoView({ behavior: 'smooth' });

      } catch (err) {
        alert('Error: ' + err.message);
        statusCard.classList.add('hidden');
      } finally {
        btn.disabled = false;
        btnText.textContent = 'Separate Vocals & Instrumental (Demucs AI)';
        spinner.classList.add('hidden');
      }
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

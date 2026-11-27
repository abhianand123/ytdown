#!/usr/bin/env python3
"""
UVR5 Headless Stem Separation Service (ARM64 CPU Memory-Safe).

Uses python-audio-separator (Ultimate Vocal Remover 5 engine) with CPU-only
onnxruntime / PyTorch, persistent ./models/uvr5 model caching, strict single-job
concurrency (asyncio.Semaphore(1)), and automatic cleanup of temporary input and
intermediate files.
"""

import asyncio
import gc
import inspect
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

# Constrain thread pools before loading native math / ONNX / Torch runtimes on ARM64
_CPU_THREADS = os.environ.get("UVR5_CPU_THREADS", "4")
os.environ.setdefault("OMP_NUM_THREADS", _CPU_THREADS)
os.environ.setdefault("OPENBLAS_NUM_THREADS", _CPU_THREADS)
os.environ.setdefault("MKL_NUM_THREADS", _CPU_THREADS)
os.environ.setdefault("ORT_NUM_THREADS", _CPU_THREADS)
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("uvr5_service")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [uvr5]: %(message)s",
        datefmt="%H:%M:%S",
    )

BASE_DIR = Path(__file__).resolve().parent
UVR5_MODELS_DIR = Path(os.environ.get("UVR5_MODELS_DIR", str(BASE_DIR / "models" / "uvr5"))).resolve()
UVR5_JOBS_DIR = Path(os.environ.get("UVR5_JOBS_DIR", str(BASE_DIR / "uvr5_jobs"))).resolve()

UVR5_MODELS_DIR.mkdir(parents=True, exist_ok=True)
UVR5_JOBS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MODEL = os.environ.get("UVR5_DEFAULT_MODEL", "UVR-MDX-NET-Inst_HQ_3.onnx")
SEGMENT_SIZE = int(os.environ.get("UVR5_SEGMENT_SIZE", "256"))
BATCH_SIZE = int(os.environ.get("UVR5_BATCH_SIZE", "1"))
DEFAULT_OUTPUT_FORMAT = os.environ.get("UVR5_OUTPUT_FORMAT", "WAV").upper()

UVR5_MODELS: Dict[str, Dict[str, Any]] = {
    "UVR-MDX-NET-Inst_HQ_3.onnx": {
        "filename": "UVR-MDX-NET-Inst_HQ_3.onnx",
        "label": "UVR-MDX-NET Inst HQ 3 (Default • Fast ARM64)",
        "arch": "MDX-Net ONNX",
        "approx_size_mb": 64,
        "recommended": True,
        "description": "Fast, low-RAM official UVR5 ONNX model tuned for ARM64 CPU stem separation.",
    },
    "UVR-MDX-NET-Voc_FT.onnx": {
        "filename": "UVR-MDX-NET-Voc_FT.onnx",
        "label": "UVR-MDX-NET Voc FT (Vocal Focus)",
        "arch": "MDX-Net ONNX",
        "approx_size_mb": 67,
        "recommended": False,
        "description": "Fine-tuned MDX-Net vocal extraction model for lead vocals and acapella isolation.",
    },
    "model_bs_roformer_ep_317_sdr_12.9755.ckpt": {
        "filename": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "label": "BS-RoFormer Ep 317 SDR 12.97 (Max Quality)",
        "arch": "BS-RoFormer PyTorch",
        "approx_size_mb": 640,
        "recommended": False,
        "description": "Ultra-high SDR transformer checkpoint; significantly heavier RAM and CPU usage.",
    },
}


def resolve_model_name(requested: Optional[str]) -> str:
    """Validate and normalize requested UVR5 model filename."""
    if not requested:
        return DEFAULT_MODEL
    cleaned = requested.strip()
    if cleaned in UVR5_MODELS:
        return cleaned
    lower_map = {k.lower(): k for k in UVR5_MODELS}
    if cleaned.lower() in lower_map:
        return lower_map[cleaned.lower()]
    raise ValueError(
        f"Unsupported UVR5 model '{cleaned}'. Allowed models: {', '.join(UVR5_MODELS.keys())}"
    )


def _onnx_providers() -> list[str]:
    try:
        import onnxruntime as ort
        return list(ort.get_available_providers())
    except Exception:
        return []


def _prepare_audio_input(
    source_path: Path,
    scratch_dir: Path,
    start_sec: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> Path:
    """
    Normalizes/optionally trims source audio into a temporary 44.1kHz stereo WAV
    inside scratch_dir using ffmpeg so audio-separator reads any container reliably.
    """
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg binary not found in PATH inside UVR5 environment.")

    prepped_wav = scratch_dir / "prepared_input.wav"
    cmd = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    if start_sec is not None and start_sec > 0:
        cmd.extend(["-ss", f"{float(start_sec):.3f}"])
    cmd.extend(["-i", str(source_path)])
    if duration_sec is not None and duration_sec > 0:
        cmd.extend(["-t", f"{float(duration_sec):.3f}"])
    cmd.extend(["-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(prepped_wav)])

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0 or not prepped_wav.exists() or prepped_wav.stat().st_size == 0:
        err = (proc.stderr or "").strip() or "ffmpeg failed to decode audio"
        raise RuntimeError(f"Audio preprocessing failed: {err}")
    return prepped_wav


def _classify_stem_outputs(raw_outputs: list, scratch_dir: Path) -> tuple[Optional[Path], Optional[Path]]:
    """Identifies Vocals and Instrumental output files emitted by audio-separator."""
    candidates: list[Path] = []
    for item in raw_outputs or []:
        p = Path(str(item))
        if not p.is_absolute():
            p = scratch_dir / p
        if p.is_file():
            candidates.append(p)

    # Fallback scan in scratch_dir if return list paths differ
    if len(candidates) < 2:
        for p in sorted(scratch_dir.iterdir()):
            if p.is_file() and p.name != "prepared_input.wav" and p not in candidates:
                candidates.append(p)

    vocals_file: Optional[Path] = None
    inst_file: Optional[Path] = None

    for p in candidates:
        low = p.name.lower()
        if any(tok in low for tok in ("(instrumental)", "_instrumental", "no_vocals", "accompaniment")):
            inst_file = p
        elif any(tok in low for tok in ("(vocals)", "_vocals", "(vocal)")):
            vocals_file = p

    # Fallback heuristics
    if not vocals_file or not inst_file:
        for p in candidates:
            low = p.name.lower()
            if inst_file is None and ("inst" in low or "music" in low or "karaoke" in low):
                inst_file = p
            elif vocals_file is None and "vocal" in low:
                vocals_file = p

    return vocals_file, inst_file


class UVR5Service:
    """
    Singleton-friendly headless UVR5 separation service for ARM64 Debian CPU.

    Initializes audio-separator's Separator once, caches model weights inside
    ./models/uvr5, serializes inference strictly one job at a time via
    asyncio.Semaphore(1) + threading.Lock, and cleans temporary files automatically.
    """

    def __init__(
        self,
        models_dir: Path = UVR5_MODELS_DIR,
        jobs_dir: Path = UVR5_JOBS_DIR,
        default_model: str = DEFAULT_MODEL,
        segment_size: int = SEGMENT_SIZE,
        batch_size: int = BATCH_SIZE,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
    ) -> None:
        self.models_dir = Path(models_dir).resolve()
        self.jobs_dir = Path(jobs_dir).resolve()
        self._stage_dir = (self.jobs_dir / "_stage").resolve()
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._stage_dir.mkdir(parents=True, exist_ok=True)

        self.default_model = resolve_model_name(default_model)
        self.segment_size = int(segment_size)
        self.batch_size = int(batch_size)
        self.output_format = output_format.upper()

        self._thread_lock = threading.Lock()
        self._semaphore_lock = threading.Lock()
        self._semaphores: Dict[int, asyncio.Semaphore] = {}

        self._separator: Any = None
        self._loaded_model: Optional[str] = None
        self._busy = False
        self._active_job_id: Optional[str] = None

    def _get_async_semaphore(self) -> asyncio.Semaphore:
        """Returns the single-concurrency asyncio.Semaphore(1) for the current event loop."""
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        with self._semaphore_lock:
            sem = self._semaphores.get(loop_id)
            if sem is None:
                sem = asyncio.Semaphore(1)
                self._semaphores[loop_id] = sem
            return sem

    def _create_separator_locked(self) -> Any:
        """Instantiates Separator once with ARM64 CPU-safe configuration."""
        if self._separator is not None:
            return self._separator

        from audio_separator.separator import Separator

        self._stage_dir.mkdir(parents=True, exist_ok=True)
        sig = inspect.signature(Separator.__init__)
        params = sig.parameters

        candidate_kwargs: Dict[str, Any] = {
            "log_level": logging.INFO,
            "model_file_dir": str(self.models_dir),
            "output_dir": str(self._stage_dir),
            "output_format": self.output_format,
            "normalization_threshold": 0.9,
            "use_autocast": False,
            "mdx_params": {
                "hop_length": 1024,
                "segment_size": self.segment_size,
                "overlap": 0.25,
                "batch_size": self.batch_size,
                "enable_denoise": False,
            },
            "mdxc_params": {
                "segment_size": self.segment_size,
                "override_model_segment_size": False,
                "batch_size": self.batch_size,
                "overlap": 8,
                "pitch_shift": 0,
            },
        }
        filtered_kwargs = {k: v for k, v in candidate_kwargs.items() if k in params}

        logger.info(
            "Initializing UVR5 Separator once (models_dir=%s, segment_size=%s, batch_size=%s)",
            self.models_dir,
            self.segment_size,
            self.batch_size,
        )
        self._separator = Separator(**filtered_kwargs)
        return self._separator

    def init_separator(self, preload_default: bool = False) -> None:
        """Initializes Separator once ahead of inference requests."""
        with self._thread_lock:
            sep = self._create_separator_locked()
            if preload_default and self._loaded_model != self.default_model:
                logger.info("Preloading default UVR5 model: %s", self.default_model)
                sep.load_model(model_filename=self.default_model)
                self._loaded_model = self.default_model

    def _ensure_model_loaded_locked(self, sep: Any, model_name: str) -> None:
        if self._loaded_model == model_name:
            return
        logger.info("Loading UVR5 model into Separator: %s (cache=%s)", model_name, self.models_dir)
        sig = inspect.signature(sep.load_model)
        if "model_filename" in sig.parameters:
            sep.load_model(model_filename=model_name)
        else:
            sep.load_model(model_name)
        self._loaded_model = model_name

    def separate_file(
        self,
        input_path: str | Path,
        output_dir: Optional[str | Path] = None,
        model_name: Optional[str] = None,
        start_sec: Optional[float] = None,
        duration_sec: Optional[float] = None,
        output_format: Optional[str] = None,
        cleanup_input: bool = True,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Synchronously separates an input audio file into Vocals and Instrumental stems.
        Protected by an exclusive threading lock and cleans up temporary/intermediate files.
        """
        src_path = Path(input_path).resolve()
        if not src_path.is_file():
            raise FileNotFoundError(f"Input audio file not found: {src_path}")

        resolved_model = resolve_model_name(model_name)
        jid = job_id or uuid.uuid4().hex[:12]
        out_dir = Path(output_dir).resolve() if output_dir else (self.jobs_dir / jid)
        out_dir.mkdir(parents=True, exist_ok=True)

        fmt = (output_format or self.output_format).upper()
        ext = fmt.lower()
        scratch_dir = out_dir / f"_scratch_{uuid.uuid4().hex[:6]}"
        scratch_dir.mkdir(parents=True, exist_ok=True)

        t0 = time.monotonic()
        with self._thread_lock:
            self._busy = True
            self._active_job_id = jid
            try:
                self._stage_dir.mkdir(parents=True, exist_ok=True)
                sep = self._create_separator_locked()
                sep.output_dir = str(self._stage_dir)
                if getattr(sep, "model_instance", None) is not None and hasattr(sep.model_instance, "output_dir"):
                    sep.model_instance.output_dir = str(self._stage_dir)
                if hasattr(sep, "output_format"):
                    sep.output_format = fmt
                    if getattr(sep, "model_instance", None) is not None and hasattr(sep.model_instance, "output_format"):
                        sep.model_instance.output_format = fmt

                self._ensure_model_loaded_locked(sep, resolved_model)

                prepared_wav = _prepare_audio_input(
                    src_path,
                    scratch_dir,
                    start_sec=start_sec,
                    duration_sec=duration_sec,
                )

                logger.info(
                    "Running UVR5 separation [job=%s, model=%s, input=%s]",
                    jid,
                    resolved_model,
                    src_path.name,
                )
                raw_outputs = sep.separate(str(prepared_wav))
                vocals_raw, inst_raw = _classify_stem_outputs(raw_outputs, self._stage_dir)

                if not vocals_raw or not inst_raw:
                    raise RuntimeError(
                        f"Expected vocals and instrumental outputs from UVR5, got: {raw_outputs}"
                    )

                vocals_final = out_dir / f"vocals.{vocals_raw.suffix.lstrip('.') or ext}"
                inst_final = out_dir / f"instrumental.{inst_raw.suffix.lstrip('.') or ext}"

                if vocals_final.exists():
                    vocals_final.unlink()
                if inst_final.exists():
                    inst_final.unlink()

                shutil.move(str(vocals_raw), str(vocals_final))
                shutil.move(str(inst_raw), str(inst_final))

                elapsed = round(time.monotonic() - t0, 2)
                logger.info(
                    "UVR5 complete [job=%s, elapsed=%.2fs, vocals=%d bytes, inst=%d bytes]",
                    jid,
                    elapsed,
                    vocals_final.stat().st_size,
                    inst_final.stat().st_size,
                )

                return {
                    "status": "success",
                    "job_id": jid,
                    "model": resolved_model,
                    "vocals_path": str(vocals_final),
                    "instrumental_path": str(inst_final),
                    "vocals_size": vocals_final.stat().st_size,
                    "instrumental_size": inst_final.stat().st_size,
                    "elapsed_sec": elapsed,
                }
            finally:
                # Always delete intermediate scratch/staging files and temporary input file
                shutil.rmtree(scratch_dir, ignore_errors=True)
                for left_file in self._stage_dir.iterdir():
                    try:
                        if left_file.is_dir():
                            shutil.rmtree(left_file, ignore_errors=True)
                        else:
                            left_file.unlink()
                    except Exception:
                        pass
                if cleanup_input and src_path.exists():
                    try:
                        src_path.unlink()
                    except Exception:
                        pass
                self._busy = False
                self._active_job_id = None
                gc.collect()

    async def async_separate_file(
        self,
        input_path: str | Path,
        output_dir: Optional[str | Path] = None,
        model_name: Optional[str] = None,
        start_sec: Optional[float] = None,
        duration_sec: Optional[float] = None,
        output_format: Optional[str] = None,
        cleanup_input: bool = True,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Strict asyncio.Semaphore(1)-gated separation wrapper running CPU work off the event loop.
        """
        sem = self._get_async_semaphore()
        async with sem:
            return await asyncio.to_thread(
                self.separate_file,
                input_path,
                output_dir,
                model_name,
                start_sec,
                duration_sec,
                output_format,
                cleanup_input,
                job_id,
            )

    def status_snapshot(self) -> Dict[str, Any]:
        cached_models = {}
        for name in UVR5_MODELS:
            p = self.models_dir / name
            cached_models[name] = p.is_file() and p.stat().st_size > 0
        return {
            "status": "ready",
            "separator_initialized": self._separator is not None,
            "loaded_model": self._loaded_model,
            "default_model": self.default_model,
            "models_dir": str(self.models_dir),
            "segment_size": self.segment_size,
            "batch_size": self.batch_size,
            "busy": self._busy,
            "active_job_id": self._active_job_id,
            "onnx_providers": _onnx_providers(),
            "cached_models": cached_models,
        }


uvr5_service = UVR5Service()

# In-memory registry of async microservice jobs
ASYNC_JOBS: Dict[str, Dict[str, Any]] = {}
ASYNC_JOBS_LOCK = threading.Lock()


def cleanup_expired_jobs(max_age_minutes: int = 60) -> int:
    """Purges completed job directories older than max_age_minutes."""
    removed = 0
    cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
    if UVR5_JOBS_DIR.exists():
        for entry in UVR5_JOBS_DIR.iterdir():
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime)
                if mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                    with ASYNC_JOBS_LOCK:
                        ASYNC_JOBS.pop(entry.name, None)
            except Exception:
                pass
    return removed


async def _periodic_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        try:
            await asyncio.to_thread(cleanup_expired_jobs, 60)
        except Exception as exc:
            logger.warning("Cleanup loop error: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    preload = os.environ.get("UVR5_PRELOAD_MODEL", "0").lower() in ("1", "true", "yes")
    await asyncio.to_thread(uvr5_service.init_separator, preload)
    cleanup_task = asyncio.create_task(_periodic_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(
    title="UVR5 Headless ARM64 Stem Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SeparatePathRequest(BaseModel):
    input_path: str
    output_dir: Optional[str] = None
    model: Optional[str] = None
    start_sec: Optional[float] = None
    duration_sec: Optional[float] = None
    output_format: Optional[str] = None
    cleanup_input: bool = True
    job_id: Optional[str] = None


async def _run_queued_job(
    job_id: str,
    input_path: Path,
    output_dir: Path,
    model_name: str,
    start_sec: Optional[float],
    duration_sec: Optional[float],
    output_format: Optional[str],
    cleanup_input: bool,
) -> None:
    with ASYNC_JOBS_LOCK:
        if job_id in ASYNC_JOBS:
            ASYNC_JOBS[job_id]["status"] = "waiting_lock"
            ASYNC_JOBS[job_id]["progress"] = 10
            ASYNC_JOBS[job_id]["message"] = f"Waiting for UVR5 CPU queue ({model_name})..."

    try:
        sem = uvr5_service._get_async_semaphore()
        async with sem:
            with ASYNC_JOBS_LOCK:
                if job_id in ASYNC_JOBS:
                    ASYNC_JOBS[job_id]["status"] = "processing"
                    ASYNC_JOBS[job_id]["progress"] = 30
                    ASYNC_JOBS[job_id]["message"] = f"Running {model_name} on ARM64 CPU..."

            result = await asyncio.to_thread(
                uvr5_service.separate_file,
                input_path,
                output_dir,
                model_name,
                start_sec,
                duration_sec,
                output_format,
                cleanup_input,
                job_id,
            )

        with ASYNC_JOBS_LOCK:
            if job_id in ASYNC_JOBS:
                ASYNC_JOBS[job_id].update({
                    "status": "finished",
                    "progress": 100,
                    "model": result["model"],
                    "vocals_path": result["vocals_path"],
                    "instrumental_path": result["instrumental_path"],
                    "vocals_size": result["vocals_size"],
                    "instrumental_size": result["instrumental_size"],
                    "vocals_url": f"/download/{job_id}/vocals",
                    "instrumental_url": f"/download/{job_id}/instrumental",
                    "elapsed_sec": result["elapsed_sec"],
                    "message": f"Complete via {result['model']} ({result['elapsed_sec']}s)",
                })
    except Exception as exc:
        logger.exception("Queued UVR5 job %s failed", job_id)
        if cleanup_input and input_path.exists():
            try:
                input_path.unlink()
            except Exception:
                pass
        with ASYNC_JOBS_LOCK:
            if job_id in ASYNC_JOBS:
                ASYNC_JOBS[job_id].update({
                    "status": "error",
                    "error": str(exc),
                    "message": f"UVR5 separation failed: {exc}",
                })


@app.get("/health")
async def health() -> Dict[str, Any]:
    snap = uvr5_service.status_snapshot()
    snap["ffmpeg"] = bool(shutil.which("ffmpeg"))
    snap["arch"] = platform.machine() or (os.uname().machine if hasattr(os, "uname") else "unknown")
    return snap


@app.get("/models")
async def list_models() -> Dict[str, Any]:
    items = []
    for key, info in UVR5_MODELS.items():
        cached_path = uvr5_service.models_dir / key
        items.append({
            **info,
            "is_default": key == uvr5_service.default_model,
            "cached": cached_path.is_file() and cached_path.stat().st_size > 0,
        })
    return {
        "default_model": uvr5_service.default_model,
        "loaded_model": uvr5_service._loaded_model,
        "models_dir": str(uvr5_service.models_dir),
        "segment_size": uvr5_service.segment_size,
        "batch_size": uvr5_service.batch_size,
        "models": items,
    }


@app.post("/separate")
async def separate_endpoint(req: SeparatePathRequest) -> JSONResponse:
    try:
        model_name = resolve_model_name(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    in_path = Path(req.input_path)
    if not in_path.is_file():
        raise HTTPException(status_code=404, detail=f"Input audio path does not exist: {req.input_path}")

    jid = req.job_id or uuid.uuid4().hex[:12]
    out_dir = Path(req.output_dir) if req.output_dir else (UVR5_JOBS_DIR / jid)

    try:
        res = await uvr5_service.async_separate_file(
            input_path=in_path,
            output_dir=out_dir,
            model_name=model_name,
            start_sec=req.start_sec,
            duration_sec=req.duration_sec,
            output_format=req.output_format,
            cleanup_input=req.cleanup_input,
            job_id=jid,
        )
        res["vocals_url"] = f"/download/{jid}/vocals"
        res["instrumental_url"] = f"/download/{jid}/instrumental"
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("UVR5 /separate error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/separate/async")
async def separate_async_endpoint(req: SeparatePathRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    try:
        model_name = resolve_model_name(req.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    in_path = Path(req.input_path)
    if not in_path.is_file():
        raise HTTPException(status_code=404, detail=f"Input audio path does not exist: {req.input_path}")

    jid = req.job_id or uuid.uuid4().hex[:12]
    out_dir = Path(req.output_dir) if req.output_dir else (UVR5_JOBS_DIR / jid)
    out_dir.mkdir(parents=True, exist_ok=True)

    with ASYNC_JOBS_LOCK:
        ASYNC_JOBS[jid] = {
            "job_id": jid,
            "status": "queued",
            "progress": 5,
            "model": model_name,
            "vocals_path": None,
            "instrumental_path": None,
            "vocals_url": None,
            "instrumental_url": None,
            "elapsed_sec": None,
            "error": None,
            "message": f"Queued for UVR5 ({model_name})...",
            "created_at": datetime.now().isoformat(),
        }

    background_tasks.add_task(
        _run_queued_job,
        jid,
        in_path,
        out_dir,
        model_name,
        req.start_sec,
        req.duration_sec,
        req.output_format,
        req.cleanup_input,
    )
    return {"status": "queued", "job_id": jid, "model": model_name}


@app.post("/separate/upload")
async def separate_upload_endpoint(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    start_sec: Optional[float] = Form(None),
    duration_sec: Optional[float] = Form(None),
) -> JSONResponse:
    try:
        model_name = resolve_model_name(model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    jid = uuid.uuid4().hex[:12]
    out_dir = UVR5_JOBS_DIR / jid
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "audio.wav").suffix.lower() or ".wav"
    temp_input = out_dir / f"upload_input{ext}"
    try:
        with temp_input.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                handle.write(chunk)
    except Exception as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {exc}")

    try:
        res = await uvr5_service.async_separate_file(
            input_path=temp_input,
            output_dir=out_dir,
            model_name=model_name,
            start_sec=start_sec,
            duration_sec=duration_sec,
            cleanup_input=True,
            job_id=jid,
        )
        res["vocals_url"] = f"/download/{jid}/vocals"
        res["instrumental_url"] = f"/download/{jid}/instrumental"
        return JSONResponse(res)
    except Exception as exc:
        logger.exception("UVR5 /separate/upload error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/status/{job_id}")
async def job_status(job_id: str) -> Dict[str, Any]:
    with ASYNC_JOBS_LOCK:
        job = ASYNC_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="UVR5 job not found")
        return job


@app.get("/download/{job_id}/{stem}")
async def download_stem(job_id: str, stem: str, preview: int = 0) -> FileResponse:
    stem_norm = "instrumental" if stem.lower() in ("instrumental", "no_vocals", "music") else "vocals"
    job_dir = UVR5_JOBS_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job folder not found or expired")

    matches = list(job_dir.glob(f"{stem_norm}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail=f"Stem '{stem_norm}' not found for job {job_id}")

    target = matches[0]
    ext = target.suffix.lower()
    media_type = "audio/wav" if ext == ".wav" else ("audio/flac" if ext == ".flac" else "audio/mpeg")
    disposition = "inline" if preview else "attachment"
    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=f"{job_id}_{stem_norm}{ext}",
        headers={"Content-Disposition": f'{disposition}; filename="{job_id}_{stem_norm}{ext}"'},
    )

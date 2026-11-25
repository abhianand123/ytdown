import os
import re
import sys
import time
import shutil
import logging
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger("AISeparator")

BASE_DIR = Path(__file__).resolve().parent
MODELS_DIR = BASE_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

MDX_MODEL_NAME = "UVR-MDX-NET-Inst_Main.onnx"
MDX_MODEL_URL = "https://huggingface.co/seanghay/uvr_models/resolve/main/UVR-MDX-NET-Inst_Main.onnx"


def has_demucs() -> bool:
    try:
        import demucs  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return bool(shutil.which("demucs"))


def has_mdx_onnx() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def find_ffmpeg() -> Optional[str]:
    path = shutil.which("ffmpeg")
    if path:
        return path
    for cand in (
        "/data/data/com.termux/files/usr/bin/ffmpeg",
        "C:\\ffmpeg\\bin\\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ):
        if os.path.isfile(cand):
            return cand
    return None


def prepare_wav_input(
    input_path: Path,
    wav_out: Path,
    start_sec: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> Path:
    """Normalizes any audio file (mp3/m4a/flac/webm/wav) to 44.1kHz stereo PCM16 WAV via ffmpeg."""
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise RuntimeError("FFmpeg executable not found on system.")

    cmd = [ffmpeg_bin, "-y"]
    if start_sec is not None and start_sec > 0:
        cmd += ["-ss", f"{start_sec:.2f}"]
    cmd += ["-i", str(input_path)]
    if duration_sec is not None and duration_sec > 0:
        cmd += ["-t", f"{duration_sec:.2f}"]
    cmd += ["-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(wav_out)]

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not wav_out.exists() or wav_out.stat().st_size < 1024:
        err = res.stderr[-400:] if res.stderr else "FFmpeg conversion failed"
        raise RuntimeError(f"Audio decode failed: {err}")
    return wav_out


def ensure_mdx_model(progress_cb: Optional[Callable[[int, str], None]] = None) -> Path:
    model_path = MODELS_DIR / MDX_MODEL_NAME
    if model_path.exists() and model_path.stat().st_size > 10_000_000:
        return model_path

    if progress_cb:
        progress_cb(8, f"Downloading neural AI weights ({MDX_MODEL_NAME})...")

    tmp_path = MODELS_DIR / f".tmp_{MDX_MODEL_NAME}"
    req = urllib.request.Request(MDX_MODEL_URL, headers={"User-Agent": "OmniDownloader-AI/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0 and progress_cb:
                pct = min(20, 8 + int((downloaded / total) * 12))
                progress_cb(pct, f"Downloading AI model: {downloaded // (1024*1024)}MB / {total // (1024*1024)}MB")
    tmp_path.replace(model_path)
    return model_path


def separate_with_mdx_onnx(
    wav_path: Path,
    vocals_out: Path,
    instrumental_out: Path,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Separates Vocals & Instrumental using UVR-MDX-NET-Inst_Main.onnx + ONNX Runtime + torch STFT.
    Low memory (~250MB RAM) and optimized for ARM64 mobile CPUs.
    """
    import numpy as np
    import torch
    import onnxruntime as ort
    from scipy.io import wavfile

    model_path = ensure_mdx_model(progress_cb)

    if progress_cb:
        progress_cb(20, "Loading UVR MDX-Net ONNX neural model...")

    sr, audio_int16 = wavfile.read(str(wav_path))
    if audio_int16.ndim == 1:
        audio_int16 = np.stack([audio_int16, audio_int16], axis=-1)
    if audio_int16.dtype == np.int16:
        mix = (audio_int16.astype(np.float32) / 32768.0).T  # shape [2, samples]
    elif audio_int16.dtype == np.int32:
        mix = (audio_int16.astype(np.float32) / 2147483648.0).T
    else:
        mix = audio_int16.astype(np.float32).T

    # Standard UVR-MDX-NET-Inst_Main STFT geometry
    n_fft = 6144
    hop = 1024
    dim_f = 2048
    dim_t = 256
    n_bins = n_fft // 2 + 1
    chunk_size = hop * (dim_t - 1)
    trim = n_fft // 2
    gen_size = chunk_size - 2 * trim
    compensate = 1.035

    window = torch.hann_window(window_length=n_fft, periodic=True)

    opts = ort.SessionOptions()
    cpu_cnt = os.cpu_count() or 4
    opts.intra_op_num_threads = min(8, cpu_cnt)
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

    session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    def stft(x: torch.Tensor) -> torch.Tensor:
        x = x.reshape([-1, chunk_size])
        spec = torch.stft(x, n_fft=n_fft, hop_length=hop, window=window, center=True, return_complex=True)
        spec_ri = torch.view_as_real(spec).permute([0, 3, 1, 2])
        spec_ri = spec_ri.reshape([-1, 4, n_bins, dim_t])
        return spec_ri[:, :, :dim_f]

    def istft(x: torch.Tensor) -> torch.Tensor:
        freq_pad = torch.zeros([x.shape[0], 4, n_bins - dim_f, dim_t], dtype=x.dtype)
        x = torch.cat([x, freq_pad], -2)
        x = x.reshape([-1, 2, 2, n_bins, dim_t]).reshape([-1, 2, n_bins, dim_t])
        x = x.permute([0, 2, 3, 1]).contiguous()
        x = torch.view_as_complex(x)
        wav = torch.istft(x, n_fft=n_fft, hop_length=hop, window=window, center=True)
        return wav.reshape([-1, 2, chunk_size])

    # Segment into ~15-second overlapping super-chunks to bound RAM strictly on phones
    samples = mix.shape[-1]
    margin = sr  # 1s overlap margin
    seg_len = 15 * sr
    if samples < seg_len:
        seg_len = samples

    segments = []
    idx = 0
    for skip in range(0, samples, seg_len):
        s_margin = 0 if idx == 0 else margin
        end = min(skip + seg_len + margin, samples)
        start = skip - s_margin
        segments.append((skip, start, end, mix[:, start:end].copy()))
        idx += 1
        if end == samples:
            break

    # Count total ONNX frame chunks for exact % tracking
    total_onnx_chunks = 0
    for _, _, _, cmix in segments:
        ns = cmix.shape[1]
        total_onnx_chunks += max(1, (ns + gen_size - 1) // gen_size)

    done_chunks = 0
    out_parts = []
    t0 = time.time()

    for seg_i, (skip, _start, _end, cmix) in enumerate(segments):
        n_sample = cmix.shape[1]
        pad = gen_size - (n_sample % gen_size) if (n_sample % gen_size) != 0 else gen_size
        mix_p = np.concatenate(
            (np.zeros((2, trim), dtype=np.float32), cmix, np.zeros((2, pad), dtype=np.float32), np.zeros((2, trim), dtype=np.float32)),
            axis=1,
        )

        tar_list = []
        pos = 0
        while pos < n_sample + pad:
            wave_chunk = torch.from_numpy(mix_p[:, pos:pos + chunk_size]).unsqueeze(0)
            with torch.no_grad():
                spek = stft(wave_chunk)
                pred_np = session.run(None, {input_name: spek.numpy()})[0]
                tar_wave = istft(torch.from_numpy(pred_np))[:, :, trim:-trim]
            tar_list.append(tar_wave.squeeze(0).numpy())
            pos += gen_size
            done_chunks += 1
            if progress_cb:
                pct = min(94, 22 + int((done_chunks / max(1, total_onnx_chunks)) * 70))
                elapsed = int(time.time() - t0)
                progress_cb(pct, f"UVR MDX-Net Neural AI: segment {done_chunks}/{total_onnx_chunks} ({elapsed}s)...")

        tar_signal = np.concatenate(tar_list, axis=1)[:, :n_sample]
        s_cut = 0 if skip == 0 else margin
        e_cut = None if seg_i == len(segments) - 1 else -margin
        out_parts.append(tar_signal[:, s_cut:e_cut])

    inst_signal = np.concatenate(out_parts, axis=1)[:, :samples] * compensate
    # Match lengths strictly before subtraction
    min_len = min(mix.shape[1], inst_signal.shape[1])
    inst_signal = inst_signal[:, :min_len]
    vocals_signal = mix[:, :min_len] - inst_signal

    def save_pcm16(path: Path, arr: np.ndarray):
        peak = np.max(np.abs(arr))
        if peak > 0.99:
            arr = arr * (0.98 / peak)
        pcm = np.clip(arr.T * 32767.0, -32768, 32767).astype(np.int16)
        wavfile.write(str(path), sr, pcm)

    save_pcm16(instrumental_out, inst_signal)
    save_pcm16(vocals_out, vocals_signal)

    elapsed = round(time.time() - t0, 2)
    return {
        "engine": "mdx_onnx",
        "engine_label": "UVR MDX-Net Neural AI (ONNX)",
        "ai_used": True,
        "elapsed_sec": elapsed,
    }


def separate_with_demucs(
    wav_path: Path,
    vocals_out: Path,
    instrumental_out: Path,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """Runs Meta Demucs v4 (htdemucs --two-stems=vocals) in subprocess with live tqdm progress parsing."""
    demucs_dir = wav_path.parent / "demucs_raw"
    demucs_dir.mkdir(parents=True, exist_ok=True)

    demucs_bin = shutil.which("demucs")
    base_cmd = [demucs_bin] if demucs_bin else [sys.executable, "-m", "demucs.separate"]
    cmd = base_cmd + [
        "-n", "htdemucs",
        "--two-stems=vocals",
        "--segment", "7",
        "--overlap", "0.1",
        "-j", "1",
        "-o", str(demucs_dir),
        str(wav_path),
    ]

    if progress_cb:
        progress_cb(15, "Starting Meta Demucs v4 (htdemucs) neural transformer...")

    t0 = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_lines = []
    assert proc.stdout is not None
    # Read character chunks so '\r' tqdm updates trigger live progress
    buf = ""
    while True:
        ch = proc.stdout.read(1)
        if ch == "" and proc.poll() is not None:
            break
        if ch in ("\r", "\n"):
            line = buf.strip()
            buf = ""
            if line:
                last_lines.append(line)
                if len(last_lines) > 20:
                    last_lines.pop(0)
                m = re.search(r"(\d+)%\|", line)
                if m and progress_cb:
                    pct_raw = int(m.group(1))
                    pct = min(95, 20 + int(pct_raw * 0.75))
                    elapsed = int(time.time() - t0)
                    progress_cb(pct, f"Meta Demucs HTDemucs: {pct_raw}% ({elapsed}s elapsed)...")
                elif "Downloading:" in line and progress_cb:
                    progress_cb(18, "Downloading HTDemucs pretrained model weights...")
        else:
            buf += ch

    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"Demucs failed (exit {rc}): {' | '.join(last_lines[-5:])}")

    stem_folder = demucs_dir / "htdemucs" / wav_path.stem
    src_voc = stem_folder / "vocals.wav"
    src_inst = stem_folder / "no_vocals.wav"
    if not src_voc.exists() or not src_inst.exists():
        raise RuntimeError("Demucs completed but vocals.wav / no_vocals.wav were missing.")

    shutil.move(str(src_voc), str(vocals_out))
    shutil.move(str(src_inst), str(instrumental_out))

    elapsed = round(time.time() - t0, 2)
    return {
        "engine": "htdemucs",
        "engine_label": "Meta Demucs v4 (HTDemucs Transformer)",
        "ai_used": True,
        "elapsed_sec": elapsed,
    }


def separate_with_ffmpeg_dsp(
    input_path: Path,
    vocals_out: Path,
    instrumental_out: Path,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """Fast frequency-split DSP fallback if neural engines are unavailable."""
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise RuntimeError("Neither AI models nor FFmpeg are available.")

    t0 = time.time()
    if progress_cb:
        progress_cb(40, "Running Studio DSP vocal isolation...")
    vocal_af = (
        "highpass=f=80,lowpass=f=12000,stereotools=slev=0.15:mlev=1.65,"
        "equalizer=f=1000:width_type=o:width=1.5:g=3.5,"
        "equalizer=f=2800:width_type=o:width=1.5:g=4.0,"
        "equalizer=f=6000:width_type=o:width=1.5:g=2.5,"
        "dialoguenhance=original=0.35:enhance=2.8:voice=4,volume=2.2,alimiter=limit=0.96"
    )
    subprocess.run([ffmpeg_bin, "-y", "-i", str(input_path), "-af", vocal_af, str(vocals_out)], capture_output=True)

    if progress_cb:
        progress_cb(75, "Running Studio DSP instrumental extraction...")
    inst_af = (
        "asplit=2[l][h];[l]lowpass=f=190,bass=g=4:f=100[low];"
        "[h]highpass=f=190,stereotools=mlev=0.03:slev=1.45,treble=g=2:f=8000[high];"
        "[low][high]amix=inputs=2:weights=1.15 1.0,alimiter=limit=0.96"
    )
    subprocess.run([ffmpeg_bin, "-y", "-i", str(input_path), "-af", inst_af, str(instrumental_out)], capture_output=True)

    return {
        "engine": "ffmpeg_dsp",
        "engine_label": "Studio DSP Fallback (Non-AI)",
        "ai_used": False,
        "elapsed_sec": round(time.time() - t0, 2),
    }


def separate_audio_track(
    input_path: Path,
    vocals_out: Path,
    instrumental_out: Path,
    preferred_engine: str = "mdx_onnx",
    start_sec: Optional[float] = None,
    duration_sec: Optional[float] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """
    Unified AI Stem Separation entry point.
    Supports 'mdx_onnx' (UVR MDX-Net ONNX), 'htdemucs' (Meta Demucs v4), or 'dsp' fallback.
    """
    work_wav = vocals_out.parent / "_normalized_44k.wav"
    if progress_cb:
        progress_cb(5, "Decoding audio into 44.1kHz stereo WAV...")
    prepare_wav_input(input_path, work_wav, start_sec=start_sec, duration_sec=duration_sec)

    engine_choice = (preferred_engine or "mdx_onnx").lower().strip()

    try:
        if engine_choice == "htdemucs" and has_demucs():
            return separate_with_demucs(work_wav, vocals_out, instrumental_out, progress_cb)
        elif engine_choice == "mdx_onnx" and has_mdx_onnx():
            return separate_with_mdx_onnx(work_wav, vocals_out, instrumental_out, progress_cb)
        elif has_mdx_onnx():
            return separate_with_mdx_onnx(work_wav, vocals_out, instrumental_out, progress_cb)
        elif has_demucs():
            return separate_with_demucs(work_wav, vocals_out, instrumental_out, progress_cb)
        else:
            return separate_with_ffmpeg_dsp(work_wav, vocals_out, instrumental_out, progress_cb)
    except Exception as ai_err:
        logger.error(f"Primary AI engine '{engine_choice}' failed: {ai_err}")
        # Try alternate neural engine before DSP fallback
        if engine_choice != "htdemucs" and has_demucs():
            logger.info("Trying Meta Demucs fallback...")
            return separate_with_demucs(work_wav, vocals_out, instrumental_out, progress_cb)
        if engine_choice != "mdx_onnx" and has_mdx_onnx():
            logger.info("Trying UVR MDX-Net ONNX fallback...")
            return separate_with_mdx_onnx(work_wav, vocals_out, instrumental_out, progress_cb)
        raise
    finally:
        work_wav.unlink(missing_ok=True)


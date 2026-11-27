#!/usr/bin/env python3
"""
On-Demand Laptop Launcher for OmniDownloader Studio.

Runs cleanly in the foreground only when launched; closing the window or pressing
Ctrl+C immediately stops all components with zero persistent background daemons.
"""

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def ensure_win_paths() -> None:
    if os.name != "nt":
        return
    extra_dirs = []
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        links = Path(local_app) / "Microsoft" / "WinGet" / "Links"
        if links.is_dir():
            extra_dirs.append(str(links))
        pkgs = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
        if pkgs.is_dir():
            for ff in pkgs.glob("Gyan.FFmpeg*/ffmpeg*/bin"):
                if ff.is_dir():
                    extra_dirs.append(str(ff))
    cur = os.environ.get("PATH", "")
    for d in extra_dirs:
        if d not in cur:
            cur = f"{d};{cur}"
    os.environ["PATH"] = cur


def _create_win_kill_job():
    """Creates a Windows Job Object so child processes die automatically if parent exits."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        JobObjectExtendedLimitInformation = 9
        kernel32.SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        return job
    except Exception:
        return None


def _assign_to_job(job, proc) -> None:
    if job and proc:
        try:
            ctypes.windll.kernel32.AssignProcessToJobObject(job, int(proc._handle))
        except Exception:
            pass


def wait_and_open_browser(url: str) -> None:
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            pass
        time.sleep(0.5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start OmniDownloader Studio on demand.")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser tab")
    parser.add_argument("--public", action="store_true", help="Also start temporary Cloudflare tunnel while open")
    args = parser.parse_args()

    ensure_win_paths()
    os.environ["HOST"] = args.host
    os.environ["PORT"] = str(args.port)

    venv_py = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    py_exe = str(venv_py) if venv_py.is_file() else sys.executable

    print("=" * 60)
    print("  OmniDownloader Studio (On-Demand Laptop Mode)")
    print(f"  Python : {py_exe}")
    print(f"  URL    : http://127.0.0.1:{args.port}")
    print("  Pages  : / (Downloader) | /8d | /stems (UVR5) | /dashboard")
    print("  Stop   : Press Ctrl+C or close this window anytime")
    print("=" * 60)

    if not args.no_browser:
        threading.Thread(
            target=wait_and_open_browser,
            args=(f"http://127.0.0.1:{args.port}",),
            daemon=True,
        ).start()

    tunnel_proc = None
    job = None
    try:
        if args.public:
            job = _create_win_kill_job()
            tunnel_proc = subprocess.Popen(
                [py_exe, str(BASE_DIR / "tunnel_manager.py")],
                cwd=str(BASE_DIR),
                env=dict(os.environ),
            )
            _assign_to_job(job, tunnel_proc)

        # Run Flask app directly in this foreground process for instant Ctrl+C / window close cleanup
        import server
        if not server._find_proot_distro():
            threading.Thread(target=server._init_inprocess_uvr5_on_laptop, daemon=True).start()
        server.app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
        return 0
    except KeyboardInterrupt:
        print("\nShutting down OmniDownloader Studio cleanly...")
        return 0
    finally:
        if tunnel_proc and tunnel_proc.poll() is None:
            try:
                tunnel_proc.terminate()
                tunnel_proc.wait(timeout=3)
            except Exception:
                try:
                    tunnel_proc.kill()
                except Exception:
                    pass
        if job:
            try:
                ctypes.windll.kernel32.CloseHandle(job)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())

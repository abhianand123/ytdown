#!/usr/bin/env python3
"""
OmniDownloader - Cloudflare Tunnel & GitHub Pages Bridge Manager (Self-Healing Edition)
- 100% Free, NO credit card needed
- Launches Cloudflare Tunnel (trycloudflare.com)
- Active Watchdog: Pings public tunnel every 30s to detect drops/disconnects
- If tunnel ever drops or phone changes Wi-Fi, automatically restarts & pushes new URL to GitHub
- Rotates before 24-hr limit (every 20 hours) or on phone reboot
"""

import os
import re
import sys
import json
import time
import shutil
import base64
import logging
import subprocess
import threading
from pathlib import Path
from datetime import datetime

# Ensure Termux bin directories are in PATH when executed via root / su / subshells
termux_bin = "/data/data/com.termux/files/usr/bin"
if os.path.isdir(termux_bin) and termux_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{termux_bin}:{os.environ.get('PATH', '')}"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BridgeManager")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "tunnel_config.json"
URL_FILE = BASE_DIR / "current_url.txt"

def load_config():
    defaults = {
        "github_repo": "abhianand123/ytdown",
        "github_token": os.environ.get("GITHUB_TOKEN", ""),
        "rotation_hours": 20,
        "port": 5000
    }
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            defaults.update(cfg)
        except Exception:
            pass
    return defaults

def update_github_pages(repo, token, active_url):
    """Updates url.txt in the GitHub repository using GitHub's REST API."""
    if not repo or not token:
        logger.info("ℹ GitHub repo/token not configured. Skipping GitHub Pages update.")
        return False
    try:
        import requests
        api_url = f"https://api.github.com/repos/{repo}/contents/url.txt"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "OmniDownloader-Phone-Bridge"
        }

        # Step 1: Get existing file sha (if it exists)
        sha = None
        get_res = requests.get(api_url, headers=headers, timeout=10)
        if get_res.status_code == 200:
            sha = get_res.json().get("sha")

        # Step 2: Push new URL
        content_b64 = base64.b64encode(active_url.strip().encode("utf-8")).decode("utf-8")
        payload = {
            "message": f"Auto-update tunnel URL to {active_url}",
            "content": content_b64
        }
        if sha:
            payload["sha"] = sha

        put_res = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if put_res.status_code in (200, 201):
            logger.info("=" * 60)
            logger.info("✔ GITHUB PAGES UPDATED SUCCESSFULLY!")
            logger.info(f"   Permanent Link: https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/")
            logger.info(f"   Now points to: {active_url}")
            logger.info("=" * 60)
            return True
        else:
            logger.warning(f"GitHub API update returned status {put_res.status_code}: {put_res.text}")
            return False
    except Exception as e:
        logger.warning(f"Failed to update GitHub Pages: {e}")
        return False

def check_tunnel_health(active_url):
    """Pings the public Cloudflare tunnel to verify it is reachable from the outside world."""
    if not active_url:
        return False
    try:
        import requests
        r = requests.get(f"{active_url}/api/health", timeout=15)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Tunnel health ping check failed: {e}")
        return False

def kill_cloudflared_only():
    """Terminates only cloudflared and its termux-chroot proot wrapper without disturbing Debian proot services."""
    import signal
    my_pid = os.getpid()
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == my_pid:
                continue
            try:
                cmd = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
                if "cloudflared tunnel" in cmd:
                    os.kill(pid, signal.SIGKILL)
            except Exception:
                continue
    except Exception:
        pass
    os.system("killall -9 cloudflared 2>/dev/null")

def run_tunnel_process(port):
    """Starts cloudflared with termux-chroot on Android or directly on PC."""
    is_android = "android" in sys.platform or os.path.exists("/data/data/com.termux")
    has_chroot = shutil.which("termux-chroot") is not None

    # Kill only stale cloudflared instances (preserve proot-distro Debian UVR5 service)
    if is_android:
        kill_cloudflared_only()
        time.sleep(1)

    if is_android and has_chroot:
        cmd = ["termux-chroot", "cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
    else:
        cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]

    logger.info(f"Starting Cloudflare tunnel: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )
    return proc

def watch_and_capture_url(proc, config):
    url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
    active_url = None

    for line in iter(proc.stdout.readline, ''):
        line = line.strip()
        if not line:
            continue
        if "api.trycloudflare.com" in line:
            continue
        match = url_pattern.search(line)
        if match and not active_url:
            active_url = match.group(0)
            logger.info("=" * 60)
            logger.info(f"🎉 TUNNEL ONLINE: {active_url}")
            logger.info("=" * 60)
            URL_FILE.write_text(active_url)
            # Update GitHub Pages
            update_github_pages(config.get("github_repo"), config.get("github_token"), active_url)

            # Drain remaining output in background so pipe buffer never fills
            def _drain():
                try:
                    for _ in iter(proc.stdout.readline, ''): pass
                except: pass
            threading.Thread(target=_drain, daemon=True).start()
            break

    return active_url

def main():
    config = load_config()
    port = config.get("port", 5000)
    rotation_seconds = int(config.get("rotation_hours", 20)) * 3600

    logger.info("OmniDownloader Self-Healing Bridge Manager started.")
    logger.info(f"Target GitHub Pages: {config.get('github_repo')}")

    while True:
        try:
            start_time = time.time()
            proc = run_tunnel_process(port)
            if not proc:
                time.sleep(10)
                continue

            active_url = watch_and_capture_url(proc, config)
            if not active_url:
                logger.error("Failed to capture tunnel URL. Retrying in 5s...")
                proc.kill()
                kill_cloudflared_only()
                time.sleep(5)
                continue

            # Give Cloudflare edge DNS 10 seconds to propagate globally
            time.sleep(10)

            fail_count = 0

            # Active Watchdog Loop: Checks health every 30 seconds
            while True:
                time.sleep(30)
                elapsed = time.time() - start_time
                if elapsed >= rotation_seconds:
                    logger.info("Rotation threshold reached (20 hours). Refreshing tunnel...")
                    break

                ret = proc.poll()
                if ret is not None:
                    logger.warning(f"Tunnel process died (exit {ret}). Re-spawning...")
                    break

                # Self-healing check: Verify the tunnel is reachable from the internet
                if not check_tunnel_health(active_url):
                    fail_count += 1
                    logger.warning(f"Public tunnel ping failed ({fail_count}/4)...")
                    if fail_count >= 4:
                        logger.error("Tunnel is down or de-registered! Forcing auto-restart...")
                        break
                else:
                    fail_count = 0

            # Cleanup before spawning new tunnel
            try:
                proc.kill()
            except:
                pass
            kill_cloudflared_only()
            time.sleep(2)

        except KeyboardInterrupt:
            logger.info("Stopping Bridge Manager...")
            try: proc.kill()
            except: pass
            kill_cloudflared_only()
            break
        except Exception as e:
            logger.error(f"Error in tunnel loop: {e}")
            kill_cloudflared_only()
            time.sleep(5)

if __name__ == "__main__":
    main()

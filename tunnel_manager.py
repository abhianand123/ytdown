#!/usr/bin/env python3
"""
OmniDownloader - Cloudflare Tunnel & GitHub Pages Bridge Manager
- 100% Free, NO credit card needed
- Launches Cloudflare Tunnel (trycloudflare.com)
- Automatically captures the new URL
- Updates 'url.txt' in your GitHub Pages repo via GitHub API
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
from pathlib import Path
from datetime import datetime

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
        "github_repo": "",          # e.g. "abhianand/ytdown"
        "github_token": "",         # GitHub Personal Access Token (starts with ghp_ or github_pat_)
        "rotation_hours": 20,       # Auto-refresh tunnel before 24h limit
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
            "message": f"Update tunnel URL to {active_url}",
            "content": content_b64
        }
        if sha:
            payload["sha"] = sha

        put_res = requests.put(api_url, headers=headers, json=payload, timeout=15)
        if put_res.status_code in (200, 201):
            logger.info("=" * 60)
            logger.info(f"✔ GITHUB PAGES UPDATED SUCCESSFULLY!")
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

def run_tunnel_process(port):
    """Starts cloudflared with termux-chroot on Android or directly on PC."""
    is_android = "android" in sys.platform or os.path.exists("/data/data/com.termux")
    has_chroot = shutil.which("termux-chroot") is not None

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
            break

    return active_url

def main():
    config = load_config()
    port = config.get("port", 5000)
    rotation_seconds = int(config.get("rotation_hours", 20)) * 3600

    logger.info("OmniDownloader Bridge Manager started.")
    if config.get("github_repo"):
        logger.info(f"Target GitHub Pages: {config['github_repo']}")
    else:
        logger.info("Tip: Set 'github_repo' and 'github_token' in tunnel_config.json for automatic redirect!")

    while True:
        try:
            start_time = time.time()
            proc = run_tunnel_process(port)
            if not proc:
                time.sleep(10)
                continue

            active_url = watch_and_capture_url(proc, config)

            # Monitor process until rotation time or crash
            while True:
                elapsed = time.time() - start_time
                if elapsed >= rotation_seconds:
                    logger.info("Rotation threshold reached (20 hours). Refreshing tunnel...")
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except: proc.kill()
                    break

                ret = proc.poll()
                if ret is not None:
                    logger.warning(f"Tunnel disconnected (exit {ret}). Re-spawning in 5s...")
                    time.sleep(5)
                    break

                time.sleep(5)

        except KeyboardInterrupt:
            logger.info("Stopping Bridge Manager...")
            if 'proc' in locals() and proc and proc.poll() is None:
                proc.terminate()
            break
        except Exception as e:
            logger.error(f"Error in tunnel loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()

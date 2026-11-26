#!/data/data/com.termux/files/usr/bin/bash
# ══════════════════════════════════════════════════════════════════
#  Termux:Boot — Auto-start OmniDownloader + Localtunnel on reboot
# ══════════════════════════════════════════════════════════════════

set -e

BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR"

APP_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

AUTOSTART="$BOOT_DIR/start-ytdown.sh"

cat << 'EOF' > "$AUTOSTART"
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
# Wait 15 seconds after Android finishes booting so Wi-Fi is fully connected
sleep 15

export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export PATH=$PREFIX/bin:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
export SSL_CERT_FILE=$PREFIX/etc/tls/cert.pem
export SSL_CERT_DIR=$PREFIX/etc/ssl/certs

# Kill any stale zombie processes (keep proot-distro Debian intact)
pkill -f "python.*server.py" 2>/dev/null || true
pkill -f "python.*tunnel_manager.py" 2>/dev/null || true
pkill -9 -f "[c]loudflared tunnel" 2>/dev/null || true
killall -9 cloudflared 2>/dev/null || true
sleep 1

APP_DIR="REPLACE_APP_DIR"
cd "$APP_DIR"
nohup python server.py > server.log 2>&1 &
sleep 2
nohup python tunnel_manager.py > tunnel.log 2>&1 &
EOF

sed -i "s|REPLACE_APP_DIR|$APP_DIR|g" "$AUTOSTART"
chmod +x "$AUTOSTART"

echo ""
echo "✔ Boot script created: $AUTOSTART"
echo "✔ Install 'Termux:Boot' from F-Droid and open it once."
echo "✔ Your server + tunnel will auto-start on every phone reboot!"
echo ""

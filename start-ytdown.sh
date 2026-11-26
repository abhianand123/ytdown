#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
sleep 15

export PREFIX=/data/data/com.termux/files/usr
export HOME=/data/data/com.termux/files/home
export PATH=$PREFIX/bin:$PATH
export LD_LIBRARY_PATH=$PREFIX/lib
export SSL_CERT_FILE=$PREFIX/etc/tls/cert.pem
export SSL_CERT_DIR=$PREFIX/etc/ssl/certs

pkill -f "python.*server.py" 2>/dev/null || true
pkill -f "python.*tunnel_manager.py" 2>/dev/null || true
pkill -9 -f "[c]loudflared tunnel" 2>/dev/null || true
killall -9 cloudflared 2>/dev/null || true
sleep 1

cd $HOME/ytdown
nohup python server.py > server.log 2>&1 &
sleep 2
nohup python tunnel_manager.py > tunnel.log 2>&1 &
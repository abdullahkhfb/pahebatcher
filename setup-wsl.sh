#!/bin/bash
# System provisioning script for Ubuntu/WSL — WSL + native Linux (Ubuntu/Debian/Mint)
# Idempotent: safe to re-run. Requires sudo for apt/wsl.conf.
# Original idea + initial Makefile/setup-wsl.sh by u/HellHarbinger on Reddit (https://www.reddit.com/user/HellHarbinger/) — vibecoded with Gemini, refined by maintainer.

set -euo pipefail

echo "==> Setting up system prerequisites..."

# 1. Install system dependencies
echo "==> Installing apt packages (ffmpeg, python3-venv)..."
# util-linux-extra was previously listed but is unused by pahebatcher — not installed.
sudo apt-get update
sudo apt-get install -y make ffmpeg curl python3-venv

# 2. Apply WSL NTFS metadata fix (only if running on a mounted Windows drive)
# Needed so venv symlinks + exec bits work on /mnt/c, /mnt/d, etc.
if grep -qi microsoft /proc/version 2>/dev/null && pwd | grep -q "^/mnt/"; then
    WSL_CONF="/etc/wsl.conf"
    NEED_FIX=false
    if [ ! -f "$WSL_CONF" ]; then
        NEED_FIX=true
    elif ! grep -q "^\[automount\]" "$WSL_CONF" 2>/dev/null; then
        NEED_FIX=true
    elif ! grep -q '^\s*options\s*=\s*".*metadata.*"' "$WSL_CONF" 2>/dev/null; then
        NEED_FIX=true
    fi

    if [ "$NEED_FIX" = true ]; then
        echo "==> Applying WSL NTFS metadata fix for Windows drives..."
        if [ -f "$WSL_CONF" ]; then
            echo "    Backing up $WSL_CONF -> ${WSL_CONF}.bak"
            sudo cp -n "$WSL_CONF" "${WSL_CONF}.bak" 2>/dev/null || true
        fi
        # Ensure [automount] section exists
        if ! grep -q "^\[automount\]" "$WSL_CONF" 2>/dev/null; then
            echo "[automount]" | sudo tee -a "$WSL_CONF" >/dev/null
        fi
        # Ensure metadata option is present inside [automount]
        if ! grep -q '^\s*options\s*=\s*".*metadata.*"' "$WSL_CONF" 2>/dev/null; then
            # Append or replace: simplest is to ensure the options line exists; sed is idempotent via check above
            echo 'options = "metadata,umask=22,fmask=11"' | sudo tee -a "$WSL_CONF" >/dev/null
        fi

        echo ""
        echo "================================================================="
        echo "  WSL CONFIGURED. A RESTART IS REQUIRED TO APPLY PERMISSIONS!  "
        echo "================================================================="
        if [ -t 0 ]; then
            read -r -p "Press [Enter] to shutdown WSL now, or [Ctrl+C] to cancel and do it later... " _ || true
            if command -v wsl.exe >/dev/null 2>&1; then
                wsl.exe --shutdown
            else
                echo "  (wsl.exe not found — please restart WSL manually: close all WSL terminals and re-open)"
            fi
            exit 0
        else
            echo "  Non-interactive shell — skipping WSL shutdown. Please restart WSL manually."
            echo "  (close all WSL terminals or run: wsl.exe --shutdown  from PowerShell)"
        fi
    fi
fi

# 3. Verify
echo ""
echo "==> Verifying installs..."
python3 --version 2>/dev/null && echo "  python3: OK" || echo "  python3: MISSING"
ffmpeg -version 2>/dev/null | head -n1 && echo "  ffmpeg: OK" || echo "  ffmpeg: MISSING (sudo apt install ffmpeg)"
make --version 2>/dev/null | head -n1 && echo "  make: OK" || echo "  make: MISSING"

echo ""
echo "==> Setup complete!"
echo "  Next: make run"
echo "  Note: the Cloudflare solver drives Chrome or Edge, so install one in WSL (e.g. google-chrome-stable)"
echo "        or set PAHEBATCHER_BROWSER to a browser executable."

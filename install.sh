#!/usr/bin/env bash
# Install frostframe for the current user (no pip needed):
#   ./install.sh            install / update
#   ./install.sh --service  also enable the live-labels background service
#   ./install.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LIB="${XDG_DATA_HOME:-$HOME/.local/share}/frostframe"
BIN="$HOME/.local/bin/frostframe"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
RULE="/etc/udev/rules.d/70-frostframe.rules"

if [ "${1:-}" = "--uninstall" ]; then
    systemctl --user disable --now frostframe-monitor.service 2>/dev/null || true
    rm -f "$UNIT_DIR/frostframe-monitor.service" "$BIN"
    rm -rf "$LIB"
    [ -f "$RULE" ] && sudo rm -f "$RULE" && sudo udevadm control --reload
    echo "frostframe uninstalled (your settings in ~/.config/frostframe were kept)."
    exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v ffmpeg >/dev/null || echo "Note: ffmpeg is not installed; it's needed to upload media (e.g. sudo apt install ffmpeg)."

echo "==> Installing to $LIB"
rm -rf "$LIB"
mkdir -p "$LIB" "$(dirname "$BIN")"
cp -r "$HERE/src/frostframe" "$LIB/"
cat >"$BIN" <<EOF
#!/bin/sh
exec python3 -I -c "import sys; sys.path.insert(0, '$LIB'); from frostframe.cli import main; sys.exit(main(sys.argv[1:]))" "\$@"
EOF
chmod +x "$BIN"

if ! cmp -s "$HERE/packaging/70-frostframe.rules" "$RULE" 2>/dev/null; then
    echo "==> Installing udev rule $RULE (needs sudo)"
    sudo install -m 0644 "$HERE/packaging/70-frostframe.rules" "$RULE"
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=usb --subsystem-match=usbmisc
fi

if [ "${1:-}" = "--service" ]; then
    echo "==> Enabling the live-labels service"
    mkdir -p "$UNIT_DIR"
    cp "$HERE/packaging/frostframe-monitor.service" "$UNIT_DIR/"
    systemctl --user daemon-reload
    systemctl --user enable --now frostframe-monitor.service
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: add ~/.local/bin to your PATH";; esac
echo "==> Done. Try: frostframe doctor"

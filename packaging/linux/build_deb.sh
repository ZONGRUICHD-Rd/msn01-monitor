#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

PKG_NAME="msn01-monitor"
PKG_ROOT="${STAGE_DIR}/${PKG_NAME}_${VERSION}_all"

mkdir -p \
  "${PKG_ROOT}/DEBIAN" \
  "${PKG_ROOT}/usr/bin" \
  "${PKG_ROOT}/usr/lib/msn01-monitor/msn01" \
  "${PKG_ROOT}/usr/share/applications" \
  "${PKG_ROOT}/usr/share/icons/hicolor/256x256/apps" \
  "${PKG_ROOT}/usr/lib/udev/rules.d" \
  "${PKG_ROOT}/usr/lib/systemd/user" \
  "${DIST_DIR}"

# Copy Python application files
cp "${ROOT_DIR}/main.py" "${PKG_ROOT}/usr/lib/msn01-monitor/main.py"
cp "${ROOT_DIR}"/msn01/*.py "${PKG_ROOT}/usr/lib/msn01-monitor/msn01/"
chmod 0755 "${PKG_ROOT}/usr/lib/msn01-monitor/main.py"

# CLI / GUI launcher
cat > "${PKG_ROOT}/usr/bin/msn01-monitor" <<'EOF'
#!/usr/bin/env bash
exec python3 /usr/lib/msn01-monitor/main.py "$@"
EOF
chmod 0755 "${PKG_ROOT}/usr/bin/msn01-monitor"

# Desktop file, icon, udev rule, systemd user unit
cp "${ROOT_DIR}/packaging/linux/msn01-monitor.desktop" \
   "${PKG_ROOT}/usr/share/applications/msn01-monitor.desktop"
cp "${ROOT_DIR}/assets/app.png" \
   "${PKG_ROOT}/usr/share/icons/hicolor/256x256/apps/msn01-monitor.png"
cp "${ROOT_DIR}/linux/99-msn01-screen.rules" \
   "${PKG_ROOT}/usr/lib/udev/rules.d/99-msn01-screen.rules"

cat > "${PKG_ROOT}/usr/lib/systemd/user/msn01-monitor.service" <<'EOF'
[Unit]
Description=MSN01 USB Mini Screen CPU/GPU/RAM Monitor (Headless)
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/msn01-monitor --cli --theme cyber_bars --fps 2
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

# Debian control metadata
cat > "${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Maintainer: ZONGRUICHD <98888228+ZONGRUICHD@users.noreply.github.com>
Depends: python3 (>= 3.9), python3-serial, python3-pil, python3-psutil
Recommends: python3-tk, python3-pil.imagetk, python3-numpy
Description: Cross-platform CPU/GPU/RAM monitor host for MSN01 USB mini screen
 Real-time hardware monitoring host application (GUI and headless CLI)
 for MSN01 / CH32x035 (VID:1A86 PID:FE0C) USB serial mini displays.
 Includes udev rules for non-root serial access and systemd user service.
EOF

cat > "${PKG_ROOT}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules >/dev/null 2>&1 || true
    udevadm trigger --subsystem-match=tty --attr-match=idVendor=1a86 >/dev/null 2>&1 || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi
exit 0
EOF
chmod 0755 "${PKG_ROOT}/DEBIAN/postinst"

cat > "${PKG_ROOT}/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 0755 "${PKG_ROOT}/DEBIAN/postrm"

OUT_DEB="${DIST_DIR}/${PKG_NAME}_${VERSION}_all.deb"
dpkg-deb --build --root-owner-group "${PKG_ROOT}" "${OUT_DEB}"
echo "Built Debian package: ${OUT_DEB}"
dpkg-deb --info "${OUT_DEB}"

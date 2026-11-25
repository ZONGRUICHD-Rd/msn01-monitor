Name:           msn01-monitor
Version:        %{_pkg_version}
Release:        1%{?dist}
Summary:        Real-time CPU/GPU/RAM monitor host for MSN01 USB mini screen
License:        MIT
URL:            https://github.com/ZONGRUICHD-Rd/msn01-monitor
BuildArch:      noarch

Requires:       python3 >= 3.9
Requires:       python3-pyserial
Requires:       python3-pillow
Requires:       python3-psutil
Recommends:     python3-tkinter
Recommends:     python3-pillow-tk
Recommends:     python3-numpy

%description
Cross-platform CPU, GPU, and RAM real-time hardware monitor host application
(desktop GUI and headless CLI daemon) for MSN01 / CH32x035 (VID:1A86 PID:FE0C)
USB serial mini displays. Includes non-root udev rules and systemd user service.

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/usr/bin
mkdir -p %{buildroot}/usr/lib/msn01-monitor/msn01
mkdir -p %{buildroot}/usr/share/applications
mkdir -p %{buildroot}/usr/share/icons/hicolor/256x256/apps
mkdir -p %{buildroot}/usr/lib/udev/rules.d
mkdir -p %{buildroot}/usr/lib/systemd/user

install -m 0755 %{_src_root}/main.py %{buildroot}/usr/lib/msn01-monitor/main.py
install -m 0644 %{_src_root}/msn01/*.py %{buildroot}/usr/lib/msn01-monitor/msn01/

cat > %{buildroot}/usr/bin/msn01-monitor <<'EOF'
#!/usr/bin/env bash
exec python3 /usr/lib/msn01-monitor/main.py "$@"
EOF
chmod 0755 %{buildroot}/usr/bin/msn01-monitor

install -m 0644 %{_src_root}/packaging/linux/msn01-monitor.desktop \
    %{buildroot}/usr/share/applications/msn01-monitor.desktop
install -m 0644 %{_src_root}/assets/app.png \
    %{buildroot}/usr/share/icons/hicolor/256x256/apps/msn01-monitor.png
install -m 0644 %{_src_root}/linux/99-msn01-screen.rules \
    %{buildroot}/usr/lib/udev/rules.d/99-msn01-screen.rules

cat > %{buildroot}/usr/lib/systemd/user/msn01-monitor.service <<'EOF'
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

%post
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules >/dev/null 2>&1 || :
    udevadm trigger --subsystem-match=tty --attr-match=idVendor=1a86 >/dev/null 2>&1 || :
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || :
fi

%postun
if command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules >/dev/null 2>&1 || :
fi

%files
/usr/bin/msn01-monitor
/usr/lib/msn01-monitor/
/usr/share/applications/msn01-monitor.desktop
/usr/share/icons/hicolor/256x256/apps/msn01-monitor.png
/usr/lib/udev/rules.d/99-msn01-screen.rules
/usr/lib/systemd/user/msn01-monitor.service

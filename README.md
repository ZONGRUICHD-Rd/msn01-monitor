# MSN01 System Monitor — USB 迷你小屏幕 CPU / GPU / RAM 监控上位机

面向 **MSN01 / CH32x035 (`VID:1A86 PID:FE0C`)** 0.96 寸 (`160×80`) 及 1.47 寸 (`320×172`) USB 串口小屏幕的跨平台实时硬件监控与上位机软件：
* **Windows 版**：原生 **WinUI 3 (.NET 8 + Windows App SDK + SkiaSharp)**，支持 Windows 11 Mica 材质、自定义标题栏与 Inno Setup `Setup.exe` 安装包。
* **Linux 版**：跨平台 Python GUI（Tkinter）+ 无界面 CLI / systemd 守护进程模式，提供 **Debian/Ubuntu `.deb`** 与 **RedHat/CentOS/Rocky/Fedora `.rpm` (`yum`/`dnf`)** 安装包，内置 udev 免 root 串口规则。

---

## 核心特性

* **零管理员权限 CPU / GPU / RAM 实时监控**
  * **Windows**：内置 `NVML (nvml.dll)` + `DXGI/PDH` 双后端，原生支持 NVIDIA / AMD / Intel 显卡利用率与显存。
  * **Linux**：支持 `libnvidia-ml.so.1` / `nvidia-smi`、AMD/Intel `/sys/class/drm` sysfs 显卡指标与温度传感器。
* **4 套高对比度仪表盘主题**
  * `cyber_bars` — 赛博分段进度条
  * `spark_graph` — 实时三通道折线波形图
  * `triple_gauge` — 2× 超采样抗锯齿三连环形仪表盘
  * `clock_hud` — 顶部时钟 + 三联大数字卡片
* **高速差分压缩协议 + 硬件触摸键切换**：采用 MSN01 硬件填充+差分补丁帧编码；支持 3× 实时预览、断线自动重连、0.5~10 Hz 刷新率、软件亮度、180° 旋转以及轻触屏幕机身按键循环切换主题。

---

## 安装与使用

前往本仓库 **[Releases](https://github.com/ZONGRUICHD-Rd/msn01-monitor/releases)** 下载对应安装包：

### Windows（WinUI 3 原生版）
运行 `MSN01-Monitor-Setup-1.0.0-win-x64.exe` 按向导安装即可（自包含 .NET 8 与 Windows App SDK，支持可选开机自启）。

### Debian / Ubuntu (`.deb`)
```bash
sudo apt install ./msn01-monitor_1.0.0_all.deb

# 桌面模式启动
msn01-monitor

# 无界面服务器模式
msn01-monitor --cli --theme cyber_bars --fps 2
```

### RedHat / Rocky / CentOS / Fedora (`.rpm` / `yum` / `dnf`)
```bash
sudo dnf install ./msn01-monitor-1.0.0-1.noarch.rpm
# 或旧版系统使用 yum：
sudo yum localinstall ./msn01-monitor-1.0.0-1.noarch.rpm

msn01-monitor
```

### Linux 开机自启（无界面 systemd 用户服务）
安装 `.deb` 或 `.rpm` 后已内置 unit 与 udev 规则：
```bash
systemctl --user enable --now msn01-monitor.service
```

---

## 从源码构建

### Windows WinUI 3
```powershell
dotnet publish windows/MSN01Monitor.WinUI/MSN01Monitor.WinUI.csproj `
  -c Release -r win-x64 -p:Platform=x64 -p:SelfContained=true `
  -p:WindowsAppSDKSelfContained=true -p:WindowsPackageType=None -o publish/win-x64
```

### Linux `.deb` & `.rpm`
```bash
./packaging/linux/build_deb.sh 1.0.0
./packaging/linux/build_rpm.sh 1.0.0
```

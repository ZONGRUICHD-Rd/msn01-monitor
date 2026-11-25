using System.Diagnostics;
using System.Runtime.InteropServices.WindowsRuntime;
using Microsoft.UI;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Controls.Primitives;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using MSN01Monitor.WinUI.Core;
using Windows.Graphics;
using Windows.UI;

namespace MSN01Monitor.WinUI;

public partial class MainWindow : Window
{
    private const int TouchThreshold = 3600;
    private const double TouchCooldownSec = 0.7;

    private readonly object _cfgLock = new();
    private readonly AppConfig _cfg;
    private readonly MSN01Screen _screen = new();
    private readonly SystemTelemetry _telemetry = new();
    private readonly DashboardRenderer _renderer = new();
    private readonly WriteableBitmap _previewBitmap = new(480, 240);
    private readonly CancellationTokenSource _cts = new();

    private readonly List<string> _portValues = [];
    private readonly List<(string Id, string Label)> _gpuItems = [("auto", "自动选择 (Auto)")];

    private bool _syncingUi = true;
    private volatile bool _wantConnected;
    private volatile bool _forceReconnect;

    public MainWindow()
    {
        InitializeComponent();

        // Native WinUI 3 title bar & crisp window sizing
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.Resize(new SizeInt32(960, 620));
        TrySetWindowIcon();

        PreviewImage.Source = _previewBitmap;

        _cfg = ConfigStore.Load();
        _wantConnected = _cfg.AutoConnect;

        PopulateInitialControls();
        RefreshPorts();

        _syncingUi = false;

        Closed += OnWindowClosed;
        Task.Run(() => WorkerLoop(_cts.Token));
    }

    private void TrySetWindowIcon()
    {
        try
        {
            var ico = Path.Combine(AppContext.BaseDirectory, "Assets", "app.ico");
            if (File.Exists(ico))
                AppWindow.SetIcon(ico);
        }
        catch { }
    }

    private void PopulateInitialControls()
    {
        ThemeCombo.Items.Clear();
        int themeIdx = 0;
        for (int i = 0; i < DashboardRenderer.Themes.Count; i++)
        {
            ThemeCombo.Items.Add(DashboardRenderer.Themes[i].Name);
            if (DashboardRenderer.Themes[i].Id == _cfg.Theme) themeIdx = i;
        }
        ThemeCombo.SelectedIndex = themeIdx;

        GpuCombo.Items.Clear();
        GpuCombo.Items.Add("自动选择 (Auto)");
        GpuCombo.SelectedIndex = 0;

        FpsSlider.Value = _cfg.RefreshHz;
        FpsValueText.Text = $"{_cfg.RefreshHz:F1} Hz";
        BrightSlider.Value = _cfg.Brightness;
        BrightValueText.Text = $"{_cfg.Brightness}%";
        RotateToggle.IsOn = _cfg.Rotate180;
        TouchToggle.IsOn = _cfg.TouchSwitchTheme;
        AutoReconnectToggle.IsOn = _cfg.AutoReconnect;
        ConnectButton.Content = _wantConnected ? "断开连接" : "连接并开始监控";
    }

    private void RefreshPorts()
    {
        bool prevSync = _syncingUi;
        _syncingUi = true;
        try
        {
            var ports = MSN01Screen.ListSerialPorts();
            _portValues.Clear();
            PortCombo.Items.Clear();

            _portValues.Add("auto");
            PortCombo.Items.Add("自动检测 (Auto)");

            int selIdx = 0;
            for (int i = 0; i < ports.Count; i++)
            {
                _portValues.Add(ports[i].Device);
                PortCombo.Items.Add(ports[i].Label);
                if (ports[i].Device.Equals(_cfg.Port, StringComparison.OrdinalIgnoreCase))
                    selIdx = i + 1;
            }
            PortCombo.SelectedIndex = selIdx;
        }
        finally
        {
            _syncingUi = prevSync;
        }
    }

    // ---- UI Event Handlers ----
    private void OnRefreshPortsClick(object sender, RoutedEventArgs e) => RefreshPorts();

    private void OnPortSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_syncingUi) return;
        int idx = PortCombo.SelectedIndex;
        if (idx < 0 || idx >= _portValues.Count) return;
        string dev = _portValues[idx];
        lock (_cfgLock)
        {
            if (_cfg.Port == dev) return;
            _cfg.Port = dev;
            ConfigStore.Save(_cfg);
        }
        if (_wantConnected) _forceReconnect = true;
    }

    private void OnToggleConnectClick(object sender, RoutedEventArgs e)
    {
        if (_wantConnected)
        {
            _wantConnected = false;
            _forceReconnect = false;
            lock (_cfgLock)
            {
                _cfg.AutoConnect = false;
                ConfigStore.Save(_cfg);
            }
            _screen.Close();
            ConnectButton.Content = "连接并开始监控";
        }
        else
        {
            _wantConnected = true;
            _forceReconnect = true;
            lock (_cfgLock)
            {
                _cfg.AutoConnect = true;
                ConfigStore.Save(_cfg);
            }
            ConnectButton.Content = "断开连接";
        }
    }

    private void OnGpuSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_syncingUi) return;
        int idx = GpuCombo.SelectedIndex;
        if (idx < 0 || idx >= _gpuItems.Count) return;
        lock (_cfgLock)
        {
            _cfg.GpuId = _gpuItems[idx].Id;
            ConfigStore.Save(_cfg);
        }
    }

    private void OnThemeSelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_syncingUi) return;
        int idx = ThemeCombo.SelectedIndex;
        if (idx < 0 || idx >= DashboardRenderer.Themes.Count) return;
        lock (_cfgLock)
        {
            _cfg.Theme = DashboardRenderer.Themes[idx].Id;
            ConfigStore.Save(_cfg);
        }
    }

    private void OnFpsChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_syncingUi) return;
        double hz = Math.Round(e.NewValue * 2.0) / 2.0;
        FpsValueText.Text = $"{hz:F1} Hz";
        lock (_cfgLock)
        {
            _cfg.RefreshHz = hz;
            ConfigStore.Save(_cfg);
        }
    }

    private void OnBrightnessChanged(object sender, RangeBaseValueChangedEventArgs e)
    {
        if (_syncingUi) return;
        int br = (int)Math.Round(e.NewValue);
        BrightValueText.Text = $"{br}%";
        lock (_cfgLock)
        {
            _cfg.Brightness = br;
            ConfigStore.Save(_cfg);
        }
    }

    private void OnSettingsToggled(object sender, RoutedEventArgs e)
    {
        if (_syncingUi) return;
        lock (_cfgLock)
        {
            _cfg.Rotate180 = RotateToggle.IsOn;
            _cfg.TouchSwitchTheme = TouchToggle.IsOn;
            _cfg.AutoReconnect = AutoReconnectToggle.IsOn;
            ConfigStore.Save(_cfg);
        }
    }

    // ---- Background Telemetry + Serial Push Worker ----
    private void WorkerLoop(CancellationToken ct)
    {
        string statusText = "准备就绪";
        string statusLevel = "idle";
        double lastFrameMs = 0;
        int lastFrameBytes = 0;
        int? lastTouchAdc = null;
        bool wasTouched = false;
        long lastTouchTick = 0;
        long nextReconnectTick = 0;

        while (!ct.IsCancellationRequested)
        {
            var loopSw = Stopwatch.StartNew();
            AppConfig cfg;
            lock (_cfgLock) cfg = _cfg.Clone();

            bool want = _wantConnected;
            bool forceReconn = _forceReconnect;
            _forceReconnect = false;

            if (!want)
            {
                if (_screen.IsConnected) _screen.Close();
                statusText = "已断开（离线预览中）";
                statusLevel = "idle";
                lastTouchAdc = null;
            }
            else
            {
                if (forceReconn && _screen.IsConnected) _screen.Close();

                long nowTick = Environment.TickCount64;
                if (!_screen.IsConnected && nowTick >= nextReconnectTick)
                {
                    try
                    {
                        statusText = $"正在连接 ({cfg.Port})...";
                        statusLevel = "warn";
                        var (w, h) = _screen.Connect(cfg.Port);
                        statusText = $"已连接 {_screen.PortName} ({w}×{h})";
                        statusLevel = "ok";
                    }
                    catch (Exception ex)
                    {
                        statusText = $"连接失败: {ex.Message}";
                        statusLevel = "err";
                        if (cfg.AutoReconnect)
                            nextReconnectTick = Environment.TickCount64 + 2500;
                        else
                            _wantConnected = false;
                    }
                }
            }

            // Poll capacitive touch sensor
            if (_screen.IsConnected && cfg.TouchSwitchTheme)
            {
                int? adc = _screen.ReadTouchAdc();
                lastTouchAdc = adc;
                if (adc.HasValue)
                {
                    bool touched = adc.Value < TouchThreshold;
                    long nowTick = Environment.TickCount64;
                    if (touched && !wasTouched && (nowTick - lastTouchTick) >= (long)(TouchCooldownSec * 1000))
                    {
                        string nextTheme = DashboardRenderer.NextTheme(cfg.Theme);
                        lock (_cfgLock)
                        {
                            _cfg.Theme = nextTheme;
                            ConfigStore.Save(_cfg);
                        }
                        cfg.Theme = nextTheme;
                        lastTouchTick = nowTick;
                    }
                    wasTouched = touched;
                }
            }

            // Sample telemetry and render
            var snap = _telemetry.Sample(cfg.GpuId);
            int targetW = _screen.IsConnected ? _screen.Width : 160;
            int targetH = _screen.IsConnected ? _screen.Height : 80;
            var (rgb565, previewBgra) = _renderer.Render(
                snap, cfg.Theme, targetW, targetH, cfg.Brightness, cfg.Rotate180);

            if (_screen.IsConnected)
            {
                var pushSw = Stopwatch.StartNew();
                try
                {
                    lastFrameBytes = _screen.PushRgb565Frame(rgb565);
                    lastFrameMs = pushSw.Elapsed.TotalMilliseconds;
                    statusText = $"已连接 {_screen.PortName} ({_screen.Width}×{_screen.Height})";
                    statusLevel = "ok";
                }
                catch (Exception ex)
                {
                    _screen.Close();
                    statusText = $"通信中断: {ex.Message}";
                    statusLevel = "err";
                    if (cfg.AutoReconnect)
                        nextReconnectTick = Environment.TickCount64 + 2000;
                    else
                        _wantConnected = false;
                }
            }

            bool connectedNow = _screen.IsConnected;
            string portLabel = _screen.PortName ?? "--";
            string st = statusText;
            string sl = statusLevel;
            double fms = lastFrameMs;
            int fbytes = lastFrameBytes;
            int? tadc = lastTouchAdc;
            string activeTheme = cfg.Theme;
            double activeFps = cfg.RefreshHz;

            DispatcherQueue.TryEnqueue(() =>
            {
                if (ct.IsCancellationRequested) return;
                ApplyUiUpdate(snap, previewBgra, connectedNow, portLabel, targetW, targetH, st, sl, fms, fbytes, tadc, activeTheme, activeFps);
            });

            int intervalMs = (int)Math.Round(1000.0 / Math.Max(0.5, cfg.RefreshHz));
            int sleepMs = Math.Max(20, intervalMs - (int)loopSw.ElapsedMilliseconds);
            try { Task.Delay(sleepMs, ct).Wait(ct); } catch (OperationCanceledException) { break; }
        }
    }

    private void ApplyUiUpdate(
        SystemSnapshot snap,
        byte[] previewBgra,
        bool connected,
        string portLabel,
        int w,
        int h,
        string statusText,
        string statusLevel,
        double frameMs,
        int frameBytes,
        int? touchAdc,
        string activeTheme,
        double activeFps)
    {
        // Update 3x preview WriteableBitmap
        using (var stream = _previewBitmap.PixelBuffer.AsStream())
        {
            stream.Seek(0, SeekOrigin.Begin);
            stream.Write(previewBgra, 0, previewBgra.Length);
        }
        _previewBitmap.Invalidate();

        // Sync GPU ComboBox choices if changed
        var nextGpuItems = new List<(string Id, string Label)> { ("auto", "自动选择 (Auto)") };
        foreach (var g in snap.Gpus)
            nextGpuItems.Add((g.Id, $"{g.ShortName} [{g.Source}]"));

        if (!nextGpuItems.SequenceEqual(_gpuItems))
        {
            _syncingUi = true;
            _gpuItems.Clear();
            _gpuItems.AddRange(nextGpuItems);
            GpuCombo.Items.Clear();
            int sel = 0;
            string curGpuId;
            lock (_cfgLock) curGpuId = _cfg.GpuId;
            for (int i = 0; i < _gpuItems.Count; i++)
            {
                GpuCombo.Items.Add(_gpuItems[i].Label);
                if (_gpuItems[i].Id == curGpuId) sel = i;
            }
            GpuCombo.SelectedIndex = sel;
            _syncingUi = false;
        }

        // Sync Theme ComboBox if touch sensor changed it
        for (int i = 0; i < DashboardRenderer.Themes.Count; i++)
        {
            if (DashboardRenderer.Themes[i].Id == activeTheme && ThemeCombo.SelectedIndex != i)
            {
                _syncingUi = true;
                ThemeCombo.SelectedIndex = i;
                _syncingUi = false;
                break;
            }
        }

        // Update hero cards
        CpuValText.Text = $"{snap.CpuPercent:F0}%";
        CpuBar.Value = snap.CpuPercent;
        CpuSubText.Text = snap.CpuFreqMhz.HasValue
            ? $"{snap.CpuFreqMhz.Value / 1000.0:F2} GHz · {snap.CpuLogicalCores}T"
            : $"{snap.CpuLogicalCores} 线程";

        if (snap.SelectedGpu is { } gpu)
        {
            GpuValText.Text = $"{gpu.UtilPercent:F0}%";
            GpuBar.Value = gpu.UtilPercent;
            var sub = gpu.ShortName;
            if (gpu.TempC.HasValue) sub += $" · {gpu.TempC:F0}°C";
            else if (!string.IsNullOrEmpty(gpu.VramText)) sub += $" · {gpu.VramText}";
            GpuSubText.Text = sub;
        }
        else
        {
            GpuValText.Text = "N/A";
            GpuBar.Value = 0;
            GpuSubText.Text = "未检测到 GPU 指标";
        }

        RamValText.Text = $"{snap.RamPercent:F0}%";
        RamBar.Value = snap.RamPercent;
        RamSubText.Text = $"{snap.RamUsedGb:F1} / {snap.RamTotalGb:F1} GB";

        // Status pill & button
        StatusBadgeText.Text = statusText;
        Color dotColor = statusLevel switch
        {
            "ok" => Color.FromArgb(255, 16, 185, 129),
            "warn" => Color.FromArgb(255, 245, 158, 11),
            "err" => Color.FromArgb(255, 239, 68, 68),
            _ => Color.FromArgb(255, 148, 163, 184),
        };
        StatusDot.Fill = new SolidColorBrush(dotColor);

        ConnectButton.Content = _wantConnected ? "断开连接" : "连接并开始监控";
        PerfText.Text = connected
            ? $"{w}×{h} · {frameMs:F1}ms · {frameBytes / 1024.0:F1}KB"
            : $"{w}×{h} · 离线预览";

        TouchAdcText.Text = touchAdc.HasValue
            ? $"触摸传感器 ADC: {touchAdc.Value}"
            : "触摸传感器 ADC: --";

        FooterText.Text = $"端口: {portLabel}   |   主题: {activeTheme}   |   刷新率: {activeFps:F1} Hz";
    }

    private void OnWindowClosed(object sender, WindowEventArgs args)
    {
        _cts.Cancel();
        lock (_cfgLock) ConfigStore.Save(_cfg);
        _screen.Dispose();
        _telemetry.Dispose();
        _renderer.Dispose();
    }
}

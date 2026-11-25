using SkiaSharp;

namespace MSN01Monitor.WinUI.Core;

public sealed record ThemeInfo(string Id, string Name);

public sealed class DashboardRenderer : IDisposable
{
    public static readonly IReadOnlyList<ThemeInfo> Themes =
    [
        new("cyber_bars", "赛博进度条 (Cyber Bars)"),
        new("spark_graph", "实时折线图 (Telemetry Graph)"),
        new("triple_gauge", "三连仪表盘 (Triple Gauges)"),
        new("clock_hud", "时钟看板 (Clock HUD)"),
    ];

    private static readonly SKColor BgColor     = new(7, 11, 20);
    private static readonly SKColor CardBg      = new(14, 21, 37);
    private static readonly SKColor CardBorder  = new(26, 40, 68);
    private static readonly SKColor TrackBg     = new(24, 36, 58);
    private static readonly SKColor TextBright  = new(244, 248, 255);
    private static readonly SKColor TextMuted   = new(142, 163, 194);
    private static readonly SKColor TextDim     = new(92, 112, 144);

    private static readonly SKColor ColorCpu    = new(0, 229, 255);
    private static readonly SKColor ColorGpu    = new(34, 245, 155);
    private static readonly SKColor ColorRam    = new(255, 176, 32);
    private static readonly SKColor ColorWarn   = new(255, 138, 0);
    private static readonly SKColor ColorCrit   = new(255, 59, 92);

    private const int HistoryLen = 46;
    private readonly double[] _cpuHist = new double[HistoryLen];
    private readonly double[] _gpuHist = new double[HistoryLen];
    private readonly double[] _ramHist = new double[HistoryLen];

    private readonly SKTypeface _tfBold;
    private readonly SKTypeface _tfReg;
    private readonly SKTypeface _tfMono;

    public DashboardRenderer()
    {
        _tfBold = SKTypeface.FromFamilyName("Segoe UI", SKFontStyleWeight.Bold, SKFontStyleWidth.Normal, SKFontStyleSlant.Upright)
                  ?? SKTypeface.Default;
        _tfReg  = SKTypeface.FromFamilyName("Segoe UI", SKFontStyleWeight.Normal, SKFontStyleWidth.Normal, SKFontStyleSlant.Upright)
                  ?? SKTypeface.Default;
        _tfMono = SKTypeface.FromFamilyName("Consolas", SKFontStyleWeight.Bold, SKFontStyleWidth.Normal, SKFontStyleSlant.Upright)
                  ?? _tfBold;
    }

    public static string NextTheme(string current)
    {
        for (int i = 0; i < Themes.Count; i++)
        {
            if (Themes[i].Id == current)
                return Themes[(i + 1) % Themes.Count].Id;
        }
        return Themes[0].Id;
    }

    private void PushHistory(SystemSnapshot snap)
    {
        Array.Copy(_cpuHist, 1, _cpuHist, 0, HistoryLen - 1);
        Array.Copy(_gpuHist, 1, _gpuHist, 0, HistoryLen - 1);
        Array.Copy(_ramHist, 1, _ramHist, 0, HistoryLen - 1);
        _cpuHist[^1] = snap.CpuPercent;
        _gpuHist[^1] = snap.SelectedGpu?.UtilPercent ?? 0.0;
        _ramHist[^1] = snap.RamPercent;
    }

    public (byte[] Rgb565Be, byte[] PreviewBgra480x240) Render(
        SystemSnapshot snap,
        string themeId,
        int targetW,
        int targetH,
        int brightness,
        bool rotate180)
    {
        PushHistory(snap);

        using SKBitmap baseBmp = themeId switch
        {
            "spark_graph" => RenderSparkGraph(snap),
            "triple_gauge" => RenderTripleGauge(snap),
            "clock_hud" => RenderClockHud(snap),
            _ => RenderCyberBars(snap),
        };

        // Resize to hardware target size if needed (e.g. 320x172)
        SKBitmap hwBmp = baseBmp;
        bool ownHw = false;
        if (baseBmp.Width != targetW || baseBmp.Height != targetH)
        {
            hwBmp = baseBmp.Resize(new SKImageInfo(targetW, targetH, SKColorType.Rgba8888, SKAlphaType.Opaque), SKFilterQuality.High);
            ownHw = true;
        }

        try
        {
            byte[] rgb565 = BitmapToRgb565Be(hwBmp, brightness, rotate180);
            byte[] previewBgra = BitmapToPreviewBgra480x240(baseBmp, brightness, rotate180);
            return (rgb565, previewBgra);
        }
        finally
        {
            if (ownHw) hwBmp.Dispose();
        }
    }

    private static SKColor UsageColor(double pct, SKColor baseCol) =>
        pct >= 90.0 ? ColorCrit : (pct >= 75.0 ? ColorWarn : baseCol);

    private static string CpuSubtext(SystemSnapshot snap) =>
        snap.CpuTempC.HasValue ? $"{snap.CpuTempC:F0}C"
        : snap.CpuFreqMhz.HasValue ? $"{snap.CpuFreqMhz.Value / 1000.0:F1}GHz"
        : $"{snap.CpuLogicalCores}T";

    private static string CpuShort(SystemSnapshot snap) =>
        snap.CpuTempC.HasValue ? $"{snap.CpuTempC:F0}C"
        : snap.CpuFreqMhz.HasValue ? $"{snap.CpuFreqMhz.Value / 1000.0:F1}G"
        : $"{snap.CpuLogicalCores}T";

    private static string GpuSubtext(SystemSnapshot snap)
    {
        var g = snap.SelectedGpu;
        if (g is null) return "N/A";
        var parts = new List<string>(2);
        if (g.TempC.HasValue) parts.Add($"{g.TempC:F0}C");
        if (!string.IsNullOrEmpty(g.VramText)) parts.Add(g.VramText);
        if (parts.Count > 0)
        {
            var s = string.Join(" ", parts);
            return s.Length > 10 ? s[..10] : s;
        }
        return g.Vendor;
    }

    private static string RamSubtext(SystemSnapshot snap) =>
        $"{snap.RamUsedGb:F1}/{snap.RamTotalGb:F0}G";

    private static void DrawTextMid(SKCanvas c, string text, float x, float midY, SKTypeface tf, float size, SKColor color, SKTextAlign align)
    {
        using var paint = new SKPaint
        {
            Typeface = tf,
            TextSize = size,
            Color = color,
            IsAntialias = true,
            TextAlign = align,
        };
        var fm = paint.FontMetrics;
        float baseline = midY - (fm.Ascent + fm.Descent) / 2f;
        c.DrawText(text, x, baseline, paint);
    }

    private static void DrawCard(SKCanvas c, float x0, float y0, float x1, float y1, float r, SKColor fill, SKColor border, float strokeW = 1f)
    {
        var rect = new SKRect(x0, y0, x1, y1);
        using var fillP = new SKPaint { Color = fill, IsAntialias = true, Style = SKPaintStyle.Fill };
        using var strokeP = new SKPaint { Color = border, IsAntialias = true, Style = SKPaintStyle.Stroke, StrokeWidth = strokeW };
        c.DrawRoundRect(rect, r, r, fillP);
        c.DrawRoundRect(rect, r, r, strokeP);
    }

    private static void DrawSegmentedBar(SKCanvas c, int x, int y, int w, int h, double pct, SKColor color, int segments = 24)
    {
        pct = Math.Clamp(pct, 0.0, 100.0);
        const int gap = 1;
        int segW = Math.Max(2, (w - (segments - 1) * gap) / segments);
        int totalW = segments * segW + (segments - 1) * gap;
        int startX = x + (w - totalW) / 2;
        int filled = (int)Math.Round(pct / 100.0 * segments);
        if (pct > 0.5 && filled == 0) filled = 1;

        using var onPaint = new SKPaint { Color = color, Style = SKPaintStyle.Fill };
        using var offPaint = new SKPaint { Color = TrackBg, Style = SKPaintStyle.Fill };
        for (int i = 0; i < segments; i++)
        {
            int sx = startX + i * (segW + gap);
            c.DrawRect(sx, y, segW, h, i < filled ? onPaint : offPaint);
        }
    }

    // ---- Theme 1: Cyber Bars ----
    private SKBitmap RenderCyberBars(SystemSnapshot snap)
    {
        var bmp = new SKBitmap(160, 80, SKColorType.Rgba8888, SKAlphaType.Opaque);
        using var c = new SKCanvas(bmp);
        c.Clear(BgColor);

        double gpuPct = snap.SelectedGpu?.UtilPercent ?? 0.0;
        (string Label, double Pct, string Sub, SKColor Base)[] rows =
        [
            ("CPU", snap.CpuPercent, CpuSubtext(snap), ColorCpu),
            ("GPU", gpuPct, GpuSubtext(snap), ColorGpu),
            ("RAM", snap.RamPercent, RamSubtext(snap), ColorRam),
        ];

        for (int i = 0; i < 3; i++)
        {
            var (label, pct, sub, baseCol) = rows[i];
            int y0 = 2 + i * 26;
            int y1 = y0 + 23;
            var col = UsageColor(pct, baseCol);

            DrawCard(c, 2, y0, 158, y1 + 1, 3, CardBg, CardBorder);
            using var accentP = new SKPaint { Color = col };
            c.DrawRect(3, y0 + 2, 3, 20, accentP);

            DrawTextMid(c, label, 9, y0 + 8.5f, _tfBold, 10, baseCol, SKTextAlign.Left);
            string pctStr = (label != "GPU" || snap.SelectedGpu != null) ? $"{pct:F0}%" : "--%";
            DrawTextMid(c, pctStr, 78, y0 + 8.5f, _tfMono, 12, pct < 90 ? TextBright : ColorCrit, SKTextAlign.Right);
            DrawTextMid(c, sub, 153, y0 + 8.5f, _tfReg, 9, TextMuted, SKTextAlign.Right);

            DrawSegmentedBar(c, 9, y0 + 16, 144, 5, pct, col, 24);
        }
        return bmp;
    }

    // ---- Theme 2: Telemetry Sparkline Graph ----
    private SKBitmap RenderSparkGraph(SystemSnapshot snap)
    {
        var bmp = new SKBitmap(160, 80, SKColorType.Rgba8888, SKAlphaType.Opaque);
        using var c = new SKCanvas(bmp);
        c.Clear(BgColor);

        double gpuPct = snap.SelectedGpu?.UtilPercent ?? 0.0;
        string gSub = GpuSubtext(snap);
        if (gSub.Length > 5) gSub = gSub[..5];
        (string Label, double Pct, string Sub, SKColor Col)[] metrics =
        [
            ("CPU", snap.CpuPercent, CpuShort(snap), ColorCpu),
            ("GPU", gpuPct, gSub, ColorGpu),
            ("RAM", snap.RamPercent, $"{snap.RamUsedGb:F0}G", ColorRam),
        ];

        for (int i = 0; i < 3; i++)
        {
            var (label, pct, sub, col) = metrics[i];
            int y0 = 2 + i * 26;
            int y1 = y0 + 23;
            DrawCard(c, 2, y0, 62, y1 + 1, 3, CardBg, CardBorder);
            using var barP = new SKPaint { Color = col };
            c.DrawRect(3, y0 + 3, 3, 18, barP);
            DrawTextMid(c, label, 8, y0 + 7.5f, _tfBold, 9, col, SKTextAlign.Left);
            DrawTextMid(c, sub, 8, y0 + 17.5f, _tfReg, 8, TextDim, SKTextAlign.Left);
            string pctS = (label != "GPU" || snap.SelectedGpu != null) ? $"{pct:F0}%" : "--";
            DrawTextMid(c, pctS, 60, y0 + 12f, _tfMono, pct >= 99.5 ? 10 : 12, UsageColor(pct, TextBright), SKTextAlign.Right);
        }

        // Right oscilloscope panel
        DrawCard(c, 65, 2, 158, 78, 3, new SKColor(10, 16, 29), CardBorder);
        float px0 = 68, py0 = 6, px1 = 154, py1 = 74;
        float pw = px1 - px0, ph = py1 - py0;

        using var hGrid = new SKPaint { Color = new SKColor(22, 34, 56), StrokeWidth = 1 };
        foreach (float frac in new[] { 0.25f, 0.50f, 0.75f })
        {
            float gy = py1 - frac * ph;
            c.DrawLine(px0, gy, px1, gy, hGrid);
        }
        using var vGrid = new SKPaint { Color = new SKColor(18, 28, 46), StrokeWidth = 1 };
        foreach (float frac in new[] { 0.33f, 0.66f })
        {
            float gx = px0 + frac * pw;
            c.DrawLine(gx, py0, gx, py1, vGrid);
        }

        (double[] Hist, SKColor Col)[] series =
        [
            (_ramHist, ColorRam),
            (_gpuHist, ColorGpu),
            (_cpuHist, ColorCpu),
        ];

        foreach (var (hist, col) in series)
        {
            using var path = new SKPath();
            float lx = px0, ly = py1;
            for (int i = 0; i < HistoryLen; i++)
            {
                float x = px0 + i / (float)(HistoryLen - 1) * pw;
                float y = py1 - (float)(Math.Clamp(hist[i], 0, 100) / 100.0) * ph;
                if (i == 0) path.MoveTo(x, y);
                else path.LineTo(x, y);
                lx = x; ly = y;
            }
            using var lineP = new SKPaint { Color = col, StrokeWidth = 1.25f, IsAntialias = true, Style = SKPaintStyle.Stroke };
            c.DrawPath(path, lineP);
            using var dotP = new SKPaint { Color = col, IsAntialias = true, Style = SKPaintStyle.Fill };
            c.DrawCircle(lx, ly, 1.8f, dotP);
        }
        return bmp;
    }

    // ---- Theme 3: Triple Circular Gauges (2x supersampled) ----
    private SKBitmap RenderTripleGauge(SystemSnapshot snap)
    {
        const int s = 2;
        using var hi = new SKBitmap(160 * s, 80 * s, SKColorType.Rgba8888, SKAlphaType.Opaque);
        using var c = new SKCanvas(hi);
        c.Clear(BgColor);

        double gpuPct = snap.SelectedGpu?.UtilPercent ?? 0.0;
        string cpuSub = CpuSubtext(snap); if (cpuSub.Length > 6) cpuSub = cpuSub[..6];
        string gpuSub = GpuSubtext(snap); if (gpuSub.Length > 6) gpuSub = gpuSub[..6];
        (string Label, double Pct, string Sub, SKColor Base)[] items =
        [
            ("CPU", snap.CpuPercent, cpuSub, ColorCpu),
            ("GPU", gpuPct, gpuSub, ColorGpu),
            ("RAM", snap.RamPercent, $"{snap.RamUsedGb:F1}G", ColorRam),
        ];

        int cardW = 50 * s, gap = 3 * s, startX = 2 * s;
        for (int i = 0; i < 3; i++)
        {
            var (label, pct, sub, baseCol) = items[i];
            int x0 = startX + i * (cardW + gap);
            int x1 = x0 + cardW;
            int y0 = 2 * s, y1 = 78 * s;
            float cx = (x0 + x1) / 2f;
            var col = UsageColor(pct, baseCol);

            DrawCard(c, x0, y0, x1, y1, 4 * s, CardBg, CardBorder, s);
            DrawTextMid(c, label, cx, y0 + 8.5f * s, _tfBold, 10 * s, baseCol, SKTextAlign.Center);

            float r = 17.5f * s;
            float cy = y0 + 38f * s;
            var oval = new SKRect(cx - r, cy - r, cx + r, cy + r);
            using var trackP = new SKPaint
            {
                Color = TrackBg,
                Style = SKPaintStyle.Stroke,
                StrokeWidth = 4 * s,
                StrokeCap = SKStrokeCap.Round,
                IsAntialias = true,
            };
            using var trackPath = new SKPath();
            trackPath.AddArc(oval, 135, 270);
            c.DrawPath(trackPath, trackP);

            float sweep = (float)(Math.Clamp(pct, 0, 100) / 100.0 * 270.0);
            if (sweep >= 1f)
            {
                using var valP = new SKPaint
                {
                    Color = col,
                    Style = SKPaintStyle.Stroke,
                    StrokeWidth = 4 * s,
                    StrokeCap = SKStrokeCap.Round,
                    IsAntialias = true,
                };
                using var valPath = new SKPath();
                valPath.AddArc(oval, 135, sweep);
                c.DrawPath(valPath, valP);
            }

            string pctS = (label != "GPU" || snap.SelectedGpu != null) ? $"{pct:F0}" : "--";
            DrawTextMid(c, pctS, cx, cy, _tfMono, 12 * s, TextBright, SKTextAlign.Center);
            DrawTextMid(c, sub, cx, y1 - 8 * s, _tfReg, 8 * s, TextMuted, SKTextAlign.Center);
        }

        return hi.Resize(new SKImageInfo(160, 80, SKColorType.Rgba8888, SKAlphaType.Opaque), SKFilterQuality.High);
    }

    // ---- Theme 4: Clock HUD ----
    private SKBitmap RenderClockHud(SystemSnapshot snap)
    {
        var bmp = new SKBitmap(160, 80, SKColorType.Rgba8888, SKAlphaType.Opaque);
        using var c = new SKCanvas(bmp);
        c.Clear(BgColor);

        DrawCard(c, 2, 2, 158, 24, 3, CardBg, CardBorder);
        DrawTextMid(c, snap.LocalTime.ToString("HH:mm:ss"), 8, 13, _tfMono, 15, TextBright, SKTextAlign.Left);
        DrawTextMid(c, snap.LocalTime.ToString("MM-dd ddd", System.Globalization.CultureInfo.InvariantCulture), 153, 13, _tfReg, 9, ColorCpu, SKTextAlign.Right);

        double gpuPct = snap.SelectedGpu?.UtilPercent ?? 0.0;
        string cpuSub = CpuSubtext(snap); if (cpuSub.Length > 6) cpuSub = cpuSub[..6];
        string gpuSub = GpuSubtext(snap); if (gpuSub.Length > 6) gpuSub = gpuSub[..6];
        (string Label, double Pct, string Sub, SKColor Base)[] tiles =
        [
            ("CPU", snap.CpuPercent, cpuSub, ColorCpu),
            ("GPU", gpuPct, gpuSub, ColorGpu),
            ("RAM", snap.RamPercent, $"{snap.RamUsedGb:F1}G", ColorRam),
        ];

        for (int i = 0; i < 3; i++)
        {
            var (label, pct, sub, baseCol) = tiles[i];
            int x0 = 2 + i * 53;
            int x1 = x0 + 50;
            int y0 = 27, y1 = 78;
            float cx = (x0 + x1) / 2f;
            var col = UsageColor(pct, baseCol);

            DrawCard(c, x0, y0, x1, y1, 3, CardBg, CardBorder);
            DrawTextMid(c, label, cx, y0 + 8, _tfBold, 9, baseCol, SKTextAlign.Center);
            string pctS = (label != "GPU" || snap.SelectedGpu != null) ? $"{pct:F0}%" : "--";
            DrawTextMid(c, pctS, cx, y0 + 23, _tfMono, pct >= 99.5 ? 12 : 14, pct < 90 ? TextBright : ColorCrit, SKTextAlign.Center);
            DrawTextMid(c, sub, cx, y0 + 36, _tfReg, 8, TextMuted, SKTextAlign.Center);

            using var trackP = new SKPaint { Color = TrackBg };
            c.DrawRect(x0 + 4, y1 - 6, 42, 3, trackP);
            int fw = (int)Math.Round(42 * Math.Clamp(pct, 0, 100) / 100.0);
            if (fw > 0)
            {
                using var fillP = new SKPaint { Color = col };
                c.DrawRect(x0 + 4, y1 - 6, fw, 3, fillP);
            }
        }
        return bmp;
    }

    private static unsafe byte[] BitmapToRgb565Be(SKBitmap bmp, int brightness, bool rotate180)
    {
        int w = bmp.Width, h = bmp.Height;
        int total = w * h;
        var outBytes = new byte[total * 2];
        int scale = Math.Clamp(brightness, 5, 100);
        byte* src = (byte*)bmp.GetPixels().ToPointer();

        for (int idx = 0; idx < total; idx++)
        {
            int srcIdx = rotate180 ? (total - 1 - idx) : idx;
            int o = srcIdx * 4;
            int r = src[o];
            int g = src[o + 1];
            int b = src[o + 2];
            if (scale < 100)
            {
                r = (r * scale) / 100;
                g = (g * scale) / 100;
                b = (b * scale) / 100;
            }
            int d = idx * 2;
            outBytes[d] = (byte)((r & 0xF8) | (g >> 5));
            outBytes[d + 1] = (byte)(((g & 0x1C) << 3) | (b >> 3));
        }
        return outBytes;
    }

    private static unsafe byte[] BitmapToPreviewBgra480x240(SKBitmap src160x80, int brightness, bool rotate180)
    {
        const int dstW = 480, dstH = 240, scaleFactor = 3;
        var dst = new byte[dstW * dstH * 4];
        int br = Math.Clamp(brightness, 5, 100);
        byte* src = (byte*)src160x80.GetPixels().ToPointer();
        int sw = src160x80.Width, sh = src160x80.Height;

        for (int y = 0; y < sh; y++)
        {
            for (int x = 0; x < sw; x++)
            {
                int sx = rotate180 ? (sw - 1 - x) : x;
                int sy = rotate180 ? (sh - 1 - y) : y;
                int so = (sy * sw + sx) * 4;
                byte r = (byte)((src[so] * br) / 100);
                byte g = (byte)((src[so + 1] * br) / 100);
                byte b = (byte)((src[so + 2] * br) / 100);

                for (int dy = 0; dy < scaleFactor; dy++)
                {
                    int rowBase = ((y * scaleFactor + dy) * dstW + x * scaleFactor) * 4;
                    for (int dx = 0; dx < scaleFactor; dx++)
                    {
                        int doff = rowBase + dx * 4;
                        dst[doff] = b;
                        dst[doff + 1] = g;
                        dst[doff + 2] = r;
                        dst[doff + 3] = 255;
                    }
                }
            }
        }
        return dst;
    }

    public void Dispose()
    {
        _tfBold.Dispose();
        _tfReg.Dispose();
        _tfMono.Dispose();
    }
}

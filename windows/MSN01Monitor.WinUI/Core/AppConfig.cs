using System.Text.Json;
using System.Text.Json.Serialization;

namespace MSN01Monitor.WinUI.Core;

public sealed class AppConfig
{
    [JsonPropertyName("port")]
    public string Port { get; set; } = "auto";

    [JsonPropertyName("gpu_id")]
    public string GpuId { get; set; } = "auto";

    [JsonPropertyName("theme")]
    public string Theme { get; set; } = "cyber_bars";

    [JsonPropertyName("refresh_hz")]
    public double RefreshHz { get; set; } = 2.0;

    [JsonPropertyName("brightness")]
    public int Brightness { get; set; } = 100;

    [JsonPropertyName("rotate_180")]
    public bool Rotate180 { get; set; } = false;

    [JsonPropertyName("touch_switch_theme")]
    public bool TouchSwitchTheme { get; set; } = true;

    [JsonPropertyName("auto_connect")]
    public bool AutoConnect { get; set; } = true;

    [JsonPropertyName("auto_reconnect")]
    public bool AutoReconnect { get; set; } = true;

    public AppConfig Clone() => new()
    {
        Port = Port,
        GpuId = GpuId,
        Theme = Theme,
        RefreshHz = RefreshHz,
        Brightness = Brightness,
        Rotate180 = Rotate180,
        TouchSwitchTheme = TouchSwitchTheme,
        AutoConnect = AutoConnect,
        AutoReconnect = AutoReconnect,
    };
}

public static class ConfigStore
{
    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };

    public static string GetConfigPath()
    {
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        return Path.Combine(appData, "msn01-monitor", "config.json");
    }

    public static AppConfig Load()
    {
        try
        {
            var path = GetConfigPath();
            if (!File.Exists(path)) return new AppConfig();
            var cfg = JsonSerializer.Deserialize<AppConfig>(File.ReadAllText(path), JsonOpts) ?? new AppConfig();
            cfg.RefreshHz = Math.Clamp(cfg.RefreshHz, 0.5, 10.0);
            cfg.Brightness = Math.Clamp(cfg.Brightness, 10, 100);
            return cfg;
        }
        catch
        {
            return new AppConfig();
        }
    }

    public static void Save(AppConfig cfg)
    {
        try
        {
            var path = GetConfigPath();
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            File.WriteAllText(path, JsonSerializer.Serialize(cfg, JsonOpts));
        }
        catch
        {
            // Ignore config write failures cleanly
        }
    }
}

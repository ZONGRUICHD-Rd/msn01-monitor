using System.Diagnostics;
using System.IO.Ports;
using Microsoft.Win32;

namespace MSN01Monitor.WinUI.Core;

public sealed record PortCandidate(string Device, bool IsMsn01Vid, string Description)
{
    public string Label => IsMsn01Vid
        ? $"{Device} [MSN01] (1A86:FE0C)"
        : string.IsNullOrWhiteSpace(Description) ? Device : $"{Device} ({Description})";
}

public sealed class MSN01Screen : IDisposable
{
    private const int DefaultBaud = 921600;
    private static readonly byte[] CmdHandshake = [0x00, 0x4D, 0x53, 0x4E, 0x43, 0x4E]; // \0MSNCN
    private static readonly byte[] CmdFlashInfo = [0x00, 0x30, 0x20, 0x02, 0x00, 0x00];
    private static readonly byte[] CmdAdcTouch  = [0x08, 0x09, 0x00, 0x00, 0x00, 0x00];
    private static readonly byte[] MagicMsn     = [0x00, 0x4D, 0x53, 0x4E];             // \0MSN
    private static readonly byte[] MagicMsn01   = [0x00, 0x4D, 0x53, 0x4E, 0x30, 0x31]; // \0MSN01

    private readonly object _lock = new();
    private SerialPort? _ser;

    public string? PortName { get; private set; }
    public int Width { get; private set; } = 160;
    public int Height { get; private set; } = 80;
    public int? FlashId { get; private set; }

    public bool IsConnected
    {
        get
        {
            lock (_lock) return _ser is { IsOpen: true };
        }
    }

    public static List<PortCandidate> ListSerialPorts()
    {
        var msnPorts = FindMsn01PortsFromRegistry();
        var names = SerialPort.GetPortNames().Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        var list = new List<PortCandidate>(names.Count);
        foreach (var p in names)
        {
            bool isMsn = msnPorts.Contains(p);
            list.Add(new PortCandidate(p, isMsn, isMsn ? "CH32x035 USB Serial" : "Serial Port"));
        }
        list.Sort((a, b) =>
        {
            int cmp = b.IsMsn01Vid.CompareTo(a.IsMsn01Vid);
            if (cmp != 0) return cmp;
            return string.Compare(a.Device, b.Device, StringComparison.OrdinalIgnoreCase);
        });
        return list;
    }

    private static HashSet<string> FindMsn01PortsFromRegistry()
    {
        var result = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using var usbKey = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Enum\USB");
            if (usbKey == null) return result;
            foreach (var sub in usbKey.GetSubKeyNames())
            {
                if (!sub.Contains("VID_1A86", StringComparison.OrdinalIgnoreCase) ||
                    !sub.Contains("PID_FE0C", StringComparison.OrdinalIgnoreCase))
                    continue;

                using var devKey = usbKey.OpenSubKey(sub);
                if (devKey == null) continue;
                foreach (var inst in devKey.GetSubKeyNames())
                {
                    using var dp = devKey.OpenSubKey($@"{inst}\Device Parameters");
                    var port = dp?.GetValue("PortName") as string;
                    if (!string.IsNullOrWhiteSpace(port))
                        result.Add(port.Trim());
                }
            }
        }
        catch
        {
            // Fallback to port probing
        }
        return result;
    }

    public static bool ProbePort(string portName)
    {
        try
        {
            using var sp = new SerialPort(portName, DefaultBaud)
            {
                ReadTimeout = 250,
                WriteTimeout = 500,
                DtrEnable = true,
                RtsEnable = true,
            };
            sp.Open();
            sp.DiscardInBuffer();
            Thread.Sleep(150);
            var hb = ReadAvailable(sp);
            if (IndexOf(hb, MagicMsn01) >= 0) return true;

            sp.Write(CmdHandshake, 0, CmdHandshake.Length);
            Thread.Sleep(120);
            var resp = ReadAvailable(sp);
            return IndexOf(resp, MagicMsn) >= 0;
        }
        catch
        {
            return false;
        }
    }

    public static string? AutoDetectPort()
    {
        var ports = ListSerialPorts();
        foreach (var p in ports.Where(x => x.IsMsn01Vid))
        {
            if (ProbePort(p.Device)) return p.Device;
        }
        foreach (var p in ports.Where(x => !x.IsMsn01Vid))
        {
            if (ProbePort(p.Device)) return p.Device;
        }
        return ports.FirstOrDefault(x => x.IsMsn01Vid)?.Device;
    }

    public (int Width, int Height) Connect(string? requestedPort = null)
    {
        lock (_lock)
        {
            Close();
            string? target = (!string.IsNullOrWhiteSpace(requestedPort) &&
                              !requestedPort.Equals("auto", StringComparison.OrdinalIgnoreCase))
                ? requestedPort
                : AutoDetectPort();

            if (string.IsNullOrWhiteSpace(target))
                throw new IOException("未检测到 MSN01 小屏幕，请检查 USB 连接。");

            var sp = new SerialPort(target, DefaultBaud)
            {
                ReadTimeout = 500,
                WriteTimeout = 1500,
                DtrEnable = true,
                RtsEnable = true,
            };
            sp.Open();
            _ser = sp;
            PortName = target;

            try
            {
                HandshakeLocked();
                QueryModelLocked();
            }
            catch
            {
                Close();
                throw;
            }
            return (Width, Height);
        }
    }

    public void Close()
    {
        lock (_lock)
        {
            if (_ser != null)
            {
                try { _ser.Close(); } catch { }
                try { _ser.Dispose(); } catch { }
                _ser = null;
            }
        }
    }

    private void HandshakeLocked()
    {
        var sp = _ser!;
        sp.DiscardInBuffer();
        sp.DiscardOutBuffer();

        for (int attempt = 0; attempt < 3; attempt++)
        {
            sp.Write(CmdHandshake, 0, CmdHandshake.Length);
            var sw = Stopwatch.StartNew();
            var buf = new List<byte>(32);
            while (sw.ElapsedMilliseconds < 250)
            {
                int avail = sp.BytesToRead;
                if (avail > 0)
                {
                    var tmp = new byte[avail];
                    int n = sp.Read(tmp, 0, avail);
                    for (int i = 0; i < n; i++) buf.Add(tmp[i]);
                    if (IndexOf(buf, MagicMsn) >= 0) return;
                }
                Thread.Sleep(5);
            }
        }
        throw new IOException($"串口 {PortName} 未响应 MSN01 握手协议。");
    }

    private void QueryModelLocked()
    {
        var sp = _ser!;
        sp.DiscardInBuffer();
        sp.Write(CmdFlashInfo, 0, CmdFlashInfo.Length);

        var sw = Stopwatch.StartNew();
        var buf = new List<byte>(16);
        while (sw.ElapsedMilliseconds < 350)
        {
            int avail = sp.BytesToRead;
            if (avail > 0)
            {
                var tmp = new byte[avail];
                int n = sp.Read(tmp, 0, avail);
                for (int i = 0; i < n; i++) buf.Add(tmp[i]);
                if (buf.Count >= 6) break;
            }
            Thread.Sleep(5);
        }

        if (buf.Count >= 6)
        {
            int idx = IndexOf(buf, [0x00, 0x30]);
            int baseOff = (idx >= 0 && buf.Count >= idx + 6) ? idx : 0;
            int fid = (buf[baseOff + 4] << 8) | buf[baseOff + 5];
            FlashId = fid;
            (Width, Height) = fid switch
            {
                0x8513 => (160, 80),
                0x8516 => (320, 172),
                _ => (160, 80),
            };
        }
        else
        {
            FlashId = null;
            Width = 160;
            Height = 80;
        }
    }

    private bool InitLcdWriteLocked(int x, int y, int w, int h)
    {
        var sp = _ser!;
        sp.DiscardInBuffer();
        byte[] header =
        [
            0x02, 0x00, (byte)(x >> 8), (byte)(x & 0xFF), (byte)(y >> 8), (byte)(y & 0xFF),
            0x02, 0x01, (byte)(w >> 8), (byte)(w & 0xFF), (byte)(h >> 8), (byte)(h & 0xFF),
            0x02, 0x03, 0x07, 0x00, 0x00, 0x00,
        ];
        sp.Write(header, 0, header.Length);

        var sw = Stopwatch.StartNew();
        var buf = new List<byte>(16);
        while (sw.ElapsedMilliseconds < 600)
        {
            int avail = sp.BytesToRead;
            if (avail > 0)
            {
                var tmp = new byte[avail];
                int n = sp.Read(tmp, 0, avail);
                for (int i = 0; i < n; i++) buf.Add(tmp[i]);
                if (IndexOf(buf, [0x02, 0x03]) >= 0) return true;
                if (buf.Count >= 12) return false;
            }
            Thread.Sleep(1);
        }
        return false;
    }

    public int PushRgb565Frame(byte[] rgb565Be)
    {
        var payload = EncodeCompressedChunks(rgb565Be);
        lock (_lock)
        {
            if (_ser is not { IsOpen: true })
                throw new IOException("MSN01 尚未连接。");

            if (!InitLcdWriteLocked(0, 0, Width, Height))
            {
                HandshakeLocked();
                if (!InitLcdWriteLocked(0, 0, Width, Height))
                    throw new IOException("LCD 写入初始化 ACK 超时。");
            }

            _ser.Write(payload, 0, payload.Length);
            return payload.Length;
        }
    }

    public int? ReadTouchAdc()
    {
        lock (_lock)
        {
            if (_ser is not { IsOpen: true }) return null;
            try
            {
                _ser.DiscardInBuffer();
                _ser.Write(CmdAdcTouch, 0, CmdAdcTouch.Length);

                var sw = Stopwatch.StartNew();
                var buf = new List<byte>(12);
                while (sw.ElapsedMilliseconds < 50)
                {
                    int avail = _ser.BytesToRead;
                    if (avail > 0)
                    {
                        var tmp = new byte[avail];
                        int n = _ser.Read(tmp, 0, avail);
                        for (int i = 0; i < n; i++) buf.Add(tmp[i]);
                        int idx = IndexOf(buf, [0x08, 0x09]);
                        if (idx >= 0 && buf.Count >= idx + 6)
                            return (buf[idx + 4] << 8) | buf[idx + 5];
                    }
                    Thread.Sleep(2);
                }
            }
            catch
            {
                return null;
            }
        }
        return null;
    }

    public static byte[] EncodeCompressedChunks(byte[] rgb565Be)
    {
        var output = new List<byte>(rgb565Be.Length / 2);
        var chunk = new byte[256];
        var words = new uint[64];
        var counts = new Dictionary<uint, int>(64);

        for (int off = 0; off < rgb565Be.Length; off += 256)
        {
            int valid = Math.Min(256, rgb565Be.Length - off);
            Buffer.BlockCopy(rgb565Be, off, chunk, 0, valid);
            for (int k = valid; k < 256; k++) chunk[k] = 0xFF;

            counts.Clear();
            uint bestWord = 0;
            int bestCount = -1;
            for (int i = 0; i < 64; i++)
            {
                int o = i * 4;
                uint w = ((uint)chunk[o] << 24) | ((uint)chunk[o + 1] << 16) | ((uint)chunk[o + 2] << 8) | chunk[o + 3];
                words[i] = w;
                int c = counts.TryGetValue(w, out int prev) ? prev + 1 : 1;
                counts[w] = c;
                if (c > bestCount)
                {
                    bestCount = c;
                    bestWord = w;
                }
            }

            // Fill command: [0x02, 0x04, b0, b1, b2, b3]
            output.Add(0x02);
            output.Add(0x04);
            output.Add((byte)(bestWord >> 24));
            output.Add((byte)(bestWord >> 16));
            output.Add((byte)(bestWord >> 8));
            output.Add((byte)bestWord);

            // Patch differing 4-byte slots: [0x04, idx, b0, b1, b2, b3]
            for (int i = 0; i < 64; i++)
            {
                uint w = words[i];
                if (w != bestWord)
                {
                    output.Add(0x04);
                    output.Add((byte)i);
                    output.Add((byte)(w >> 24));
                    output.Add((byte)(w >> 16));
                    output.Add((byte)(w >> 8));
                    output.Add((byte)w);
                }
            }

            // Commit chunk: [0x02, 0x03, 0x08, validH, validL, 0x00]
            output.Add(0x02);
            output.Add(0x03);
            output.Add(0x08);
            output.Add((byte)(valid >> 8));
            output.Add((byte)(valid & 0xFF));
            output.Add(0x00);
        }

        return output.ToArray();
    }

    private static byte[] ReadAvailable(SerialPort sp)
    {
        int n = sp.BytesToRead;
        if (n <= 0) return [];
        var buf = new byte[n];
        int read = sp.Read(buf, 0, n);
        if (read == n) return buf;
        Array.Resize(ref buf, read);
        return buf;
    }

    private static int IndexOf(IReadOnlyList<byte> haystack, byte[] needle)
    {
        int limit = haystack.Count - needle.Length;
        for (int i = 0; i <= limit; i++)
        {
            bool match = true;
            for (int j = 0; j < needle.Length; j++)
            {
                if (haystack[i + j] != needle[j])
                {
                    match = false;
                    break;
                }
            }
            if (match) return i;
        }
        return -1;
    }

    public void Dispose() => Close();
}

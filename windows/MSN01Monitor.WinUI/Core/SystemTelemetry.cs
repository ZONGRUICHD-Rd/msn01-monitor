using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

namespace MSN01Monitor.WinUI.Core;

public sealed record GpuSample(
    string Id,
    string Name,
    string Vendor,
    double UtilPercent,
    double? VramUsedMb,
    double? VramTotalMb,
    double? TempC,
    string Source)
{
    public string ShortName
    {
        get
        {
            string n = Name;
            string[] prefixes = ["NVIDIA GeForce ", "NVIDIA ", "AMD Radeon(TM) ", "AMD Radeon ", "Intel(R) "];
            foreach (var p in prefixes)
            {
                if (n.StartsWith(p, StringComparison.OrdinalIgnoreCase))
                {
                    n = n[p.Length..];
                    break;
                }
            }
            return n.Length > 18 ? n[..18] : n;
        }
    }

    public string VramText
    {
        get
        {
            if (VramUsedMb is null) return "";
            if (VramTotalMb is >= 1024)
                return $"{VramUsedMb.Value / 1024.0:F1}/{VramTotalMb.Value / 1024.0:F0}G";
            if (VramUsedMb.Value >= 1024)
                return $"{VramUsedMb.Value / 1024.0:F1}G";
            return $"{VramUsedMb.Value:F0}M";
        }
    }
}

public sealed record SystemSnapshot(
    double CpuPercent,
    double? CpuFreqMhz,
    double? CpuTempC,
    int CpuLogicalCores,
    double RamPercent,
    double RamUsedGb,
    double RamTotalGb,
    List<GpuSample> Gpus,
    GpuSample? SelectedGpu,
    DateTime LocalTime);

public sealed class SystemTelemetry : IDisposable
{
    private ulong _prevIdle;
    private ulong _prevKernel;
    private ulong _prevUser;
    private readonly int _logicalCores = Environment.ProcessorCount;
    private readonly NvmlBackend _nvml = new();
    private readonly DxgiPdhBackend _dxgiPdh = new();

    public SystemTelemetry()
    {
        ReadCpuTimes(out _prevIdle, out _prevKernel, out _prevUser);
    }

    public SystemSnapshot Sample(string gpuId = "auto")
    {
        double cpuPct = SampleCpuPercent();
        double? cpuMhz = SampleCpuFreqMhz();
        var (ramPct, ramUsedGb, ramTotalGb) = SampleRam();

        var gpus = new List<GpuSample>();
        bool hasNvidia = false;
        if (_nvml.Available)
        {
            var nv = _nvml.Sample();
            gpus.AddRange(nv);
            hasNvidia = nv.Count > 0;
        }
        if (_dxgiPdh.Available)
        {
            foreach (var g in _dxgiPdh.Sample())
            {
                if (hasNvidia && g.Vendor.Equals("NVIDIA", StringComparison.OrdinalIgnoreCase))
                    continue;
                gpus.Add(g);
            }
        }

        GpuSample? selected = SelectGpu(gpus, gpuId);
        return new SystemSnapshot(
            cpuPct,
            cpuMhz,
            null,
            _logicalCores,
            ramPct,
            ramUsedGb,
            ramTotalGb,
            gpus,
            selected,
            DateTime.Now);
    }

    public static GpuSample? SelectGpu(List<GpuSample> gpus, string gpuId)
    {
        if (gpus.Count == 0) return null;
        if (!string.IsNullOrWhiteSpace(gpuId) && !gpuId.Equals("auto", StringComparison.OrdinalIgnoreCase))
        {
            var match = gpus.FirstOrDefault(g => g.Id == gpuId);
            if (match != null) return match;
        }
        return gpus
            .OrderBy(g => g.Source == "NVML" ? 0 : 1)
            .ThenByDescending(g => g.VramTotalMb ?? 0.0)
            .ThenByDescending(g => g.UtilPercent)
            .First();
    }

    // ---- CPU via Win32 GetSystemTimes + CallNtPowerInformation ----
    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME
    {
        public uint dwLowDateTime;
        public uint dwHighDateTime;
        public ulong ToUInt64() => ((ulong)dwHighDateTime << 32) | dwLowDateTime;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetSystemTimes(out FILETIME idle, out FILETIME kernel, out FILETIME user);

    private static bool ReadCpuTimes(out ulong idle, out ulong kernel, out ulong user)
    {
        if (GetSystemTimes(out var i, out var k, out var u))
        {
            idle = i.ToUInt64();
            kernel = k.ToUInt64();
            user = u.ToUInt64();
            return true;
        }
        idle = kernel = user = 0;
        return false;
    }

    private double SampleCpuPercent()
    {
        if (!ReadCpuTimes(out ulong idle, out ulong kernel, out ulong user))
            return 0.0;
        ulong dIdle = idle - _prevIdle;
        ulong dKernel = kernel - _prevKernel;
        ulong dUser = user - _prevUser;
        _prevIdle = idle;
        _prevKernel = kernel;
        _prevUser = user;

        ulong total = dKernel + dUser;
        if (total == 0) return 0.0;
        double busy = (double)(total - dIdle) / total * 100.0;
        return Math.Clamp(busy, 0.0, 100.0);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESSOR_POWER_INFORMATION
    {
        public uint Number;
        public uint MaxMhz;
        public uint CurrentMhz;
        public uint MhzLimit;
        public uint MaxIdleState;
        public uint CurrentIdleState;
    }

    [DllImport("powrprof.dll")]
    private static extern uint CallNtPowerInformation(
        int informationLevel,
        IntPtr inputBuffer,
        uint inputBufferLength,
        IntPtr outputBuffer,
        uint outputBufferLength);

    private double? SampleCpuFreqMhz()
    {
        try
        {
            int count = Math.Max(1, _logicalCores);
            int structSize = Marshal.SizeOf<PROCESSOR_POWER_INFORMATION>();
            int totalBytes = structSize * count;
            IntPtr mem = Marshal.AllocHGlobal(totalBytes);
            try
            {
                // ProcessorInformation = 11
                if (CallNtPowerInformation(11, IntPtr.Zero, 0, mem, (uint)totalBytes) == 0)
                {
                    double sum = 0;
                    int valid = 0;
                    for (int i = 0; i < count; i++)
                    {
                        var info = Marshal.PtrToStructure<PROCESSOR_POWER_INFORMATION>(mem + i * structSize);
                        if (info.CurrentMhz > 0)
                        {
                            sum += info.CurrentMhz;
                            valid++;
                        }
                    }
                    if (valid > 0) return sum / valid;
                }
            }
            finally
            {
                Marshal.FreeHGlobal(mem);
            }
        }
        catch { }
        return null;
    }

    // ---- RAM via Win32 GlobalMemoryStatusEx ----
    [StructLayout(LayoutKind.Sequential)]
    private struct MEMORYSTATUSEX
    {
        public uint dwLength;
        public uint dwMemoryLoad;
        public ulong ullTotalPhys;
        public ulong ullAvailPhys;
        public ulong ullTotalPageFile;
        public ulong ullAvailPageFile;
        public ulong ullTotalVirtual;
        public ulong ullAvailVirtual;
        public ulong ullAvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GlobalMemoryStatusEx(ref MEMORYSTATUSEX lpBuffer);

    private static (double Pct, double UsedGb, double TotalGb) SampleRam()
    {
        var ms = new MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<MEMORYSTATUSEX>() };
        if (!GlobalMemoryStatusEx(ref ms) || ms.ullTotalPhys == 0)
            return (0, 0, 0);
        double totalGb = ms.ullTotalPhys / (1024.0 * 1024.0 * 1024.0);
        double usedGb = (ms.ullTotalPhys - ms.ullAvailPhys) / (1024.0 * 1024.0 * 1024.0);
        double pct = Math.Clamp(usedGb / totalGb * 100.0, 0.0, 100.0);
        return (pct, usedGb, totalGb);
    }

    public void Dispose()
    {
        _nvml.Dispose();
        _dxgiPdh.Dispose();
    }

    // ---- GPU Backend 1: NVML (nvml.dll) ----
    private sealed class NvmlBackend : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)]
        private struct NvmlUtilization { public uint Gpu; public uint Memory; }

        [StructLayout(LayoutKind.Sequential)]
        private struct NvmlMemory { public ulong Total; public ulong Free; public ulong Used; }

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int InitDel();
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int ShutdownDel();
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int GetCountDel(out uint count);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int GetHandleDel(uint idx, out IntPtr handle);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int GetNameDel(IntPtr handle, byte[] name, uint len);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int GetUtilDel(IntPtr handle, out NvmlUtilization util);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int GetTempDel(IntPtr handle, uint sensorType, out uint temp);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] private delegate int GetMemDel(IntPtr handle, out NvmlMemory mem);

        private IntPtr _lib;
        private uint _count;
        private ShutdownDel? _shutdown;
        private GetHandleDel? _getHandle;
        private GetNameDel? _getName;
        private GetUtilDel? _getUtil;
        private GetTempDel? _getTemp;
        private GetMemDel? _getMem;

        public bool Available => _lib != IntPtr.Zero && _count > 0;

        public NvmlBackend()
        {
            string[] paths =
            [
                @"C:\Windows\System32\nvml.dll",
                @"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll",
                "nvml.dll",
            ];
            foreach (var p in paths)
            {
                if (Path.IsPathRooted(p) && !File.Exists(p)) continue;
                if (!NativeLibrary.TryLoad(p, out IntPtr h)) continue;
                try
                {
                    var init = Marshal.GetDelegateForFunctionPointer<InitDel>(NativeLibrary.GetExport(h, "nvmlInit_v2"));
                    var shutdown = Marshal.GetDelegateForFunctionPointer<ShutdownDel>(NativeLibrary.GetExport(h, "nvmlShutdown"));
                    var getCount = Marshal.GetDelegateForFunctionPointer<GetCountDel>(NativeLibrary.GetExport(h, "nvmlDeviceGetCount_v2"));
                    if (init() == 0 && getCount(out uint cnt) == 0 && cnt > 0)
                    {
                        _lib = h;
                        _count = cnt;
                        _shutdown = shutdown;
                        _getHandle = Marshal.GetDelegateForFunctionPointer<GetHandleDel>(NativeLibrary.GetExport(h, "nvmlDeviceGetHandleByIndex_v2"));
                        _getName = Marshal.GetDelegateForFunctionPointer<GetNameDel>(NativeLibrary.GetExport(h, "nvmlDeviceGetName"));
                        _getUtil = Marshal.GetDelegateForFunctionPointer<GetUtilDel>(NativeLibrary.GetExport(h, "nvmlDeviceGetUtilizationRates"));
                        _getTemp = Marshal.GetDelegateForFunctionPointer<GetTempDel>(NativeLibrary.GetExport(h, "nvmlDeviceGetTemperature"));
                        _getMem = Marshal.GetDelegateForFunctionPointer<GetMemDel>(NativeLibrary.GetExport(h, "nvmlDeviceGetMemoryInfo"));
                        return;
                    }
                    try { shutdown(); } catch { }
                    NativeLibrary.Free(h);
                }
                catch
                {
                    NativeLibrary.Free(h);
                }
            }
        }

        public List<GpuSample> Sample()
        {
            var list = new List<GpuSample>();
            if (!Available) return list;
            var nameBuf = new byte[128];
            for (uint i = 0; i < _count; i++)
            {
                try
                {
                    if (_getHandle!(i, out IntPtr handle) != 0) continue;
                    Array.Clear(nameBuf);
                    string name = _getName!(handle, nameBuf, (uint)nameBuf.Length) == 0
                        ? Encoding.UTF8.GetString(nameBuf).TrimEnd('\0')
                        : $"NVIDIA GPU {i}";
                    double utilPct = _getUtil!(handle, out var u) == 0 ? u.Gpu : 0.0;
                    double? tempC = _getTemp!(handle, 0, out uint t) == 0 ? t : null;
                    double? vUsed = null, vTotal = null;
                    if (_getMem!(handle, out var m) == 0)
                    {
                        vUsed = m.Used / (1024.0 * 1024.0);
                        vTotal = m.Total / (1024.0 * 1024.0);
                    }
                    list.Add(new GpuSample($"nvml:{i}", name, "NVIDIA", Math.Clamp(utilPct, 0, 100), vUsed, vTotal, tempC, "NVML"));
                }
                catch { }
            }
            return list;
        }

        public void Dispose()
        {
            if (_lib != IntPtr.Zero)
            {
                try { _shutdown?.Invoke(); } catch { }
                try { NativeLibrary.Free(_lib); } catch { }
                _lib = IntPtr.Zero;
            }
        }
    }

    // ---- GPU Backend 2: Windows DXGI + PDH English Counters (AMD/Intel/NVIDIA, zero admin) ----
    private sealed class DxgiPdhBackend : IDisposable
    {
        private static readonly Regex LuidRe = new(@"(luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", RegexOptions.Compiled);
        private static readonly Regex EngRe = new(@"engtype_([A-Za-z0-9_]+)", RegexOptions.Compiled);

        private readonly Dictionary<string, (string Name, string Vendor, double TotalMb)> _adapters = new(StringComparer.OrdinalIgnoreCase);
        private IntPtr _hq;
        private IntPtr _hcEng;
        private IntPtr _hcMem;

        public bool Available => _hq != IntPtr.Zero;

        [StructLayout(LayoutKind.Sequential)]
        private struct LUID { public uint LowPart; public int HighPart; }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct DXGI_ADAPTER_DESC1
        {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string Description;
            public uint VendorId;
            public uint DeviceId;
            public uint SubSysId;
            public uint Revision;
            public nuint DedicatedVideoMemory;
            public nuint DedicatedSystemMemory;
            public nuint SharedSystemMemory;
            public LUID AdapterLuid;
            public uint Flags;
        }

        [DllImport("dxgi.dll")]
        private static extern int CreateDXGIFactory1(ref Guid riid, out IntPtr ppFactory);

        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate uint ReleaseDel(IntPtr obj);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int EnumAdapters1Del(IntPtr factory, uint index, out IntPtr adapter);
        [UnmanagedFunctionPointer(CallingConvention.StdCall)]
        private delegate int GetDesc1Del(IntPtr adapter, out DXGI_ADAPTER_DESC1 desc);

        [StructLayout(LayoutKind.Sequential)]
        private struct PDH_FMT_COUNTERVALUE { public uint CStatus; public double doubleValue; }

        [StructLayout(LayoutKind.Sequential)]
        private struct PDH_FMT_COUNTERVALUE_ITEM_W { public IntPtr szName; public PDH_FMT_COUNTERVALUE FmtValue; }

        [DllImport("pdh.dll", CharSet = CharSet.Unicode)]
        private static extern uint PdhOpenQueryW(string? dataSource, IntPtr userData, out IntPtr query);
        [DllImport("pdh.dll", CharSet = CharSet.Unicode)]
        private static extern uint PdhAddEnglishCounterW(IntPtr query, string fullCounterPath, IntPtr userData, out IntPtr counter);
        [DllImport("pdh.dll")]
        private static extern uint PdhCollectQueryData(IntPtr query);
        [DllImport("pdh.dll")]
        private static extern uint PdhGetFormattedCounterArrayW(IntPtr counter, uint format, ref uint bufferSize, ref uint itemCount, IntPtr itemBuffer);
        [DllImport("pdh.dll")]
        private static extern uint PdhCloseQuery(IntPtr query);

        public DxgiPdhBackend()
        {
            EnumerateDxgiAdapters();
            OpenPdh();
        }

        private unsafe void EnumerateDxgiAdapters()
        {
            try
            {
                Guid iid = new("770aae78-f26f-4dba-a829-253c83d1b387");
                if (CreateDXGIFactory1(ref iid, out IntPtr factory) != 0 || factory == IntPtr.Zero) return;
                IntPtr* fVtbl = *(IntPtr**)factory;
                var enumAdapters = Marshal.GetDelegateForFunctionPointer<EnumAdapters1Del>(fVtbl[12]);
                var fRelease = Marshal.GetDelegateForFunctionPointer<ReleaseDel>(fVtbl[2]);

                uint idx = 0;
                while (enumAdapters(factory, idx++, out IntPtr adapter) == 0 && adapter != IntPtr.Zero)
                {
                    IntPtr* aVtbl = *(IntPtr**)adapter;
                    var getDesc1 = Marshal.GetDelegateForFunctionPointer<GetDesc1Del>(aVtbl[10]);
                    var aRelease = Marshal.GetDelegateForFunctionPointer<ReleaseDel>(aVtbl[2]);
                    if (getDesc1(adapter, out var desc) == 0 && (desc.Flags & 2) == 0)
                    {
                        string luid = $"luid_0x{(uint)desc.AdapterLuid.HighPart:x8}_0x{desc.AdapterLuid.LowPart:x8}";
                        if (!_adapters.ContainsKey(luid))
                        {
                            string vendor = desc.VendorId switch
                            {
                                0x10DE => "NVIDIA",
                                0x1002 => "AMD",
                                0x8086 => "Intel",
                                _ => "GPU",
                            };
                            double totalMb = desc.DedicatedVideoMemory / (1024.0 * 1024.0);
                            _adapters[luid] = (desc.Description.Trim(), vendor, totalMb);
                        }
                    }
                    aRelease(adapter);
                }
                fRelease(factory);
            }
            catch { }
        }

        private void OpenPdh()
        {
            try
            {
                if (PdhOpenQueryW(null, IntPtr.Zero, out IntPtr hq) != 0) return;
                uint r1 = PdhAddEnglishCounterW(hq, @"\GPU Engine(*)\Utilization Percentage", IntPtr.Zero, out IntPtr hEng);
                uint r2 = PdhAddEnglishCounterW(hq, @"\GPU Adapter Memory(*)\Dedicated Usage", IntPtr.Zero, out IntPtr hMem);
                if (r1 != 0 && r2 != 0)
                {
                    PdhCloseQuery(hq);
                    return;
                }
                PdhCollectQueryData(hq);
                _hq = hq;
                _hcEng = r1 == 0 ? hEng : IntPtr.Zero;
                _hcMem = r2 == 0 ? hMem : IntPtr.Zero;
            }
            catch { }
        }

        private List<(string Name, double Value)> ReadArray(IntPtr hc)
        {
            var result = new List<(string, double)>();
            if (hc == IntPtr.Zero) return result;
            uint bufSize = 0, itemCount = 0;
            PdhGetFormattedCounterArrayW(hc, 0x00000200, ref bufSize, ref itemCount, IntPtr.Zero);
            if (bufSize == 0) return result;
            IntPtr buf = Marshal.AllocHGlobal((int)bufSize);
            try
            {
                if (PdhGetFormattedCounterArrayW(hc, 0x00000200, ref bufSize, ref itemCount, buf) == 0)
                {
                    int itemSize = Marshal.SizeOf<PDH_FMT_COUNTERVALUE_ITEM_W>();
                    for (uint i = 0; i < itemCount; i++)
                    {
                        var item = Marshal.PtrToStructure<PDH_FMT_COUNTERVALUE_ITEM_W>(buf + (int)(i * itemSize));
                        if (item.FmtValue.CStatus is 0 or 1 && item.szName != IntPtr.Zero)
                        {
                            string name = Marshal.PtrToStringUni(item.szName) ?? "";
                            result.Add((name, item.FmtValue.doubleValue));
                        }
                    }
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buf);
            }
            return result;
        }

        public List<GpuSample> Sample()
        {
            var list = new List<GpuSample>();
            if (_hq == IntPtr.Zero) return list;
            try
            {
                if (PdhCollectQueryData(_hq) != 0) return list;

                var engSums = new Dictionary<string, Dictionary<string, double>>(StringComparer.OrdinalIgnoreCase);
                foreach (var (inst, val) in ReadArray(_hcEng))
                {
                    var ml = LuidRe.Match(inst);
                    if (!ml.Success) continue;
                    string luid = ml.Groups[1].Value.ToLowerInvariant();
                    var me = EngRe.Match(inst);
                    string etype = me.Success ? me.Groups[1].Value : "other";
                    if (!engSums.TryGetValue(luid, out var bucket))
                    {
                        bucket = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
                        engSums[luid] = bucket;
                    }
                    bucket[etype] = bucket.TryGetValue(etype, out double cur) ? cur + val : val;
                }

                var dedMem = new Dictionary<string, double>(StringComparer.OrdinalIgnoreCase);
                foreach (var (inst, val) in ReadArray(_hcMem))
                {
                    var ml = LuidRe.Match(inst);
                    if (!ml.Success) continue;
                    string luid = ml.Groups[1].Value.ToLowerInvariant();
                    double mb = val / (1024.0 * 1024.0);
                    dedMem[luid] = dedMem.TryGetValue(luid, out double cur) ? Math.Max(cur, mb) : mb;
                }

                foreach (var kv in _adapters)
                {
                    string luid = kv.Key;
                    if (!engSums.ContainsKey(luid) && !dedMem.ContainsKey(luid)) continue;
                    var (name, vendor, totalMb) = kv.Value;
                    double util = engSums.TryGetValue(luid, out var perEng) && perEng.Count > 0
                        ? perEng.Values.Max()
                        : 0.0;
                    list.Add(new GpuSample(
                        $"pdh:{luid}",
                        name,
                        vendor,
                        Math.Clamp(util, 0.0, 100.0),
                        dedMem.TryGetValue(luid, out double usedMb) ? usedMb : null,
                        totalMb > 0 ? totalMb : null,
                        null,
                        "DXGI/PDH"));
                }
            }
            catch { }
            return list;
        }

        public void Dispose()
        {
            if (_hq != IntPtr.Zero)
            {
                try { PdhCloseQuery(_hq); } catch { }
                _hq = IntPtr.Zero;
            }
        }
    }
}

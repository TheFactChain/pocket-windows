using System;
using System.IO;
using System.Text;

internal static class OfflineMaintenanceRegistryRegression
{
    private static int checks;
    private static void Assert(bool condition, string message)
    { if (!condition) throw new Exception(message); checks++; }
    private static void Refused<T>(Action action, string message) where T : Exception
    { try { action(); } catch (T) { checks++; return; } throw new Exception(message); }
    private static void Check(uint error) { if (error != 0) throw new Exception("Fixture offreg error " + error); }
    private static IntPtr CreateKey(IntPtr root, string path)
    {
        IntPtr key; uint disposition;
        uint error = OfflineMaintenanceRegistry.Native.ORCreateKey(root, path, null, 0, IntPtr.Zero, out key, out disposition);
        if (error != 0) throw new Exception("Fixture ORCreateKey " + path + " error " + error);
        return key;
    }
    private static void Set(IntPtr key, string name, uint type, byte[] bytes)
    { Check(OfflineMaintenanceRegistry.Native.ORSetValue(key, name, type, bytes, (uint)bytes.Length)); }
    private static byte[] Read(IntPtr root, string path, string name, uint expectedType)
    {
        byte[] bytes = new byte[4096]; uint size = (uint)bytes.Length, type;
        Check(OfflineMaintenanceRegistry.Native.ORGetValue(root, path, name, out type, bytes, ref size));
        Assert(type == expectedType, "Wrong registry value type for " + name);
        Array.Resize(ref bytes, (int)size); return bytes;
    }
    private static void Fixture(string path, int controlSet)
    {
        IntPtr root; Check(OfflineMaintenanceRegistry.Native.ORCreateHive(out root));
        try
        {
            IntPtr select = CreateKey(root, "Select");
            try { Set(select, "Default", 4, BitConverter.GetBytes(controlSet)); }
            finally { Check(OfflineMaintenanceRegistry.Native.ORCloseKey(select)); }
            foreach (string parent in new[] { "ControlSet001", @"ControlSet001\Services" })
                Check(OfflineMaintenanceRegistry.Native.ORCloseKey(CreateKey(root, parent)));
            IntPtr other = CreateKey(root, @"ControlSet001\Services\Unrelated");
            try { Set(other, "Sentinel", 1, Encoding.Unicode.GetBytes("preserved\0")); }
            finally { Check(OfflineMaintenanceRegistry.Native.ORCloseKey(other)); }
            Check(OfflineMaintenanceRegistry.Native.ORSaveHive(root, path, 6, 1));
        }
        finally { Check(OfflineMaintenanceRegistry.Native.ORCloseHive(root)); }
    }
    private static void VerifyValues(string path, string nonce, string hash)
    {
        IntPtr root; Check(OfflineMaintenanceRegistry.Native.OROpenHive(path, out root));
        try
        {
            string service = @"ControlSet001\Services\WindowsClaudeMaintenance";
            foreach (string name in new[] { "Type", "Start", "ErrorControl" })
                Assert(BitConverter.ToInt32(Read(root, service, name, 4), 0) == (name == "Type" ? 16 : name == "Start" ? 2 : 1), "Wrong " + name);
            Assert(Encoding.Unicode.GetString(Read(root, service, "ObjectName", 1)) == "LocalSystem\0", "Wrong service account");
            Assert(Encoding.Unicode.GetString(Read(root, service, "ImagePath", 1)) ==
                "\"C:\\ProgramData\\WindowsClaudeMaintenance\\MaintenanceService.exe\" " + nonce + " " + hash + "\0", "Wrong fixed ImagePath");
            Assert(Encoding.Unicode.GetString(Read(root, service, "DependOnService", 7)) == "Winmgmt\0\0", "Wrong REG_MULTI_SZ dependency");
            // SCM may create a service child; removal must stay inside the owned tree.
            IntPtr child = CreateKey(root, service + @"\Security");
            Check(OfflineMaintenanceRegistry.Native.ORCloseKey(child));
            Check(OfflineMaintenanceRegistry.Native.ORSaveHive(root, path + ".with-child", 6, 1));
        }
        finally { Check(OfflineMaintenanceRegistry.Native.ORCloseHive(root)); }
    }
    private static void VerifyUnrelated(string path)
    {
        IntPtr root; Check(OfflineMaintenanceRegistry.Native.OROpenHive(path, out root));
        try { Assert(Encoding.Unicode.GetString(Read(root, @"ControlSet001\Services\Unrelated", "Sentinel", 1)) == "preserved\0", "Unrelated data changed"); }
        finally { Check(OfflineMaintenanceRegistry.Native.ORCloseHive(root)); }
    }
    public static int Main(string[] args)
    {
        string original = Path.Combine(args[0], "fixture.hive"), installed = Path.Combine(args[0], "installed.hive"), cleaned = Path.Combine(args[0], "cleaned.hive");
        string nonce = new string('a', 32), hash = new string('b', 64);
        Fixture(original, 1);
        byte[] before = File.ReadAllBytes(original);
        using (var hive = OfflineMaintenanceRegistry.Open(original))
        {
            Assert(hive.GetDefaultControlSet() == 1, "Wrong default control set");
            Assert(!hive.ServiceExists(), "Service unexpectedly exists");
            Refused<ArgumentException>(delegate { hive.Install("../bad", hash); }, "Invalid nonce accepted");
            Refused<ArgumentException>(delegate { hive.Install(nonce, hash + "\n"); }, "Invalid hash accepted");
            Assert(!hive.ServiceExists(), "Failed validation changed hive");
            hive.Install(nonce, hash);
            Assert(hive.ServiceExists(), "Installation missing");
            Refused<InvalidOperationException>(delegate { hive.Install(nonce, hash); }, "Existing service overwritten");
            Refused<InvalidOperationException>(delegate { hive.Remove(new string('c', 32), hash); }, "Wrong nonce removed service");
            Refused<InvalidOperationException>(delegate { hive.Remove(nonce, new string('c', 64)); }, "Wrong hash removed service");
            Assert(hive.ServiceExists(), "Mismatched removal changed hive");
            Refused<IOException>(delegate { hive.Save(original); }, "Source hive overwritten");
            hive.Save(installed);
        }
        Assert(Convert.ToBase64String(before) == Convert.ToBase64String(File.ReadAllBytes(original)), "Input file mutated");
        VerifyValues(installed, nonce, hash);
        using (var hive = OfflineMaintenanceRegistry.Open(installed + ".with-child"))
        {
            Assert(hive.Remove(nonce, hash), "Owned service not removed");
            Assert(!hive.Remove(nonce, hash), "Absent service removal not idempotent");
            hive.Save(cleaned);
        }
        using (var hive = OfflineMaintenanceRegistry.Open(cleaned)) Assert(!hive.ServiceExists(), "Service survived save/open");
        VerifyUnrelated(installed); VerifyUnrelated(cleaned);
        foreach (int bad in new[] { 0, 1000 })
        {
            string path = Path.Combine(args[0], "bad-" + bad + ".hive"); Fixture(path, bad);
            using (var hive = OfflineMaintenanceRegistry.Open(path))
                Refused<InvalidDataException>(delegate { hive.GetDefaultControlSet(); }, "Invalid control set accepted");
        }
        var disposed = OfflineMaintenanceRegistry.Open(original); disposed.Dispose(); disposed.Dispose();
        Refused<ObjectDisposedException>(delegate { disposed.ServiceExists(); }, "Disposed hive remained usable");
        Console.WriteLine("Offline fixture install/remove/save/open: " + checks + " checks PASS; no host registry or real SYSTEM hive used");
        return 0;
    }
}

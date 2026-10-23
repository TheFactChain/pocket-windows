using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;

// Offreg edits memory only. The caller owns mounted-path checks and atomic replacement.
public sealed class OfflineMaintenanceRegistry : IDisposable
{
    private const string ServiceName = "WindowsClaudeMaintenance";
    private IntPtr hive;
    private OfflineMaintenanceRegistry(IntPtr handle) { hive = handle; }

    public static OfflineMaintenanceRegistry Open(string path)
    {
        IntPtr handle;
        Check(Native.OROpenHive(Path.GetFullPath(path), out handle), "OROpenHive");
        return new OfflineMaintenanceRegistry(handle);
    }

    private IntPtr Handle
    {
        get { if (hive == IntPtr.Zero) throw new ObjectDisposedException("OfflineMaintenanceRegistry"); return hive; }
    }
    private static void Check(uint error, string operation)
    { if (error != 0) throw new Win32Exception((int)error, operation + " failed with code " + error); }

    private byte[] Value(string path, string name, uint expectedType, int limit)
    {
        byte[] bytes = new byte[limit]; uint size = (uint)limit, type;
        Check(Native.ORGetValue(Handle, path, name, out type, bytes, ref size), "ORGetValue");
        if (type != expectedType || size > limit) throw new InvalidDataException("Unexpected registry value type or size.");
        Array.Resize(ref bytes, (int)size); return bytes;
    }

    public int GetDefaultControlSet()
    {
        byte[] bytes = Value("Select", "Default", 4, 4);
        if (bytes.Length != 4) throw new InvalidDataException("Select Default must be a DWORD.");
        int selected = BitConverter.ToInt32(bytes, 0);
        if (selected < 1 || selected > 999) throw new InvalidDataException("Default control set must be 1..999.");
        return selected;
    }

    private string ServicesPath { get { return "ControlSet" + GetDefaultControlSet().ToString("D3") + @"\Services"; } }

    public bool ServiceExists()
    {
        IntPtr key;
        uint error = Native.OROpenKey(Handle, ServicesPath + "\\" + ServiceName, out key);
        if (error == 2 || error == 3 || error == 1168) return false;
        Check(error, "OROpenKey");
        Check(Native.ORCloseKey(key), "ORCloseKey");
        return true;
    }

    private static string ImagePath(string nonce, string scriptSHA)
    {
        if (nonce == null || scriptSHA == null || !Regex.IsMatch(nonce, @"\A[0-9a-fA-F]{32}\z")
            || !Regex.IsMatch(scriptSHA, @"\A[0-9a-fA-F]{64}\z"))
            throw new ArgumentException("Expected nonce32hex and scriptSHA25664hex only.");
        return "\"C:\\ProgramData\\WindowsClaudeMaintenance\\MaintenanceService.exe\" " + nonce + " " + scriptSHA;
    }

    private static void Set(IntPtr key, string name, uint type, byte[] bytes)
    { Check(Native.ORSetValue(key, name, type, bytes, (uint)bytes.Length), "ORSetValue"); }

    public void Install(string nonce, string scriptSHA)
    {
        string command = ImagePath(nonce, scriptSHA);
        if (ServiceExists()) throw new InvalidOperationException("Maintenance service already exists; refusing overwrite.");
        IntPtr services;
        Check(Native.OROpenKey(Handle, ServicesPath, out services), "OROpenKey Services");
        try
        {
            IntPtr key; uint disposition;
            Check(Native.ORCreateKey(services, ServiceName, null, 0, IntPtr.Zero, out key, out disposition), "ORCreateKey");
            try
            {
                if (disposition != 1) throw new InvalidOperationException("Maintenance service already exists.");
                Set(key, "Type", 4, BitConverter.GetBytes(16));
                Set(key, "Start", 4, BitConverter.GetBytes(2));
                Set(key, "ErrorControl", 4, BitConverter.GetBytes(1));
                Set(key, "ObjectName", 1, Encoding.Unicode.GetBytes("LocalSystem\0"));
                Set(key, "ImagePath", 1, Encoding.Unicode.GetBytes(command + "\0"));
                Set(key, "DependOnService", 7, Encoding.Unicode.GetBytes("Winmgmt\0\0"));
            }
            finally { Check(Native.ORCloseKey(key), "ORCloseKey"); }
        }
        finally { Check(Native.ORCloseKey(services), "ORCloseKey Services"); }
    }

    public bool Remove(string nonce, string scriptSHA)
    {
        string expected = ImagePath(nonce, scriptSHA);
        if (!ServiceExists()) return false;
        string path = ServicesPath + "\\" + ServiceName;
        string actual = Encoding.Unicode.GetString(Value(path, "ImagePath", 1, 4096));
        if (!String.Equals(actual, expected + "\0", StringComparison.Ordinal))
            throw new InvalidOperationException("Maintenance service ImagePath does not match this operation.");
        DeleteTree(Handle, path, 0);
        return true;
    }

    private static void DeleteTree(IntPtr parent, string name, int depth)
    {
        if (depth > 64) throw new InvalidDataException("Maintenance service subtree is unexpectedly deep.");
        IntPtr key; Check(Native.OROpenKey(parent, name, out key), "OROpenKey owned subtree");
        try
        {
            while (true)
            {
                var child = new StringBuilder(256); uint length = (uint)child.Capacity;
                uint error = Native.OREnumKey(key, 0, child, ref length, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
                if (error == 259) break;
                Check(error, "OREnumKey owned subtree");
                DeleteTree(key, child.ToString(), depth + 1);
            }
        }
        finally { Check(Native.ORCloseKey(key), "ORCloseKey owned subtree"); }
        Check(Native.ORDeleteKey(parent, name), "ORDeleteKey owned subtree");
    }

    public void Save(string newPath)
    {
        string path = Path.GetFullPath(newPath);
        if (File.Exists(path) || Directory.Exists(path)) throw new IOException("Save requires a fresh destination path.");
        Check(Native.ORSaveHive(Handle, path, 6, 1), "ORSaveHive");
    }

    public void Dispose()
    {
        if (hive == IntPtr.Zero) return;
        Check(Native.ORCloseHive(hive), "ORCloseHive");
        hive = IntPtr.Zero;
    }

    internal static class Native
    {
        private const string Dll = @"C:\Windows\System32\offreg.dll";
        [DllImport(Dll, ExactSpelling = true)] internal static extern uint ORCreateHive(out IntPtr hive);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint OROpenHive(string path, out IntPtr hive);
        [DllImport(Dll, ExactSpelling = true)] internal static extern uint ORCloseHive(IntPtr hive);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint ORSaveHive(IntPtr hive, string path, uint major, uint minor);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint OROpenKey(IntPtr key, string path, out IntPtr opened);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint ORCreateKey(IntPtr key, string path, string keyClass, uint options, IntPtr security, out IntPtr created, out uint disposition);
        [DllImport(Dll, ExactSpelling = true)] internal static extern uint ORCloseKey(IntPtr key);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint ORSetValue(IntPtr key, string name, uint type, byte[] bytes, uint size);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint ORGetValue(IntPtr key, string path, string name, out uint type, [Out] byte[] bytes, ref uint size);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint ORDeleteKey(IntPtr key, string path);
        [DllImport(Dll, ExactSpelling = true, CharSet = CharSet.Unicode)] internal static extern uint OREnumKey(IntPtr key, uint index, StringBuilder name, ref uint nameLength, IntPtr keyClass, IntPtr classLength, IntPtr lastWrite);
    }
}

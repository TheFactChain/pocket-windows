using System;
using System.Reflection;
using System.Text;

public static class GuestEnvironmentRegression
{
    static void Check(bool value, string message) { if (!value) throw new Exception(message); }
    static byte[] Table(string oem, string id)
    {
        var table = new byte[116];
        Encoding.ASCII.GetBytes("FACP").CopyTo(table, 0);
        BitConverter.GetBytes(table.Length).CopyTo(table, 4);
        Encoding.ASCII.GetBytes(oem).CopyTo(table, 10);
        Encoding.ASCII.GetBytes(id).CopyTo(table, 16);
        int checksum = 0; foreach (byte value in table) checksum += value;
        table[9] = unchecked((byte)-checksum);
        return table;
    }
    public static int Main()
    {
        var type = Assembly.GetExecutingAssembly().GetType("WindowsClaude.GuestEnvironment");
        Check(type != null, "Guest guard must recognize QEMU firmware independently of the Dell SMBIOS label");
        var method = type.GetMethod("IsQemuFirmware");
        Func<byte[], bool> recognized = table => (bool)method.Invoke(null, new object[] { table });
        Check(recognized(Table("BOCHS ", "BXPC    ")), "QEMU ACPI must pass with branded SMBIOS");
        Check(!recognized(Table("DELL  ", "CBX3    ")), "Physical Dell must not pass");
        Check(!recognized(Table("BOCHS ", "OTHER   ")), "OEM alone is insufficient");
        Check(!recognized(null) && !recognized(new byte[35]), "Absent or truncated firmware must fail closed");
        var broken = Table("BOCHS ", "BXPC    "); broken[4] = 36;
        Check(!recognized(broken), "ACPI length must match the buffer");
        broken = Table("BOCHS ", "BXPC    "); broken[30] ^= 1;
        Check(!recognized(broken), "Corrupt firmware must fail closed");
        broken = Table("BOCHS ", "BXPC    "); broken[0] = (byte)'X';
        Check(!recognized(broken), "Wrong table must fail closed");
        try { type.GetMethod("Require").Invoke(null, null); throw new Exception("Physical host guard accepted"); }
        catch (TargetInvocationException error) { Check(error.InnerException is InvalidOperationException && error.InnerException.Message == "GUEST_GUARD_REFUSED", "Host rejection must remain explicit"); }
        Console.WriteLine("PASS: branded QEMU firmware, physical Dell refusal, malformed ACPI and real host guard");
        return 0;
    }
}

using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Runtime.Versioning;

[assembly: TargetFramework(".NETFramework,Version=v4.8", FrameworkDisplayName = ".NET Framework 4.8")]

namespace WindowsClaude
{
    public static class GuestEnvironment
    {
        public static bool IsQemuFirmware(byte[] table)
        {
            if (table == null || table.Length < 36 || table.Length > 1024 * 1024 ||
                BitConverter.ToUInt32(table, 4) != table.Length ||
                Encoding.ASCII.GetString(table, 0, 4) != "FACP" ||
                Encoding.ASCII.GetString(table, 10, 6) != "BOCHS " ||
                Encoding.ASCII.GetString(table, 16, 8) != "BXPC    ") return false;
            int checksum = 0; foreach (byte value in table) checksum += value;
            return (checksum & 255) == 0;
        }

        public static void Require()
        {
            // SMBIOS is branded Dell; QEMU's ACPI identity still distinguishes it from the physical PC.
            const uint acpi = 0x41435049, facp = 0x50434146;
            uint size = GetSystemFirmwareTable(acpi, facp, null, 0);
            if (size >= 36 && size <= 1024 * 1024)
            {
                var table = new byte[size];
                if (GetSystemFirmwareTable(acpi, facp, table, size) == size && IsQemuFirmware(table)) return;
            }
            throw new InvalidOperationException("GUEST_GUARD_REFUSED");
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern uint GetSystemFirmwareTable(uint provider, uint id, [Out] byte[] buffer, uint size);
    }
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Ports;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WindowsClaude.GuestSettings
{
    public sealed class Settings
    {
        public string Nonce;
        public int Width, Height;
    }

    public static class Program
    {
        const string TaskName = "WindowsClaude.GuestSettings";
        static string InstallStage = "START";
        public const string SettingsFileName = "SETTINGS.JSN";
        public const string SetupFileName = "SETUP.JSN";
        static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 8192, RecursionLimit = 8 };

        // Deliberately accepts only this protocol's ASCII keys, string values and unsigned integers.
        public static Dictionary<string, string> ParseObject(string input)
        {
            if (input == null || input.Length > 4096) throw new FormatException("CONFIG_INVALID");
            var result = new Dictionary<string, string>(StringComparer.Ordinal);
            string body = input.Trim();
            if (!body.StartsWith("{") || !body.EndsWith("}")) throw new FormatException("CONFIG_INVALID");
            body = body.Substring(1, body.Length - 2);
            int offset = 0;
            var pair = new Regex(@"\G\s*""(?<key>[a-z_]+)""\s*:\s*(?<value>""[A-Za-z0-9]+""|0|[1-9][0-9]*)\s*(?<end>,|$)", RegexOptions.CultureInvariant);
            while (offset < body.Length)
            {
                Match match = pair.Match(body, offset);
                if (!match.Success || result.ContainsKey(match.Groups["key"].Value)) throw new FormatException("CONFIG_INVALID");
                result.Add(match.Groups["key"].Value, match.Groups["value"].Value);
                offset += match.Length;
                if (match.Groups["end"].Value == "," && offset == body.Length) throw new FormatException("CONFIG_INVALID");
            }
            return result;
        }

        static int Integer(Dictionary<string, string> values, string name, int min, int max)
        {
            int value;
            if (!values.ContainsKey(name) || !Int32.TryParse(values[name], NumberStyles.None, CultureInfo.InvariantCulture, out value) || value < min || value > max)
                throw new FormatException("CONFIG_INVALID");
            return value;
        }

        public static Settings ParseSettings(string input)
        {
            var values = ParseObject(input);
            if (values.Count != 4 || Integer(values, "schema", 1, 1) != 1 || !values.ContainsKey("session_nonce") || !Regex.IsMatch(values["session_nonce"], "^\"[a-f0-9]{32}\"$"))
                throw new FormatException("CONFIG_INVALID");
            return new Settings { Nonce = values["session_nonce"].Trim('"'), Width = Integer(values, "width", 640, 7680), Height = Integer(values, "height", 480, 4320) };
        }

        public static bool IsSetupMarker(string input)
        {
            try { var v = ParseObject(input); return v.Count == 2 && v.ContainsKey("schema") && v["schema"] == "1" && v.ContainsKey("app") && v["app"] == "\"WindowsClaude\""; }
            catch (FormatException) { return false; }
        }

        public static bool SameLocalUser(string expectedSid, string actualSid, string domain, string machine)
        {
            return Regex.IsMatch(actualSid ?? "", @"^S-1-5-21-[0-9]+-[0-9]+-[0-9]+-[0-9]+$") &&
                (expectedSid == null || String.Equals(expectedSid, actualSid, StringComparison.Ordinal)) &&
                String.Equals(domain, machine, StringComparison.OrdinalIgnoreCase);
        }

        public static string TaskXml(string exe, string sid)
        {
            if (!Regex.IsMatch(sid ?? "", @"^S-1-5-21-[0-9]+-[0-9]+-[0-9]+-[0-9]+$")) throw new InvalidOperationException("LOCAL_USER_REQUIRED");
            return "<?xml version=\"1.0\" encoding=\"UTF-16\"?><Task version=\"1.3\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\"><RegistrationInfo><Description>WindowsClaude guest settings schema 1</Description></RegistrationInfo><Triggers><LogonTrigger><Enabled>true</Enabled><UserId>" + sid + "</UserId><Delay>PT10S</Delay></LogonTrigger></Triggers><Principals><Principal id=\"User\"><UserId>" + sid + "</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals><Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><AllowHardTerminate>false</AllowHardTerminate><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>true</Enabled></Settings><Actions Context=\"User\"><Exec><Command>" + SecurityElement.Escape(exe) + "</Command><Arguments>--apply</Arguments></Exec></Actions></Task>";
        }

        public static bool TaskMissing(Exception error)
        {
            // COM interop maps HRESULT_FILE_NOT_FOUND to FileNotFoundException on .NET Framework.
            return error != null && unchecked((uint)error.HResult) == 0x80070002;
        }

        public static string InstallFailure(Exception error, string stage)
        {
            string reason = error is InvalidOperationException && Regex.IsMatch(error.Message, "^[A-Z_]{1,64}$") ? error.Message : error is System.ComponentModel.Win32Exception ? "UAC_CANCELLED_OR_START_FAILED" : error is UnauthorizedAccessException ? "ADMIN_OR_PATH_ACCESS_REQUIRED" : "INSTALLATION_FAILED";
            return reason + "; этап " + stage + "; " + error.GetType().Name + "; 0x" + error.HResult.ToString("X8", CultureInfo.InvariantCulture);
        }

        public static string Receipt(Settings settings, int width, int height, string code, string displayCode = "NOT_ATTEMPTED")
        {
            return Json.Serialize(new { schema = 1, session_nonce = settings == null ? null : settings.Nonce,
                requested = settings == null ? null : new { width = settings.Width, height = settings.Height },
                current = new { width = width, height = height },
                success = code == "OK", code = code, display_code = displayCode });
        }

        static string ReadBounded(string path)
        {
            NoReparse(path);
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (stream.Length > 4096) throw new FormatException("CONFIG_INVALID");
                using (var reader = new StreamReader(stream, new UTF8Encoding(false, true), true)) return reader.ReadToEnd();
            }
        }

        static string FindMedium(string label, string file)
        {
            string found = null;
            foreach (var drive in DriveInfo.GetDrives())
            {
                if (!drive.IsReady || !String.Equals(drive.VolumeLabel, label, StringComparison.Ordinal)) continue;
                if (found != null) throw new InvalidOperationException("AMBIGUOUS_MEDIUM");
                if (label == "WCPSETTINGS" && (!drive.DriveFormat.StartsWith("FAT", StringComparison.OrdinalIgnoreCase) || !ReadOnlyVolume(drive.RootDirectory.FullName))) throw new InvalidOperationException("SETTINGS_MEDIUM_NOT_READONLY_FAT");
                found = Path.Combine(drive.RootDirectory.FullName, file);
            }
            if (found == null) throw new InvalidOperationException("MEDIUM_NOT_FOUND");
            return ReadBounded(found);
        }

        static void NoReparse(string path)
        {
            for (string p = Path.GetFullPath(path); !String.IsNullOrEmpty(p); p = Path.GetDirectoryName(p))
                if ((File.Exists(p) || Directory.Exists(p)) && (File.GetAttributes(p) & FileAttributes.ReparsePoint) != 0) throw new InvalidOperationException("REPARSE_PATH_REFUSED");
        }

        static bool ReadOnlyVolume(string root)
        {
            uint serial, length, flags;
            if (!GetVolumeInformation(root, null, 0, out serial, out length, out flags, null, 0)) throw new InvalidOperationException("VOLUME_INSPECTION_FAILED");
            return (flags & 0x80000) != 0;
        }

        static void RequireGuest()
        {
            GuestEnvironment.Require();
        }

        static bool IsAdmin() { return new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator); }

        static void LockDirectory(string path)
        {
            NoReparse(path);
            Directory.CreateDirectory(path);
            NoReparse(path);
            var acl = new DirectorySecurity();
            acl.SetAccessRuleProtection(true, false);
            foreach (var rule in new[] { new { Sid = "S-1-5-18", Rights = FileSystemRights.FullControl }, new { Sid = "S-1-5-32-544", Rights = FileSystemRights.FullControl }, new { Sid = "S-1-5-32-545", Rights = FileSystemRights.ReadAndExecute } })
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier(rule.Sid), rule.Rights, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            acl.SetOwner(new SecurityIdentifier("S-1-5-32-544"));
            Directory.SetAccessControl(path, acl);
        }

        static int Install(string expectedSid)
        {
            InstallStage = "GUEST_CHECK";
            RequireGuest();
            InstallStage = "SETUP_MEDIUM";
            if (!IsSetupMarker(FindMedium("WCPSETUP", SetupFileName))) throw new InvalidOperationException("SETUP_MARKER_INVALID");
            string sid = WindowsIdentity.GetCurrent().User.Value;
            InstallStage = "LOCAL_ACCOUNT";
            TaskXml("validation", sid);
            if (!SameLocalUser(expectedSid, sid, Environment.UserDomainName, Environment.MachineName)) throw new InvalidOperationException("LOCAL_USER_REQUIRED");
            if (!IsAdmin())
            {
                InstallStage = "UAC";
                using (var elevated = Process.Start(new ProcessStartInfo(Process.GetCurrentProcess().MainModule.FileName, "--install " + sid) { UseShellExecute = true, Verb = "runas" })) { elevated.WaitForExit(); return elevated.ExitCode; }
            }
            string parent = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "WindowsClaude");
            string directory = Path.Combine(parent, "GuestSettings");
            InstallStage = "DIRECTORY_PERMISSIONS";
            LockDirectory(parent); LockDirectory(directory);
            string destination = Path.Combine(directory, "GuestSettings.exe");
            NoReparse(destination);
            string source = Process.GetCurrentProcess().MainModule.FileName;
            InstallStage = "COPY_HELPER";
            if (!String.Equals(source, destination, StringComparison.OrdinalIgnoreCase)) File.Copy(source, destination, true);
            InstallStage = "FILE_PERMISSIONS";
            var fileAcl = new FileSecurity(); fileAcl.SetAccessRuleProtection(false, false); fileAcl.SetOwner(new SecurityIdentifier("S-1-5-32-544")); File.SetAccessControl(destination, fileAcl);
            InstallStage = "TASK_SERVICE";
            dynamic scheduler = Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service"));
            scheduler.Connect();
            dynamic folder = scheduler.GetFolder("\\");
            dynamic existing = null;
            InstallStage = "TASK_LOOKUP";
            try { existing = folder.GetTask(TaskName); }
            catch (Exception e) { if (!TaskMissing(e)) throw; }
            if (existing != null)
            {
                dynamic definition = existing.Definition;
                if (definition.Actions.Count != 1 || definition.Actions.Item(1).Type != 0 || !String.Equals((string)definition.Actions.Item(1).Path, destination, StringComparison.OrdinalIgnoreCase) || (string)definition.Actions.Item(1).Arguments != "--apply" || (string)definition.Principal.UserId != sid)
                    throw new InvalidOperationException("TASK_NAME_CONFLICT");
            }
            InstallStage = "REGISTER_TASK";
            folder.RegisterTask(TaskName, TaskXml(destination, sid), 6, sid, null, 3, null);
            MessageBox.Show("Настройка разрешения установлена. После следующего входа в Windows выбранное в панели разрешение применится автоматически.", "Настройка Windows", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 0;
        }

        static bool Display(Settings settings)
        {
            DEVMODE current = Mode();
            if (!EnumDisplaySettings(null, -1, ref current)) throw new InvalidOperationException("DISPLAY_INSPECTION_FAILED");
            if (current.dmPelsWidth == settings.Width && current.dmPelsHeight == settings.Height) return true;
            for (int i = 0; i < 4096; i++)
            {
                DEVMODE mode = Mode();
                if (!EnumDisplaySettings(null, i, ref mode)) break;
                if (mode.dmPelsWidth != settings.Width || mode.dmPelsHeight != settings.Height) continue;
                if (ChangeDisplaySettingsEx(null, ref mode, IntPtr.Zero, 2, IntPtr.Zero) != 0) continue;
                return ChangeDisplaySettingsEx(null, ref mode, IntPtr.Zero, 0, IntPtr.Zero) == 0;
            }
            return false;
        }

        static int Apply()
        {
            RequireGuest();
            Settings settings = null; int width = 0, height = 0; string code = "CONFIG_INVALID", displayCode = "NOT_ATTEMPTED";
            try
            {
                settings = ParseSettings(FindMedium("WCPSETTINGS", SettingsFileName));
                if (!Environment.UserInteractive || Process.GetCurrentProcess().SessionId == 0) throw new InvalidOperationException("INTERACTIVE_DESKTOP_REQUIRED");
                if (!IsAdmin()) throw new InvalidOperationException("ADMIN_REQUIRED");
                try { displayCode = Display(settings) ? "OK" : "DISPLAY_MODE_UNSUPPORTED"; }
                catch { displayCode = "DISPLAY_OPERATION_FAILED"; }
                code = displayCode;
            }
            catch (Exception e) { code = e is InvalidOperationException && Regex.IsMatch(e.Message, "^[A-Z_]{1,64}$") ? e.Message : "CONFIG_INVALID"; }
            DEVMODE actual = Mode();
            if (EnumDisplaySettings(null, -1, ref actual)) { width = actual.dmPelsWidth; height = actual.dmPelsHeight; }
            if (displayCode == "OK" && (width != settings.Width || height != settings.Height)) { displayCode = "DISPLAY_APPLY_INCOMPLETE"; if (code == "OK") code = displayCode; }
            string receipt = Receipt(settings, width, height, code, displayCode);
            try
            {
                using (var serial = new SerialPort("COM1", 115200) { WriteTimeout = 3000, Encoding = new UTF8Encoding(false), NewLine = "\n" }) { serial.Open(); serial.WriteLine(receipt); }
            }
            catch { return 3; }
            return code == "OK" ? 0 : 2;
        }

        public static int Main(string[] args)
        {
            try
            {
                if (args.Length == 2 && args[0] == "--install") return Install(args[1]);
                if (args.Length != 1) return 64;
                if (args[0] == "--install") return Install(null);
                if (args[0] == "--apply")
                {
                    // Global scope also prevents a second interactive session from changing the desktop concurrently.
                    using (var mutex = new Mutex(false, @"Global\WindowsClaude.GuestSettings.Apply"))
                    {
                        bool held;
                        try { held = mutex.WaitOne(0); } catch (AbandonedMutexException) { held = true; }
                        if (!held) return 4;
                        try { return Apply(); } finally { mutex.ReleaseMutex(); }
                    }
                }
                return 64;
            }
            catch (Exception e)
            {
                if (args.Length > 0 && args[0] == "--install")
                {
                    MessageBox.Show("Не удалось установить настройку разрешения.\nКод: " + InstallFailure(e, InstallStage) + "\nПередай этот код для диагностики. Повторная установка не меняет диск Windows и личные файлы.", "Настройка Windows", MessageBoxButtons.OK, MessageBoxIcon.Error);
                }
                return 1;
            }
        }

        static DEVMODE Mode() { return new DEVMODE { dmSize = (short)Marshal.SizeOf(typeof(DEVMODE)) }; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct DEVMODE
        {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
            public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
            public int dmFields, dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
            public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
            public short dmLogPixels;
            public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency, dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight;
        }
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool EnumDisplaySettings(string device, int mode, ref DEVMODE settings);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int ChangeDisplaySettingsEx(string device, ref DEVMODE settings, IntPtr hwnd, uint flags, IntPtr parameter);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool GetVolumeInformation(string root, StringBuilder name, uint nameSize, out uint serial, out uint maxComponent, out uint flags, StringBuilder fs, uint fsSize);
    }
}

using System;
using System.Xml;
using System.IO;
using System.Web.Script.Serialization;
using WindowsClaude.GuestSettings;

public static class GuestSettingsRegression
{
    static int checks;
    static void Check(bool value, string name) { checks++; if (!value) throw new Exception(name); }
    static void Invalid(string value) { bool rejected = false; try { Program.ParseSettings(value); } catch (FormatException) { rejected = true; } Check(rejected, "reject " + value); }
    public static int Main(string[] args)
    {
        const string valid = "{\"schema\":1,\"session_nonce\":\"0123456789abcdef0123456789abcdef\",\"width\":1920,\"height\":1080}";
        var settings = Program.ParseSettings(valid);
        Check(settings.Width == 1920 && settings.Height == 1080, "valid config");
        Invalid(valid.Replace("1920", "639")); Invalid(valid.Replace("1080", "4321")); Invalid(valid.Replace("1920", "7681"));
        Invalid(valid.Replace("1920", "1920.0")); Invalid(valid.Replace("1920", "\"1920\"")); Invalid(valid.Replace("1920", "01920"));
        Invalid(valid.Replace("}", ",\"disk_size_gib\":80}"));
        Invalid(valid.Replace("{", "{\"schema\":1,")); Invalid(valid.Replace("}", ",\"command\":\"run\"}"));
        Invalid(valid.Replace("}", ",}")); Invalid(valid + "{}"); Invalid(valid.Replace("abcdef", "ABCDEF")); Invalid(new string(' ', 4097));
        Check(Program.IsSetupMarker("{\"app\":\"WindowsClaude\",\"schema\":1}"), "setup marker");
        Check(!Program.IsSetupMarker("{\"app\":\"WindowsClaude\",\"schema\":1,\"path\":\"x\"}"), "strict marker");
        Check(Program.IsQemu("QEMU") && !Program.IsQemu("QEMU compatible") && !Program.IsQemu("Microsoft Corporation"), "guest guard");
        Check(Program.SameLocalUser(null,"S-1-5-21-1-2-3-1001","guest","GUEST"),"local identity");
        Check(!Program.SameLocalUser("S-1-5-21-1-2-3-1001","S-1-5-21-1-2-3-1002","guest","guest"),"UAC identity changed");
        Check(!Program.SameLocalUser(null,"S-1-5-21-1-2-3-1001","domain","guest"),"domain identity refused");
        Check(Program.SettingsFileName=="SETTINGS.JSN" && Program.SetupFileName=="SETUP.JSN", "FAT 8.3 protocol");
        var doc=new XmlDocument(); doc.LoadXml(Program.TaskXml(@"C:\ProgramData\WindowsClaude\GuestSettings\GuestSettings.exe", "S-1-5-21-1-2-3-1001"));
        Check(doc.OuterXml.Contains("InteractiveToken") && doc.OuterXml.Contains("HighestAvailable") && doc.OuterXml.Contains("--apply") && !doc.OuterXml.Contains("Password"), "task contract");
        var ns=new XmlNamespaceManager(doc.NameTable); ns.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task");
        Check(doc.SelectSingleNode("//t:ExecutionTimeLimit",ns).InnerText=="PT0S", "no scheduler timeout");
        Check(doc.SelectSingleNode("//t:AllowHardTerminate",ns).InnerText=="false", "no forced task termination");
        Check(doc.SelectSingleNode("//t:MultipleInstancesPolicy",ns).InnerText=="IgnoreNew", "no scheduled overlap");
        bool sidRejected=false; try { Program.TaskXml("x", "S-1-5-18"); } catch(InvalidOperationException) { sidRejected=true; } Check(sidRejected,"no system task");
        Check(Program.TaskMissing(new FileNotFoundException()),"missing task is mapped to FileNotFoundException by COM interop");
        Check(Program.TaskMissing(new System.Runtime.InteropServices.COMException("missing",unchecked((int)0x80070002))),"missing task COM HRESULT");
        Check(!Program.TaskMissing(new UnauthorizedAccessException()) && !Program.TaskMissing(new IOException()),"do not ignore other scheduler failures");
        string failure=Program.InstallFailure(new FileNotFoundException("private path must not be exposed"),"TASK_LOOKUP");
        Check(failure.Contains("TASK_LOOKUP") && failure.Contains("0x80070002") && !failure.Contains("private path"),"bounded useful installation error without raw exception contents");
        string receipt=Program.Receipt(settings,1920,1080,"OK","OK");
        var parsed=new JavaScriptSerializer().DeserializeObject(receipt);
        Check(parsed != null && receipt.Length < 4096 && receipt.Contains(settings.Nonce) && receipt.Contains("\"success\":true"), "receipt");
        Check(Program.Receipt(settings,800,600,"DISPLAY_MODE_UNSUPPORTED","DISPLAY_MODE_UNSUPPORTED").Contains("\"success\":false"),"unsupported receipt");
        Check(receipt.Contains("\"display_code\":\"OK\"") && !receipt.Contains("disk"),"display only receipt");
        if(args.Length!=1) throw new Exception("Pass repository root for source safety checks");
        string source=File.ReadAllText(Path.Combine(args[0],@"guest\settings\GuestSettings.cs"));
        Check(!source.Contains(".Kill(") && !source.Contains("WaitForExit(90000)"),"no forced interruption");
        Check(!source.Contains("Resize-Partition") && !source.Contains("Get-Partition") && !source.Contains("Get-Disk") && !source.Contains("PowerShell("),"no storage commands or PowerShell dependency");
        Check(source.Contains(@"Global\WindowsClaude.GuestSettings.Apply") && source.Contains("mutex.WaitOne(0)") && source.Contains("mutex.ReleaseMutex()"),"one global apply instance");
        Check(!source.Contains("setup-marker.json") && !source.Contains("\"settings.json\""),"no long protocol paths");
        string build=File.ReadAllText(Path.Combine(args[0],@"scripts\Build-GuestSettings.ps1"));
        Check(build.Contains("/target:winexe") && source.Contains("MessageBox.Show("),"hidden apply and installer feedback");
        Console.WriteLine("GuestSettings pure regression: " + checks + " passed (no host device calls)."); return 0;
    }
}

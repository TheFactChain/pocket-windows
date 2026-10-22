using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

internal static class DistributionHarness
{
    static readonly object Gate = new object();
    static string phase = "Starting";
    static DateTime printed = DateTime.MinValue;
    [StructLayout(LayoutKind.Sequential)]
    struct FileInfoNative
    {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Modified;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInfoNative info);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFile(string path, uint access, uint sharing, IntPtr security, uint creation, uint flags, IntPtr template);
    static void Assert(bool condition, string message) { if (!condition) throw new InvalidDataException(message); }
    static bool ValidSetupSha(string value) { return value!=null && Regex.IsMatch(value,@"\A[a-f0-9]{64}\z"); }
    static void Progress(string value)
    {
        lock (Gate) {
            phase = value;
            if ((DateTime.UtcNow - printed).TotalSeconds >= 5) Print();
        }
    }
    static void Print() { Console.WriteLine(DateTime.UtcNow.ToString("o") + " " + phase); Console.Out.Flush(); printed=DateTime.UtcNow; }
    static string Hash(Stream input)
    {
        using (var sha=SHA256.Create()) {
            byte[] bytes=new byte[4*1024*1024]; int count;
            while ((count=input.Read(bytes,0,bytes.Length))!=0) sha.TransformBlock(bytes,0,count,null,0);
            sha.TransformFinalBlock(new byte[0],0,0);
            return BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant();
        }
    }
    static FileStream OpenSetup(string path)
    {
        var handle=CreateFile(path,0x80000000,1,IntPtr.Zero,3,0x00200000,IntPtr.Zero);
        try {
            if(handle.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            var stream=new FileStream(handle,FileAccess.Read);
            try { Identity(stream); return stream; } catch { stream.Dispose(); throw; }
        }
        catch { handle.Dispose(); throw; }
    }
    static string Identity(FileStream file)
    {
        FileInfoNative info;
        if (!GetFileInformationByHandle(file.SafeFileHandle,out info)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        Assert(info.Links==1 && (info.Attributes&0x410)==0,"File must be plain with exactly one link.");
        return info.Volume+":"+info.IndexHigh+":"+info.IndexLow;
    }
    static ulong BigEndian(byte[] data, int offset, int length)
    { ulong value=0; for(int i=offset;i<offset+length;i++) value=(value<<8)|data[i]; return value; }
    static Dictionary<string,object> Qcow(Stream disk)
    {
        byte[] h=new byte[104];
        Assert(disk.Read(h,0,h.Length)==h.Length && BigEndian(h,0,4)==0x514649fb,"Invalid qcow2 header.");
        ulong version=BigEndian(h,4,4), backing=BigEndian(h,8,8), backingLength=BigEndian(h,16,4), size=BigEndian(h,24,8), encryption=BigEndian(h,32,4);
        ulong incompatible=version==3 ? BigEndian(h,72,8) : 0;
        Assert((version==2 || version==3) && backing==0 && backingLength==0 && size>0 && encryption==0 && incompatible==0,"Disk is not standalone, clean, unencrypted qcow2.");
        return new Dictionary<string,object>{{"version",version},{"virtual_bytes",size},{"backing_offset",backing},{"backing_length",backingLength},{"encryption",encryption},{"incompatible_features",incompatible}};
    }
    static void Inventory(string root, string directory, HashSet<string> files, HashSet<string> directories)
    {
        foreach(string path in Directory.EnumerateFileSystemEntries(directory)) {
            var attributes=File.GetAttributes(path);
            Assert((attributes&FileAttributes.ReparsePoint)==0,"Reparse point in installation.");
            string relative=path.Substring(root.Length+1).Replace('\\','/');
            if((attributes&FileAttributes.Directory)!=0) { directories.Add(relative); Inventory(root,path,files,directories); }
            else Assert(files.Add(relative),"Duplicate installed filename.");
        }
    }
    static void Install(MethodInfo method, string payload, string output)
    {
        try { method.Invoke(null,new object[]{payload,output,CancellationToken.None,(Action<string>)Progress}); }
        catch(TargetInvocationException error) { throw error.InnerException ?? error; }
    }
    static void SelfTest()
    {
        byte[] bytes=new byte[104]; bytes[0]=81; bytes[1]=70; bytes[2]=73; bytes[3]=251; bytes[7]=3; bytes[31]=1;
        using(var stream=new MemoryStream(bytes)) Assert((ulong)Qcow(stream)["virtual_bytes"]==1,"qcow self-check failed");
        foreach(int offset in new[]{15,19,35,79}) {
            bytes[offset]=1; bool refused=false;
            try { using(var stream=new MemoryStream(bytes)) Qcow(stream); } catch(InvalidDataException) { refused=true; }
            Assert(refused,"qcow invalid metadata accepted"); bytes[offset]=0;
        }
        Assert(ValidSetupSha(new string('a',64)),"Expected Setup SHA256 must accept lowercase64hex");
        foreach(string invalid in new[]{"",new string('A',64),new string('g',64),new string('a',63),new string('a',65),new string('a',64)+"\n"})
            Assert(!ValidSetupSha(invalid),"Invalid expected Setup SHA256 accepted");
        Console.WriteLine("Distribution harness self-check: 12 assertions OK");
    }
    public static int Main(string[] args)
    {
        Console.OutputEncoding=new System.Text.UTF8Encoding(false);
        if(args.Length==1 && args[0]=="--self-test") { try { SelfTest(); return 0; } catch(Exception error) { Console.Error.WriteLine(error); return 1; } }
        if(args.Length!=5 || !ValidSetupSha(args[4])) { Console.Error.WriteLine("Use acceptance_distribution.ps1 with ExpectedSetupSHA256 from RELEASE.json (64 lowercase hex characters)."); return 2; }
        string setup=args[0], payload=args[1], output=args[2], report=args[3], expectedSetupSha=args[4];
        var result=new Dictionary<string,object>{{"passed",false},{"shortcut",false},{"ui_invoked",false},{"vm_started",false},{"second_pc_tested",false}};
        using(var heartbeat=new Timer(delegate { lock(Gate) Print(); },null,15000,15000)) {
            try {
                Assert(!File.Exists(output) && !Directory.Exists(output),"Output must be absent.");
                using(var setupLock=OpenSetup(setup)) {
                Assert(Hash(setupLock)==expectedSetupSha,"Native Setup SHA256 mismatch."); result["setup_sha256"]=expectedSetupSha;
                // Load the exact production executable; never call its entry point or create its Form.
                var assembly=Assembly.LoadFrom(setup);
                var installer=assembly.GetType("SetupInstaller",true);
                var install=installer.GetMethod("Install",BindingFlags.Static|BindingFlags.NonPublic,null,new[]{typeof(string),typeof(string),typeof(CancellationToken),typeof(Action<string>)},null);
                Assert(install!=null,"Production Install signature missing.");
                Progress("Production Setup validation and installation"); Install(install,payload,output);
                Dictionary<string,object> manifest;
                using(var stream=File.OpenRead(payload)) using(var zip=new ZipArchive(stream,ZipArchiveMode.Read)) {
                    var entry=zip.GetEntry("Windows-Claude/MANIFEST.json");
                    Assert(entry!=null && entry.Length<=16*1024*1024,"Missing or oversized manifest.");
                    using(var reader=new StreamReader(entry.Open())) manifest=new JavaScriptSerializer {MaxJsonLength=16*1024*1024,RecursionLimit=32}.Deserialize<Dictionary<string,object>>(reader.ReadToEnd());
                }
                Assert((string)manifest["kind"]=="public_distribution" && (bool)manifest["encrypted_profiles_included"]==false,"Public manifest required.");
                var rows=(System.Collections.IEnumerable)manifest["files"];
                var expected=new HashSet<string>(StringComparer.Ordinal);
                var expectedDirectories=new HashSet<string>(StringComparer.Ordinal);
                var disks=new Dictionary<string,object>();
                string windowsHash=null, windowsIdentity=null;
                int checkedFiles=0; long checkedBytes=0;
                foreach(Dictionary<string,object> row in rows) {
                    string name=(string)row["path"], path=Path.Combine(output,name.Replace('/',Path.DirectorySeparatorChar));
                    Assert(expected.Add(name),"Duplicate manifest path.");
                    string[] parts=name.Split('/');
                    Assert(!parts.Any(part=>new[]{"profiles","private","logs",".git",".beads","buildtools"}.Contains(part.ToLowerInvariant())) && !Path.GetFileName(name).StartsWith("Build-",StringComparison.OrdinalIgnoreCase),"Forbidden distribution file: "+name);
                    for(int i=name.LastIndexOf('/');i>=0;i=name.LastIndexOf('/',i-1)) { expectedDirectories.Add(name.Substring(0,i)); if(i==0) break; }
                    Progress("Verify installed file: "+name);
                    using(var file=File.OpenRead(path)) {
                        Assert(file.Length==Convert.ToInt64(row["bytes"]),"Installed length mismatch: "+name);
                        string digest=Hash(file); Assert(digest==(string)row["sha256"],"Installed SHA256 mismatch: "+name);
                        checkedBytes=checked(checkedBytes+file.Length); checkedFiles++;
                        if(name.EndsWith(".qcow2",StringComparison.Ordinal)) { file.Position=0; disks[name]=Qcow(file); }
                        if(name=="images/workvm-empty.qcow2") { windowsHash=digest; windowsIdentity=Identity(file); }
                    }
                }
                var actual=new HashSet<string>(StringComparer.Ordinal); var actualDirectories=new HashSet<string>(StringComparer.Ordinal);
                Inventory(output,output,actual,actualDirectories);
                Assert(actual.SetEquals(expected) && actualDirectories.SetEquals(expectedDirectories),"Installed file/directory set differs from internal manifest.");
                Assert(windowsHash!=null && disks.Count>=1,"Windows disk was not verified.");
                Progress("Repeat installation must refuse existing destination"); bool refused=false;
                try { Install(install,payload,output); } catch(InvalidDataException error) { refused=true; result["repeat_refusal"]=error.Message; }
                Assert(refused,"Repeat installation unexpectedly accepted existing destination.");
                using(var disk=File.OpenRead(Path.Combine(output,"images/workvm-empty.qcow2"))) {
                    Assert(Identity(disk)==windowsIdentity,"Windows disk identity changed after refusal.");
                    Progress("Verify Windows disk SHA256 after repeat refusal"); Assert(Hash(disk)==windowsHash,"Windows disk changed after refusal.");
                }
                var after=new HashSet<string>(StringComparer.Ordinal); var afterDirectories=new HashSet<string>(StringComparer.Ordinal);
                Inventory(output,output,after,afterDirectories); Assert(after.SetEquals(expected) && afterDirectories.SetEquals(expectedDirectories),"Repeat installation changed inventory.");
                result["passed"]=true; result["exact_manifest_files"]=checkedFiles; result["verified_bytes"]=checkedBytes;
                result["qcow_metadata"]=disks; result["windows_disk_identity"]=windowsIdentity; result["windows_disk_sha256"]=windowsHash;
                result["repeat_install_refused"]=true; result["output"]=output;
                Progress("PASS"); lock(Gate) Print(); return 0;
                }
            }
            catch(Exception error) { result["error"]=error.ToString(); Console.Error.WriteLine(error); return 1; }
            finally {
                result["completed_utc"]=DateTime.UtcNow.ToString("o");
                using(var file=new StreamWriter(new FileStream(report,FileMode.CreateNew,FileAccess.Write))) file.WriteLine(new JavaScriptSerializer {MaxJsonLength=16*1024*1024}.Serialize(result));
            }
        }
    }
}

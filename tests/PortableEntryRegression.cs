using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

internal static class PortableEntryRegression
{
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile(string path,uint access,uint sharing,IntPtr security,uint creation,uint flags,IntPtr template);
    static int checks;
    static void Assert(bool value,string message) { if(!value)throw new Exception(message);checks++; }
    [STAThread] public static int Main(string[] args)
    {
        string root=Path.Combine(args[0],"Копия программы"), desktop=Path.Combine(args[0],"Desktop");
        Directory.CreateDirectory(root);Directory.CreateDirectory(desktop);
        File.WriteAllText(Path.Combine(root,"Launcher.exe"),"fixture");
        Type entry=typeof(Bridge).Assembly.GetType("PortableEntry");
        Assert(entry!=null,"Launcher does not provide portable shortcut creation");
        MethodInfo create=entry.GetMethod("CreateShortcut",BindingFlags.Static|BindingFlags.NonPublic);
        string target=(string)create.Invoke(null,new object[]{root,desktop});
        Assert(File.Exists(target),"shortcut was not created");
        object shell=null,link=null;
        try
        {
            shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            link=shell.GetType().InvokeMember("CreateShortcut",BindingFlags.InvokeMethod,null,shell,new object[]{target});
            Assert((string)link.GetType().InvokeMember("TargetPath",BindingFlags.GetProperty,null,link,null)==Path.Combine(root,"Launcher.exe"),"shortcut launches another copy");
            Assert((string)link.GetType().InvokeMember("WorkingDirectory",BindingFlags.GetProperty,null,link,null)==root,"shortcut has wrong working directory");
        }
        finally { if(link!=null)Marshal.FinalReleaseComObject(link);if(shell!=null)Marshal.FinalReleaseComObject(shell); }
        byte[] before=File.ReadAllBytes(target);
        try {create.Invoke(null,new object[]{root,desktop});throw new Exception("existing shortcut overwritten");}
        catch(TargetInvocationException error) {Assert(error.InnerException is IOException,"conflict must report an IO error");}
        Assert(Convert.ToBase64String(before)==Convert.ToBase64String(File.ReadAllBytes(target)),"existing shortcut changed");
        Assert(Directory.GetFiles(desktop).Length==1,"temporary shortcut was left behind");
        File.Delete(target);File.WriteAllText(target,"another user's file");
        try {create.Invoke(null,new object[]{root,desktop});throw new Exception("foreign shortcut overwritten");}
        catch(TargetInvocationException error) {Assert(error.InnerException is IOException,"foreign shortcut not refused");}
        Assert(File.ReadAllText(target)=="another user's file","foreign shortcut changed");
        string scripts=Path.Combine(root,"scripts");Directory.CreateDirectory(scripts);
        string script=Path.Combine(scripts,"Prepare-Dependencies.ps1");
        File.WriteAllText(script,"param([string]$Project)\n[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)\n@{ok=$true;code='ready';received_root=$Project}|ConvertTo-Json -Compress",new System.Text.UTF8Encoding(true));
        using(var handle=CreateFile(script+":Zone.Identifier",0x40000000,3,IntPtr.Zero,2,0,IntPtr.Zero))
        using(var stream=new FileStream(handle,FileAccess.Write))
        using(var writer=new StreamWriter(stream))writer.Write("[ZoneTransfer]\r\nZoneId=3\r\n");
        string policy=Environment.GetEnvironmentVariable("PSExecutionPolicyPreference");
        try
        {
            Environment.SetEnvironmentVariable("PSExecutionPolicyPreference","RemoteSigned");
            var reply=Bridge.Run(root,"prepare-components",null);
            Assert(Bridge.Ok(reply),"bundled script marked from Internet did not run: "+Bridge.Message(reply));
            Assert(Convert.ToString(reply["received_root"])==root,"bundled script received wrong root");
            Assert(Environment.GetEnvironmentVariable("PSExecutionPolicyPreference")=="RemoteSigned","parent execution policy changed");
            using(var handle=CreateFile(script+":Zone.Identifier",0x80000000,3,IntPtr.Zero,3,0,IntPtr.Zero))
            using(var stream=new FileStream(handle,FileAccess.Read))
            using(var reader=new StreamReader(stream))Assert(reader.ReadToEnd().Contains("ZoneId=3"),"script Internet mark was removed");
        }
        finally {Environment.SetEnvironmentVariable("PSExecutionPolicyPreference",policy);}
        Console.WriteLine("PASS portable entry: "+checks+" checks");return 0;
    }
}

using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using SMBLibrary;
using Attributes=SMBLibrary.FileAttributes;

internal static class HostFileStoreRegression
{
    static bool junctionBlocked,hardlinkBlocked;
    static FileStatus lastFileStatus;
    static readonly System.Collections.Generic.List<ConfinedFileStore.RecoveryRecord> recoveryRecords=new System.Collections.Generic.List<ConfinedFileStore.RecoveryRecord>();
    static ConfinedFileStore RecordedStore(string root)
    {
        var store=new ConfinedFileStore(root);store.RecoveryRecorded=record=>recoveryRecords.Add(record);return store;
    }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool CreateHardLink(string path,string target,IntPtr security);
    [DllImport("kernel32.dll",SetLastError=true)]static extern Microsoft.Win32.SafeHandles.SafeFileHandle ReOpenFile(IntPtr original,uint access,uint share,uint flags);
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static void Ok(NTStatus status,string message){Assert(status==NTStatus.STATUS_SUCCESS,message+": "+status);}
    static NTStatus Open(INTFileStore store,string path,CreateDisposition disposition,out object handle,ShareAccess share=ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete)
    {
        return store.CreateFile(out handle,out lastFileStatus,path,AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.Normal,share,disposition,CreateOptions.FILE_NON_DIRECTORY_FILE,null);
    }
    static void CreateMatrix(string fixtures,bool nativeOnly)
    {
        int cases=0,recordedBefore=recoveryRecords.Count;
        foreach(CreateDisposition disposition in new[]{CreateDisposition.FILE_SUPERSEDE,CreateDisposition.FILE_OPEN,CreateDisposition.FILE_CREATE,CreateDisposition.FILE_OPEN_IF,CreateDisposition.FILE_OVERWRITE,CreateDisposition.FILE_OVERWRITE_IF})
        foreach(string kind in new[]{"missing","missing-directory","file","directory","directory-option"})
        foreach(bool deleteAccess in new[]{false,true})
        foreach(bool deleteOnClose in new[]{false,true})
        {
            string description=disposition+"/"+kind+"/DELETE="+deleteAccess+"/DOC="+deleteOnClose;
            NTStatus expected=NTStatus.STATUS_SUCCESS;FileStatus expectedInformation=0;
            foreach(bool control in nativeOnly?new[]{true}:new[]{true,false})
            {
                string directory=Path.Combine(fixtures,"create-"+cases+"-"+control);Directory.CreateDirectory(directory);string path=Path.Combine(directory,"item");
                bool missing=kind.StartsWith("missing",StringComparison.Ordinal);
                if(kind=="file")File.WriteAllText(path,"unchanged file contents");else if(!missing)Directory.CreateDirectory(path);
                if(!missing)File.SetAttributes(path,kind=="file"?System.IO.FileAttributes.Archive:System.IO.FileAttributes.Directory|System.IO.FileAttributes.Archive);
                System.IO.FileAttributes before=missing?0:File.GetAttributes(path);
                INTFileStore store=control?(INTFileStore)new SMBLibrary.Win32.NTDirectoryFileSystem(directory):RecordedStore(directory);
                AccessMask access=AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|(deleteAccess?AccessMask.DELETE:0);
                CreateOptions options=(kind=="directory"?(CreateOptions)0:kind=="missing-directory"||kind=="directory-option"?CreateOptions.FILE_DIRECTORY_FILE:CreateOptions.FILE_NON_DIRECTORY_FILE)|(deleteOnClose?CreateOptions.FILE_DELETE_ON_CLOSE:0);
                object handle;FileStatus information;NTStatus status=store.CreateFile(out handle,out information,"item",access,Attributes.Hidden,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,disposition,options,null);
                if(status==NTStatus.STATUS_SUCCESS)Ok(store.CloseFile(handle),"Matrix close "+description);
                else
                {
                    Assert(missing?!File.Exists(path)&&!Directory.Exists(path):File.GetAttributes(path)==before,"Failed create changed attributes or created an orphan: "+description+" control="+control+" status="+status);
                    if(kind=="file")Assert(File.ReadAllText(path)=="unchanged file contents","Failed create changed file contents: "+description+" control="+control);
                }
                foreach(string entry in Directory.GetFileSystemEntries(directory))Assert(Path.GetFileName(entry)=="item","Create left a temporary orphan: "+description+" control="+control);
                if(control){expected=status;expectedInformation=information;if(nativeOnly)Console.WriteLine(description+": "+status+"/"+information);}
                else
                {
                    bool unsupported=disposition==CreateDisposition.FILE_SUPERSEDE&&(options&CreateOptions.FILE_DIRECTORY_FILE)!=0&&(!deleteOnClose||deleteAccess);
                    if(unsupported)Assert(status==NTStatus.STATUS_NOT_SUPPORTED,"Supersede DIRECTORY_FILE is outside the supported whitelist: "+description);
                    else Assert((status==NTStatus.STATUS_SUCCESS)==(expected==NTStatus.STATUS_SUCCESS),"Create success differs from native control: "+description+" expected="+expected+" got="+status);
                    if(status==NTStatus.STATUS_SUCCESS)Assert(information==expectedInformation,"Create FileStatus differs from native control: "+description);
                }
            }
            cases++;
        }
        Assert(recoveryRecords.Count==recordedBefore,"Ordinary create matrix unexpectedly retained recovery data");
        Console.WriteLine("PASS: "+cases+" native create disposition/type/DELETE/delete-on-close controls"+(nativeOnly?"":"; confined parity except documented Supersede DIRECTORY_FILE whitelist; failed requests unchanged"));
    }
    static void SupersedeHardlinkControl(string fixtures)
    {
        string directory=Path.Combine(fixtures,"native-supersede-links");Directory.CreateDirectory(directory);string outside=Path.Combine(directory,"counterpart.txt"),link=Path.Combine(directory,"replace.txt");File.WriteAllText(outside,"counterpart must survive");
        Assert(CreateHardLink(link,outside,IntPtr.Zero),"Native hardlink control fixture could not be created: "+Marshal.GetLastWin32Error());
        INTFileStore store=new SMBLibrary.Win32.NTDirectoryFileSystem(directory);object handle;
        Ok(Open(store,"replace.txt",CreateDisposition.FILE_SUPERSEDE,out handle),"Native hardlink supersede control");Ok(store.CloseFile(handle),"Native supersede control close");
        Assert(new FileInfo(link).Length==0,"Native supersede control did not empty the requested file");
        Console.WriteLine("Native FILE_SUPERSEDE hardlink control: counterpart length="+new FileInfo(outside).Length+"; direct supersede must not be trusted to preserve other links.");
    }
    static void ParentPinCases(string fixtures)
    {
        string directory=Path.Combine(fixtures,"pinned-parent"),child=Path.Combine(directory,"parent");Directory.CreateDirectory(child);
        var store=new ConfinedFileStore(directory);var method=typeof(ConfinedFileStore).GetMethod("PinParents",System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic);
        object[] arguments={"parent\\child.txt",null};Ok((NTStatus)method.Invoke(store,arguments),"Pin parent for rename test");
        var pins=(System.Collections.Generic.List<Microsoft.Win32.SafeHandles.SafeFileHandle>)arguments[1];
        bool refused=false;
        try{try{Directory.Move(child,Path.Combine(directory,"moved-parent"));}catch(IOException){refused=true;}Assert(refused&&Directory.Exists(child),"Pinned parent must prevent local rename before a destructive create");}
        finally{foreach(var pin in pins)pin.Dispose();}
        Console.WriteLine("PASS: actual parent pins prevent local directory rename during a confined create.");
    }
    static void SupersedeCases(string fixtures)
    {
        int recordedBefore=recoveryRecords.Count;
        string root=Path.Combine(fixtures,"safe-supersede"),outside=Path.Combine(fixtures,"supersede-counterpart.txt");Directory.CreateDirectory(root);File.WriteAllText(outside,"counterpart must survive");
        INTFileStore store=RecordedStore(root);object handle;string target=Path.Combine(root,"replace.txt");
        Assert(CreateHardLink(target,outside,IntPtr.Zero),"Confined supersede hardlink fixture: "+Marshal.GetLastWin32Error());
        Ok(Open(store,"replace.txt",CreateDisposition.FILE_SUPERSEDE,out handle),"Confined hardlink supersede");int written;
        Ok(store.WriteFile(out written,handle,0,Encoding.UTF8.GetBytes("new entry")),"Write replacement identity");Ok(store.CloseFile(handle),"Replacement close");
        Assert(File.ReadAllText(outside)=="counterpart must survive"&&File.ReadAllText(target)=="new entry","Supersede replacement altered another hardlink's content");
        File.WriteAllText(target,"keep on sharing failure");File.SetAttributes(target,System.IO.FileAttributes.Archive);
        using(var busy=new FileStream(target,FileMode.Open,FileAccess.ReadWrite,FileShare.ReadWrite))
            Assert(Open(store,"replace.txt",CreateDisposition.FILE_SUPERSEDE,out handle)!=NTStatus.STATUS_SUCCESS&&handle==null,"Supersede must respect an existing handle without delete sharing");
        Assert(File.ReadAllText(target)=="keep on sharing failure"&&File.GetAttributes(target)==System.IO.FileAttributes.Archive,"Failed supersede changed old content/attributes");
        Assert(Directory.GetFileSystemEntries(root).Length==1,"Failed replacement left a stage file orphan");
        FileStatus information;
        Ok(store.CreateFile(out handle,out information,"limited.txt",AccessMask.GENERIC_READ|AccessMask.DELETE,Attributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_SUPERSEDE,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"Read-only supersede with explicit DELETE handle");
        Assert(store.WriteFile(out written,handle,0,new byte[]{1})==NTStatus.STATUS_ACCESS_DENIED,"Staging must not grant write access absent in the client's request");
        Assert(store.SetFileInformation(handle,new FileBasicInformation{FileAttributes=Attributes.Hidden})==NTStatus.STATUS_ACCESS_DENIED,"Staging must not grant WRITE_ATTRIBUTES absent in the client's request");
        Ok(store.SetFileInformation(handle,new FileDispositionInformation{DeletePending=true}),"Explicitly requested DELETE remains available");Ok(store.CloseFile(handle),"Limited supersede handle close");
        Assert(recoveryRecords.Count==recordedBefore,"Ordinary supersede unexpectedly retained recovery data");
        Console.WriteLine("PASS: fresh-entry supersede preserves hardlink counterpart, failed replace has no orphan, staged privileges are not granted to the client.");
    }
    static void SupersedeSharingCases(string fixtures)
    {
        bool matched=true;int recordedBefore=recoveryRecords.Count;
        foreach(bool existing in new[]{false,true})
        foreach(bool control in new[]{true,false})
        {
            string root=Path.Combine(fixtures,"supersede-sharing-"+existing+"-"+control);Directory.CreateDirectory(root);if(existing)File.WriteAllText(Path.Combine(root,"item"),"before");
            INTFileStore store=control?(INTFileStore)new SMBLibrary.Win32.NTDirectoryFileSystem(root):RecordedStore(root);object first,second;FileStatus information;
            Ok(store.CreateFile(out first,out information,"item",AccessMask.GENERIC_READ,Attributes.Normal,ShareAccess.Read,CreateDisposition.FILE_SUPERSEDE,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"First supersede read/shared-read existing="+existing+" control="+control);
            try
            {
                NTStatus status=store.CreateFile(out second,out information,"item",AccessMask.GENERIC_READ,Attributes.Normal,ShareAccess.Read,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null);
                if(status==NTStatus.STATUS_SUCCESS)Ok(store.CloseFile(second),"Second shared read close");
                Console.WriteLine("Second shared read existing="+existing+" native="+control+": "+status);
                matched&=status==NTStatus.STATUS_SUCCESS;
            }
            finally{Ok(store.CloseFile(first),"First shared read close");}
        }
        Assert(matched,"Supersede read-only FILE_OBJECT sharing must match native for both missing and existing files");
        Assert(recoveryRecords.Count==recordedBefore,"Shared-read supersede unexpectedly retained recovery data");
        Console.WriteLine("PASS: supersede read-only FILE_OBJECT sharing matches native for missing and existing files.");
    }
    static void MissingRecoveryRecorderCases(string fixtures)
    {
        foreach(bool existing in new[]{false,true})
        foreach(bool deleteAccess in new[]{false,true})
        {
            string root=Path.Combine(fixtures,"supersede-no-recorder-"+existing+"-"+deleteAccess),path=Path.Combine(root,"item");Directory.CreateDirectory(root);if(existing){File.WriteAllText(path,"guard sentinel");File.SetAttributes(path,System.IO.FileAttributes.Archive);}
            INTFileStore store=new ConfinedFileStore(root);object handle;FileStatus information;
            Assert(store.CreateFile(out handle,out information,"item",AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|(deleteAccess?AccessMask.DELETE:0),Attributes.Hidden,ShareAccess.Read,CreateDisposition.FILE_SUPERSEDE,CreateOptions.FILE_NON_DIRECTORY_FILE,null)==NTStatus.STATUS_NOT_SUPPORTED&&handle==null,"Supersede without recovery recorder must be rejected before mutation");
            Assert(existing?File.ReadAllText(path)=="guard sentinel"&&File.GetAttributes(path)==System.IO.FileAttributes.Archive:!File.Exists(path),"Guarded supersede changed the target");
            Assert(Directory.GetFileSystemEntries(root).Length==(existing?1:0),"Guarded supersede left an orphan");
        }
        Console.WriteLine("PASS: missing recovery recorder rejects Supersede before mutation for missing/existing and with/without DELETE.");
    }
    static void NativeReopenSharingControl(string fixtures)
    {
        string root=Path.Combine(fixtures,"native-reopen-sharing");Directory.CreateDirectory(root);INTFileStore store=new SMBLibrary.Win32.NTDirectoryFileSystem(root);object owner,second;FileStatus information;
        Ok(store.CreateFile(out owner,out information,"item",AccessMask.GENERIC_READ|AccessMask.DELETE,Attributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_CREATE,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"Reopen control owner");
        using(var observer=ReOpenFile((IntPtr)owner,0x80,7,0x00200000))
        {
            Assert(!observer.IsInvalid,"Native metadata observer reopen failed: "+Marshal.GetLastWin32Error());Ok(store.CloseFile(owner),"Close original DELETE FILE_OBJECT");
            using(var client=ReOpenFile(observer.DangerousGetHandle(),(uint)AccessMask.GENERIC_READ,(uint)ShareAccess.Read,0x00200000))
            {
                Assert(!client.IsInvalid,"Native exact client reopen failed: "+Marshal.GetLastWin32Error());
                Ok(store.CreateFile(out second,out information,"item",AccessMask.GENERIC_READ,Attributes.Normal,ShareAccess.Read,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"Second native shared read after metadata observer reopen");Ok(store.CloseFile(second),"Second native reopen-control close");
            }
        }
        Console.WriteLine("PASS: native metadata observer retains identity after DELETE owner closes; exact ReOpenFile client allows a second shared read.");
    }
    static void FailureEligibilityCases(string fixtures)
    {
        foreach(CreateDisposition disposition in new[]{CreateDisposition.FILE_SUPERSEDE,CreateDisposition.FILE_OPEN,CreateDisposition.FILE_CREATE,CreateDisposition.FILE_OPEN_IF,CreateDisposition.FILE_OVERWRITE,CreateDisposition.FILE_OVERWRITE_IF})
        foreach(bool deleteOnClose in new[]{false,true})
        {
            NTStatus controlStatus=0;
            foreach(bool control in new[]{true,false})
            {
                string directory=Path.Combine(fixtures,"readonly-"+disposition+"-"+deleteOnClose+"-"+control);Directory.CreateDirectory(directory);string path=Path.Combine(directory,"item");File.WriteAllText(path,"read-only sentinel");File.SetAttributes(path,System.IO.FileAttributes.Archive|System.IO.FileAttributes.ReadOnly);
                INTFileStore store=control?(INTFileStore)new SMBLibrary.Win32.NTDirectoryFileSystem(directory):RecordedStore(directory);object handle;FileStatus information;
                NTStatus status=store.CreateFile(out handle,out information,"item",AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.Hidden,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,disposition,CreateOptions.FILE_NON_DIRECTORY_FILE|(deleteOnClose?CreateOptions.FILE_DELETE_ON_CLOSE:0),null);
                if(status==NTStatus.STATUS_SUCCESS)Ok(store.CloseFile(handle),"Readonly control close");
                Assert(File.ReadAllText(path)=="read-only sentinel"&&File.GetAttributes(path)==(System.IO.FileAttributes.Archive|System.IO.FileAttributes.ReadOnly),"Read-only file changed: "+disposition+"/DOC="+deleteOnClose+" control="+control);
                if(control)controlStatus=status;else Assert((status==NTStatus.STATUS_SUCCESS)==(controlStatus==NTStatus.STATUS_SUCCESS),"Read-only create success differs from native: "+disposition+"/DOC="+deleteOnClose);
            }
        }
        string root=Path.Combine(fixtures,"eligibility"),file=Path.Combine(root,"mapped.txt");Directory.CreateDirectory(root);File.WriteAllText(file,"mapped sentinel");File.SetAttributes(file,System.IO.FileAttributes.Archive);
        INTFileStore confined=RecordedStore(root);object opened;FileStatus created;
        using(var stream=new FileStream(file,FileMode.Open,FileAccess.ReadWrite,FileShare.ReadWrite|FileShare.Delete))
        using(var mapping=System.IO.MemoryMappedFiles.MemoryMappedFile.CreateFromFile(stream,null,0,System.IO.MemoryMappedFiles.MemoryMappedFileAccess.ReadWrite,HandleInheritability.None,true))
        using(var view=mapping.CreateViewAccessor())
        {
            NTStatus status=confined.CreateFile(out opened,out created,"mapped.txt",AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.Hidden,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OVERWRITE,CreateOptions.FILE_NON_DIRECTORY_FILE,null);
            Assert(status!=NTStatus.STATUS_SUCCESS&&opened==null,"Mapped file truncation must fail");
            Assert(File.GetAttributes(file)==System.IO.FileAttributes.Archive,"Failed mapped-file overwrite must preserve attributes");
        }
        Assert(File.ReadAllText(file)=="mapped sentinel","Failed mapped-file overwrite must preserve content");
        foreach(CreateDisposition disposition in new[]{CreateDisposition.FILE_SUPERSEDE,CreateDisposition.FILE_CREATE,CreateDisposition.FILE_OPEN_IF,CreateDisposition.FILE_OVERWRITE_IF})
        {
            string name="readonly-new-"+disposition;
            NTStatus status=confined.CreateFile(out opened,out created,name,AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.ReadOnly,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,disposition,CreateOptions.FILE_NON_DIRECTORY_FILE|CreateOptions.FILE_DELETE_ON_CLOSE,null);
            if(status==NTStatus.STATUS_SUCCESS)confined.CloseFile(opened);
            if(disposition==CreateDisposition.FILE_SUPERSEDE)
            {
                var native=new SMBLibrary.Win32.NTDirectoryFileSystem(root);object control;FileStatus controlInfo;string controlName="native-"+name;
                NTStatus controlStatus=native.CreateFile(out control,out controlInfo,controlName,AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.ReadOnly,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,disposition,CreateOptions.FILE_NON_DIRECTORY_FILE|CreateOptions.FILE_DELETE_ON_CLOSE,null);
                if(controlStatus==NTStatus.STATUS_SUCCESS)native.CloseFile(control);
                Assert(controlStatus==NTStatus.STATUS_CANNOT_DELETE&&status==controlStatus,"Readonly Supersede/DOC must reach the native CANNOT_DELETE guard: native="+controlStatus+" confined="+status);
                Assert(!File.Exists(Path.Combine(root,controlName)),"Native readonly Supersede/DOC left an orphan");
                Console.WriteLine("PASS: readonly Supersede/DOC exact status native="+controlStatus+" confined="+status);
            }
            Assert(status!=NTStatus.STATUS_SUCCESS&&!File.Exists(Path.Combine(root,name)),"Rejected read-only delete-on-close create must not leave an orphan: "+disposition);
        }
        Assert(Directory.GetFileSystemEntries(root).Length==1,"Failure checks left a temporary file orphan");
        Console.WriteLine("PASS: readonly eligibility and mapped-file truncation failures preserve original content/attributes; readonly delete-on-close creates leave no orphan.");
    }
    sealed class SmbClient : SMBLibrary.Client.SMB2Client
    {
        internal SmbClient() : base(4000) { }
        internal bool Open(int port){return Connect(System.Net.IPAddress.Loopback,SMBTransportType.DirectTCPTransport,port);}
    }
    static void SupersedeOverSmb(string fixtures)
    {
        string root=Path.Combine(fixtures,"supersede-smb"),program=Path.Combine(root,"program");Directory.CreateDirectory(program);string code=Path.Combine(program,"run.py");File.WriteAllText(code,"protected sentinel");
        string input=new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(new{schema=1,session_nonce=new string('a',32),username="WCP_fixture",password="fixture_password_01234567890123456789",shares=new[]{new{name="PC_TEST",path=root}},@protected=new[]{program}});
        using(var server=HostFilesServer.Start(input))
        {
            var client=new SmbClient();try
            {
                Assert(client.Open(server.Port),"Supersede SMB connect");Ok(client.Login("","WCP_fixture","fixture_password_01234567890123456789"),"Supersede SMB login");NTStatus status;var files=client.TreeConnect("PC_TEST",out status);Ok(status,"Supersede SMB tree");
                object handle;FileStatus information;
                foreach(bool existing in new[]{false,true})
                {
                    Ok(files.CreateFile(out handle,out information,"wire.txt",AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_SUPERSEDE,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"SMB supersede with explicit DELETE access");
                    Assert(information==(existing?FileStatus.FILE_SUPERSEDED:FileStatus.FILE_CREATED),"SMB supersede create action lost");
                    int written;Ok(files.WriteFile(out written,handle,0,Encoding.UTF8.GetBytes(existing?"second":"first")),"SMB replacement write");Ok(files.CloseFile(handle),"SMB replacement close");
                    Assert(File.ReadAllText(Path.Combine(root,"wire.txt"))==(existing?"second":"first"),"SMB supersede did not replace the selected entry");
                }
                Assert(files.CreateFile(out handle,out information,"wire.txt",AccessMask.GENERIC_WRITE,Attributes.Hidden,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OVERWRITE,CreateOptions.FILE_NON_DIRECTORY_FILE|CreateOptions.FILE_DELETE_ON_CLOSE,null)==NTStatus.STATUS_INVALID_PARAMETER,"SMB overwrite DOC without DELETE must fail before mutation");
                Assert(File.ReadAllText(Path.Combine(root,"wire.txt"))=="second"&&File.GetAttributes(Path.Combine(root,"wire.txt"))!=System.IO.FileAttributes.Hidden,"Rejected SMB overwrite changed data/attributes");
                Ok(files.CreateFile(out handle,out information,"wire.txt",AccessMask.GENERIC_READ,Attributes.Normal,ShareAccess.Read,CreateDisposition.FILE_SUPERSEDE,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"SMB supersede without requested DELETE");
                object secondRead;
                try{Ok(files.CreateFile(out secondRead,out information,"wire.txt",AccessMask.GENERIC_READ,Attributes.Normal,ShareAccess.Read,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"SMB second shared read after no-DELETE supersede");Ok(files.CloseFile(secondRead),"SMB second shared read close");}
                finally{Ok(files.CloseFile(handle),"SMB no-DELETE supersede close");}
                Assert(new FileInfo(Path.Combine(root,"wire.txt")).Length==0,"SMB no-DELETE supersede did not create a fresh empty entry");
                Assert(files.CreateFile(out handle,out information,"program\\run.py",AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_SUPERSEDE,CreateOptions.FILE_NON_DIRECTORY_FILE,null)!=NTStatus.STATUS_SUCCESS,"Protected namespace accepted supersede over SMB");
                Ok(files.Disconnect(),"Supersede SMB disconnect");Ok(client.Logoff(),"Supersede SMB logoff");
            }
            finally{client.Disconnect();}
        }
        Assert(File.ReadAllText(code)=="protected sentinel"&&Directory.GetFileSystemEntries(root).Length==2,"SMB supersede changed protected content or left an orphan");
        Console.WriteLine("PASS: actual loopback SMB supersede with/without requested DELETE and second shared read, failed DOC overwrite unchanged, protected namespace denied.");
    }
    static bool Junction(string path,string target)
    {
        if(Directory.Exists(path)&&(File.GetAttributes(path)&System.IO.FileAttributes.ReparsePoint)!=0)return true;
        ProcessStartInfo info=new ProcessStartInfo("cmd.exe","/d /c mklink /J \""+path+"\" \""+target+"\""){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};
        using(Process process=Process.Start(info)){string output=process.StandardOutput.ReadToEnd()+process.StandardError.ReadToEnd();process.WaitForExit();if(process.ExitCode!=0){Console.Error.WriteLine("BLOCKED junction fixture: "+output);junctionBlocked=true;return false;}return true;}
    }
    sealed class Notification : IDisposable
    {
        internal readonly ManualResetEvent Done=new ManualResetEvent(false);
        internal NTStatus Status;internal byte[] Data;internal int Count;internal object Context;
        internal void Complete(NTStatus status,byte[] data,object context){Status=status;Data=data;Context=context;Interlocked.Increment(ref Count);Done.Set();}
        internal void Wait(string message){Assert(Done.WaitOne(3000),message+" exceeded three seconds");Assert(Count==1,message+" callback count");}
        public void Dispose(){Done.Dispose();}
    }
    static object OpenDirectory(INTFileStore store,string path)
    {
        object handle;FileStatus status;Ok(store.CreateFile(out handle,out status,path,AccessMask.GENERIC_READ,Attributes.Directory,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_DIRECTORY_FILE,null),"Notify directory open");return handle;
    }
    static void NotifyCases(string fixtures)
    {
        string root=Path.Combine(fixtures,"notify-selected");Directory.CreateDirectory(root);INTFileStore store=new ConfinedFileStore(root);object request,handle=OpenDirectory(store,"");
        using(Notification completion=new Notification())
        {
            Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,65537,completion.Complete,completion)==NTStatus.STATUS_INVALID_PARAMETER,"Notify must reject buffers over 64 KiB synchronously");
            Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,0,completion.Complete,completion)==NTStatus.STATUS_INVALID_PARAMETER,"Notify must reject an empty buffer");
            Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,4096,completion.Complete,completion)==NTStatus.STATUS_PENDING,"Directory notification must pend");
            File.WriteAllText(Path.Combine(root,"уведомление.txt"),"changed");completion.Wait("Directory write notification");
            Assert(completion.Status==NTStatus.STATUS_SUCCESS&&Object.ReferenceEquals(completion.Context,completion),"Notify status/context lost");
            Assert(completion.Data!=null&&completion.Data.Length>=12,"Notify must return FILE_NOTIFY_INFORMATION");int nameLength=BitConverter.ToInt32(completion.Data,8);
            Assert(nameLength>0&&nameLength<=completion.Data.Length-12&&Encoding.Unicode.GetString(completion.Data,12,nameLength)=="уведомление.txt","Notify Unicode record was not preserved");
        }
        Ok(store.CloseFile(handle),"Notify initial close");
        Stopwatch clock=new Stopwatch();
        for(int index=0;index<50;index++)
        using(Notification completion=new Notification())
        {
            handle=OpenDirectory(store,"");Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,index==0?65536:4096,completion.Complete,completion)==NTStatus.STATUS_PENDING,"Immediate notify must pend");clock.Restart();
            if(index%2==0){NTStatus cancelled=store.Cancel(request);Assert(cancelled==NTStatus.STATUS_SUCCESS||cancelled==NTStatus.STATUS_NOT_FOUND,"Immediate cancel status: "+cancelled);completion.Wait("Immediate cancel");Assert(completion.Status==NTStatus.STATUS_CANCELLED,"Cancel completion status: "+completion.Status);}
            else
            {
                Ok(store.CloseFile(handle),"Immediate notify close");Assert(clock.ElapsedMilliseconds<3000,"Close blocked on notification worker");
                object replacement;Ok(Open(store,"reuse-"+index+".txt",CreateDisposition.FILE_CREATE,out replacement),"Create while old native handle retires");Ok(store.CloseFile(replacement),"Replacement close");completion.Wait("Immediate close");Assert(completion.Status==NTStatus.STATUS_NOTIFY_CLEANUP,"Close completion must report cleanup: "+completion.Status);
            }
            if(index%2==0)Ok(store.CloseFile(handle),"Cancelled original close");
            Assert(store.Cancel(request)==NTStatus.STATUS_NOT_FOUND,"Completed request must not cancel a reused handle");
        }
        Notification[] simultaneous=new Notification[8];handle=OpenDirectory(store,"");
        try
        {
            foreach(int index in new[]{0,1,2,3,4,5,6,7}){simultaneous[index]=new Notification();Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,4096,simultaneous[index].Complete,simultaneous[index])==NTStatus.STATUS_PENDING,"Concurrent notify must pend");}
            clock.Restart();Ok(store.CloseFile(handle),"Close multiple pending requests");Assert(clock.ElapsedMilliseconds<3000,"Multiple-request Close blocked");
            foreach(Notification completion in simultaneous){completion.Wait("Multiple-request cleanup");Assert(completion.Status==NTStatus.STATUS_NOTIFY_CLEANUP,"All pending requests need cleanup");}
        }
        finally{foreach(Notification completion in simultaneous)if(completion!=null)completion.Dispose();}
        using(Notification completion=new Notification())
        {
            FileStatus status;Ok(store.CreateFile(out handle,out status,"",(AccessMask)DirectoryAccessMask.FILE_READ_ATTRIBUTES,Attributes.Directory,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_DIRECTORY_FILE,null),"Attributes-only directory open");
            Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,4096,completion.Complete,completion)==NTStatus.STATUS_ACCESS_DENIED,"Separate notify handle must not grant list access absent on original handle");Ok(store.CloseFile(handle),"Attributes-only directory close");
            string original=Path.Combine(root,"identity");Directory.CreateDirectory(original);handle=OpenDirectory(store,"identity");Directory.Move(original,Path.Combine(root,"old-identity"));Directory.CreateDirectory(original);
            Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,false,4096,completion.Complete,completion)==NTStatus.STATUS_ACCESS_DENIED,"Reopened notify directory must have the original file identity");Ok(store.CloseFile(handle),"Replaced directory close");
        }
        Console.WriteLine("PASS: real Unicode directory notification, 64KiB bound, immediate cancel/close, native-handle reuse, once-only callbacks within three seconds.");
    }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern uint GetShortPathName(string path,StringBuilder shortPath,uint size);
    static bool shortNameBlocked;
    static NTStatus OpenFolder(INTFileStore store,string path,out object handle,CreateOptions extra=0)
    {
        FileStatus status;return store.CreateFile(out handle,out status,path,AccessMask.GENERIC_READ|AccessMask.DELETE,Attributes.Directory,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_DIRECTORY_FILE|extra,null);
    }
    static void Denied(NTStatus status,object handle,INTFileStore store,string message){if(status==NTStatus.STATUS_SUCCESS)store.CloseFile(handle);Assert(status!=NTStatus.STATUS_SUCCESS,message);}
    static void ProtectedCases(string fixtures)
    {
        string root=Path.Combine(fixtures,"protect-share"),outer=Path.Combine(root,"Внешняя папка"),program=Path.Combine(outer,"Program Folder With Long Name");
        string scripts=Path.Combine(program,"scripts"),runner=Path.Combine(scripts,"run.py");Directory.CreateDirectory(scripts);File.WriteAllText(runner,"original code");
        string previousDirectory=Environment.CurrentDirectory;
        try
        {
            Environment.CurrentDirectory=program;
            INTFileStore outsideStore=new ConfinedFileStore(root,new[]{program});object outsideHandle;
            Ok(Open(outsideStore,"beside-program.txt",CreateDisposition.FILE_CREATE,out outsideHandle),"Protected current directory must not deny the whole volume");
            Ok(outsideStore.CloseFile(outsideHandle),"Beside program close");
        }
        finally{Environment.CurrentDirectory=previousDirectory;}
        string missing=Path.Combine(outer,"missing-protected");
        INTFileStore store=new ConfinedFileStore(root,new[]{program,missing});object handle;int written;
        FileStatus createdStatus;
        Denied(store.CreateFile(out handle,out createdStatus,"Внешняя папка\\missing-protected",AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE,Attributes.Directory,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_CREATE,CreateOptions.FILE_DIRECTORY_FILE,null),handle,store,"Missing protected directory created");
        Assert(!Directory.Exists(missing),"Denied creation left a protected directory");
        Ok(Open(store,"rename-source.txt",CreateDisposition.FILE_CREATE,out handle),"Missing protected rename source");
        Denied(store.SetFileInformation(handle,new FileRenameInformationType2{FileName="Внешняя папка\\missing-protected"}),null,store,"Rename into missing protected path");
        Ok(store.CloseFile(handle),"Rename source close");
        string relative="Внешняя папка\\Program Folder With Long Name";
        Denied(Open(store,relative+"\\scripts\\run.py",CreateDisposition.FILE_OPEN,out handle),handle,store,"Protected file opened through its own path");
        Denied(Open(store,relative.ToUpperInvariant()+"\\SCRIPTS\\RUN.PY",CreateDisposition.FILE_OVERWRITE,out handle),handle,store,"Protected file opened through a case variant");
        Denied(OpenFolder(store,relative,out handle),handle,store,"Protected folder itself opened for listing");
        Denied(Open(store,relative+"\\new.txt",CreateDisposition.FILE_CREATE,out handle),handle,store,"New file created inside protected folder");
        var name=new StringBuilder(1024);
        if(GetShortPathName(program,name,1024)>0&&!String.Equals(Path.GetFileName(name.ToString()),Path.GetFileName(program),StringComparison.OrdinalIgnoreCase))
            Denied(Open(store,"Внешняя папка\\"+Path.GetFileName(name.ToString())+"\\scripts\\run.py",CreateDisposition.FILE_OVERWRITE_IF,out handle),handle,store,"Protected folder reached through its 8.3 short name");
        else{shortNameBlocked=true;Console.Error.WriteLine("BLOCKED short-name fixture: 8.3 names are disabled on this volume");}
        Ok(Open(store,"outside-file.txt",CreateDisposition.FILE_CREATE,out handle),"Ordinary file beside protected folder");Ok(store.WriteFile(out written,handle,0,new byte[]{1,2,3}),"Ordinary write");
        Assert(store.SetFileInformation(handle,new FileRenameInformationType2{FileName=relative+"\\scripts\\run.py",ReplaceIfExists=true})!=NTStatus.STATUS_SUCCESS,"File moved over protected content");
        Assert(store.SetFileInformation(handle,new FileRenameInformationType2{FileName=relative+"\\moved.txt"})!=NTStatus.STATUS_SUCCESS,"File moved into protected folder");Ok(store.CloseFile(handle),"Ordinary close");
        Ok(Open(store,"Внешняя папка\\рядом.txt",CreateDisposition.FILE_CREATE,out handle),"Ancestor of protected folder remains usable");Ok(store.CloseFile(handle),"Ancestor file close");
        Ok(OpenFolder(store,"Внешняя папка",out handle),"Ancestor opens for listing");
        Assert(store.SetFileInformation(handle,new FileRenameInformationType2{FileName="Подмена"})!=NTStatus.STATUS_SUCCESS,"Ancestor of protected folder renamed");
        Assert(store.SetFileInformation(handle,new FileDispositionInformation{DeletePending=true})!=NTStatus.STATUS_SUCCESS,"Ancestor of protected folder marked for deletion");Ok(store.CloseFile(handle),"Ancestor close");
        Denied(OpenFolder(store,"Внешняя папка",out handle,CreateOptions.FILE_DELETE_ON_CLOSE),handle,store,"Ancestor opened with delete-on-close");
        Assert(Directory.Exists(outer)&&File.ReadAllText(runner)=="original code"&&!File.Exists(Path.Combine(program,"new.txt"))&&!File.Exists(Path.Combine(program,"moved.txt")),"Protected content changed");
        bool refused=false;try{new ConfinedFileStore(program,new[]{program});}catch(IOException){refused=true;}Assert(refused,"A protected folder cannot become a share root");
        refused=false;try{new ConfinedFileStore(scripts,new[]{program});}catch(IOException){refused=true;}Assert(refused,"A share inside a protected folder must be refused");
        Console.WriteLine("PASS: protected program folder denied by identity (path, case"+(shortNameBlocked?"":", 8.3 name")+", create, move-in), ancestors usable but not renamed or deleted.");
    }
    static void MainCases(string fixtures)
    {
        string root=Path.Combine(fixtures,"selected"),outside=Path.Combine(fixtures,"outside");Directory.CreateDirectory(root);Directory.CreateDirectory(outside);
        string outsideFile=Path.Combine(outside,"sentinel.txt");File.WriteAllText(outsideFile,"outside unchanged");
        INTFileStore store=RecordedStore(root);object handle;int written;byte[] data;
        foreach(string marker in new[]{"","\\"}){FileStatus rootStatus;Ok(store.CreateFile(out handle,out rootStatus,marker,AccessMask.GENERIC_READ,Attributes.Directory,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_DIRECTORY_FILE,null),"Share root marker");Ok(store.CloseFile(handle),"Root close");}
        Ok(Open(store,"Привет.txt",CreateDisposition.FILE_CREATE,out handle),"Unicode create");
        Assert(lastFileStatus==FileStatus.FILE_CREATED,"Native create FileStatus lost");
        byte[] expected=Encoding.UTF8.GetBytes("данные Windows");Ok(store.WriteFile(out written,handle,0,expected),"Write");Assert(written==expected.Length,"Write count");Ok(store.FlushFileBuffers(handle),"Flush");Ok(store.ReadFile(out data,handle,0,100),"Read");Assert(Encoding.UTF8.GetString(data)=="данные Windows","Read bytes");
        FileRenameInformationType2 rename=new FileRenameInformationType2{FileName="Переименован.txt",ReplaceIfExists=false};Ok(store.SetFileInformation(handle,rename),"Rename");Assert(rename.FileName=="Переименован.txt"&&rename.RootDirectory==0,"Rename must not mutate received information");
        var firstRename=new FileRenameInformationType1{FileName="\\Тип1.txt"};Ok(store.SetFileInformation(handle,firstRename),"Type1 rename");Assert(firstRename.FileName=="\\Тип1.txt"&&firstRename.RootDirectory==0,"Type1 rename mutated request");Ok(store.SetFileInformation(handle,rename),"Rename back");Ok(store.CloseFile(handle),"Close renamed");Assert(store.CloseFile(handle)==NTStatus.STATUS_INVALID_HANDLE,"Double close must preserve invalid-handle status");Assert(store.ReadFile(out data,handle,0,1)==NTStatus.STATUS_INVALID_HANDLE,"Closed handle must stay invalid");
        foreach(string path in new[]{"..\\outside\\sentinel.txt",".\\file","a\\..\\file","a\\\\file","a/../file","C:\\file","\\\\host\\share","\\??\\C:\\file","a:stream","trailing.","trailing ","NUL"})
        {
            NTStatus denied=Open(store,path,CreateDisposition.FILE_OPEN_IF,out handle);if(denied==NTStatus.STATUS_SUCCESS)store.CloseFile(handle);Assert(denied!=NTStatus.STATUS_SUCCESS,"Unsafe path was accepted: "+path);
        }
        Assert(File.ReadAllText(outsideFile)=="outside unchanged","Traversal changed outside data");
        if(Junction(Path.Combine(root,"junction"),outside)){Assert(Open(store,"junction\\sentinel.txt",CreateDisposition.FILE_OVERWRITE,out handle)!=NTStatus.STATUS_SUCCESS,"Junction ancestor accepted");Assert(Open(store,"junction",CreateDisposition.FILE_OPEN,out handle)!=NTStatus.STATUS_SUCCESS,"Final junction accepted");Assert(File.ReadAllText(outsideFile)=="outside unchanged","Junction changed outside data");}
        if(File.Exists(Path.Combine(root,"hard.txt"))||CreateHardLink(Path.Combine(root,"hard.txt"),outsideFile,IntPtr.Zero))
        {
            foreach(CreateDisposition mode in new[]{CreateDisposition.FILE_OPEN,CreateDisposition.FILE_OPEN_IF,CreateDisposition.FILE_OVERWRITE,CreateDisposition.FILE_OVERWRITE_IF})
            {NTStatus denied=Open(store,"hard.txt",mode,out handle);if(denied==NTStatus.STATUS_SUCCESS)store.CloseFile(handle);Assert(denied!=NTStatus.STATUS_SUCCESS,"Hardlink accepted: "+mode);Assert(File.ReadAllText(outsideFile)=="outside unchanged","Hardlink overwrite truncated outside data: "+mode);}
        }
        else{hardlinkBlocked=true;Console.Error.WriteLine("BLOCKED hardlink fixture: "+Marshal.GetLastWin32Error());}
        Ok(Open(store,"Переименован.txt",CreateDisposition.FILE_OPEN,out handle),"Open rename source");
        var escape=new FileRenameInformationType2{FileName="..\\outside\\escape.txt"};Assert(store.SetFileInformation(handle,escape)!=NTStatus.STATUS_SUCCESS,"Rename escaped root");Assert(escape.FileName=="..\\outside\\escape.txt","Denied rename mutated request");
        Assert(store.SetFileInformation(handle,new FileLinkInformationType2{FileName="linked.txt"})!=NTStatus.STATUS_SUCCESS,"Remote hardlink creation accepted");byte[] output;Assert(store.DeviceIOControl(handle,0x900A4,new byte[0],out output,0)==NTStatus.STATUS_NOT_SUPPORTED,"Arbitrary FSCTL accepted");
        Assert(store.SetFileSystemInformation(null)==NTStatus.STATUS_ACCESS_DENIED,"Volume mutation accepted");
        Ok(store.SetFileInformation(handle,new FileDispositionInformation{DeletePending=true}),"Delete mark");Ok(store.CloseFile(handle),"Delete close");Assert(!File.Exists(Path.Combine(root,"Переименован.txt")),"Delete failed");
        string lockPath=Path.Combine(root,"locked.txt");File.WriteAllText(lockPath,"native locking");
        using(FileStream locked=new FileStream(lockPath,FileMode.Open,FileAccess.ReadWrite,FileShare.None))Assert(Open(store,"locked.txt",CreateDisposition.FILE_OPEN,out handle)==NTStatus.STATUS_SHARING_VIOLATION,"ShareAccess conflict status lost");
        using(FileStream locked=new FileStream(lockPath,FileMode.Open,FileAccess.ReadWrite,FileShare.ReadWrite|FileShare.Delete))
        {
            locked.Lock(0,4);Ok(Open(store,"locked.txt",CreateDisposition.FILE_OPEN,out handle),"Native lock open");Assert(store.WriteFile(out written,handle,0,new byte[]{1})==NTStatus.STATUS_FILE_LOCK_CONFLICT,"Local byte lock not enforced");Ok(store.CloseFile(handle),"Close locked");locked.Unlock(0,4);
        }
        Assert(Open(store,"missing.txt",CreateDisposition.FILE_OPEN,out handle)==NTStatus.STATUS_OBJECT_NAME_NOT_FOUND,"Native missing-file status lost");
        NTStatus missingParent=Open(store,"missing-parent\\missing-child.txt",CreateDisposition.FILE_OPEN,out handle);Assert(missingParent==NTStatus.STATUS_OBJECT_NAME_NOT_FOUND||missingParent==NTStatus.STATUS_OBJECT_PATH_NOT_FOUND,"Win32 pin failure must return a mapped missing-parent status");
        Ok(Open(store,"overwrite.txt",CreateDisposition.FILE_CREATE,out handle),"Overwrite fixture");Ok(store.WriteFile(out written,handle,0,new byte[20]),"Overwrite fixture write");Ok(store.CloseFile(handle),"Overwrite fixture close");
        Ok(Open(store,"overwrite.txt",CreateDisposition.FILE_OVERWRITE,out handle),"Safe overwrite");Assert(lastFileStatus==FileStatus.FILE_OVERWRITTEN,"Safe overwrite FileStatus lost");Ok(store.CloseFile(handle),"Overwrite close");Assert(new FileInfo(Path.Combine(root,"overwrite.txt")).Length==0,"Overwrite did not truncate");
        Ok(Open(store,"overwrite.txt",CreateDisposition.FILE_SUPERSEDE,out handle),"Safe supersede");Assert(lastFileStatus==FileStatus.FILE_SUPERSEDED,"Supersede FileStatus lost");Ok(store.CloseFile(handle),"Supersede close");
        string race=Path.Combine(root,"race.txt");Exception raceError=null;bool stop=false;int linksCreated=0;
        Thread swapper=new Thread(delegate(){try{while(!Volatile.Read(ref stop)){try{File.Delete(race);if(CreateHardLink(race,outsideFile,IntPtr.Zero))Interlocked.Increment(ref linksCreated);}catch(IOException){}catch(UnauthorizedAccessException){}}}catch(Exception error){raceError=error;}});swapper.Start();
        try{for(int index=0;index<100;index++)foreach(CreateDisposition mode in new[]{CreateDisposition.FILE_OVERWRITE_IF,CreateDisposition.FILE_SUPERSEDE}){NTStatus status=Open(store,"race.txt",mode,out handle);if(status==NTStatus.STATUS_SUCCESS)store.CloseFile(handle);Assert(File.ReadAllText(outsideFile)=="outside unchanged","Hardlink race changed outside contents: "+mode);}}
        finally{Volatile.Write(ref stop,true);swapper.Join();}Assert(raceError==null,"Race fixture failed");
        if(linksCreated==0){hardlinkBlocked=true;Console.Error.WriteLine("BLOCKED hardlink race: no link could be created");}
        string moved=Path.Combine(fixtures,"old-root");Directory.Move(root,moved);Directory.CreateDirectory(root);Assert(Open(store,"new.txt",CreateDisposition.FILE_CREATE,out handle)!=NTStatus.STATUS_SUCCESS,"Replaced root accepted");Assert(!File.Exists(Path.Combine(root,"new.txt")),"Root swap wrote replacement directory");
        Directory.Move(root,Path.Combine(fixtures,"replacement-root"));if(Junction(root,outside)){Assert(Open(store,"sentinel.txt",CreateDisposition.FILE_OVERWRITE,out handle)!=NTStatus.STATUS_SUCCESS,"Root replaced by junction accepted");Assert(File.ReadAllText(outsideFile)=="outside unchanged","Replaced junction pin changed outside data");}
    }
    static int Main(string[] args)
    {
        try
        {
            string home=Path.GetDirectoryName(typeof(HostFileStoreRegression).Assembly.Location);
            bool notifyOnly=args.Length>0&&args[0]=="--notify-only";
            string fixtures=args.Length==2&&args[0]=="--fixtures"?Path.GetFullPath(args[1]):Path.Combine(home,"test-"+Guid.NewGuid().ToString("N"));
            Assert(String.Equals(Path.GetDirectoryName(fixtures),home,StringComparison.OrdinalIgnoreCase)&&Path.GetFileName(fixtures).StartsWith("test-",StringComparison.Ordinal),"Fixtures must be a direct test-* folder beside the isolated harness executable");
            Directory.CreateDirectory(fixtures);Console.WriteLine("Fixtures: "+fixtures);
            if(args.Length>0&&args[0]=="--native-create-only"){CreateMatrix(fixtures,true);SupersedeHardlinkControl(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--create-only"){CreateMatrix(fixtures,false);return 0;}
            if(args.Length>0&&args[0]=="--parent-pin-only"){ParentPinCases(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--native-hardlink-only"){SupersedeHardlinkControl(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--supersede-only"){SupersedeCases(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--eligibility-only"){FailureEligibilityCases(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--smb-create-only"){SupersedeOverSmb(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--supersede-sharing-only"){SupersedeSharingCases(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--supersede-contract-only"){SupersedeSharingCases(fixtures);MissingRecoveryRecorderCases(fixtures);return 0;}
            if(args.Length>0&&args[0]=="--native-reopen-sharing-only"){NativeReopenSharingControl(fixtures);return 0;}
            NotifyCases(fixtures);if(notifyOnly)return 0;ParentPinCases(fixtures);ProtectedCases(fixtures);MainCases(fixtures);CreateMatrix(fixtures,false);SupersedeCases(fixtures);SupersedeSharingCases(fixtures);MissingRecoveryRecorderCases(fixtures);NativeReopenSharingControl(fixtures);FailureEligibilityCases(fixtures);SupersedeOverSmb(fixtures);Console.WriteLine("PASS: confined native Unicode IO/rename/delete, traversal/root-swap denial, native sharing/byte locks/status; Supersede requires an explicit recovery recorder and retains documented compatibility restrictions.");
            if(!hardlinkBlocked)Console.WriteLine("PASS: hardlink open/overwrite denial and concurrent replacement leaves outside unchanged.");
            if(!junctionBlocked)Console.WriteLine("PASS: junction ancestor/final denial and outside unchanged.");
            return junctionBlocked||hardlinkBlocked?2:0;
        }
        catch(Exception error){Console.Error.WriteLine(error);Environment.Exit(1);return 1;}
    }
}

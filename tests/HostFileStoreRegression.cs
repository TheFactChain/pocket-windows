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
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool CreateHardLink(string path,string target,IntPtr security);
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static void Ok(NTStatus status,string message){Assert(status==NTStatus.STATUS_SUCCESS,message+": "+status);}
    static NTStatus Open(INTFileStore store,string path,CreateDisposition disposition,out object handle,ShareAccess share=ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete)
    {
        return store.CreateFile(out handle,out lastFileStatus,path,AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,Attributes.Normal,share,disposition,CreateOptions.FILE_NON_DIRECTORY_FILE,null);
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
    static void MainCases(string fixtures)
    {
        string root=Path.Combine(fixtures,"selected"),outside=Path.Combine(fixtures,"outside");Directory.CreateDirectory(root);Directory.CreateDirectory(outside);
        string outsideFile=Path.Combine(outside,"sentinel.txt");File.WriteAllText(outsideFile,"outside unchanged");
        INTFileStore store=new ConfinedFileStore(root);object handle;int written;byte[] data;
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
        Assert(Open(store,"overwrite.txt",CreateDisposition.FILE_SUPERSEDE,out handle)==NTStatus.STATUS_NOT_SUPPORTED,"Unsafe supersede must be explicitly unsupported");
        string race=Path.Combine(root,"race.txt");Exception raceError=null;bool stop=false;int linksCreated=0;
        Thread swapper=new Thread(delegate(){try{while(!Volatile.Read(ref stop)){try{File.Delete(race);if(CreateHardLink(race,outsideFile,IntPtr.Zero))Interlocked.Increment(ref linksCreated);}catch(IOException){}catch(UnauthorizedAccessException){}}}catch(Exception error){raceError=error;}});swapper.Start();
        try{for(int index=0;index<100;index++){NTStatus status=Open(store,"race.txt",CreateDisposition.FILE_OVERWRITE_IF,out handle);if(status==NTStatus.STATUS_SUCCESS)store.CloseFile(handle);Assert(File.ReadAllText(outsideFile)=="outside unchanged","Hardlink race changed outside contents");}}
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
            Directory.CreateDirectory(fixtures);Console.WriteLine("Fixtures: "+fixtures);NotifyCases(fixtures);if(notifyOnly)return 0;MainCases(fixtures);Console.WriteLine("PASS: confined native Unicode IO/rename/delete, traversal/root-swap denial, native sharing/byte locks/status.");
            if(!hardlinkBlocked)Console.WriteLine("PASS: hardlink open/overwrite denial and concurrent replacement leaves outside unchanged.");
            if(!junctionBlocked)Console.WriteLine("PASS: junction ancestor/final denial and outside unchanged.");
            return junctionBlocked||hardlinkBlocked?2:0;
        }
        catch(Exception error){Console.Error.WriteLine(error);Environment.Exit(1);return 1;}
    }
}

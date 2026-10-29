using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using SMBLibrary;
using SMBLibrary.Win32;
using Attributes=SMBLibrary.FileAttributes;

internal sealed partial class ConfinedFileStore : INTFileStore
{
    private readonly string root;
    private readonly INTFileStore inner;
    private uint rootVolume,rootHigh,rootLow;
    private bool rootKnown;
    // Identities, not paths: short names, case and links cannot alias a protected folder.
    private readonly HashSet<string> protectedFolders=new HashSet<string>(StringComparer.Ordinal),protectedAncestors=new HashSet<string>(StringComparer.Ordinal);
    private readonly HashSet<string> protectedNames=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    private sealed class OpenHandle
    {
        internal object Native;
        internal string Path;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileIdentity
    {
        internal uint Attributes,CreationLow,CreationHigh,AccessLow,AccessHigh,WriteLow,WriteHigh,Volume,SizeHigh,SizeLow,Links,IndexHigh,IndexLow;
    }
    [DllImport("kernel32.dll",EntryPoint="CreateFileW",CharSet=CharSet.Unicode,SetLastError=true)]
    private static extern SafeFileHandle PinFile(string path,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)]private static extern bool GetFileInformationByHandle(IntPtr handle,out FileIdentity information);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]private static extern uint GetFinalPathNameByHandle(IntPtr handle,StringBuilder path,uint size,uint flags);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)]private static extern uint GetDriveType(string root);
    [DllImport("kernel32.dll",EntryPoint="ReOpenFile",SetLastError=true)]private static extern SafeFileHandle NativeReopen(IntPtr original,uint access,uint share,uint flags);
    internal Func<IntPtr,uint,uint,uint,SafeFileHandle> Reopen=NativeReopen;
    internal string RecoveryDetail { get; private set; }
    internal sealed class RecoveryRecord { internal string Original,Backup,Code; }
    internal Action<RecoveryRecord> RecoveryRecorded;
    private static NTStatus LastStatus(){return Win32Status(Marshal.GetLastWin32Error());}
    internal ConfinedFileStore(string selectedRoot) : this(selectedRoot,new string[0]) { }
    internal ConfinedFileStore(string selectedRoot,IEnumerable<string> protectedPaths)
    {
        if(String.IsNullOrEmpty(selectedRoot)||selectedRoot.Length<3||!Char.IsLetter(selectedRoot[0])||selectedRoot[1]!=':'||selectedRoot[2]!='\\')throw new ArgumentException("A local drive path is required.","selectedRoot");
        string relative;string suffix=selectedRoot.Substring(3).TrimEnd('\\');
        if(!ValidPath(suffix,out relative)||relative!=suffix)throw new ArgumentException("An ordinary local folder is required.","selectedRoot");
        root=Path.GetFullPath(selectedRoot).TrimEnd('\\');if(root.Length==2)root+="\\";
        uint drive=GetDriveType(root.Substring(0,3));if(drive!=2&&drive!=3&&drive!=6)throw new ArgumentException("Network and unknown drives are not supported.","selectedRoot");
        if(protectedPaths==null)throw new ArgumentNullException("protectedPaths");
        foreach(string path in protectedPaths)Protect(path);
        inner=new NTDirectoryFileSystem(root);List<SafeFileHandle> pins;
        NTStatus status=PinParents("",out pins);Release(pins);if(status!=NTStatus.STATUS_SUCCESS)throw new IOException("Cannot confine the selected folder: "+status);
    }
    private static string Key(FileIdentity identity){return identity.Volume.ToString("x8")+":"+identity.IndexHigh.ToString("x8")+identity.IndexLow.ToString("x8");}
    private static string HandlePath(IntPtr handle)
    {
        var value=new StringBuilder(32768);uint size=GetFinalPathNameByHandle(handle,value,(uint)value.Capacity,0);
        if(size==0||size>=value.Capacity)throw new IOException("Cannot resolve an open file path.");
        string path=value.ToString();if(path.StartsWith(@"\\?\",StringComparison.Ordinal))path=path.Substring(4);
        path=Path.GetFullPath(BaseDataPath(path));return path.Length==3?path:path.TrimEnd('\\');
    }
    private bool ProtectedPath(string path)
    {
        path=Path.GetFullPath(BaseDataPath(path)).TrimEnd('\\');
        foreach(string folder in protectedNames)if(String.Equals(path,folder,StringComparison.OrdinalIgnoreCase)||path.StartsWith(folder+"\\",StringComparison.OrdinalIgnoreCase))return true;
        return false;
    }
    private void Protect(string path)
    {
        if(String.IsNullOrEmpty(path)||path.Length<3||!Char.IsLetter(path[0])||path[1]!=':'||path[2]!='\\')throw new ArgumentException("A local protected folder is required.","protectedPaths");
        // Follow links here: the real folder is protected, whichever name the guest later uses for it.
        string full=Path.GetFullPath(path).TrimEnd('\\'),current=full;bool target=true,canonical=false;
        protectedNames.Add(full);
        while(current.Length>2)
        {
            using(SafeFileHandle pin=PinFile(current,0x80,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero))
            {
                FileIdentity identity;
                if(pin.IsInvalid)
                {
                    int error=Marshal.GetLastWin32Error();if(error!=2&&error!=3)throw new IOException("Cannot inspect a protected folder: "+error);
                }
                else
                {
                    if(!GetFileInformationByHandle(pin.DangerousGetHandle(),out identity)||(identity.Attributes&0x10)==0)throw new IOException("Cannot inspect a protected folder.");
                    if(!canonical){protectedNames.Add(HandlePath(pin.DangerousGetHandle())+full.Substring(current.Length));canonical=true;}
                    (target?protectedFolders:protectedAncestors).Add(Key(identity));
                }
            }
            // Ancestors stay usable but cannot be renamed or deleted to swap in a look-alike path.
            target=false;string parent=Path.GetDirectoryName(current);current=parent==null?"":parent.TrimEnd('\\');
        }
    }
    private bool Pinned(object native)
    {
        FileIdentity identity;if(!GetFileInformationByHandle((IntPtr)native,out identity))return true;
        string key=Key(identity);return protectedFolders.Contains(key)||protectedAncestors.Contains(key);
    }
    private static bool ValidPath(string path,out string relative)
    {
        relative=null;if(path==null||path.StartsWith("\\\\",StringComparison.Ordinal)||path.IndexOf('/')>=0)return false;
        if(path.StartsWith("\\",StringComparison.Ordinal))path=path.Substring(1);
        if(path.Length==0){relative="";return true;}
        foreach(string part in path.Split('\\'))
        {
            if(part.Length==0||part=="."||part==".."||part.EndsWith(".",StringComparison.Ordinal)||part.EndsWith(" ",StringComparison.Ordinal))return false;
            foreach(char letter in part)if(letter<32||"<>:\"|?*".IndexOf(letter)>=0)return false;
            string device=part.Split('.')[0].ToUpperInvariant();
            if(device=="CON"||device=="PRN"||device=="AUX"||device=="NUL"||(device.Length==4&&(device.StartsWith("COM",StringComparison.Ordinal)||device.StartsWith("LPT",StringComparison.Ordinal))&&device[3]>='1'&&device[3]<='9'))return false;
        }
        relative=path;return true;
    }
    private static void Release(List<SafeFileHandle> pins){for(int index=pins.Count-1;index>=0;index--)pins[index].Dispose();}
    private NTStatus PinParents(string relative,out List<SafeFileHandle> pins)
    {
        pins=new List<SafeFileHandle>();string current=root.Substring(0,3);
        if(ProtectedPath(Path.Combine(root,relative)))return NTStatus.STATUS_ACCESS_DENIED;
        List<string> directories=new List<string>{current};
        if(root.Length>3)foreach(string part in root.Substring(3).Split('\\')){current=Path.Combine(current,part);directories.Add(current);}
        int separator=relative.LastIndexOf('\\');string parent=separator<0?"":relative.Substring(0,separator);
        if(!String.IsNullOrEmpty(parent))foreach(string part in parent.Split('\\')){current=Path.Combine(current,part);directories.Add(current);}
        foreach(string directory in directories)
        {
            SafeFileHandle pin=PinFile(directory,0x81,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);pins.Add(pin);
            if(pin.IsInvalid)return LastStatus();
            FileIdentity identity;if(!GetFileInformationByHandle(pin.DangerousGetHandle(),out identity))return LastStatus();
            if((identity.Attributes&0x400)!=0)return NTStatus.STATUS_ACCESS_DENIED;
            if((identity.Attributes&0x10)==0)return NTStatus.STATUS_NOT_A_DIRECTORY;
            if(protectedFolders.Contains(Key(identity)))return NTStatus.STATUS_ACCESS_DENIED;
            try{if(ProtectedPath(HandlePath(pin.DangerousGetHandle())))return NTStatus.STATUS_ACCESS_DENIED;}
            catch(IOException){return NTStatus.STATUS_ACCESS_DENIED;}
            if(String.Equals(directory.TrimEnd('\\'),root.TrimEnd('\\'),StringComparison.OrdinalIgnoreCase))
            {
                if(rootKnown&&(identity.Volume!=rootVolume||identity.IndexHigh!=rootHigh||identity.IndexLow!=rootLow))return NTStatus.STATUS_ACCESS_DENIED;
                rootVolume=identity.Volume;rootHigh=identity.IndexHigh;rootLow=identity.IndexLow;rootKnown=true;
            }
        }
        try
        {
            string parentPath=HandlePath(pins[pins.Count-1].DangerousGetHandle());
            if(relative.Length>0&&ProtectedPath(Path.Combine(parentPath+"\\",Path.GetFileName(relative))))return NTStatus.STATUS_ACCESS_DENIED;
        }
        catch(IOException){return NTStatus.STATUS_ACCESS_DENIED;}
        return NTStatus.STATUS_SUCCESS;
    }
    private NTStatus UseHandle(object handle,Func<object,NTStatus> action)
    {
        OpenHandle opened=handle as OpenHandle;if(opened==null)return NTStatus.STATUS_INVALID_HANDLE;
        // Keep Close from recycling a native handle while another request still uses it.
        lock(opened){return opened.Native.Equals(IntPtr.Zero)?NTStatus.STATUS_INVALID_HANDLE:action(opened.Native);}
    }
    private NTStatus CheckHandle(object native)
    {
        try{string current=HandlePath((IntPtr)native);if(!WithinCanonicalRoot(current)||ProtectedPath(current))return NTStatus.STATUS_ACCESS_DENIED;}
        catch(IOException){return NTStatus.STATUS_ACCESS_DENIED;}
        FileIdentity identity;if(!GetFileInformationByHandle((IntPtr)native,out identity))return LastStatus();
        return (identity.Attributes&0x400)!=0||((identity.Attributes&0x10)==0&&identity.Links>1)||protectedFolders.Contains(Key(identity))?NTStatus.STATUS_ACCESS_DENIED:NTStatus.STATUS_SUCCESS;
    }
    public NTStatus CreateFile(out object handle,out FileStatus fileStatus,string path,AccessMask desiredAccess,Attributes fileAttributes,ShareAccess shareAccess,CreateDisposition disposition,CreateOptions options,SecurityContext context)
    {
        handle=null;fileStatus=FileStatus.FILE_DOES_NOT_EXIST;string relative,baseRelative,stream;
        if(!TryDataPath(path,out relative,out baseRelative,out stream))return NTStatus.STATUS_ACCESS_DENIED;
        if(stream!=null&&((options&(CreateOptions.FILE_DIRECTORY_FILE|CreateOptions.FILE_DELETE_ON_CLOSE))!=0||disposition==CreateDisposition.FILE_SUPERSEDE))return NTStatus.STATUS_NOT_SUPPORTED;
        if((options&CreateOptions.FILE_OPEN_BY_FILE_ID)!=0)return NTStatus.STATUS_NOT_SUPPORTED;
        bool deleteOnClose=(options&CreateOptions.FILE_DELETE_ON_CLOSE)!=0;
        if(deleteOnClose&&((uint)desiredAccess&((uint)AccessMask.DELETE|(uint)AccessMask.GENERIC_ALL))==0)return NTStatus.STATUS_INVALID_PARAMETER;
        List<SafeFileHandle> pins;NTStatus status=PinParents(baseRelative,out pins);
        try
        {
            if(status!=NTStatus.STATUS_SUCCESS)return status;
            int directoryPin=pins.Count-1; // Capture the directory pin before an ADS base-file pin can be appended.
            if(stream!=null){SafeFileHandle basePin;status=PinDataBase(baseRelative,out basePin);pins.Add(basePin);if(status!=NTStatus.STATUS_SUCCESS)return status;}
            if(disposition==CreateDisposition.FILE_SUPERSEDE)return Supersede(out handle,out fileStatus,relative,desiredAccess,fileAttributes,shareAccess,options,context,HandlePath(pins[directoryPin].DangerousGetHandle()));
            bool overwrite=disposition==CreateDisposition.FILE_OVERWRITE||disposition==CreateDisposition.FILE_OVERWRITE_IF;
            if(overwrite&&(options&CreateOptions.FILE_DIRECTORY_FILE)!=0)return NTStatus.STATUS_INVALID_PARAMETER;
            if(deleteOnClose&&(fileAttributes&Attributes.ReadOnly)!=0&&(disposition==CreateDisposition.FILE_CREATE||disposition==CreateDisposition.FILE_OPEN_IF||disposition==CreateDisposition.FILE_OVERWRITE_IF))
            {
                using(SafeFileHandle probe=PinFile(Path.Combine(root,relative),0x80,7,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
                {if(probe.IsInvalid){int error=Marshal.GetLastWin32Error();if(error==2)return NTStatus.STATUS_CANNOT_DELETE;return Win32Status(error);}}
            }
            options=(options|CreateOptions.FILE_OPEN_REPARSE_POINT)&~CreateOptions.FILE_DELETE_ON_CLOSE;
            object native;
            status=inner.CreateFile(out native,out fileStatus,relative,desiredAccess|(AccessMask)FileAccessMask.FILE_READ_ATTRIBUTES,fileAttributes,shareAccess,overwrite?CreateDisposition.FILE_OPEN:disposition,options,context);
            if(overwrite&&disposition==CreateDisposition.FILE_OVERWRITE_IF&&status==NTStatus.STATUS_OBJECT_NAME_NOT_FOUND)
            {
                status=inner.CreateFile(out native,out fileStatus,relative,desiredAccess|(AccessMask)FileAccessMask.FILE_READ_ATTRIBUTES,fileAttributes,shareAccess,CreateDisposition.FILE_CREATE,options,context);
                if(status==NTStatus.STATUS_OBJECT_NAME_COLLISION)status=inner.CreateFile(out native,out fileStatus,relative,desiredAccess|(AccessMask)FileAccessMask.FILE_READ_ATTRIBUTES,fileAttributes,shareAccess,CreateDisposition.FILE_OPEN,options,context);
            }
            if(status!=NTStatus.STATUS_SUCCESS)return status;
            status=CheckHandle(native);
            FileIdentity identity=new FileIdentity();
            if(status==NTStatus.STATUS_SUCCESS&&!GetFileInformationByHandle((IntPtr)native,out identity))status=LastStatus();
            else if(status==NTStatus.STATUS_SUCCESS)
            {
                if(overwrite&&(identity.Attributes&0x10)!=0)status=NTStatus.STATUS_OBJECT_NAME_COLLISION;
                else if((overwrite||deleteOnClose)&&(identity.Attributes&1)!=0)status=NTStatus.STATUS_ACCESS_DENIED;
                else if(deleteOnClose&&Pinned(native))status=NTStatus.STATUS_ACCESS_DENIED;
            }
            if(status==NTStatus.STATUS_SUCCESS&&overwrite&&fileStatus!=FileStatus.FILE_CREATED)
            {
                if(((uint)desiredAccess&((uint)AccessMask.GENERIC_WRITE|(uint)AccessMask.GENERIC_ALL|(uint)FileAccessMask.FILE_WRITE_DATA))==0)status=NTStatus.STATUS_ACCESS_DENIED;
                else
                {
                    FileInformation original=null;
                    if(fileAttributes!=0&&stream==null)
                    {
                        status=inner.GetFileInformation(out original,native,FileInformationClass.FileBasicInformation);
                        if(status==NTStatus.STATUS_SUCCESS)status=inner.SetFileInformation(native,new FileBasicInformation{FileAttributes=fileAttributes});
                    }
                    if(status==NTStatus.STATUS_SUCCESS)
                    {
                        status=inner.SetFileInformation(native,new FileEndOfFileInformation{EndOfFile=0});
                        // A mapped view can refuse truncation after the attribute update.
                        if(status!=NTStatus.STATUS_SUCCESS&&original!=null)
                        {NTStatus restored=inner.SetFileInformation(native,original);if(restored!=NTStatus.STATUS_SUCCESS)status=restored;}
                    }
                    if(status==NTStatus.STATUS_SUCCESS)fileStatus=FileStatus.FILE_OVERWRITTEN;
                }
            }
            if(status==NTStatus.STATUS_SUCCESS&&deleteOnClose)status=inner.SetFileInformation(native,new FileDispositionInformation{DeletePending=true});
            if(status!=NTStatus.STATUS_SUCCESS){inner.CloseFile(native);return status;}
            handle=new OpenHandle{Native=native,Path=relative};return status;
        }
        finally{Release(pins);}
    }
    private NTStatus Supersede(out object handle,out FileStatus fileStatus,string relative,AccessMask desiredAccess,Attributes attributes,ShareAccess share,CreateOptions options,SecurityContext context,string canonicalParent)
    {
        handle=null;fileStatus=FileStatus.FILE_DOES_NOT_EXIST;if(relative.Length==0)return NTStatus.STATUS_ACCESS_DENIED;
        const CreateOptions supported=CreateOptions.FILE_NON_DIRECTORY_FILE|CreateOptions.FILE_OPEN_REPARSE_POINT|CreateOptions.FILE_WRITE_THROUGH|CreateOptions.FILE_SEQUENTIAL_ONLY|CreateOptions.FILE_RANDOM_ACCESS|CreateOptions.FILE_NO_INTERMEDIATE_BUFFERING|CreateOptions.FILE_DELETE_ON_CLOSE|CreateOptions.FILE_OPEN_NO_RECALL;
        if((options&~supported)!=0||RecoveryRecorded==null)return NTStatus.STATUS_NOT_SUPPORTED;
        if((options&CreateOptions.FILE_DELETE_ON_CLOSE)!=0&&(attributes&Attributes.ReadOnly)!=0)return NTStatus.STATUS_CANNOT_DELETE;
        string parent=Path.GetDirectoryName(relative)??"",id=Guid.NewGuid().ToString("N");
        string temporary=Path.Combine(parent,".~wcp-"+id+".tmp"),backup=Path.Combine(parent,".~wcp-"+id+".backup");
        string targetPath=Path.Combine(canonicalParent,Path.GetFileName(relative)),temporaryPath=Path.Combine(canonicalParent,Path.GetFileName(temporary)),backupPath=Path.Combine(canonicalParent,Path.GetFileName(backup));
        bool existed;FileIdentity original=new FileIdentity(),fresh=new FileIdentity();
        using(SafeFileHandle probe=PinFile(Path.Combine(root,relative),0x80,7,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
        {
            existed=!probe.IsInvalid;
            if(existed)
            {
                if(!GetFileInformationByHandle(probe.DangerousGetHandle(),out original))return LastStatus();
                if((original.Attributes&0x400)!=0)return NTStatus.STATUS_ACCESS_DENIED;
                try{if(ProtectedPath(HandlePath(probe.DangerousGetHandle())))return NTStatus.STATUS_ACCESS_DENIED;}
                catch(IOException){return NTStatus.STATUS_ACCESS_DENIED;}
                if((original.Attributes&0x10)!=0)return NTStatus.STATUS_OBJECT_NAME_COLLISION;
                if((original.Attributes&1)!=0)return NTStatus.STATUS_ACCESS_DENIED;
            }
            else{int error=Marshal.GetLastWin32Error();if(error!=2)return Win32Status(error);}
        }
        object staged=null,previous=null;SafeFileHandle observer=null,client=null;
        bool backed=false,placed=false,committed=false;NTStatus status;
        try
        {
            // Prepare a fresh identity; never truncate an existing hardlink.
            FileStatus ignored;
            if(existed)
            {
                // Deliberate FILE_IN_USE limitation: do not share DELETE merely to
                // match native supersede when another DELETE FILE_OBJECT is alive.
                status=inner.CreateFile(out previous,out ignored,relative,AccessMask.DELETE|(AccessMask)FileAccessMask.FILE_READ_ATTRIBUTES,0,ShareAccess.Read|ShareAccess.Write,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE|CreateOptions.FILE_OPEN_REPARSE_POINT,context);
                if(status!=NTStatus.STATUS_SUCCESS){previous=null;return status;}
                if(!OwnedAt((IntPtr)previous,original,targetPath))return NTStatus.STATUS_ACCESS_DENIED;
                FileIdentity current;if(!GetFileInformationByHandle((IntPtr)previous,out current))return LastStatus();
                if((current.Attributes&1)!=0)return NTStatus.STATUS_ACCESS_DENIED;
            }
            status=inner.CreateFile(out staged,out ignored,temporary,desiredAccess|AccessMask.DELETE|(AccessMask)(FileAccessMask.FILE_READ_ATTRIBUTES|FileAccessMask.FILE_WRITE_ATTRIBUTES),attributes,ShareAccess.Read|ShareAccess.Write,CreateDisposition.FILE_CREATE,(options&~CreateOptions.FILE_DELETE_ON_CLOSE)|CreateOptions.FILE_NON_DIRECTORY_FILE|CreateOptions.FILE_OPEN_REPARSE_POINT,context);
            if(status!=NTStatus.STATUS_SUCCESS){staged=null;return status;}
            if(!GetFileInformationByHandle((IntPtr)staged,out fresh))return LastStatus();
            status=CheckHandle(staged);if(status!=NTStatus.STATUS_SUCCESS)return status;
            observer=NativeReopen((IntPtr)staged,0x80,7,0x00200000);
            if(observer.IsInvalid)return ReopenFailure();
            if(!OwnedAt(observer.DangerousGetHandle(),fresh,temporaryPath))return NTStatus.STATUS_ACCESS_DENIED;
            if(existed)
            {
                status=inner.SetFileInformation(previous,new FileRenameInformationType2{FileName=backup,ReplaceIfExists=false});
                if(status!=NTStatus.STATUS_SUCCESS)return status;
                backed=true;
            }
            status=inner.SetFileInformation(staged,new FileRenameInformationType2{FileName=relative,ReplaceIfExists=false});
            if(status!=NTStatus.STATUS_SUCCESS)return status;
            placed=true;inner.CloseFile(staged);staged=null;
            // DuplicateHandle would retain staging FILE_OBJECT.DeleteAccess. Close
            // that FILE_OBJECT before reopening the observed identity for the client.
            AccessMask access=desiredAccess|AccessMask.SYNCHRONIZE|(AccessMask)FileAccessMask.FILE_READ_ATTRIBUTES;
            if((options&CreateOptions.FILE_NO_INTERMEDIATE_BUFFERING)!=0)access=(AccessMask)((uint)access&~(uint)FileAccessMask.FILE_APPEND_DATA);
            client=Reopen(observer.DangerousGetHandle(),(uint)access,(uint)share,ReopenFlags(options));
            if(client==null||client.IsInvalid)return ReopenFailure();
            status=CheckHandle(client.DangerousGetHandle());if(status!=NTStatus.STATUS_SUCCESS)return status;
            if(!OwnedAt(client.DangerousGetHandle(),fresh,targetPath))return NTStatus.STATUS_ACCESS_DENIED;
            // Only this point commits; failures above still have the original entry.
            committed=true;handle=new OpenHandle{Native=client.DangerousGetHandle(),Path=relative};client.SetHandleAsInvalid();fileStatus=existed?FileStatus.FILE_SUPERSEDED:FileStatus.FILE_CREATED;return NTStatus.STATUS_SUCCESS;
        }
        finally
        {
            if(client!=null)client.Dispose();
            try
            {
                if(committed)
                {
                    if(backed&&(!OwnedAt((IntPtr)previous,original,backupPath)||inner.SetFileInformation(previous,new FileDispositionInformation{DeletePending=true})!=NTStatus.STATUS_SUCCESS))RecordRecovery("SUPERSEDE_BACKUP_RETAINED",relative,Path.GetFileName(backup));
                }
                else
                {
                    bool removed=true;object cleanup=staged;SafeFileHandle reopened=null;
                    try
                    {
                        if(cleanup==null&&observer!=null&&!observer.IsInvalid)
                        {
                            reopened=NativeReopen(observer.DangerousGetHandle(),0x10180,3,0x00200000);
                            if(!reopened.IsInvalid)cleanup=reopened.DangerousGetHandle();else removed=false;
                        }
                        if(cleanup!=null)
                        {
                            removed=OwnedAt((IntPtr)cleanup,fresh,placed?targetPath:temporaryPath);
                            if(removed)removed=inner.SetFileInformation(cleanup,new FileBasicInformation{FileAttributes=Attributes.Normal})==NTStatus.STATUS_SUCCESS&&inner.SetFileInformation(cleanup,new FileDispositionInformation{DeletePending=true})==NTStatus.STATUS_SUCCESS;
                        }
                    }
                    finally
                    {
                        if(reopened!=null)reopened.Dispose();
                        if(staged!=null){inner.CloseFile(staged);staged=null;}
                        // Release the observer before restoring the old name, so a
                        // deleted new entry cannot keep that name delete-pending.
                        if(observer!=null){observer.Dispose();observer=null;}
                    }
                    if(backed)
                    {
                        if(!OwnedAt((IntPtr)previous,original,backupPath)||inner.SetFileInformation(previous,new FileRenameInformationType2{FileName=relative,ReplaceIfExists=false})!=NTStatus.STATUS_SUCCESS)RecordRecovery("SUPERSEDE_ROLLBACK_BACKUP_RETAINED",relative,Path.GetFileName(backup));
                        else if(!removed)RecordRecovery("SUPERSEDE_CLEANUP_UNVERIFIED",relative,placed?"":Path.GetFileName(temporary));
                    }
                    else if(!removed)RecordRecovery("SUPERSEDE_NEW_ENTRY_RETAINED",relative,placed?"":Path.GetFileName(temporary));
                }
            }
            finally
            {
                if(observer!=null)observer.Dispose();
                if(previous!=null)inner.CloseFile(previous);
            }
        }
    }
    private void RecordRecovery(string code,string original,string backup)
    {
        RecoveryDetail=code+":"+backup;
        try{RecoveryRecorded(new RecoveryRecord{Code=code,Original=original,Backup=backup});}
        catch(Exception){RecoveryDetail+=":RECOVERY_OUTPUT_FAILED";}
    }
    private static NTStatus ReopenFailure()
    {
        NTStatus status=LastStatus();return status==NTStatus.STATUS_SUCCESS?NTStatus.STATUS_ACCESS_DENIED:status;
    }
    private bool OwnedAt(IntPtr handle,FileIdentity identity,string canonicalPath)
    {
        try
        {
            FileIdentity current;if(!GetFileInformationByHandle(handle,out current)||(current.Attributes&0x400)!=0||Key(current)!=Key(identity))return false;
            string actual=HandlePath(handle);return String.Equals(actual,canonicalPath,StringComparison.OrdinalIgnoreCase)&&!ProtectedPath(actual);
        }
        catch(IOException){return false;}
    }
    private static uint ReopenFlags(CreateOptions options)
    {
        uint flags=0x00200000;
        if((options&CreateOptions.FILE_WRITE_THROUGH)!=0)flags|=0x80000000;
        if((options&CreateOptions.FILE_NO_INTERMEDIATE_BUFFERING)!=0)flags|=0x20000000;
        if((options&CreateOptions.FILE_RANDOM_ACCESS)!=0)flags|=0x10000000;
        if((options&CreateOptions.FILE_SEQUENTIAL_ONLY)!=0)flags|=0x08000000;
        if((options&CreateOptions.FILE_DELETE_ON_CLOSE)!=0)flags|=0x04000000;
        if((options&CreateOptions.FILE_OPEN_NO_RECALL)!=0)flags|=0x00100000;
        return flags;
    }
    public NTStatus SetFileInformation(object handle,FileInformation information)
    {
        return UseHandle(handle,native=>SetInformation((OpenHandle)handle,information));
    }
    private NTStatus SetInformation(OpenHandle opened,FileInformation information)
    {
        NTStatus status=CheckHandle(opened.Native);if(status!=NTStatus.STATUS_SUCCESS)return status;
        if(information==null)return NTStatus.STATUS_INVALID_PARAMETER;
        Type type=information.GetType();
        if(NamedDataPath(opened.Path)&&type!=typeof(FileEndOfFileInformation)&&type!=typeof(FileAllocationInformation)&&type!=typeof(FilePositionInformation))return NTStatus.STATUS_NOT_SUPPORTED;
        if((type==typeof(FileRenameInformationType1)||type==typeof(FileRenameInformationType2)||type==typeof(FileDispositionInformation))&&Pinned(opened.Native))return NTStatus.STATUS_ACCESS_DENIED;
        if(type==typeof(FileRenameInformationType1)||type==typeof(FileRenameInformationType2))
        {
            if(opened.Path.Length==0)return NTStatus.STATUS_ACCESS_DENIED;
            var first=information as FileRenameInformationType1;var second=information as FileRenameInformationType2;
            if((first!=null&&first.RootDirectory!=0)||(second!=null&&second.RootDirectory!=0))return NTStatus.STATUS_ACCESS_DENIED;
            string destination;if(!ValidPath(first!=null?first.FileName:second.FileName,out destination)||destination.Length==0)return NTStatus.STATUS_ACCESS_DENIED;
            List<SafeFileHandle> sourcePins,targetPins;status=PinParents(opened.Path,out sourcePins);
            try
            {
                if(status!=NTStatus.STATUS_SUCCESS)return status;
                status=PinParents(destination,out targetPins);
                try
                {
                    if(status!=NTStatus.STATUS_SUCCESS)return status;
                    string target=Path.Combine(root,destination);
                    using(SafeFileHandle probe=PinFile(target,0x80,1,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
                    {
                        if(!probe.IsInvalid){status=CheckHandle(probe.DangerousGetHandle());if(status!=NTStatus.STATUS_SUCCESS)return status;}
                        else{int error=Marshal.GetLastWin32Error();if(error!=2&&error!=3)return Win32Status(error);}
                    }
                    var copy=new FileRenameInformationType2{FileName=destination,ReplaceIfExists=first!=null?first.ReplaceIfExists:second.ReplaceIfExists};
                    status=inner.SetFileInformation(opened.Native,copy);if(status==NTStatus.STATUS_SUCCESS)opened.Path=destination;return status;
                }
                finally{Release(targetPins);}
            }
            finally{Release(sourcePins);}
        }
        if(type!=typeof(FileBasicInformation)&&type!=typeof(FileDispositionInformation)&&type!=typeof(FileEndOfFileInformation)&&type!=typeof(FileAllocationInformation)&&type!=typeof(FilePositionInformation))return NTStatus.STATUS_NOT_SUPPORTED;
        if(opened.Path.Length==0)return NTStatus.STATUS_ACCESS_DENIED;
        return inner.SetFileInformation(opened.Native,information);
    }
    public NTStatus CloseFile(object handle){return UseHandle(handle,native=>{CancelNotifications((OpenHandle)handle);((OpenHandle)handle).Native=IntPtr.Zero;return inner.CloseFile(native);});}
    public NTStatus ReadFile(out byte[] data,object handle,long offset,int count){byte[] bytes=null;NTStatus status=UseCheckedHandle(handle,native=>inner.ReadFile(out bytes,native,offset,count));data=bytes;return status;}
    public NTStatus WriteFile(out int count,object handle,long offset,byte[] data){int written=0;NTStatus status=UseHandle(handle,native=>{NTStatus checkedStatus=CheckHandle(native);return checkedStatus==NTStatus.STATUS_SUCCESS?inner.WriteFile(out written,native,offset,data):checkedStatus;});count=written;return status;}
    public NTStatus FlushFileBuffers(object handle){return UseHandle(handle,native=>inner.FlushFileBuffers(native));}
    public NTStatus LockFile(object handle,long offset,long length,bool exclusive){return UseHandle(handle,native=>inner.LockFile(native,offset,length,exclusive));}
    public NTStatus UnlockFile(object handle,long offset,long length){return UseHandle(handle,native=>inner.UnlockFile(native,offset,length));}
    public NTStatus QueryDirectory(out List<QueryDirectoryFileInformation> result,object handle,string pattern,FileInformationClass informationClass){List<QueryDirectoryFileInformation> entries=null;NTStatus status=UseCheckedHandle(handle,native=>inner.QueryDirectory(out entries,native,pattern,informationClass));result=entries;return status;}
    public NTStatus GetFileInformation(out FileInformation result,object handle,FileInformationClass informationClass){FileInformation value=null;NTStatus status=UseCheckedHandle(handle,native=>inner.GetFileInformation(out value,native,informationClass));result=value;return status;}
    public NTStatus GetFileSystemInformation(out FileSystemInformation result,FileSystemInformationClass informationClass){NTStatus status=inner.GetFileSystemInformation(out result,informationClass);if(status==NTStatus.STATUS_SUCCESS)result=MaskCapabilities(result);return status;}
    public NTStatus SetFileSystemInformation(FileSystemInformation information){return NTStatus.STATUS_ACCESS_DENIED;}
    public NTStatus GetSecurityInformation(out SecurityDescriptor result,object handle,SecurityInformation information){SecurityDescriptor value=null;NTStatus status=UseCheckedHandle(handle,native=>inner.GetSecurityInformation(out value,native,information));result=value;return status;}
    public NTStatus SetSecurityInformation(object handle,SecurityInformation information,SecurityDescriptor descriptor){return UseCheckedHandle(handle,native=>inner.SetSecurityInformation(native,information,descriptor));}
    public NTStatus DeviceIOControl(object handle,uint code,byte[] input,out byte[] output,int size){output=null;return NTStatus.STATUS_NOT_SUPPORTED;}
}

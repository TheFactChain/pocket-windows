using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
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
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)]private static extern uint GetDriveType(string root);
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
    private void Protect(string path)
    {
        if(String.IsNullOrEmpty(path)||path.Length<3||!Char.IsLetter(path[0])||path[1]!=':'||path[2]!='\\')throw new ArgumentException("A local protected folder is required.","protectedPaths");
        // Follow links here: the real folder is protected, whichever name the guest later uses for it.
        string current=Path.GetFullPath(path).TrimEnd('\\');bool target=true;
        while(current.Length>2)
        {
            using(SafeFileHandle pin=PinFile(current,0x80,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero))
            {
                FileIdentity identity;
                if(pin.IsInvalid||!GetFileInformationByHandle(pin.DangerousGetHandle(),out identity)||(identity.Attributes&0x10)==0){if(target)return;}
                else (target?protectedFolders:protectedAncestors).Add(Key(identity));
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
        List<string> directories=new List<string>{current};
        if(root.Length>3)foreach(string part in root.Substring(3).Split('\\')){current=Path.Combine(current,part);directories.Add(current);}
        int separator=relative.LastIndexOf('\\');string parent=separator<0?"":relative.Substring(0,separator);
        if(!String.IsNullOrEmpty(parent))foreach(string part in parent.Split('\\')){current=Path.Combine(current,part);directories.Add(current);}
        foreach(string directory in directories)
        {
            SafeFileHandle pin=PinFile(directory,0x80,1,IntPtr.Zero,3,0x02200000,IntPtr.Zero);pins.Add(pin);
            if(pin.IsInvalid)return LastStatus();
            FileIdentity identity;if(!GetFileInformationByHandle(pin.DangerousGetHandle(),out identity))return LastStatus();
            if((identity.Attributes&0x400)!=0)return NTStatus.STATUS_ACCESS_DENIED;
            if((identity.Attributes&0x10)==0)return NTStatus.STATUS_NOT_A_DIRECTORY;
            if(protectedFolders.Contains(Key(identity)))return NTStatus.STATUS_ACCESS_DENIED;
            if(String.Equals(directory.TrimEnd('\\'),root.TrimEnd('\\'),StringComparison.OrdinalIgnoreCase))
            {
                if(rootKnown&&(identity.Volume!=rootVolume||identity.IndexHigh!=rootHigh||identity.IndexLow!=rootLow))return NTStatus.STATUS_ACCESS_DENIED;
                rootVolume=identity.Volume;rootHigh=identity.IndexHigh;rootLow=identity.IndexLow;rootKnown=true;
            }
        }
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
        FileIdentity identity;if(!GetFileInformationByHandle((IntPtr)native,out identity))return LastStatus();
        return (identity.Attributes&0x400)!=0||((identity.Attributes&0x10)==0&&identity.Links>1)||protectedFolders.Contains(Key(identity))?NTStatus.STATUS_ACCESS_DENIED:NTStatus.STATUS_SUCCESS;
    }
    public NTStatus CreateFile(out object handle,out FileStatus fileStatus,string path,AccessMask desiredAccess,Attributes fileAttributes,ShareAccess shareAccess,CreateDisposition disposition,CreateOptions options,SecurityContext context)
    {
        handle=null;fileStatus=FileStatus.FILE_DOES_NOT_EXIST;string relative;
        if(!ValidPath(path,out relative))return NTStatus.STATUS_ACCESS_DENIED;
        // shortcut: native supersede replaces file identity before validation; reject until a safe equivalent exists.
        if(disposition==CreateDisposition.FILE_SUPERSEDE||(options&CreateOptions.FILE_OPEN_BY_FILE_ID)!=0)return NTStatus.STATUS_NOT_SUPPORTED;
        List<SafeFileHandle> pins;NTStatus status=PinParents(relative,out pins);
        try
        {
            if(status!=NTStatus.STATUS_SUCCESS)return status;
            bool overwrite=disposition==CreateDisposition.FILE_OVERWRITE||disposition==CreateDisposition.FILE_OVERWRITE_IF;
            bool deleteOnClose=(options&CreateOptions.FILE_DELETE_ON_CLOSE)!=0;
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
            if(status==NTStatus.STATUS_SUCCESS&&overwrite&&fileStatus!=FileStatus.FILE_CREATED)
            {
                if(((uint)desiredAccess&((uint)AccessMask.GENERIC_WRITE|(uint)AccessMask.GENERIC_ALL|(uint)FileAccessMask.FILE_WRITE_DATA))==0)status=NTStatus.STATUS_ACCESS_DENIED;
                else
                {
                    if(fileAttributes!=0)status=inner.SetFileInformation(native,new FileBasicInformation{FileAttributes=fileAttributes});
                    if(status==NTStatus.STATUS_SUCCESS)status=inner.SetFileInformation(native,new FileEndOfFileInformation{EndOfFile=0});
                    if(status==NTStatus.STATUS_SUCCESS)fileStatus=FileStatus.FILE_OVERWRITTEN;
                }
            }
            if(status==NTStatus.STATUS_SUCCESS&&deleteOnClose&&Pinned(native))status=NTStatus.STATUS_ACCESS_DENIED;
            if(status==NTStatus.STATUS_SUCCESS&&deleteOnClose)status=inner.SetFileInformation(native,new FileDispositionInformation{DeletePending=true});
            if(status!=NTStatus.STATUS_SUCCESS){inner.CloseFile(native);return status;}
            handle=new OpenHandle{Native=native,Path=relative};return status;
        }
        finally{Release(pins);}
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
    public NTStatus ReadFile(out byte[] data,object handle,long offset,int count){byte[] bytes=null;NTStatus status=UseHandle(handle,native=>inner.ReadFile(out bytes,native,offset,count));data=bytes;return status;}
    public NTStatus WriteFile(out int count,object handle,long offset,byte[] data){int written=0;NTStatus status=UseHandle(handle,native=>{NTStatus checkedStatus=CheckHandle(native);return checkedStatus==NTStatus.STATUS_SUCCESS?inner.WriteFile(out written,native,offset,data):checkedStatus;});count=written;return status;}
    public NTStatus FlushFileBuffers(object handle){return UseHandle(handle,native=>inner.FlushFileBuffers(native));}
    public NTStatus LockFile(object handle,long offset,long length,bool exclusive){return UseHandle(handle,native=>inner.LockFile(native,offset,length,exclusive));}
    public NTStatus UnlockFile(object handle,long offset,long length){return UseHandle(handle,native=>inner.UnlockFile(native,offset,length));}
    public NTStatus QueryDirectory(out List<QueryDirectoryFileInformation> result,object handle,string pattern,FileInformationClass informationClass){List<QueryDirectoryFileInformation> entries=null;NTStatus status=UseHandle(handle,native=>inner.QueryDirectory(out entries,native,pattern,informationClass));result=entries;return status;}
    public NTStatus GetFileInformation(out FileInformation result,object handle,FileInformationClass informationClass){FileInformation value=null;NTStatus status=UseHandle(handle,native=>inner.GetFileInformation(out value,native,informationClass));result=value;return status;}
    public NTStatus GetFileSystemInformation(out FileSystemInformation result,FileSystemInformationClass informationClass){return inner.GetFileSystemInformation(out result,informationClass);}
    public NTStatus SetFileSystemInformation(FileSystemInformation information){return NTStatus.STATUS_ACCESS_DENIED;}
    public NTStatus GetSecurityInformation(out SecurityDescriptor result,object handle,SecurityInformation information){SecurityDescriptor value=null;NTStatus status=UseHandle(handle,native=>inner.GetSecurityInformation(out value,native,information));result=value;return status;}
    public NTStatus SetSecurityInformation(object handle,SecurityInformation information,SecurityDescriptor descriptor){return UseHandle(handle,native=>inner.SetSecurityInformation(native,information,descriptor));}
    public NTStatus DeviceIOControl(object handle,uint code,byte[] input,out byte[] output,int size){output=null;return NTStatus.STATUS_NOT_SUPPORTED;}
}

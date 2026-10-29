using System;
using System.IO;
using Microsoft.Win32.SafeHandles;
using SMBLibrary;

internal sealed partial class ConfinedFileStore
{
    // Stream syntax is accepted only after an ordinary, nonempty base file path.
    internal static bool TryDataPath(string path,out string relative,out string baseRelative,out string stream)
    {
        relative=baseRelative=stream=null;
        if(path==null)return false;
        int colon=path.IndexOf(':');
        if(colon<0){if(!ValidPath(path,out relative))return false;baseRelative=relative;return true;}
        string candidate;
        if(!ValidPath(path.Substring(0,colon),out candidate)||candidate.Length==0)return false;
        string[] suffix=path.Substring(colon+1).Split(':');
        if(suffix.Length>2||(suffix.Length==2&&!String.Equals(suffix[1],"$DATA",StringComparison.OrdinalIgnoreCase)))return false;
        if(suffix[0].Length==0)
        {
            if(suffix.Length!=2)return false;
            relative=baseRelative=candidate;stream="";return true; // Still preflight the base before accepting unnamed :$DATA.
        }
        string name;
        if(suffix[0].Length>255||suffix[0].IndexOf('\\')>=0||!ValidPath(suffix[0],out name))return false;
        baseRelative=candidate;stream=name;relative=candidate+":"+name;return true;
    }
    internal static string BaseDataPath(string path)
    {
        if(path==null)return null;
        int start=path.Length>=3&&Char.IsLetter(path[0])&&path[1]==':'&&path[2]=='\\'?2:0;
        int colon=path.IndexOf(':',start);return colon<0?path:path.Substring(0,colon);
    }
    private static bool NamedDataPath(string relative){return relative.IndexOf(':')>=0;}
    private bool WithinCanonicalRoot(string path)
    {
        using(SafeFileHandle pin=PinFile(root,0x81,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
        {
            FileIdentity identity;
            if(pin.IsInvalid||!GetFileInformationByHandle(pin.DangerousGetHandle(),out identity)||
               (identity.Attributes&0x410)!=0x10||!rootKnown||identity.Volume!=rootVolume||identity.IndexHigh!=rootHigh||identity.IndexLow!=rootLow)return false;
            string canonical;
            try{canonical=HandlePath(pin.DangerousGetHandle());}catch(IOException){return false;}
            return String.Equals(path,canonical,StringComparison.OrdinalIgnoreCase)||path.StartsWith(canonical.TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase);
        }
    }
    private NTStatus PinDataBase(string baseRelative,out SafeFileHandle pin)
    {
        pin=PinFile(Path.Combine(root,baseRelative),0x81,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);
        if(pin.IsInvalid)return LastStatus();
        FileIdentity identity;
        if(!GetFileInformationByHandle(pin.DangerousGetHandle(),out identity))return LastStatus();
        if((identity.Attributes&0x410)!=0||identity.Links!=1)return NTStatus.STATUS_ACCESS_DENIED;
        return CheckHandle(pin.DangerousGetHandle());
    }
    private NTStatus UseCheckedHandle(object handle,Func<object,NTStatus> action)
    {
        return UseHandle(handle,native=>{NTStatus status=CheckHandle(native);return status==NTStatus.STATUS_SUCCESS?action(native):status;});
    }
    internal static FileSystemInformation MaskCapabilities(FileSystemInformation information)
    {
        var attributes=information as FileFsAttributeInformation;if(attributes==null)return information;
        // Existing hardlinks are still denied: enumerating names cannot fence new local links.
        const FileSystemAttributes supported=FileSystemAttributes.CasePreservedNames|FileSystemAttributes.UnicodeOnDisk|FileSystemAttributes.NamedStreams|FileSystemAttributes.ReadOnlyVolume;
        return new FileFsAttributeInformation{FileSystemAttributes=attributes.FileSystemAttributes&supported,
            MaximumComponentNameLength=attributes.MaximumComponentNameLength,FileSystemName=attributes.FileSystemName};
    }
}

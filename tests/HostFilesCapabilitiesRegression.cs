using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using SMBLibrary;
using Attributes=SMBLibrary.FileAttributes;

internal static class HostFilesCapabilitiesRegression
{
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern bool CreateHardLinkW(string name,string existing,IntPtr security);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string name,uint access,uint share,IntPtr security,uint disposition,uint flags,IntPtr template);
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static void Ok(NTStatus status,string message){Assert(status==NTStatus.STATUS_SUCCESS,message+": "+status);}
    static NTStatus Open(ConfinedFileStore store,string path,out object handle,CreateDisposition disposition=CreateDisposition.FILE_OPEN,CreateOptions options=CreateOptions.FILE_NON_DIRECTORY_FILE)
    {
        FileStatus ignored;return store.CreateFile(out handle,out ignored,path,AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE,Attributes.Normal,
            ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,disposition,options,null);
    }
    static void Parser()
    {
        string relative,basePath,stream;
        foreach(string path in new[]{"file.txt:Zone.Identifier","sub\\файл:метаданные:$DATA","\\sub\\file:meta:$data","file::$DATA","ordinary.txt",""})
            Assert(ConfinedFileStore.TryDataPath(path,out relative,out basePath,out stream),"Valid data path refused: "+path);
        Assert(ConfinedFileStore.TryDataPath("sub\\file.txt:meta:$DATA",out relative,out basePath,out stream)&&relative=="sub\\file.txt:meta"&&basePath=="sub\\file.txt"&&stream=="meta","Explicit DATA type normalization");
        Assert(ConfinedFileStore.TryDataPath("file::$DATA",out relative,out basePath,out stream)&&relative=="file"&&stream=="","Unnamed DATA still requires base preflight");
        foreach(string path in new[]{null,":Zone.Identifier","file:","file::","file:meta:","file:meta:$INDEX_ALLOCATION","file:meta:$DATA:extra","parent:stream\\file","file:meta\\child","file:meta/child","file:..","file:.","file:bad ","file:bad.","file:bad\n","file:NUL","file:CON.txt","file:meta*","..\\file:Zone.Identifier","\\\\server\\file:meta","C:\\file:meta","\\??\\file:meta","file::$INDEX_ALLOCATION","file:::$DATA","file:"+new string('x',256)})
            Assert(!ConfinedFileStore.TryDataPath(path,out relative,out basePath,out stream),"Malicious data path accepted: "+path);
        Assert(ConfinedFileStore.BaseDataPath(@"C:\folder\file:Zone.Identifier:$DATA")==@"C:\folder\file","Absolute base stripping");
        Assert(ConfinedFileStore.BaseDataPath(@"C:\folder\file")==@"C:\folder\file","Drive colon retained");
        Assert(ConfinedFileStore.BaseDataPath("a:Zone.Identifier")=="a","Single-character relative base is not a drive prefix");
    }
    static void Streams(string fixtures)
    {
        string root=Path.Combine(fixtures,"streams"),protectedFolder=Path.Combine(root,"private");Directory.CreateDirectory(protectedFolder);
        string file=Path.Combine(root,"file.txt");File.WriteAllText(file,"original base bytes",Encoding.UTF8);
        File.SetAttributes(file,System.IO.FileAttributes.Hidden|System.IO.FileAttributes.Archive);var baseAttributes=File.GetAttributes(file);
        Directory.CreateDirectory(Path.Combine(root,"ordinary-directory"));
        File.WriteAllText(Path.Combine(protectedFolder,"secret.txt"),"private fixture");
        var store=new ConfinedFileStore(root,new[]{protectedFolder});object handle;int written;byte[] bytes;
        byte[] zone=Encoding.UTF8.GetBytes("[ZoneTransfer]\r\nZoneId=3\r\n");
        File.WriteAllText(Path.Combine(root,"a"),"short base");
        Ok(Open(store,"a:Zone.Identifier:$DATA",out handle,CreateDisposition.FILE_OPEN_IF),"Create stream on a one-character base");
        Ok(store.WriteFile(out written,handle,0,zone),"Write stream on a one-character base");Ok(store.CloseFile(handle),"Close short base stream");
        Ok(Open(store,"a:Zone.Identifier",out handle),"Reopen short base stream");
        Ok(store.ReadFile(out bytes,handle,0,1024),"Read short base stream");Assert(Convert.ToBase64String(bytes)==Convert.ToBase64String(zone),"Short base stream bytes differ");Ok(store.CloseFile(handle),"Close short base read");
        Assert(File.ReadAllText(Path.Combine(root,"a"))=="short base","Short base contents changed");
        Ok(Open(store,"file.txt:Zone.Identifier",out handle,CreateDisposition.FILE_OPEN_IF),"Create Zone.Identifier");
        Ok(store.WriteFile(out written,handle,0,zone),"Write Zone.Identifier");Assert(written==zone.Length,"Zone short write");
        Ok(store.ReadFile(out bytes,handle,0,1024),"Read Zone.Identifier");Assert(Convert.ToBase64String(bytes)==Convert.ToBase64String(zone),"Zone bytes differ");
        Ok(store.SetFileInformation(handle,new FileEndOfFileInformation{EndOfFile=zone.Length}),"Stream EOF");
        Assert(store.SetFileInformation(handle,new FileRenameInformationType2{FileName="other:meta",ReplaceIfExists=true})==NTStatus.STATUS_NOT_SUPPORTED,"Stream rename must remain unsupported");
        Assert(store.SetFileInformation(handle,new FileBasicInformation{FileAttributes=Attributes.Hidden})==NTStatus.STATUS_NOT_SUPPORTED,"Stream metadata must not mutate base attrs");
        Ok(store.CloseFile(handle),"Close Zone");
        Ok(Open(store,"file.txt:метаданные:$DATA",out handle,CreateDisposition.FILE_OPEN_IF),"Create explicit DATA Unicode metadata");
        Ok(store.WriteFile(out written,handle,0,new byte[]{1,2,3,4}),"Write metadata");Ok(store.CloseFile(handle),"Close metadata");
        Ok(Open(store,"file.txt:метаданные",out handle),"Reopen normalized stream");
        Ok(store.ReadFile(out bytes,handle,0,10),"Read metadata");Assert(bytes.Length==4&&bytes[3]==4,"Metadata stream independent");Ok(store.CloseFile(handle),"Close metadata read");
        Ok(Open(store,"file.txt:метаданные",out handle,CreateDisposition.FILE_OVERWRITE),"Overwrite metadata stream");
        Assert(store.ReadFile(out bytes,handle,0,10)==NTStatus.STATUS_END_OF_FILE,"Overwritten stream should return native EOF");
        FileInformation standard;Ok(store.GetFileInformation(out standard,handle,FileInformationClass.FileStandardInformation),"Overwritten stream size");
        Assert(((FileStandardInformation)standard).EndOfFile==0,"Overwrite did not truncate stream");Ok(store.CloseFile(handle),"Overwrite close");
        Assert(File.GetAttributes(file)==baseAttributes,"ADS create/overwrite changed base attributes");
        Assert(File.ReadAllText(file,Encoding.UTF8)=="original base bytes","ADS changed base contents");
        Ok(Open(store,"file.txt::$DATA",out handle),"Unnamed DATA existing base");Ok(store.ReadFile(out bytes,handle,0,100),"Unnamed DATA read");Ok(store.CloseFile(handle),"Unnamed close");
        foreach(string invalid in new[]{"private\\secret.txt:Zone.Identifier","private:meta","private::$DATA","ordinary-directory:meta","ordinary-directory::$DATA","missing.txt:meta","missing.txt::$DATA","file.txt:meta:$INDEX_ALLOCATION","file.txt:meta\\child"})
            Assert(Open(store,invalid,out handle,CreateDisposition.FILE_OPEN_IF)!=NTStatus.STATUS_SUCCESS&&handle==null,"Unsafe ADS mutated/opened: "+invalid);
        Assert(!File.Exists(Path.Combine(root,"missing.txt")),"Failed ADS created base orphan");
        Assert(Open(store,"file.txt:directory",out handle,CreateDisposition.FILE_OPEN_IF,CreateOptions.FILE_DIRECTORY_FILE)!=NTStatus.STATUS_SUCCESS,"Directory ADS option accepted");
        Assert(Open(store,"file.txt:doc",out handle,CreateDisposition.FILE_OPEN_IF,CreateOptions.FILE_DELETE_ON_CLOSE)!=NTStatus.STATUS_SUCCESS,"ADS delete-on-close accepted");
        Assert(Open(store,"file.txt:supersede",out handle,CreateDisposition.FILE_SUPERSEDE)!=NTStatus.STATUS_SUCCESS,"ADS supersede accepted");
        Assert(File.ReadAllText(file,Encoding.UTF8)=="original base bytes","Rejected ADS changed base");
    }
    static void RenamedHandles(string fixtures)
    {
        string root=Path.Combine(fixtures,"moves"),restricted=Path.Combine(root,"private");Directory.CreateDirectory(restricted);
        var store=new ConfinedFileStore(root,new[]{restricted});object handle;byte[] bytes;int count;FileInformation information;
        string source=Path.Combine(root,"open.txt");File.WriteAllText(source,"owned move fixture");
        Ok(Open(store,"open.txt",out handle),"Open before protected move");File.Move(source,Path.Combine(restricted,"open.txt"));
        Assert(store.ReadFile(out bytes,handle,0,20)==NTStatus.STATUS_ACCESS_DENIED,"Renamed protected handle leaked read bytes");
        Assert(store.WriteFile(out count,handle,0,new byte[]{1})==NTStatus.STATUS_ACCESS_DENIED,"Renamed protected handle accepted write");
        Assert(store.GetFileInformation(out information,handle,FileInformationClass.FileBasicInformation)==NTStatus.STATUS_ACCESS_DENIED,"Renamed protected handle leaked information");
        Ok(store.CloseFile(handle),"Close protected handle must remain possible");
        File.WriteAllText(source,"outside move fixture");Ok(Open(store,"open.txt",out handle),"Open before root escape");
        File.Move(source,Path.Combine(fixtures,"escaped.txt"));
        Assert(store.ReadFile(out bytes,handle,0,20)==NTStatus.STATUS_ACCESS_DENIED,"Outside-root handle leaked read bytes");
        Ok(store.CloseFile(handle),"Close outside-root handle");
        File.WriteAllText(source,"stream move fixture");Ok(Open(store,"open.txt:Zone.Identifier",out handle,CreateDisposition.FILE_OPEN_IF),"ADS before protected move");
        File.Move(source,Path.Combine(restricted,"stream.txt"));
        Assert(store.WriteFile(out count,handle,0,new byte[]{1})==NTStatus.STATUS_ACCESS_DENIED,"ADS handle bypassed protected BASE after rename");
        Ok(store.CloseFile(handle),"Close renamed stream");
    }
    static void Links(string fixtures)
    {
        string root=Path.Combine(fixtures,"links"),restricted=Path.Combine(root,"private");Directory.CreateDirectory(restricted);
        string original=Path.Combine(root,"original.txt");File.WriteAllText(original,"all aliases must stay unchanged");
        // Proof, not permission: even a real read handle denying DELETE does not fence link creation.
        using(SafeFileHandle pinned=CreateFileW(original,1,3,IntPtr.Zero,3,0x00200000,IntPtr.Zero))
        {
            Assert(!pinned.IsInvalid,"Native link guard open");
            Assert(CreateHardLinkW(Path.Combine(root,"second.txt"),original,IntPtr.Zero),"CreateHardLink fixture/proof refused: "+Marshal.GetLastWin32Error());
            Console.WriteLine("BLOCKER: native CreateHardLink succeeds while FILE_READ_DATA/no-SHARE_DELETE is held; alias enumeration cannot authorize writes.");
        }
        var store=new ConfinedFileStore(root,new[]{restricted});object handle;
        foreach(string path in new[]{"original.txt","second.txt","original.txt:Zone.Identifier"})
            Assert(Open(store,path,out handle,CreateDisposition.FILE_OPEN_IF)==NTStatus.STATUS_ACCESS_DENIED&&handle==null,"Existing two-alias file must remain failclosed: "+path);
        Assert(CreateHardLinkW(Path.Combine(fixtures,"external-link.txt"),original,IntPtr.Zero),"External alias fixture");
        Assert(CreateHardLinkW(Path.Combine(restricted,"protected-link.txt"),original,IntPtr.Zero),"Protected alias fixture");
        Assert(Open(store,"original.txt",out handle)==NTStatus.STATUS_ACCESS_DENIED,"External/protected aliases accepted");
        Assert(File.ReadAllText(original)=="all aliases must stay unchanged"&&File.ReadAllText(Path.Combine(fixtures,"external-link.txt"))=="all aliases must stay unchanged","Denied alias request changed bytes");
        string single=Path.Combine(root,"later.txt");File.WriteAllText(single,"late alias bytes");Ok(Open(store,"later.txt",out handle),"Open single link");
        Assert(CreateHardLinkW(Path.Combine(restricted,"later-link.txt"),single,IntPtr.Zero),"Late protected alias fixture");int written;
        Assert(store.WriteFile(out written,handle,0,new byte[]{1})==NTStatus.STATUS_ACCESS_DENIED,"Write did not recheck link count");
        Assert(store.SetFileInformation(handle,new FileEndOfFileInformation{EndOfFile=0})==NTStatus.STATUS_ACCESS_DENIED,"Set did not recheck link count");
        Ok(store.CloseFile(handle),"Close late linked handle");Assert(File.ReadAllText(single)=="late alias bytes","Late-link deny changed bytes");
    }
    static void Capabilities(string fixtures)
    {
        var root=Path.Combine(fixtures,"caps");Directory.CreateDirectory(root);var store=new ConfinedFileStore(root);FileSystemInformation information;
        Ok(store.GetFileSystemInformation(out information,FileSystemInformationClass.FileFsAttributeInformation),"Capabilities query");
        var attributes=(FileFsAttributeInformation)information;
        var supported=FileSystemAttributes.CasePreservedNames|FileSystemAttributes.UnicodeOnDisk|FileSystemAttributes.NamedStreams|FileSystemAttributes.ReadOnlyVolume;
        Assert((attributes.FileSystemAttributes&~supported)==0,"Capabilities advertise unsupported ACL/reparse/link creation or device controls");
        Assert((attributes.FileSystemAttributes&FileSystemAttributes.NamedStreams)!=0,"NTFS named streams not advertised");
        var original=new FileFsAttributeInformation{FileSystemAttributes=(FileSystemAttributes)0xffffffff,MaximumComponentNameLength=510,FileSystemName="fixture"};
        var masked=(FileFsAttributeInformation)ConfinedFileStore.MaskCapabilities(original);
        Assert(masked.FileSystemAttributes==supported&&original.FileSystemAttributes==(FileSystemAttributes)0xffffffff,"Mask mutates shared/native information or drops supported flags");
        Assert(masked.MaximumComponentNameLength==510&&masked.FileSystemName=="fixture","Mask changed volume identity");
    }
    public static int Main(string[] args)
    {
        try
        {
            if(args.Length!=1)throw new ArgumentException("Own temporary fixture parent required");
            string fixtures=Path.Combine(args[0],"capability-fixtures");Directory.CreateDirectory(fixtures);
            Parser();Streams(fixtures);RenamedHandles(fixtures);Links(fixtures);Capabilities(fixtures);
            Console.WriteLine("PASS: staged ADS/base protection, renamed handles, strict parser, conservative capabilities; hardlink support remains blocked.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

using System;
using System.Diagnostics;
using System.IO;
using SMBLibrary;
using SMBLibrary.Win32;

internal static class HostFilesMemoryRegression
{
    static long Memory()
    {
        GC.Collect();GC.WaitForPendingFinalizers();GC.Collect();
        using(var process=Process.GetCurrentProcess()){process.Refresh();return process.PrivateMemorySize64;}
    }
    static void Opens(INTFileStore store,int count)
    {
        string missing=new string('x',4096);
        for(int index=0;index<count;index++)
        {
            object handle;FileStatus state;
            var result=store.CreateFile(out handle,out state,missing,AccessMask.GENERIC_READ,
                SMBLibrary.FileAttributes.Normal,ShareAccess.Read,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null);
            if(result==NTStatus.STATUS_SUCCESS){store.CloseFile(handle);throw new Exception("Oversized missing path unexpectedly opened");}
        }
    }
    static int Main(string[] args)
    {
        try
        {
            string root=Path.Combine(args[0],"memory-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
            var store=new NTDirectoryFileSystem(root);Opens(store,200);long before=Memory();Opens(store,6000);long growth=Memory()-before;
            Console.WriteLine("Private memory growth after 6000 failed native opens: "+growth+" bytes");
            if(growth>16*1024*1024)throw new Exception("Failed native opens must not accumulate unmanaged path buffers");
            return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

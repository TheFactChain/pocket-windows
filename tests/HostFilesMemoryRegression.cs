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
    static void FailedQueries(INTFileStore store,object regularFile,int count)
    {
        string pattern=new string('x',8192);
        for(int index=0;index<count;index++)
        {
            System.Collections.Generic.List<QueryDirectoryFileInformation> entries;
            var status=store.QueryDirectory(out entries,regularFile,pattern,FileInformationClass.FileNamesInformation);
            if(status==NTStatus.STATUS_SUCCESS)throw new Exception("Directory query on a regular file unexpectedly succeeded");
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
            string regular=Path.Combine(root,"regular.txt");File.WriteAllText(regular,"fixture");
            object handle;FileStatus opened;
            var status=store.CreateFile(out handle,out opened,"regular.txt",AccessMask.GENERIC_READ,
                SMBLibrary.FileAttributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null);
            if(status!=NTStatus.STATUS_SUCCESS)throw new Exception("Cannot open the query error fixture: "+status);
            try
            {
                FailedQueries(store,handle,200);before=Memory();FailedQueries(store,handle,6000);growth=Memory()-before;
                Console.WriteLine("Private memory growth after 6000 failed directory pattern queries: "+growth+" bytes");
                if(growth>16*1024*1024)throw new Exception("Query failures must release UNICODE_STRING pattern buffers");
            }
            finally{store.CloseFile(handle);}
            return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

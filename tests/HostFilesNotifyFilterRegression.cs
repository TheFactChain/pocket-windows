using System;
using System.IO;
using System.Diagnostics;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using SMBLibrary;

internal static class HostFilesNotifyFilterRegression
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static object Open(ConfinedFileStore store,string path)
    {
        object handle;FileStatus information;
        NTStatus status=store.CreateFile(out handle,out information,path,AccessMask.GENERIC_READ,SMBLibrary.FileAttributes.Directory,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_DIRECTORY_FILE,null);
        Assert(status==NTStatus.STATUS_SUCCESS,"Open watched directory: "+status);return handle;
    }
    sealed class Completion:IDisposable
    {
        internal readonly ManualResetEvent Done=new ManualResetEvent(false);
        internal int Calls;internal NTStatus Status;internal byte[] Data;
        internal void Callback(NTStatus status,byte[] data,object context){Status=status;Data=data;Interlocked.Increment(ref Calls);Done.Set();}
        internal void Wait(){Assert(Done.WaitOne(4000),"Native notify timed out");Assert(Calls==1,"Notify callback must complete once");}
        public void Dispose(){Done.Dispose();}
    }
    static object Begin(ConfinedFileStore store,object handle,Completion completed)
    {
        object request;Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName|NotifyChangeFilter.DirName,true,65536,completed.Callback,null)==NTStatus.STATUS_PENDING,"Notify must pend");return request;
    }
    static byte[] Records(params object[] values)
    {
        using(var memory=new MemoryStream())using(var writer=new BinaryWriter(memory))
        {
            for(int index=0;index<values.Length;index+=2)
            {
                byte[] name=Encoding.Unicode.GetBytes((string)values[index+1]);int length=12+name.Length,padded=(length+3)&~3;
                writer.Write(index+2<values.Length?padded:0);writer.Write((uint)(int)values[index]);writer.Write(name.Length);writer.Write(name);
                if(index+2<values.Length)for(int pad=length;pad<padded;pad++)writer.Write((byte)0);
            }
            return memory.ToArray();
        }
    }
    static NTStatus Filter(ConfinedFileStore store,object handle,byte[] input,out byte[] output)
    {
        FieldInfo field=handle.GetType().GetField("Native",BindingFlags.NonPublic|BindingFlags.Instance);
        MethodInfo method=typeof(ConfinedFileStore).GetMethod("FilterNotification",BindingFlags.NonPublic|BindingFlags.Instance);
        Assert(method!=null,"Notify filter is missing");object[] args={field.GetValue(handle),input,null};
        NTStatus status=(NTStatus)method.Invoke(store,args);output=(byte[])args[2];return status;
    }
    static void Rescan(ConfinedFileStore store,object handle,byte[] input,string reason)
    {byte[] output;Assert(Filter(store,handle,input,out output)==NTStatus.STATUS_NOTIFY_ENUM_DIR&&output.Length==0,"Malformed/unverifiable notify must rescan without raw bytes: "+reason);}
    static void Synthetic(string root)
    {
        string secret=Path.Combine(root,"private"),allowed=Path.Combine(root,"обычный 😀.txt");Directory.CreateDirectory(secret);File.WriteAllText(Path.Combine(secret,"secret.txt"),"fixture");File.WriteAllText(allowed,"fixture");File.WriteAllText(Path.Combine(root,"renamed.txt"),"fixture");
        var store=new ConfinedFileStore(root,new[]{secret});object handle=Open(store,"");byte[] output;
        try
        {
            Assert(Filter(store,handle,Records(1,"обычный 😀.txt",3,"private\\secret.txt"),out output)==NTStatus.STATUS_SUCCESS,"Mixed valid event filter status");
            Assert(output.SequenceEqual(Records(1,"обычный 😀.txt")),"Protected descendant leaked or Unicode allowed entry changed");
            Assert(Filter(store,handle,Records(4,"private\\old.txt",5,"renamed.txt",1,"обычный 😀.txt"),out output)==NTStatus.STATUS_SUCCESS,"Rename protected old-side filter");
            Assert(output.SequenceEqual(Records(1,"обычный 😀.txt")),"Half of a protected rename pair leaked");
            Rescan(store,handle,Records(4,"old.txt",5,"private\\secret.txt"),"protected new side");
            Assert(Filter(store,handle,Records(4,"old.txt",5,"renamed.txt"),out output)==NTStatus.STATUS_SUCCESS&&output.SequenceEqual(Records(4,"old.txt",5,"renamed.txt")),"Allowed adjacent rename pair changed");
            foreach(byte[] malformed in new[]{new byte[0],new byte[11],Records(1,"..\\escape"),Records(1,"\\absolute"),Records(1,"x:y"),Records(1,"private/secret.txt"),Records(4,"old.txt"),Records(5,"renamed.txt"),Records(4,"old.txt",1,"обычный 😀.txt",5,"renamed.txt"),Records(6,"обычный 😀.txt"),Records(1,"missing.txt"),Records(1,String.Join("\\",Enumerable.Repeat("x",257)))})Rescan(store,handle,malformed,"shape/path/action/depth");
            byte[] bad=Records(1,"обычный 😀.txt");bad[8]=3;bad[9]=bad[10]=bad[11]=0;Rescan(store,handle,bad,"odd UTF16 length");
            bad=Records(1,"обычный 😀.txt");bad[12]=0;bad[13]=0xD8;Rescan(store,handle,bad,"unpaired surrogate");
            bad=Records(1,"обычный 😀.txt",3,"renamed.txt");bad[0]=13;bad[1]=bad[2]=bad[3]=0;Rescan(store,handle,bad,"unaligned next");
            bad=Records(1,"обычный 😀.txt");bad[0]=0xFC;bad[1]=bad[2]=bad[3]=0xFF;Rescan(store,handle,bad,"overflowing next");
            bad=Records(1,"обычный 😀.txt").Concat(new byte[4]).ToArray();Rescan(store,handle,bad,"trailing bytes");
            Rescan(store,handle,new byte[65537],"over bound");
        }
        finally{Assert(store.CloseFile(handle)==NTStatus.STATUS_SUCCESS,"Synthetic handle close");}
        Directory.Move(secret,Path.Combine(root,"renamed-private"));
        handle=Open(store,"");try{Rescan(store,handle,Records(3,"renamed-private\\secret.txt"),"renamed protected identity");}finally{store.CloseFile(handle);}
        Console.WriteLine("PASS synthetic bounded UTF16/offset/path parsing, protected descendants and rename pairs, renamed protected identity");
    }
    static void Native(string root)
    {
        string secret=Path.Combine(root,"private");Directory.CreateDirectory(secret);
        var store=new ConfinedFileStore(root,new[]{secret});object handle=Open(store,"");
        try
        {
            using(var completed=new Completion())
            {Begin(store,handle,completed);File.WriteAllText(Path.Combine(secret,"SECRET-NAME.txt"),"fixture");completed.Wait();Assert(completed.Status==NTStatus.STATUS_NOTIFY_ENUM_DIR&&completed.Data.Length==0,"Native protected descendant name leaked through tree notify");}
            using(var completed=new Completion())
            {Begin(store,handle,completed);File.WriteAllText(Path.Combine(root,"видимый 😀.txt"),"fixture");completed.Wait();Assert(completed.Status==NTStatus.STATUS_SUCCESS&&completed.Data.SequenceEqual(Records(1,"видимый 😀.txt")),"Native allowed Unicode notify lost");}
            using(var completed=new Completion())
            {object request=Begin(store,handle,completed);NTStatus cancelled=store.Cancel(request);Assert(cancelled==NTStatus.STATUS_SUCCESS||cancelled==NTStatus.STATUS_NOT_FOUND,"Cancel request status");completed.Wait();Assert(completed.Status==NTStatus.STATUS_CANCELLED&&completed.Data.Length==0,"Cancel semantics changed");}
        }
        finally{store.CloseFile(handle);}
        string watch=Path.Combine(root,"watch");Directory.CreateDirectory(watch);handle=Open(store,"watch");
        using(var completed=new Completion())
        {
            Begin(store,handle,completed);Directory.Move(watch,Path.Combine(secret,"moved-watch"));File.WriteAllText(Path.Combine(secret,"moved-watch","SECRET-MOVED.txt"),"fixture");completed.Wait();
            Assert(completed.Status==NTStatus.STATUS_NOTIFY_ENUM_DIR&&completed.Data.Length==0,"Current canonical protected watched directory leaked");
        }
        store.CloseFile(handle);
        handle=Open(store,"");using(var completed=new Completion())
        {Begin(store,handle,completed);Assert(store.CloseFile(handle)==NTStatus.STATUS_SUCCESS,"Pending close");completed.Wait();Assert(completed.Status==NTStatus.STATUS_NOTIFY_CLEANUP&&completed.Data.Length==0,"Pending close semantics changed");}
        handle=Open(store,"");var pending=new Completion[256];int threads=Process.GetCurrentProcess().Threads.Count;
        try
        {
            for(int index=0;index<pending.Length;index++)
            {pending[index]=new Completion();object request;Assert(store.NotifyChange(out request,handle,NotifyChangeFilter.FileName,true,1024,pending[index].Callback,null)==NTStatus.STATUS_PENDING,"Bounded notify registration "+index);}
            object excess;using(var rejected=new Completion())Assert(store.NotifyChange(out excess,handle,NotifyChangeFilter.FileName,true,1024,rejected.Callback,null)==NTStatus.STATUS_INSUFFICIENT_RESOURCES&&excess==null,"Pending limit changed");
            int growth=Process.GetCurrentProcess().Threads.Count-threads;Assert(growth<32,"Notification registration created per-request threads: "+growth);
            Assert(store.CloseFile(handle)==NTStatus.STATUS_SUCCESS,"Close bounded pending notifications");handle=null;
            foreach(var completed in pending){completed.Wait();Assert(completed.Status==NTStatus.STATUS_NOTIFY_CLEANUP&&completed.Data.Length==0,"Bounded close lost status/data");}
            Console.WriteLine("PASS 256 pending bound, once-only close callbacks, registration thread growth="+growth);
        }
        finally{if(handle!=null)store.CloseFile(handle);foreach(var completed in pending)if(completed!=null)completed.Dispose();}
        Console.WriteLine("PASS real tree protected suppression, Unicode allowed event, canonical watched move, cancel and close once-only completion");
    }
    static int Main(string[] args)
    {
        try
        {
            Assert(args.Length==1&&(args[0]=="--native"||args[0]=="--synthetic"),"Only bounded own-fixture modes are supported");
            string home=Path.GetDirectoryName(typeof(HostFilesNotifyFilterRegression).Assembly.Location),root=Path.Combine(home,"test-"+Guid.NewGuid().ToString("N"));
            Assert(!Directory.Exists(root),"Own native fixture must be fresh");Directory.CreateDirectory(root);Console.WriteLine("Fixtures: "+root);
            if(args[0]=="--native")Native(root);else Synthetic(root);return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

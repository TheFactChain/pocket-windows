using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;
using SMBLibrary;
using SMBLibrary.Client;

internal static class HostFilesServerRegression
{
    sealed class Client : SMB2Client
    {
        internal Client() : base(4000) { }
        internal bool Open(int port)
        {
            bool connected=Connect(IPAddress.Loopback,SMBTransportType.DirectTCPTransport,port);
            typeof(SMB2Client).GetField("m_signingRequired",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(this,true);
            return connected;
        }
    }
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static void Success(NTStatus status,string message){Assert(status==NTStatus.STATUS_SUCCESS,message+": "+status);}
    static string Input(string root,params string[] protectedPaths)
    {
        return new JavaScriptSerializer().Serialize(new Dictionary<string,object>{{"schema",1},{"session_nonce",new string('a',32)},
            {"username","WCP_fixture"},{"password","fixture_password_01234567890123456789"},
            {"shares",new[]{new Dictionary<string,object>{{"name","PC_TEST"},{"path",root}}}},{"protected",protectedPaths}});
    }
    static void ProtectedOverSmb(string directory)
    {
        string program=Path.Combine(directory,"program"),code=Path.Combine(program,"run.py");Directory.CreateDirectory(program);File.WriteAllText(code,"original");
        using(var server=HostFilesServer.Start(Input(directory,program)))
        {
            var client=new Client();try
            {
                Assert(client.Open(server.Port),"Protected fixture connect");Success(client.Login("","WCP_fixture","fixture_password_01234567890123456789"),"Protected fixture login");
                NTStatus status;var files=client.TreeConnect("PC_TEST",out status);Success(status,"Protected fixture tree");object handle;FileStatus state;
                Assert(files.CreateFile(out handle,out state,"program\\run.py",AccessMask.GENERIC_WRITE,SMBLibrary.FileAttributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,
                    CreateDisposition.FILE_OVERWRITE,CreateOptions.FILE_NON_DIRECTORY_FILE,null)!=NTStatus.STATUS_SUCCESS,"Protected program file must be unreachable over SMB");
                Success(files.CreateFile(out handle,out state,"beside.txt",AccessMask.GENERIC_WRITE,SMBLibrary.FileAttributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,
                    CreateDisposition.FILE_CREATE,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"Ordinary file beside protected folder");Success(files.CloseFile(handle),"Ordinary close");
                Success(files.Disconnect(),"Protected fixture disconnect");
            }
            finally{client.Disconnect();}
        }
        Assert(File.ReadAllText(code)=="original","Protected program file changed");
    }
    static void Reject(string value){bool failed=false;try{using(var server=HostFilesServer.Start(value)){} }catch(ArgumentException){failed=true;} Assert(failed,"Invalid session must be rejected before listening");}
    static void Authentication(int port,string username,string password)
    {
        var client=new Client();try { Assert(client.Open(port),"Loopback transport must connect");Assert(client.Login("",username,password)!=NTStatus.STATUS_SUCCESS,"Invalid/guest authentication must fail"); } finally {client.Disconnect();}
    }
    static void Files(int port,string directory)
    {
        var client=new Client();try
        {
            Assert(client.Open(port),"SMB transport must connect");Success(client.Login("","WCP_fixture","fixture_password_01234567890123456789"),"NTLMv2 login");
            NTStatus status;var files=client.TreeConnect("PC_TEST",out status);Success(status,"Tree connect");
            object handle;FileStatus state;
            Success(files.CreateFile(out handle,out state,"Проверка.txt",AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE|AccessMask.DELETE,
                SMBLibrary.FileAttributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_CREATE,
                CreateOptions.FILE_NON_DIRECTORY_FILE,null),"Create Unicode file");
            try
            {
                byte[] content=Encoding.UTF8.GetBytes(new string('Ж',600000));
                for(int offset=0;offset<content.Length;offset+=32768)
                {
                    int length=Math.Min(32768,content.Length-offset),written;byte[] part=new byte[length];Array.Copy(content,offset,part,0,length);
                    Success(files.WriteFile(out written,handle,offset,part),"Signed write");Assert(written==length,"Write must not be truncated");
                }
                Success(files.FlushFileBuffers(handle),"Flush to host");
                using(var saved=new FileStream(Path.Combine(directory,"Проверка.txt"),FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
                    Assert(saved.Length==content.Length,"Signed writes must reach the real test file");
                byte[] read;Success(files.ReadFile(out read,handle,0,32768),"Signed read");Assert(read.Length==32768&&read[0]==content[0],"Read contents must match");
                Success(files.SetFileInformation(handle,new FileRenameInformationType2 { ReplaceIfExists=false,FileName="Новое имя.txt" }),"Rename");
            }
            finally {Success(files.CloseFile(handle),"Close");}
            Assert(File.Exists(Path.Combine(directory,"Новое имя.txt")),"Rename must reach the host");
            Assert(files.CreateFile(out handle,out state,"..\\outside.txt",AccessMask.GENERIC_WRITE,SMBLibrary.FileAttributes.Normal,
                ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OVERWRITE_IF,CreateOptions.FILE_NON_DIRECTORY_FILE,null)!=NTStatus.STATUS_SUCCESS,"Traversal must fail through SMB");
            using(var local=new FileStream(Path.Combine(directory,"Новое имя.txt"),FileMode.Open,FileAccess.ReadWrite,FileShare.None))
                Assert(files.CreateFile(out handle,out state,"Новое имя.txt",AccessMask.GENERIC_WRITE,SMBLibrary.FileAttributes.Normal,
                    ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null)==NTStatus.STATUS_SHARING_VIOLATION,"Local exclusive open must block remote writer");
            Success(files.Disconnect(),"Tree disconnect");Success(client.Logoff(),"Logoff");
        }
        finally {client.Disconnect();}
    }
    static int Main(string[] args)
    {
        try
        {
            string directory=Path.Combine(args[0],"server-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(directory);
            string outside=Path.Combine(Path.GetDirectoryName(directory),"outside.txt");File.WriteAllText(outside,"unchanged");
            Reject("{}");Reject(Input(directory).Replace("\"schema\":1","\"schema\":true"));
            Reject(Input(directory).Replace("\"PC_TEST\"","\"../escape\""));
            Reject(Input(directory).Replace(",\"protected\":[]",""));Reject(Input(directory,"relative\\program"));Reject(Input(directory,"\\\\server\\share"));
            ProtectedOverSmb(Path.Combine(args[0],"protected-"+Guid.NewGuid().ToString("N")));
            using(var server=HostFilesServer.Start(Input(directory)))
            {
                Assert(server.Port>=49152&&server.Port<=65535,"Server must use a high loopback port");
                Authentication(server.Port,"Guest","");Authentication(server.Port,"WCP_fixture","wrong-password");
                Files(server.Port,directory);FilesReconnect(server.Port);
                Tamper(server.Port,directory,false);Tamper(server.Port,directory,true);
                Assert(File.ReadAllText(outside)=="unchanged","Outside contents must remain unchanged");
            }
            Console.WriteLine("PASS: loopback SMB2, NTLMv2, guest/wrong password denied, signed Unicode reads/writes over 1MB, flush, rename, share conflict, path confinement, protected program folder, reconnect.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
    static void FilesReconnect(int port)
    {
        var client=new Client();try{Assert(client.Open(port),"Reconnect");Success(client.Login("","WCP_fixture","fixture_password_01234567890123456789"),"Reconnect login");NTStatus status;var files=client.TreeConnect("PC_TEST",out status);Success(status,"Signed tree connect after reconnect");Success(files.Disconnect(),"Reconnect tree disconnect");Success(client.Logoff(),"Reconnect logoff");}finally{client.Disconnect();}
    }
    static void Tamper(int port,string directory,bool missing)
    {
        string name=missing?"unsigned.txt":"tampered.txt";File.WriteAllText(Path.Combine(directory,name),"original");
        var client=new Client();try
        {
            Assert(client.Open(port),"Tamper connection");Success(client.Login("","WCP_fixture","fixture_password_01234567890123456789"),"Tamper login");
            NTStatus status;var files=client.TreeConnect("PC_TEST",out status);Success(status,"Tamper tree connect");object handle;FileStatus state;
            Success(files.CreateFile(out handle,out state,name,AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE,
                SMBLibrary.FileAttributes.Normal,ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition.FILE_OPEN,CreateOptions.FILE_NON_DIRECTORY_FILE,null),"Tamper open");
            if(missing)typeof(SMB2Client).GetField("m_signingRequired",BindingFlags.NonPublic|BindingFlags.Instance).SetValue(client,false);
            else ((byte[])typeof(SMB2Client).GetField("m_signingKey",BindingFlags.NonPublic|BindingFlags.Instance).GetValue(client))[0]^=0x80;
            int written;status=files.WriteFile(out written,handle,0,Encoding.ASCII.GetBytes("corrupt!"));
            Assert(status!=NTStatus.STATUS_SUCCESS,"Server must reject "+(missing?"missing":"invalid")+" request signatures before WRITE");
            using(var saved=new FileStream(Path.Combine(directory,name),FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
                Assert(saved.ReadByte()=='o',"Invalid signature must leave host data unchanged");
        }
        finally{client.Disconnect();}
    }
}

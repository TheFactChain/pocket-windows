using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using SMBLibrary;
using SMBLibrary.Client;
using SMBLibrary.Client.Authentication;
using SMBLibrary.Server;
using SMBLibrary.SMB2;

internal static class HostFilesCompoundRegression
{
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static byte[] Crypto(string method,params object[] values){return (byte[])typeof(SMB2Command).Assembly.GetType("SMBLibrary.SMB2.SMB2Cryptography").GetMethod(method,BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic).Invoke(null,values);}
    sealed class Wire : IDisposable
    {
        readonly TcpClient client=new TcpClient(); readonly NetworkStream stream; ulong message,session;
        internal byte[] Key;
        internal ulong SessionID{get{return session;}}
        internal Wire(int port){client.Connect("127.0.0.1",port);client.ReceiveTimeout=4000;client.SendTimeout=4000;stream=client.GetStream();}
        byte[] Read(int size){var bytes=new byte[size];for(int offset=0;offset<size;){int count=stream.Read(bytes,offset,size-offset);if(count==0)throw new EndOfStreamException();offset+=count;}return bytes;}
        internal ulong Send(params SMB2Command[] commands)
        {
            foreach(var command in commands){command.Header.MessageID=message++;command.Header.Credits=16;command.Header.CreditCharge=1;if(!command.Header.IsRelatedOperations)command.Header.SessionID=session;command.Header.IsSigned=Key!=null&&!(command is SessionSetupRequest);}
            byte[] data=SMB2Command.GetCommandChainBytes(new List<SMB2Command>(commands),Key,SMB2Dialect.SMB210);
            stream.Write(new[]{(byte)0,(byte)(data.Length>>16),(byte)(data.Length>>8),(byte)data.Length},0,4);stream.Write(data,0,data.Length);
            return commands[0].Header.MessageID;
        }
        internal List<SMB2Command> Exchange(params SMB2Command[] commands){Send(commands);return Receive();}
        internal bool HasResponse(int milliseconds){return client.Client.Poll(milliseconds*1000,SelectMode.SelectRead);}
        internal void Cancel(ulong originalID,ulong asyncID,uint tree,bool asynchronous)
        {
            var cancel=new CancelRequest();cancel.Header.SessionID=session;cancel.Header.MessageID=asynchronous?0:originalID;
            cancel.Header.IsAsync=asynchronous;cancel.Header.AsyncID=asyncID;cancel.Header.TreeID=tree;cancel.Header.IsSigned=true;
            byte[] bytes=SMB2Command.GetCommandChainBytes(new List<SMB2Command>{cancel},Key,SMB2Dialect.SMB210);
            stream.Write(new[]{(byte)0,(byte)(bytes.Length>>16),(byte)(bytes.Length>>8),(byte)bytes.Length},0,4);stream.Write(bytes,0,bytes.Length);
        }
        internal List<SMB2Command> Receive()
        {
            var prefix=Read(4);int length=(prefix[1]<<16)|(prefix[2]<<8)|prefix[3];Assert(prefix[0]==0&&length>=64&&length<1048576,"Bounded direct TCP response");byte[] data=Read(length);
            var result=new List<SMB2Command>();int offset=0;
            do
            {
                var response=SMB2Command.ReadResponse(data,offset);int count=response.Header.NextCommand==0?data.Length-offset:(int)response.Header.NextCommand;
                bool interim=response.Header.IsAsync&&response.Header.Status==NTStatus.STATUS_PENDING;
                if(Key!=null&&!interim){Assert(response.Header.IsSigned,"Authenticated final response must be signed");byte[] part=new byte[count];Array.Copy(data,offset,part,0,count);byte[] received=new byte[16];Array.Copy(part,48,received,0,16);Array.Clear(part,48,16);byte[] expected=Crypto("CalculateSignature",Key,SMB2Dialect.SMB210,part,0,count);for(int index=0;index<16;index++)Assert(received[index]==expected[index],"Compound response signature mismatch");}
                if(interim)Assert(!response.Header.IsSigned,"MS-SMB2 interim responses are not signed");
                result.Add(response);offset+=count;
            }while(offset<data.Length);
            return result;
        }
        internal void Login()
        {
            var negotiate=new NegotiateRequest{SecurityMode=SecurityMode.SigningEnabled,ClientGuid=Guid.NewGuid(),ClientStartTime=DateTime.UtcNow};negotiate.Dialects.Add(SMB2Dialect.SMB210);
            var accepted=Exchange(negotiate)[0] as NegotiateResponse;Assert(accepted!=null,"Negotiate");
            var auth=new NTLMAuthenticationClient("","WCP_fixture","fixture_password_01234567890123456789","cifs/127.0.0.1",AuthenticationMethod.NTLMv2);
            var challenge=Exchange(new SessionSetupRequest{SecurityMode=SecurityMode.SigningEnabled,SecurityBuffer=auth.InitializeSecurityContext(accepted.SecurityBuffer)})[0] as SessionSetupResponse;
            Assert(challenge!=null&&challenge.Header.Status==NTStatus.STATUS_MORE_PROCESSING_REQUIRED,"NTLMv2 challenge");session=challenge.Header.SessionID;
            byte[] token=auth.InitializeSecurityContext(challenge.SecurityBuffer);Key=Crypto("GenerateSigningKey",auth.GetSessionKey(),SMB2Dialect.SMB210,null);
            Assert(Exchange(new SessionSetupRequest{SecurityMode=SecurityMode.SigningEnabled,SecurityBuffer=token})[0].Header.Status==NTStatus.STATUS_SUCCESS,"NTLMv2 authenticate");
        }
        public void Dispose(){client.Close();}
    }
    static void AuthenticationCompound()
    {
        var setup=new SessionSetupRequest();var tree=new TreeConnectRequest{Path=@"\\192.168.234.4\PC_TEST"};tree.Header.IsRelatedOperations=true;tree.Header.SessionID=UInt64.MaxValue;
        var commands=new List<SMB2Command>{setup,tree};byte[] bytes=SMB2Command.GetCommandChainBytes(commands);
        var validate=typeof(SMBServer).GetMethod("ValidateIncomingSignatures",BindingFlags.NonPublic|BindingFlags.Static);
        Assert(!(bool)validate.Invoke(null,new object[]{bytes,commands,null}),"An authentication compound must be rejected before it can create a new unsigned session");
        Console.WriteLine("PASS: authentication compound rejected before dispatch.");
    }
    static void Related(int port,string root,bool placeholders)
    {
        using(var wire=new Wire(port))
        {
            wire.Login();var tree=wire.Exchange(new TreeConnectRequest{Path=@"\\192.168.234.4\PC_TEST"})[0];Assert(tree.Header.Status==NTStatus.STATUS_SUCCESS,"Tree connect");
            string name=placeholders?"related-placeholders.txt":"related-explicit.txt";byte[] content=Encoding.UTF8.GetBytes("Связанные операции сохраняют данные");
            var create=new CreateRequest{Name=name,DesiredAccess=AccessMask.GENERIC_READ|AccessMask.GENERIC_WRITE,ShareAccess=ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,CreateDisposition=CreateDisposition.FILE_CREATE,CreateOptions=CreateOptions.FILE_NON_DIRECTORY_FILE,FileAttributes=SMBLibrary.FileAttributes.Normal,ImpersonationLevel=ImpersonationLevel.Impersonation};create.Header.TreeID=tree.Header.TreeID;
            var write=new WriteRequest{Data=content,FileId=new FileID{Persistent=UInt64.MaxValue,Volatile=UInt64.MaxValue}};
            var close=new CloseRequest{FileId=write.FileId};
            foreach(var command in new SMB2Command[]{write,close}){command.Header.IsRelatedOperations=true;command.Header.SessionID=placeholders?UInt64.MaxValue:999999;command.Header.TreeID=placeholders?UInt32.MaxValue:999999;}
            var responses=wire.Exchange(create,write,close);Assert(responses.Count==3,"All compound responses returned");
            foreach(var response in responses)Assert(response.Header.Status==NTStatus.STATUS_SUCCESS,"Related "+response.CommandName+": "+response.Header.Status);
            Assert(File.ReadAllText(Path.Combine(root,name))==Encoding.UTF8.GetString(content),"Compound WRITE did not reach the host");
            using(var file=new FileStream(Path.Combine(root,name),FileMode.Open,FileAccess.ReadWrite,FileShare.None))Assert(file.Length==content.Length,"Compound CLOSE must release the file");
        }
        Console.WriteLine("PASS: signed related CREATE/WRITE/CLOSE, inherited identifiers, host bytes and close; placeholders="+placeholders);
    }
    static uint ConnectTree(Wire wire)
    {
        var response=wire.Exchange(new TreeConnectRequest{Path=@"\\192.168.234.4\PC_TEST"})[0];
        Assert(response.Header.Status==NTStatus.STATUS_SUCCESS,"Tree connect fixture");return response.Header.TreeID;
    }
    static FileID OpenDirectory(Wire wire,uint tree)
    {
        var request=new CreateRequest{Name="",DesiredAccess=AccessMask.GENERIC_READ,ShareAccess=ShareAccess.Read|ShareAccess.Write|ShareAccess.Delete,
            CreateDisposition=CreateDisposition.FILE_OPEN,CreateOptions=CreateOptions.FILE_DIRECTORY_FILE,FileAttributes=SMBLibrary.FileAttributes.Directory,ImpersonationLevel=ImpersonationLevel.Impersonation};
        request.Header.TreeID=tree;var response=wire.Exchange(request)[0] as CreateResponse;
        Assert(response!=null&&response.Header.Status==NTStatus.STATUS_SUCCESS,"Open directory fixture");return response.FileId;
    }
    static ChangeNotifyRequest Notification(uint tree,FileID file)
    {
        var request=new ChangeNotifyRequest{FileId=file,CompletionFilter=NotifyChangeFilter.FileName,OutputBufferLength=65536};
        request.Header.TreeID=tree;return request;
    }
    static SMB2Command Pending(Wire wire,uint tree,FileID file,out ulong messageID)
    {
        messageID=wire.Send(Notification(tree,file));var response=wire.Receive()[0];
        Assert(response.Header.Status==NTStatus.STATUS_PENDING&&response.Header.IsAsync&&response.Header.AsyncID!=0,"Notification must become asynchronous");
        Assert(response.CommandName==SMB2CommandName.ChangeNotify&&response.Header.MessageID==messageID&&response.Header.SessionID==wire.SessionID,"Interim response retains the original request identity");
        return response;
    }
    static void FinalNotification(Wire wire,SMB2Command response,ulong messageID,ulong asyncID,NTStatus status)
    {
        Assert(response.CommandName==SMB2CommandName.ChangeNotify&&response.Header.Status==status,"Only the original notification completes: "+response.CommandName+" "+response.Header.Status);
        Assert(response.Header.IsAsync&&response.Header.AsyncID==asyncID&&response.Header.MessageID==messageID&&response.Header.SessionID==wire.SessionID,"Final response retains the original command, message, session and async identifiers");
    }
    static void WarmNotificationPool(Wire wire,uint tree,FileID file)
    {
        // CLR registered-wait/worker threads allocate retained Event/Thread handles on first use.
        // Warm the same bounded workload before measuring three further resource-release waves.
        for(int wave=0;wave<8;wave++)
        {
            var requests=new Dictionary<ulong,ulong>();ulong message;
            for(int index=0;index<64;index++)
            {var pending=Pending(wire,tree,file,out message);requests.Add(message,pending.Header.AsyncID);}
            foreach(var request in requests)wire.Cancel(request.Key,request.Value,tree,true);
            var seen=new HashSet<ulong>();
            for(int index=0;index<64;index++)
            {
                var response=wire.Receive()[0];Assert(seen.Add(response.Header.MessageID)&&requests.ContainsKey(response.Header.MessageID),"Warm-up retains one completion per original request");
                FinalNotification(wire,response,response.Header.MessageID,requests[response.Header.MessageID],NTStatus.STATUS_CANCELLED);
            }
            Assert(wire.Exchange(new EchoRequest())[0] is EchoResponse&&!wire.HasResponse(20),"Warm-up has no trailing CANCEL response or duplicate completion");
            Thread.Sleep(100);GC.Collect();GC.WaitForPendingFinalizers();
            using(var process=Process.GetCurrentProcess())
            {process.Refresh();Console.WriteLine("INFO: completed 64-notify warm-up wave "+(wave+1)+", handles="+process.HandleCount);}
        }
    }
    static void AsyncNotifications(int port,string root)
    {
        using(var wire=new Wire(port))
        {
            wire.Login();uint tree=ConnectTree(wire);FileID file=OpenDirectory(wire,tree);ulong message;
            var pending=Pending(wire,tree,file,out message);
            wire.Cancel(message,pending.Header.AsyncID,tree,true);
            FinalNotification(wire,wire.Receive()[0],message,pending.Header.AsyncID,NTStatus.STATUS_CANCELLED);
            wire.Cancel(message,pending.Header.AsyncID,tree,true);
            Assert(wire.Exchange(new EchoRequest())[0] is EchoResponse,"Repeated CANCEL must have no response of its own");
            Assert(!wire.HasResponse(100),"Cancelled notification completes exactly once");

            message=wire.Send(Notification(tree,file));wire.Cancel(message,0,tree,false);
            pending=wire.Receive()[0];Assert(pending.Header.Status==NTStatus.STATUS_PENDING,"Synchronous CANCEL follows the original pending request");
            FinalNotification(wire,wire.Receive()[0],message,pending.Header.AsyncID,NTStatus.STATUS_CANCELLED);
            wire.Cancel(message,UInt64.MaxValue,tree,true);
            Assert(wire.Exchange(new EchoRequest())[0] is EchoResponse,"Unknown CANCEL has no separate response");

            pending=Pending(wire,tree,file,out message);
            File.WriteAllText(Path.Combine(root,"notify-event.txt"),"own fixture event");
            var completed=wire.Receive()[0];FinalNotification(wire,completed,message,pending.Header.AsyncID,NTStatus.STATUS_SUCCESS);
            Assert(((ChangeNotifyResponse)completed).OutputBuffer.Length>0,"Natural completion contains directory changes");

            WarmNotificationPool(wire,tree,file);
            int threadsBefore,handlesBefore;
            using(var process=Process.GetCurrentProcess()){process.Refresh();threadsBefore=process.Threads.Count;handlesBefore=process.HandleCount;}
            var requests=new Dictionary<ulong,ulong>();
            for(int index=0;index<64;index++){pending=Pending(wire,tree,file,out message);requests.Add(message,pending.Header.AsyncID);}
            var refused=wire.Exchange(Notification(tree,file))[0];
            Assert(refused.Header.Status==NTStatus.STATUS_INSUFFICIENT_RESOURCES&&!refused.Header.IsAsync,"Per-session pending notify limit must reject request 65");
            using(var other=new Wire(port))
            {
                other.Login();uint otherTree=ConnectTree(other);FileID otherFile=OpenDirectory(other,otherTree);ulong otherMessage;
                var otherPending=Pending(other,otherTree,otherFile,out otherMessage);
                other.Cancel(otherMessage,otherPending.Header.AsyncID,otherTree,true);
                FinalNotification(other,other.Receive()[0],otherMessage,otherPending.Header.AsyncID,NTStatus.STATUS_CANCELLED);
                var otherClose=new CloseRequest{FileId=otherFile};otherClose.Header.TreeID=otherTree;
                Assert(other.Exchange(otherClose)[0].Header.Status==NTStatus.STATUS_SUCCESS,"Another session remains independent of the full pending limit");
            }
            int threadGrowth;
            using(var process=Process.GetCurrentProcess()){process.Refresh();threadGrowth=process.Threads.Count-threadsBefore;}
            Assert(threadGrowth<16,"Pending notifications must share wait threads; growth="+threadGrowth);
            foreach(var request in requests)wire.Cancel(request.Key,request.Value,tree,true);
            var seen=new HashSet<ulong>();
            for(int index=0;index<requests.Count;index++)
            {
                var response=wire.Receive()[0];Assert(seen.Add(response.Header.MessageID),"No duplicate final notification");
                Assert(requests.ContainsKey(response.Header.MessageID),"No CANCEL response may consume a completion slot");
                FinalNotification(wire,response,response.Header.MessageID,requests[response.Header.MessageID],NTStatus.STATUS_CANCELLED);
            }
            Assert(wire.Exchange(new EchoRequest())[0] is EchoResponse&&!wire.HasResponse(100),"No trailing CANCEL response or duplicate notification");
            Thread.Sleep(200);GC.Collect();GC.WaitForPendingFinalizers();
            int handleGrowth;
            using(var process=Process.GetCurrentProcess()){process.Refresh();handleGrowth=process.HandleCount-handlesBefore;}
            Assert(handleGrowth<24,"Completed notifications must release native handles; growth="+handleGrowth);
            for(int wave=0;wave<2;wave++)
            {
                using(var process=Process.GetCurrentProcess()){process.Refresh();handlesBefore=process.HandleCount;}
                requests.Clear();seen.Clear();
                for(int index=0;index<64;index++){pending=Pending(wire,tree,file,out message);requests.Add(message,pending.Header.AsyncID);}
                foreach(var request in requests)wire.Cancel(request.Key,request.Value,tree,true);
                for(int index=0;index<64;index++)
                {
                    var response=wire.Receive()[0];Assert(seen.Add(response.Header.MessageID)&&requests.ContainsKey(response.Header.MessageID),"Repeated wave retains one completion per original request");
                    FinalNotification(wire,response,response.Header.MessageID,requests[response.Header.MessageID],NTStatus.STATUS_CANCELLED);
                }
                Thread.Sleep(100);GC.Collect();GC.WaitForPendingFinalizers();
                int growth;
                using(var process=Process.GetCurrentProcess()){process.Refresh();growth=process.HandleCount-handlesBefore;}
                Assert(growth<8,"Repeated cancellation must reach a stable native handle count; growth="+growth);
                Console.WriteLine("PASS: repeated 64-notify cancellation handle plateau, wave "+(wave+2)+": "+growth);
            }
            var close=new CloseRequest{FileId=file};close.Header.TreeID=tree;
            Assert(wire.Exchange(close)[0].Header.Status==NTStatus.STATUS_SUCCESS,"Close notification directory");
            Console.WriteLine("PASS: signed async identity, natural notify, sync/async CANCEL without reply, exactly-once completion, 64/session limit; threads +"+threadGrowth+", handles +"+handleGrowth);
        }
    }
    static QueryDirectoryRequest Query(uint tree,FileID file,uint size,bool restart=false,bool reopen=false)
    {
        var request=new QueryDirectoryRequest{FileId=file,FileInformationClass=FileInformationClass.FileNamesInformation,
            FileName="*.scan",OutputBufferLength=size,Restart=restart,Reopen=reopen};request.Header.TreeID=tree;return request;
    }
    static HashSet<string> Names(SMB2Command response)
    {
        Assert(response.Header.Status==NTStatus.STATUS_SUCCESS&&response is QueryDirectoryResponse,"Directory query returns entries");
        var result=new HashSet<string>();
        foreach(var entry in ((QueryDirectoryResponse)response).GetFileInformationList(FileInformationClass.FileNamesInformation))result.Add(((FileNamesInformation)entry).FileName);
        return result;
    }
    static void DirectoryBuffersAndRestart(int port,string root)
    {
        string first="first-"+new string('x',60)+"-Юникод.scan";File.WriteAllText(Path.Combine(root,first),"own query fixture");
        using(var wire=new Wire(port))
        {
            wire.Login();uint tree=ConnectTree(wire);FileID file=OpenDirectory(wire,tree);
            foreach(uint size in new uint[]{0,11})Assert(wire.Exchange(Query(tree,file,size))[0].Header.Status==NTStatus.STATUS_INFO_LENGTH_MISMATCH,"Buffer smaller than fixed record must fail");
            foreach(uint size in new uint[]{12,13,13})
            {
                var response=wire.Exchange(Query(tree,file,size))[0] as QueryDirectoryResponse;
                Assert(response!=null&&response.Header.Status==NTStatus.STATUS_BUFFER_OVERFLOW&&response.OutputBuffer.Length==size,"First record truncation must report overflow instead of empty success");
                Assert(BitConverter.ToUInt32(response.OutputBuffer,0)==0&&BitConverter.ToUInt32(response.OutputBuffer,8)==first.Length*2,"Partial first record retains fixed fields and full filename byte length");
            }
            Assert(Names(wire.Exchange(Query(tree,file,4096))[0]).Contains(first),"Retry after overflow must retain the first entry");
            Assert(wire.Exchange(Query(tree,file,4096))[0].Header.Status==NTStatus.STATUS_NO_MORE_FILES,"Enumeration reaches its end");
            File.Delete(Path.Combine(root,first));File.WriteAllText(Path.Combine(root,"second.scan"),"new file after snapshot");
            var fresh=Names(wire.Exchange(Query(tree,file,4096,true))[0]);
            Assert(fresh.Contains("second.scan")&&!fresh.Contains(first),"RESTART_SCANS refreshes added and deleted host entries");
            File.WriteAllText(Path.Combine(root,"third.scan"),"reopen fixture");
            fresh=Names(wire.Exchange(Query(tree,file,4096,false,true))[0]);
            Assert(fresh.Contains("second.scan")&&fresh.Contains("third.scan"),"REOPEN also refreshes the host snapshot");
            var close=new CloseRequest{FileId=file};close.Header.TreeID=tree;Assert(wire.Exchange(close)[0].Header.Status==NTStatus.STATUS_SUCCESS,"Close query directory");
        }
        Console.WriteLine("PASS: signed QUERY_DIRECTORY tiny/partial/full buffers, retained entry on retry and refreshed RESTART_SCANS/REOPEN.");
    }
    static int Main(string[] args)
    {
        try
        {
            if(args.Length>1&&args[1]=="--auth-only"){AuthenticationCompound();return 0;}
            string root=Path.Combine(args[0],"compound-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
            string input=new JavaScriptSerializer().Serialize(new{schema=1,session_nonce=new string('a',32),username="WCP_fixture",password="fixture_password_01234567890123456789",shares=new[]{new{name="PC_TEST",path=root}},@protected=new string[0]});
            using(var server=HostFilesServer.Start(input)){Related(server.Port,root,true);Related(server.Port,root,false);AsyncNotifications(server.Port,root);DirectoryBuffersAndRestart(server.Port,root);}
            AuthenticationCompound();return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
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
        internal Wire(int port){client.Connect("127.0.0.1",port);client.ReceiveTimeout=4000;client.SendTimeout=4000;stream=client.GetStream();}
        byte[] Read(int size){var bytes=new byte[size];for(int offset=0;offset<size;){int count=stream.Read(bytes,offset,size-offset);if(count==0)throw new EndOfStreamException();offset+=count;}return bytes;}
        internal List<SMB2Command> Exchange(params SMB2Command[] commands)
        {
            foreach(var command in commands){command.Header.MessageID=message++;command.Header.Credits=16;command.Header.CreditCharge=1;if(!command.Header.IsRelatedOperations)command.Header.SessionID=session;command.Header.IsSigned=Key!=null&&!(command is SessionSetupRequest);}
            byte[] data=SMB2Command.GetCommandChainBytes(new List<SMB2Command>(commands),Key,SMB2Dialect.SMB210);
            stream.Write(new[]{(byte)0,(byte)(data.Length>>16),(byte)(data.Length>>8),(byte)data.Length},0,4);stream.Write(data,0,data.Length);
            var prefix=Read(4);int length=(prefix[1]<<16)|(prefix[2]<<8)|prefix[3];Assert(prefix[0]==0&&length>=64&&length<1048576,"Bounded direct TCP response");data=Read(length);
            var result=new List<SMB2Command>();int offset=0;
            do
            {
                var response=SMB2Command.ReadResponse(data,offset);int count=response.Header.NextCommand==0?data.Length-offset:(int)response.Header.NextCommand;
                if(Key!=null){Assert(response.Header.IsSigned,"Every authenticated response must be signed");byte[] part=new byte[count];Array.Copy(data,offset,part,0,count);byte[] received=new byte[16];Array.Copy(part,48,received,0,16);Array.Clear(part,48,16);byte[] expected=Crypto("CalculateSignature",Key,SMB2Dialect.SMB210,part,0,count);for(int index=0;index<16;index++)Assert(received[index]==expected[index],"Compound response signature mismatch");}
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
    static int Main(string[] args)
    {
        try
        {
            if(args.Length>1&&args[1]=="--auth-only"){AuthenticationCompound();return 0;}
            string root=Path.Combine(args[0],"compound-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
            string input=new JavaScriptSerializer().Serialize(new{schema=1,session_nonce=new string('a',32),username="WCP_fixture",password="fixture_password_01234567890123456789",shares=new[]{new{name="PC_TEST",path=root}},@protected=new string[0]});
            using(var server=HostFilesServer.Start(input)){Related(server.Port,root,true);Related(server.Port,root,false);}
            AuthenticationCompound();return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

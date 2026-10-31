using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using SMBLibrary;
using SMBLibrary.Authentication.GSSAPI;
using SMBLibrary.Authentication.NTLM;
using SMBLibrary.Server;

internal sealed class HostFilesServer : IDisposable
{
    sealed class LoopbackServer : SMBServer
    {
        internal LoopbackServer(SMBShareCollection shares,GSSProvider authentication) : base(shares,authentication) { }
        internal void Listen(int port) { Start(IPAddress.Loopback,SMBTransportType.DirectTCPTransport,port,false,true,true,null); }
    }
    sealed class RecoveryOutput
    {
        internal volatile bool Failed;
        readonly object gate=new object();long sequence;
        void Write(object value,int limit)
        {
            try
            {
                string json=new JavaScriptSerializer{MaxJsonLength=262144,RecursionLimit=8}.Serialize(value);var ascii=new StringBuilder();
                foreach(char item in json){if(item>127)ascii.Append("\\u"+((int)item).ToString("x4"));else ascii.Append(item);}
                if(ascii.Length>limit)throw new IOException("Recovery output limit");
                Console.Out.WriteLine(ascii.ToString());Console.Out.Flush();
            }
            catch(Exception){Failed=true;throw;}
        }
        internal void Ready(HostFilesServer value)
        {lock(gate)Write(new{ok=true,schema=1,session_nonce=value.Nonce,port=value.Port,share_count=value.ShareCount},4096);}
        internal void Record(string nonce,string share,ConfinedFileStore.RecoveryRecord value)
        {
            lock(gate)Write(new{t="files-recovery",schema=1,session_nonce=nonce,seq=++sequence,share=share,original=value.Original,backup=value.Backup,code=value.Code},262144);
        }
    }
    readonly LoopbackServer server;
    readonly RecoveryOutput recovery;
    internal int Port { get; private set; }
    internal string Nonce { get; private set; }
    internal int ShareCount { get; private set; }
    HostFilesServer(LoopbackServer value,RecoveryOutput output) { server=value;recovery=output; }
    static string Text(Dictionary<string,object> value,string key,string pattern)
    {
        object item;
        if(!value.TryGetValue(key,out item)||!(item is string)||!Regex.IsMatch((string)item,pattern,RegexOptions.CultureInvariant))throw new ArgumentException("Invalid session");
        return (string)item;
    }
    static void Keys(Dictionary<string,object> value,params string[] keys)
    {
        if(value==null||value.Count!=keys.Length)throw new ArgumentException("Invalid session");
        foreach(string key in keys)if(!value.ContainsKey(key))throw new ArgumentException("Invalid session");
    }
    internal static HostFilesServer Start(string input)
    {
        if(input==null||input.Length>65536)throw new ArgumentException("Invalid session");
        var value=new JavaScriptSerializer{MaxJsonLength=65536,RecursionLimit=8}.Deserialize<Dictionary<string,object>>(input);
        Keys(value,"schema","session_nonce","username","password","shares","protected");
        if(!(value["schema"] is int)||(int)value["schema"]!=1)throw new ArgumentException("Invalid session");
        string nonce=Text(value,"session_nonce","\\A[a-f0-9]{32}\\z");
        string user=Text(value,"username","\\AWCP_[A-Za-z0-9_]{1,32}\\z");
        string password=Text(value,"password","\\A[A-Za-z0-9_-]{32,128}\\z");
        var entries=value["shares"] as ArrayList;
        if(entries==null||entries.Count<1||entries.Count>64)throw new ArgumentException("Invalid session");
        // This program, its private state and autostart folders stay unreachable even in all-drives mode.
        var protectedEntries=value["protected"] as ArrayList;
        if(protectedEntries==null||protectedEntries.Count>16)throw new ArgumentException("Invalid session");
        var protectedPaths=new List<string>();
        foreach(object entry in protectedEntries)
        {
            string path=entry as string;
            if(path==null||path.Length>32767||!Regex.IsMatch(path,"\\A[A-Za-z]:\\\\[^\\x00-\\x1f]*\\z",RegexOptions.CultureInvariant))throw new ArgumentException("Invalid session");
            protectedPaths.Add(path);
        }
        var output=new RecoveryOutput();var shares=new SMBShareCollection();var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(object entry in entries)
        {
            var item=entry as Dictionary<string,object>;Keys(item,"name","path");
            string name=Text(item,"name","\\APC_[A-Za-z0-9_]{1,48}\\z");
            if(!names.Add(name)||!(item["path"] is string))throw new ArgumentException("Invalid session");
            var store=new ConfinedFileStore((string)item["path"],protectedPaths);
            store.RecoveryRecorded=record=>output.Record(nonce,name,record);
            var share=new FileSystemShare(name,store,CachingPolicy.NoCaching);
            share.AccessRequested+=delegate(object sender,AccessRequestArgs request){request.Allow=String.Equals(request.UserName,user,StringComparison.Ordinal);};
            shares.Add(share);
        }
        // An unknown user (including Guest) must never receive a password from the callback.
        var authentication=new IndependentNTLMAuthenticationProvider(name=>String.Equals(name,user,StringComparison.Ordinal)?password:null);
        var result=new HostFilesServer(new LoopbackServer(shares,new GSSProvider(authentication)),output){Nonce=nonce,ShareCount=shares.Count};
        try
        {
            using(var random=RandomNumberGenerator.Create())
            {
                byte[] bytes=new byte[2];
                for(int attempt=0;attempt<32;attempt++)
                {
                    random.GetBytes(bytes);int port=49152+(BitConverter.ToUInt16(bytes,0)%16384);
                    try {result.server.Listen(port);result.Port=port;return result;}
                    catch(SocketException error){if(error.SocketErrorCode!=SocketError.AddressAlreadyInUse&&error.SocketErrorCode!=SocketError.AccessDenied)throw;} // WinNAT/Hyper-V reserve ranges inside 49152-65535
                }
            }
            throw new IOException("No session port available");
        }
        catch{result.Dispose();throw;}
    }
    public void Dispose(){server.Stop();}
    static string ReadRequest()
    {
        var input=new StringBuilder();int next;
        while((next=Console.In.Read())>=0&&next!='\n')
        {
            if(input.Length>=65536)throw new ArgumentException("Invalid session");input.Append((char)next);
        }
        return input.ToString();
    }
    static int Main()
    {
        try
        {
            using(var server=Start(ReadRequest()))
            {
                server.recovery.Ready(server);
                // Keep stdin ownership, but observe an output failure instead of
                // silently accepting mutations after the recovery channel is gone.
                int inputEnded=0;var input=new Thread(()=>{try{while(Console.In.Read()>=0){}}finally{Interlocked.Exchange(ref inputEnded,1);}}){IsBackground=true};input.Start();
                while(Volatile.Read(ref inputEnded)==0){if(server.recovery.Failed)return 3;Thread.Sleep(100);}
            }
            return 0;
        }
        catch(Exception){Console.Out.WriteLine("{\"ok\":false,\"schema\":1,\"code\":\"files_server_failed\"}");return 1;}
    }
}

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
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
    readonly LoopbackServer server;
    internal int Port { get; private set; }
    internal string Nonce { get; private set; }
    internal int ShareCount { get; private set; }
    HostFilesServer(LoopbackServer value) { server=value; }
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
        Keys(value,"schema","session_nonce","username","password","shares");
        if(!(value["schema"] is int)||(int)value["schema"]!=1)throw new ArgumentException("Invalid session");
        string nonce=Text(value,"session_nonce","\\A[a-f0-9]{32}\\z");
        string user=Text(value,"username","\\AWCP_[A-Za-z0-9_]{1,32}\\z");
        string password=Text(value,"password","\\A[A-Za-z0-9_-]{32,128}\\z");
        var entries=value["shares"] as ArrayList;
        if(entries==null||entries.Count<1||entries.Count>64)throw new ArgumentException("Invalid session");
        var shares=new SMBShareCollection();var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(object entry in entries)
        {
            var item=entry as Dictionary<string,object>;Keys(item,"name","path");
            string name=Text(item,"name","\\APC_[A-Za-z0-9_]{1,48}\\z");
            if(!names.Add(name)||!(item["path"] is string))throw new ArgumentException("Invalid session");
            var share=new FileSystemShare(name,new ConfinedFileStore((string)item["path"]),CachingPolicy.NoCaching);
            share.AccessRequested+=delegate(object sender,AccessRequestArgs request){request.Allow=String.Equals(request.UserName,user,StringComparison.Ordinal);};
            shares.Add(share);
        }
        // An unknown user (including Guest) must never receive a password from the callback.
        var authentication=new IndependentNTLMAuthenticationProvider(name=>String.Equals(name,user,StringComparison.Ordinal)?password:null);
        var result=new HostFilesServer(new LoopbackServer(shares,new GSSProvider(authentication))){Nonce=nonce,ShareCount=shares.Count};
        try
        {
            using(var random=RandomNumberGenerator.Create())
            {
                byte[] bytes=new byte[2];
                for(int attempt=0;attempt<8;attempt++)
                {
                    random.GetBytes(bytes);int port=49152+(BitConverter.ToUInt16(bytes,0)%16384);
                    try {result.server.Listen(port);result.Port=port;return result;}
                    catch(SocketException error){if(error.SocketErrorCode!=SocketError.AddressAlreadyInUse)throw;}
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
                Console.Out.WriteLine(new JavaScriptSerializer().Serialize(new{ok=true,schema=1,session_nonce=server.Nonce,port=server.Port,share_count=server.ShareCount}));Console.Out.Flush();
                // Only the work controller owns the pipe; closing the panel does not close it.
                while(Console.In.Read()>=0) { }
            }
            return 0;
        }
        catch(Exception){Console.Out.WriteLine("{\"ok\":false,\"schema\":1,\"code\":\"files_server_failed\"}");return 1;}
    }
}

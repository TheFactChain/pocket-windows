using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using WindowsClaude;

internal static class ClipboardLargeTextRegression
{
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer{MaxJsonLength=64*1024*1024,RecursionLimit=16};
    static readonly object Output=new object();
    static void Emit(object value){lock(Output){Console.WriteLine(Json.Serialize(value));Console.Out.Flush();}}
    static string Digest(string text){using(var sha=SHA256.Create())return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(text))).Replace("-","").ToLowerInvariant();}
    static void Assert(bool value){if(!value)throw new Exception("Large text fixture assertion failed");}
    static void Limits()
    {
        string nonce=new string('a',32),code="CLIPBOARD_TEXT_TOO_LARGE";
        Assert(GuestClipboard.MaxChars==4*1024*1024&&GuestClipboard.MaxLine==16*1024*1024&&ClipboardTransport.MaxLineBytes==GuestClipboard.MaxLine&&ClipboardTransport.MaxHelloBytes==4096);
        var state=new GuestClipboard.SyncState(nonce,"guest");
        state.Local(new string('x',GuestClipboard.MaxChars+1));Assert(state.TextError==code&&GuestClipboard.DecodeFrame(state.Outgoing,nonce).Type=="skip");
        state.Resend();Assert(state.TextError==code);
        state.Acknowledge(new GuestClipboard.Frame{Type="ack",Origin="guest",Sequence=state.Sequence,Ok=true});Assert(state.TextError==code);
        state.Commit(new GuestClipboard.Frame{Type="skip",Origin="host",Sequence=state.Sequence+1});Assert(state.TextError==code);
        state.Local("new copy");Assert(state.TextError==null);
        string maximum=new string('x',GuestClipboard.MaxChars);Assert(GuestClipboard.Decode(GuestClipboard.Encode(nonce,maximum),nonce)==maximum);
        Assert(GuestClipboard.Encode(nonce,maximum+"x")==null);
        Assert(GuestClipboard.CombinedStatus(code,"CONNECTED",null)==code);
        state.Local(maximum+"x");state.Commit(new GuestClipboard.Frame{Type="text",Origin="host",Sequence=state.Sequence+1,Text="published copy"});Assert(state.TextError==null);
        Emit(new{ok=true,maxChars=GuestClipboard.MaxChars,maxWire=GuestClipboard.MaxLine,status=code});
    }
    static void Codec()
    {
        var command=Json.Deserialize<Dictionary<string,object>>(Console.ReadLine());
        string nonce=new string('a',32),text=Encoding.UTF8.GetString(Convert.FromBase64String((string)command["text"]));
        var watch=Stopwatch.StartNew();string line=GuestClipboard.Encode(nonce,text);long encodeMs=watch.ElapsedMilliseconds;
        watch.Restart();string decoded=GuestClipboard.Decode((string)command["line"],nonce);long decodeMs=watch.ElapsedMilliseconds;
        bool invalid=GuestClipboard.Decode((string)command["invalid"],nonce)==null;
        Emit(new{line,encodeMs,decodeMs,units=text.Length,decoded=decoded==null?null:Digest(decoded),invalid,
            workingSet=Process.GetCurrentProcess().WorkingSet64});
    }
    static void Transport(int port)
    {
        string slot=null;object gate=new object();ClipboardTransport transport=null;
        transport=new ClipboardTransport(new string('a',32),ClipboardTransport.Port,
            ()=>{lock(gate){string result=slot;slot=null;return result;}},
            line=>{Emit(new{kind="received",sha256=Digest(line),bytes=Encoding.UTF8.GetByteCount(line)});lock(gate){Assert(slot==null);slot=line;}},
            value=>Emit(new{kind="peer",value}),()=>true,value=>Emit(new{kind="status",value}),
            ()=>{var client=new TcpClient(AddressFamily.InterNetwork);try{client.Connect(IPAddress.Loopback,port);return client;}catch{client.Close();throw;}});
        var worker=new Thread(transport.Run){IsBackground=true};worker.Start();
        Console.ReadLine();transport.Dispose();Assert(worker.Join(6000));Emit(new{kind="stopped",peakReaders=transport.PeakReaders});
    }
    static int Main(string[] args)
    {
        Console.InputEncoding=new UTF8Encoding(false,true);Console.OutputEncoding=new UTF8Encoding(false,true);
        try{if(args[0]=="limits")Limits();else if(args[0]=="codec")Codec();else if(args[0]=="transport")Transport(Int32.Parse(args[1]));else return 2;return 0;}
        catch(Exception){Emit(new{kind="fixture_failed"});return 1;}
    }
}

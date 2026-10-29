using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using WindowsClaude.GuestFiles;
using Microsoft.Win32;

internal static class GuestFilesRegression
{
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer();
    static string ConfigText(){return "{\"schema\":1,\"session_nonce\":\"0123456789abcdef0123456789abcdef\",\"username\":\"WCP_session\",\"password\":\"0123456789abcdefghijklmnopqrstuvwxyz_-\",\"shares\":[{\"name\":\"PC_work\",\"label\":\"Работа и 📁\",\"preferred_letter\":\"Z\"}]}";}
    static void Reject(string text){bool rejected=false;try{Program.ParseConfig(text);}catch(FormatException){rejected=true;}Assert(rejected,"Invalid config accepted: "+text.Substring(0,Math.Min(60,text.Length)));}
    static void ClipboardConfigRegression()
    {
        Config legacy=Program.ParseConfig(ConfigText());
        Assert(!legacy.ClipboardEnabled&&legacy.ClipboardNonce==null,"Legacy bootstrap must leave clipboard disabled");
        var value=Json.Deserialize<Dictionary<string,object>>(ConfigText());string secret=new string('f',32);
        value["clipboard"]=new{enabled=true,port=27844,nonce=secret};
        Config config=Program.ParseConfig(Json.Serialize(value));
        Assert(config.ClipboardEnabled&&config.ClipboardPort==27844&&config.ClipboardNonce==secret&&config.Nonce==legacy.Nonce,"Clipboard must use its separate private handshake nonce");
        var outcome=new Outcome{Code="OK"};
        string publicText=Program.Receipt(config,outcome)+Program.LinkRequest(config)+Program.LiveReceipt(config,outcome,1,"READY");
        Assert(!publicText.Contains(secret)&&publicText.Contains(config.Nonce),"Receipts must identify the session without exposing clipboard authentication");
        foreach(object bad in new object[]{new{enabled=true,port=27844},new{enabled=true,port=27844,nonce=legacy.Nonce},
            new{enabled=true,port=27844,nonce=new string('F',32)},new{enabled=true,port=27844,nonce=new string('f',31)},
            new{enabled=true,port=27844,nonce=(string)null},new{enabled=true,port=27844,nonce=123},
            new{enabled="true",port=27844,nonce=secret},new{enabled=1,port=27844,nonce=secret},
            new{enabled=true,port=27844,nonce=secret,extra=true}})
        {value["clipboard"]=bad;Reject(Json.Serialize(value));}
        value["clipboard"]=new{enabled=false,port=27844,nonce=secret};
        config=Program.ParseConfig(Json.Serialize(value));Assert(!config.ClipboardEnabled,"Explicitly disabled clipboard must remain disabled");
    }
    sealed class FakeNet : INetwork
    {
        internal readonly Dictionary<string,string> Connections=new Dictionary<string,string>();
        internal readonly HashSet<string> Deviceless=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        internal readonly HashSet<string> Used=new HashSet<string>();
        internal int Calls,Adds,Cancels;internal int AddError,CancelError,AddFailOn,EnumerationError;
        internal bool ThrowAfterAdd,ThrowEnumerate;internal string ReplaceAfterAdd;
        internal readonly List<string> CancelTargets=new List<string>();
        public IEnumerable<string> UsedLetters(){Calls++;return Used.Concat(Connections.Keys).ToArray();}
        public int Enumerate(out List<NetworkConnection> connections){Calls++;if(ThrowEnumerate)throw new IOException("fixture probe failure");connections=Connections.Select(item=>new NetworkConnection{Letter=item.Key,Remote=item.Value}).Concat(Deviceless.Select(remote=>new NetworkConnection{Letter="",Remote=remote})).ToList();return EnumerationError;}
        public int GetConnection(string letter,out string remote){Calls++;return Connections.TryGetValue(letter,out remote)?0:2250;}
        public int Add(string remote,string letter,string username,string password,uint flags){Calls++;Adds++;Assert(flags==4,"Mapping must use CONNECT_TEMPORARY only");Assert(remote.StartsWith(Program.Server+"\\PC_",StringComparison.Ordinal),"Mapping endpoint escaped");if(AddError!=0&&(AddFailOn==0||Adds==AddFailOn))return AddError;if(letter.Length>0)Connections[letter]=ReplaceAfterAdd??remote;else Deviceless.Add(remote);if(ThrowAfterAdd)throw new IOException("uncertain fixture add");return 0;}
        public int Cancel(string target,uint flags,bool force){Calls++;Cancels++;CancelTargets.Add(target);Assert(flags==0&&!force,"Cleanup must not force open files or modify a persistent profile");if(CancelError!=0)return CancelError;if(target.StartsWith("\\\\",StringComparison.Ordinal))Deviceless.Remove(target);else Connections.Remove(target);return 0;}
    }
    sealed class FakeNic : INicConfiguration
    {
        internal NicState[] States;internal int Reads,Changes;internal string ChangedId;
        public NicState[] Read(){Reads++;return States;}
        public void Configure(string id){Changes++;ChangedId=id;}
    }
    static void DisplayReceiptRegression()
    {
        // SETTINGS.JSN exactly as windows_guest_settings.write_settings writes it.
        string settings="{\"schema\":1,\"width\":1864,\"height\":1048,\"session_nonce\":\"0123456789abcdef0123456789abcdef\"}";
        var ok=Json.Deserialize<Dictionary<string,object>>(Program.DisplayReceipt(settings,1864,1048));
        var keys=new[]{"schema","session_nonce","requested","current","success","code","display_code"};
        Assert(ok.Count==keys.Length&&keys.All(ok.ContainsKey),"Display receipt must carry exactly the keys validate_receipt accepts");
        Assert((int)ok["schema"]==1&&(string)ok["session_nonce"]=="0123456789abcdef0123456789abcdef"&&(bool)ok["success"]&&(string)ok["code"]=="OK"&&(string)ok["display_code"]=="OK","Applied size must confirm");
        var requested=(Dictionary<string,object>)ok["requested"];var current=(Dictionary<string,object>)ok["current"];
        Assert((int)requested["width"]==1864&&(int)requested["height"]==1048&&(int)current["width"]==1864&&(int)current["height"]==1048&&requested.Count==2&&current.Count==2,"Requested and current sizes must be reported as given");
        var short_=Json.Deserialize<Dictionary<string,object>>(Program.DisplayReceipt(settings,1280,800));
        Assert(!(bool)short_["success"]&&(string)short_["code"]=="DISPLAY_APPLY_INCOMPLETE"&&(string)short_["display_code"]=="DISPLAY_APPLY_INCOMPLETE","A different desktop size must not be reported as applied");
        var offered=new List<int[]>{new[]{1280,800,32,60},new[]{1736,976,32,60},new[]{1864,1048,16,75},new[]{1864,1048,32,60},new[]{1864,1048,32,75}};
        Assert(Program.DisplayTarget(offered,new[]{1864,1048})==4,"The window's size is chosen in true colour at the highest refresh");
        Assert(Program.DisplayTarget(offered.Take(3).ToList(),new[]{1864,1048})==0,"A window size Windows does not offer falls back to the standard safe mode");
        Assert(Program.DisplayTarget(offered.Skip(1).Take(2).ToList(),new[]{1864,1048})==-1,"Without the safe mode nothing is switched");
        Assert(Program.DisplayTarget(offered,null)==-1,"Without the panel's request nothing is switched or saved");
        Assert(Program.ModeIndex(offered,Program.SafeWidth,Program.SafeHeight)==0&&Program.SafeWidth==1280&&Program.SafeHeight==800,"Safe mode is the standard 1280x800");
        var detailed=Json.Deserialize<Dictionary<string,object>>(Program.DisplayReceipt(settings,1736,976,"REQ_UNLISTED_MAX_BELOW_ALREADY"));
        Assert((string)detailed["code"]=="DISPLAY_APPLY_INCOMPLETE"&&(string)detailed["display_code"]=="REQ_UNLISTED_MAX_BELOW_ALREADY","A failed fit must carry its reason");
        Assert((string)Json.Deserialize<Dictionary<string,object>>(Program.DisplayReceipt(settings,1864,1048,"REQ_LISTED_MAX_SAME_SAVE_OK"))["display_code"]=="OK","A confirmed size reports plain OK");
        Assert((string)Json.Deserialize<Dictionary<string,object>>(Program.DisplayReceipt(settings,1736,976,"bad detail 1"))["display_code"]=="DISPLAY_APPLY_INCOMPLETE","A malformed reason must not reach the host");
        var modes=new List<int[]>{new[]{1280,800,32,60},new[]{1736,976,32,60},new[]{1864,1048,16,60}};
        Assert(Program.DisplayDetail(modes,modes[1],1864,1048,"ALREADY")=="REQ_UNLISTED_MAX_BELOW_ALREADY","Only true-colour modes count as offered");
        modes.Add(new[]{1864,1048,32,60});
        Assert(Program.DisplayDetail(modes,modes[3],1864,1048,"SAVE_BADMODE_LIVE_BADMODE")=="REQ_LISTED_MAX_SAME_SAVE_BADMODE_LIVE_BADMODE","Refused switch must name both results");
        Assert(Program.DisplayDetail(modes,null,1864,1048,"NO_MODE")=="REQ_LISTED_MAX_NONE_NO_MODE"&&Program.DisplayDetail(modes,new[]{2560,1600,32,60},1864,1048,"SAVE_NOTUPDATED_LIVE_NOTUPDATED").Length<=64,"Details stay within the host's code limit");
        foreach(string bad in new[]{"{\"schema\":2,\"width\":1864,\"height\":1048,\"session_nonce\":\"0123456789abcdef0123456789abcdef\"}","{\"schema\":1,\"width\":1864,\"height\":1048,\"session_nonce\":\"NOT-A-NONCE\"}","{\"schema\":1,\"width\":1864,\"height\":1048}"})
        {bool rejected=false;try{Program.DisplayReceipt(bad,1864,1048);}catch(FormatException){rejected=true;}Assert(rejected,"Invalid settings must not produce a receipt: "+bad);}
    }
    static void LinkRegression()
    {
        string id="{d4fd0ad7-e160-4146-903c-2337af221325}";
        Assert(Program.NicMarker(@"C:\State",id)==@"C:\State\nic-d4fd0ad7e1604146903c2337af221325.ok"&&Program.NicMarker(@"C:\State",id.ToUpperInvariant())==Program.NicMarker(@"C:\State",id),"Adapter marker must be one fixed file per adapter GUID");
        Refuse(()=>Program.NicMarker(@"C:\State",@"..\escape"),"ISOLATED_NIC_NOT_UNIQUE");
        Assert(Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"},DnsServers=new[]{"fec0:0:0:ffff::1%1"}}),"Windows' built-in IPv6 resolvers alone must not block readiness (observed live)");
        Assert(Program.DefaultIpv6Dns("fec0:0:0:ffff::1%1")&&Program.DefaultIpv6Dns("fec0::ffff:0:0:0:3")&&!Program.DefaultIpv6Dns("192.168.234.3")&&!Program.DefaultIpv6Dns("fec0:0:0:ffff::4")&&!Program.DefaultIpv6Dns(null),"Only Windows' built-in unreachable IPv6 resolvers are ignorable");
        Config config=Program.ParseConfig(ConfigText());string request=Program.LinkRequest(config);var value=Json.Deserialize<Dictionary<string,object>>(request);
        Assert(value.Count==5&&(string)value["code"]=="NIC_STATIC"&&(bool)value["success"]==false&&(string)value["session_nonce"]==config.Nonce&&((System.Collections.ArrayList)value["mappings"]).Count==0,"Link request must be an exact receipt the host validates");
        Assert(!request.Contains(config.Password)&&!request.Contains(config.Username),"Link request leaked credentials");
        string described=Program.Describe(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},DnsServers=new[]{"fec0:0:0:ffff::1%1"}});
        Assert(described.Contains("192.168.234.15")&&described.Contains("fec0:0:0:ffff::1%1")&&!described.Contains(config.Password),"NIC diagnostics must name the observed addresses");
        Assert(Program.Describe(null).Length>0,"Missing NIC must still be described");
    }
    static void Refuse(Action action,string code)
    {
        try{action();}catch(InvalidOperationException error){Assert(error.Message==code,"Unexpected refusal: "+error.Message);return;}
        throw new Exception("Expected refusal: "+code);
    }
    static void BootstrapRegression()
    {
        string id="{d4fd0ad7-e160-4146-903c-2337af221325}";
        var isolated=new NicState{Id=id,Mac="525400770004",Up=true};
        var main=new NicState{Id="{824caf81-7b76-4594-ac96-dcb383b6526c}",Mac="525400770003",Up=true,HasGateway=true,HasDns=true,Addresses=new[]{"10.0.2.15"},Masks=new[]{"255.255.255.0"}};
        var nic=new FakeNic{States=new[]{main,isolated}};
        Refuse(()=>Program.ConfigureNic(nic,false),"ADMIN_TOKEN_REQUIRED");Assert(nic.Reads==0&&nic.Changes==0,"Ordinary token cannot configure adapters");
        Program.ConfigureNic(nic,true);Assert(nic.Changes==1&&nic.ChangedId==id&&main.HasGateway&&main.HasDns,"Only exact-MAC adapter may cross configuration boundary");
        nic=new FakeNic{States=new[]{main}};Refuse(()=>Program.ConfigureNic(nic,true),"ISOLATED_NIC_NOT_UNIQUE");Assert(nic.Changes==0,"Missing isolated NIC must not fall back to VPN/LAN");
        nic=new FakeNic{States=new[]{isolated,isolated}};Refuse(()=>Program.ConfigureNic(nic,true),"ISOLATED_NIC_NOT_UNIQUE");Assert(nic.Changes==0,"Duplicate MAC must not pick an arbitrary NIC");
        nic=new FakeNic{States=new[]{new NicState{Id="arbitrary & command",Mac="525400770004"}}};Refuse(()=>Program.ConfigureNic(nic,true),"ISOLATED_NIC_NOT_UNIQUE");Assert(nic.Changes==0,"Adapter identifier cannot inject commands");
        string[] commands=Program.NicCommands(7,19);
        Assert(commands.SequenceEqual(new[]{"interface ipv4 set address name=7 source=static address=192.168.234.15 mask=255.255.255.0 gateway=none store=persistent","interface ipv4 set dnsservers name=7 source=static address=none register=none validate=no","interface ipv6 set dnsservers name=19 source=static address=none register=none validate=no"}),"Native command plan must target only selected per-family indices and remove DNS/default gateway on that NIC");
        Assert(Program.NicCommands(7,null).Length==2,"IPv4-only NIC cannot require an IPv6 command");Refuse(()=>Program.NicCommands(0,19),"ISOLATED_NIC_NOT_UNIQUE");Refuse(()=>Program.NicCommands(7,-1),"ISOLATED_NIC_NOT_UNIQUE");
        isolated.Addresses=new[]{"192.168.234.15"};isolated.Masks=new[]{"255.255.255.0"};nic=new FakeNic{States=new[]{main,isolated}};Program.ConfigureNic(nic,true);Assert(nic.Changes==0,"Ready NIC must remain untouched");
        isolated.Up=false;nic=new FakeNic{States=new[]{main,isolated}};Program.ConfigureNic(nic,true);Assert(nic.Changes==1,"Unplugged NIC must still receive its persistent static setup");
        Assert(Program.StoredNicMatches(0,new[]{"192.168.234.15"},new[]{"255.255.255.0"},new string[0],""),"A shipped static setup without gateway and DNS counts as configured");
        Assert(Program.StoredNicMatches(0,new[]{"192.168.234.15"},new[]{"255.255.255.0"},null,null),"Missing gateway and DNS values are fine");
        Assert(!Program.StoredNicMatches(1,new[]{"192.168.234.15"},new[]{"255.255.255.0"},null,null)&&!Program.StoredNicMatches(null,new[]{"192.168.234.15"},new[]{"255.255.255.0"},null,null),"DHCP is never the isolated setup");
        Assert(!Program.StoredNicMatches(0,new[]{"192.168.234.16"},new[]{"255.255.255.0"},null,null)&&!Program.StoredNicMatches(0,new[]{"192.168.234.15"},new[]{"255.255.0.0"},null,null),"Another address or mask is not the isolated setup");
        Assert(!Program.StoredNicMatches(0,new[]{"192.168.234.15"},new[]{"255.255.255.0"},new[]{"192.168.234.2"},"")&&!Program.StoredNicMatches(0,new[]{"192.168.234.15"},new[]{"255.255.255.0"},null,"192.168.234.3"),"A gateway or DNS server would open a route and is refused");
        int prompts=0,reads=0,waits=0,links=0;Program.EnsureNic(()=>true,()=>{prompts++;return 0;},()=>links++,()=>waits++);Assert(prompts==0&&waits==0&&links==0,"Ready NIC must not request UAC or the link");
        var order=new List<string>();Program.EnsureNic(()=>{order.Add("ready");return order.Contains("link")&&order.Count(item=>item=="ready")>2;},()=>{order.Add("configure");return 0;},()=>order.Add("link"),()=>order.Add("wait"));
        Assert(String.Join(",",order)=="ready,configure,link,ready,wait,ready","Static setup must precede the link request, and readiness must be rechecked only after it: "+String.Join(",",order));
        Refuse(()=>Program.EnsureNic(()=>false,()=>1,()=>{throw new Exception("Failed child must not request the link");},()=>{throw new Exception("Failed child must not proceed");}),"NIC_CONFIGURATION_FAILED");
        reads=0;waits=0;Refuse(()=>Program.EnsureNic(()=>{reads++;return false;},()=>0,()=>links++,()=>waits++),"NIC_NOT_READY");Assert(reads==32&&waits==30&&links==1,"Successful child exit cannot replace bounded state verification");
        reads=0;waits=0;prompts=0;Program.EnsureNic(()=>++reads>5,()=>{prompts++;return 0;},()=>links++,()=>waits++,20);Assert(prompts==0&&reads==6&&waits==5,"A remembered adapter gets time to come up before any UAC prompt");
        string root=Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location),"upgrade-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(root);
        string source=Path.Combine(root,"source.exe"),installed=Path.Combine(root,"installed"),old=null,current=null;
        File.WriteAllText(source,"first binary");Program.StageHelper(source,installed,target=>old=target);Assert(File.ReadAllText(old)=="first binary","First helper must stage complete bytes");
        using(var running=File.Open(old,FileMode.Open,FileAccess.Read,FileShare.Read))
        {
            File.WriteAllText(source,"next binary");Refuse(()=>Program.StageHelper(source,installed,target=>{throw new InvalidOperationException("TEST_STARTUP_FAILURE");}),"TEST_STARTUP_FAILURE");
            Assert(File.ReadAllText(old)=="first binary","Startup failure must preserve the previous runnable helper");
            Program.StageHelper(source,installed,target=>current=target);Assert(current!=old&&File.ReadAllText(current)=="next binary"&&File.Exists(old),"Upgrade must use a new immutable version and preserve the running old version");
        }
        Assert(Program.StartupTargetMatches(old,"--apply",installed)&&Program.StartupTargetMatches(Path.Combine(installed,"HostFilesGuest.exe"),"--apply",installed),"Versioned and legacy startup must be upgradeable");
        Assert(!Program.StartupTargetMatches(source,"--apply",installed)&&!Program.StartupTargetMatches(old,"--install",installed)&&!Program.StartupTargetMatches(Path.Combine(installed,"other.exe"),"--apply",installed),"Foreign startup targets/arguments must survive untouched");
        File.WriteAllText(current,"tampered");Refuse(()=>Program.StageHelper(source,installed,target=>{throw new Exception("Corrupt staged binary cannot become startup");}),"EXISTING_HELPER_CONFLICT");
    }
    static Outcome Apply(Config config,List<Mapping> old,FakeNet net,Func<bool> ready=null,bool elevated=false,bool interactive=true)
    {return Program.ApplyMappings(config,old,net,ready??(()=>true),()=>{},elevated,interactive,owned=>{});}
    static void MappingJournalRegression()
    {
        Config config=Program.ParseConfig(ConfigText());var empty=Program.ParseConfig(ConfigText());empty.Shares.Clear();
        var net=new FakeNet();string durable="[]";
        Outcome result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>{throw new IOException("intent write failure");});
        Assert(result.Code=="OWN_RECEIPT_WRITE_FAILED"&&net.Adds==0&&result.Owned.Count==0&&net.Connections.Count==0,"Failed intent persistence must precede Add and preserve durable ownership");
        net=new FakeNet();int writes=0;
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>{if(writes++==0)Assert(net.Adds==0,"Intent must be persisted before native Add");durable=Program.OwnedReceipt(owned);});
        Assert(result.Success&&Program.ParseOwned(durable).Count==1&&net.Connections.Count==1,"Successful native add already has durable recovery intent");
        Assert(writes==1,"Successful Add requires only the durable pre-add intent, not a second commit write");
        net=new FakeNet{AddError=1219};durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Code=="CREDENTIAL_CONFLICT"&&result.Owned.Count==0&&Program.ParseOwned(durable).Count==0,"Definite Add refusal rolls intent back");
        net=new FakeNet{AddError=5};writes=0;durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>{if(writes++>0)throw new IOException("intent rollback write failure");durable=Program.OwnedReceipt(owned);});
        Assert(result.Code=="OWN_RECEIPT_WRITE_FAILED"&&result.Owned.Count==1&&Program.ParseOwned(durable).Count==1,"Failed intent rollback must retain its full durable ownership");
        net=new FakeNet{ThrowAfterAdd=true};durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(!result.Success&&net.Cancels==1&&net.Connections.Count==0&&result.Owned.Count==0&&Program.ParseOwned(durable).Count==0,"Uncertain Add is inspected and rolled back without orphaning its mapping");
        net=new FakeNet{ThrowAfterAdd=true,CancelError=2401};durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Code=="OWN_MAPPING_BUSY"&&net.Connections.Count==1&&result.Owned.Count==1&&Program.ParseOwned(durable).Count==1,"Failed uncertain-add cancellation preserves recovery ownership");
        net.CancelError=0;
        result=Program.ApplyMappings(empty,Program.ParseOwned(durable),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Success&&net.Connections.Count==0&&Program.ParseOwned(durable).Count==0,"Next helper run can recover the durable uncertain-add intent");
        net=new FakeNet{ThrowAfterAdd=true,ReplaceAfterAdd="\\\\foreign\\manual"};durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Code=="MAPPING_OUTCOME_UNKNOWN"&&net.Cancels==0&&net.Connections.Values.Single()=="\\\\foreign\\manual"&&Program.ParseOwned(durable).Count==1,"Uncertain Add must never cancel a replacement foreign mapping");
        net=new FakeNet();foreach(char letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")net.Used.Add(letter.ToString());durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Success&&net.Deviceless.Count==1,"Deviceless fixture created");
        result=Program.ApplyMappings(empty,Program.ParseOwned(durable),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Success&&net.Deviceless.Count==0&&net.CancelTargets.Single()==Program.Server+"\\PC_work","Owned deviceless mapping is cancelled by exact UNC");
        net=new FakeNet();net.Deviceless.Add(Program.Server+"\\PC_manual");result=Apply(empty,new List<Mapping>(),net);
        Assert(result.Code=="UNOWNED_CONNECTION_REMAINS"&&net.Cancels==0&&net.Deviceless.Count==1,"A manually created reserved-namespace UNC is not adopted or cancelled");
        net=new FakeNet();net.Deviceless.Add(Program.Server+"\\PC_work");net.Connections["X"]=Program.Server+"\\PC_work";
        result=Apply(empty,new List<Mapping>{new Mapping{Name="PC_work",Letter=""}},net);
        Assert(result.Code=="UNOWNED_CONNECTION_REMAINS"&&net.Cancels==0&&result.Owned.Count==1,"An owned deviceless UNC must not disconnect an unowned lettered use of the same resource");
        net=new FakeNet{EnumerationError=5};result=Apply(config,new List<Mapping>(),net);
        Assert(result.Code=="CONNECTION_INSPECTION_FAILED"&&net.Adds==0&&net.Cancels==0,"Incomplete enumeration must not create or cancel mappings");
        net=new FakeNet{ThrowAfterAdd=true};foreach(char letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")net.Used.Add(letter.ToString());durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Code=="MAPPING_FAILED"&&net.Deviceless.Count==0&&net.CancelTargets.Single()==Program.Server+"\\PC_work"&&Program.ParseOwned(durable).Count==0,"Uncertain deviceless Add also rolls back through its exact durable UNC intent");
        net=new FakeNet{ThrowAfterAdd=true};writes=0;durable="[]";
        result=Program.ApplyMappings(config,new List<Mapping>(),net,()=>true,()=>{},false,true,owned=>{if(writes++>0)throw new IOException("uncertain rollback write failure");durable=Program.OwnedReceipt(owned);});
        Assert(result.Code=="OWN_RECEIPT_WRITE_FAILED"&&net.Connections.Count==0&&result.Owned.Count==1&&Program.ParseOwned(durable).Count==1,"Cancellation followed by failed receipt write must retain recovery intent");
        result=Program.ApplyMappings(empty,Program.ParseOwned(durable),net,()=>true,()=>{},false,true,owned=>durable=Program.OwnedReceipt(owned));
        Assert(result.Success&&Program.ParseOwned(durable).Count==0&&net.Cancels==1,"Recovery clears missing actual mappings without another cancellation");
        net=new FakeNet();net.Connections["Z"]=Program.Server+"\\PC_work";durable=Program.OwnedReceipt(new[]{new Mapping{Name="PC_work",Letter="Z"}});
        result=Program.ApplyMappings(empty,Program.ParseOwned(durable),net,()=>true,()=>{},false,true,owned=>{throw new IOException("cleanup receipt failure");});
        Assert(result.Code=="OWN_RECEIPT_WRITE_FAILED"&&net.Connections.Count==0&&result.Owned.Count==1&&Program.ParseOwned(durable).Count==1,"Failed post-cancel persistence keeps the previous durable ownership state");
    }
    static void LiveProbeRegression()
    {
        Config config=Program.ParseConfig(ConfigText());var net=new FakeNet();Outcome applied=Apply(config,new List<Mapping>(),net);
        string original=Program.Receipt(config,applied),ownership=Program.OwnedReceipt(applied.Owned);int adds=net.Adds,cancels=net.Cancels,calls=net.Calls;
        Outcome result=Program.VerifyMappings(config,applied,net,()=>true);
        Assert(result.Success&&result.Mappings.Single().Letter=="Z"&&net.Calls==calls+1,"Live confirmation must freshly enumerate exact owned mappings");
        result.Owned[0].Letter="Y";result.Mappings[0].Letter="X";
        Assert(Program.Receipt(config,applied)==original&&Program.OwnedReceipt(applied.Owned)==ownership,"Live reports must own their metadata copies");
        net.EnumerationError=5;result=Program.VerifyMappings(config,applied,net,()=>true);
        Assert(!result.Success&&result.Code=="CONNECTION_INSPECTION_FAILED"&&applied.Success,"Transient enum failure does not change the original outcome");
        net.EnumerationError=0;Assert(Program.VerifyMappings(config,applied,net,()=>true).Success,"The next successful fresh enumeration clears a transient probe failure");
        net.ThrowEnumerate=true;Assert(Program.VerifyMappings(config,applied,net,()=>true).Code=="CONNECTION_INSPECTION_FAILED","Thrown inspection failure is contained");net.ThrowEnumerate=false;
        Assert(Program.VerifyMappings(config,applied,net,()=>false).Code=="NIC_NOT_READY","Lost isolated NIC is no longer reported ready");
        Assert(Program.VerifyMappings(config,applied,net,()=>{throw new IOException("fixture NIC read");}).Code=="NIC_NOT_READY","NIC read failure is a failed read-only confirmation");
        net.Connections.Remove("Z");result=Program.VerifyMappings(config,applied,net,()=>true);
        Assert(result.Code=="MAPPING_MISSING"&&result.Mappings.Count==0&&result.Owned.Count==1,"Removed mapping is reported without losing durable ownership");
        net.Connections["Z"]="\\\\foreign\\manual";Assert(Program.VerifyMappings(config,applied,net,()=>true).Code=="MAPPING_CHANGED","Foreign letter replacement must not be reported as our connected mapping");
        Assert(net.Connections["Z"]=="\\\\foreign\\manual"&&net.Adds==adds&&net.Cancels==cancels,"Live probing must never reconnect or cancel anything");
        net.Connections["Z"]=Program.Server+"\\PC_work";Assert(Program.VerifyMappings(config,applied,net,()=>true).Success,"Read-only confirmation can recover once the exact mapping reappears");
        var unowned=new Outcome();unowned.Mappings.Add(new Mapping{Name="PC_work",Letter="Z"});
        Assert(Program.VerifyMappings(config,unowned,net,()=>true).Code=="OWN_RECEIPT_MISMATCH","A coincidentally matching unowned mapping is insufficient");
        var uncertain=new Outcome{Code="MAPPING_OUTCOME_UNKNOWN"};uncertain.Owned.Add(new Mapping{Name="PC_work",Letter="Z"});
        Assert(Program.VerifyMappings(config,uncertain,net,()=>true).Success&&uncertain.Code=="MAPPING_OUTCOME_UNKNOWN"&&uncertain.Mappings.Count==0,"Fresh exact ownership can confirm an uncertain apply without modifying its original result");
        var deviceless=new Outcome{NeedsShortcut=true};deviceless.Owned.Add(new Mapping{Name="PC_work",Letter=""});net=new FakeNet();net.Deviceless.Add(Program.Server+"\\PC_work");
        Assert(Program.VerifyMappings(config,deviceless,net,()=>true).Success,"Deviceless ownership requires its exact fresh UNC entry");
        net.Deviceless.Clear();Assert(Program.VerifyMappings(config,deviceless,net,()=>true).Code=="MAPPING_MISSING","Lost deviceless mapping is detected");
        config.Shares.Clear();net=new FakeNet();Assert(Program.VerifyMappings(config,new Outcome(),net,()=>{throw new Exception("Off folders do not require NIC");}).Success&&net.Calls==1,"Even off-mode heartbeat has a fresh enumeration without configuring/probing NIC");
        var invalid=new Outcome{Code="CONFIG_INVALID"};var legacy=Json.Deserialize<Dictionary<string,object>>(Program.Receipt(null,invalid));
        Assert((int)legacy["schema"]==1&&legacy.Count==5&&legacy["session_nonce"]==null&&!(bool)legacy["success"],"Initial config failure retains the exact legacy receipt shape");
    }
    static void LiveReceiptRegression()
    {
        Config config=Program.ParseConfig(ConfigText());var net=new FakeNet();Outcome applied=Apply(config,new List<Mapping>(),net);config.ClipboardEnabled=true;
        var statuses=new Dictionary<string,string>{{"READY","ready"},{"FILES_READY","ready"},{"CONNECTED","ready"},{"WAITING","connecting"},{"RECONNECTING","connecting"},{"NIC_UNAVAILABLE","connecting"},{"START_FAILED","failed"},{"PROTOCOL_FAILED","failed"},{"FILES_FAILED","failed"},{"CLEANUP_FAILED","failed"},{"CLIPBOARD_READ_FAILED","failed"},{"CLIPBOARD_BUSY","unconfirmed"},{"CLIPBOARD_UNSUPPORTED","unconfirmed"},{"FUTURE_CODE","unconfirmed"},{"OFF","unconfirmed"}};
        foreach(var expected in statuses)
        {
            var value=Json.Deserialize<Dictionary<string,object>>(Program.LiveReceipt(config,applied,123456789,expected.Key));var clip=(Dictionary<string,object>)value["clipboard"];
            Assert(value.Count==7&&new[]{"schema","session_nonce","success","code","mappings","serial","clipboard"}.All(value.ContainsKey)&&clip.Count==2,"Live receipt exact schema fields");
            Assert((int)value["schema"]==2&&Convert.ToInt64(value["serial"])==123456789&&(string)clip["status"]==expected.Value&&(string)clip["code"]==expected.Key,"Fixed clipboard status mapping: "+expected.Key);
        }
        foreach(string malformed in new[]{null,"secret/path",new string('A',65),"lower"})
        {
            string wire=Program.LiveReceipt(config,applied,1,malformed);var clip=(Dictionary<string,object>)Json.Deserialize<Dictionary<string,object>>(wire)["clipboard"];
            Assert((string)clip["status"]=="unconfirmed"&&(string)clip["code"]=="UNCONFIRMED","Unknown/unsafe clipboard strings must not be echoed");
            Assert(!wire.Contains(config.Password)&&!wire.Contains(config.Username)&&!wire.Contains("192.168")&&!wire.Contains("secret/path"),"Live report contains no credentials, endpoints, contents or paths");
        }
        config.ClipboardEnabled=false;var off=(Dictionary<string,object>)Json.Deserialize<Dictionary<string,object>>(Program.LiveReceipt(config,applied,1,"READY"))["clipboard"];
        Assert((string)off["status"]=="off"&&(string)off["code"]=="OFF","Requested off is authoritative even if an old global status exists");
        foreach(long serial in new[]{0L,-1L,Program.MaxReceiptSerial+1})
        {bool rejected=false;try{Program.LiveReceipt(config,applied,serial);}catch(ArgumentOutOfRangeException){rejected=true;}Assert(rejected,"Live serial outside exact host integer range rejected");}
        Assert(Program.LiveReceipt(config,applied,Program.MaxReceiptSerial).Contains("9007199254740991"),"Largest exact serial preserved");
        Assert(Program.NextReceiptSerial(0,100000)==100000&&Program.NextReceiptSerial(100000,90000)==100001&&Program.NextReceiptSerial(100001,103000)==103000,"UTC seed plus previous+1 remains monotonic across local clock changes");
    }
    static void HeartbeatOwnerRegression()
    {
        Config config=Program.ParseConfig(ConfigText());var net=new FakeNet();Outcome applied=Apply(config,new List<Mapping>(),net);string original=Program.OwnedReceipt(applied.Owned);long serial=0;var sent=new List<Dictionary<string,object>>();
        string name=@"Local\GuestFilesRegression."+Guid.NewGuid().ToString("N");
        using(var owner=new Mutex(false,name))
        {
            int result=Program.WithApplyMutex(owner,()=>
            {
                bool entered=false;int blocked=-1;var contender=new Thread(()=>{using(var second=new Mutex(false,name))blocked=Program.WithApplyMutex(second,()=>{entered=true;return 0;});});contender.Start();Assert(contender.Join(3000)&&!entered&&blocked==2,"Another helper performs no work/serial writes while the resident owner holds its mutex");
                for(int tick=0;tick<3;tick++)
                {
                    net.EnumerationError=tick==1?5:0;
                    serial=Program.HeartbeatTick(config,applied,net,()=>true,serial,()=>1000,()=>null,line=>sent.Add(Json.Deserialize<Dictionary<string,object>>(line)));
                }
                serial=Program.HeartbeatTick(config,applied,net,()=>true,serial,()=>900,()=>{throw new IOException("status fixture");},line=>{throw new IOException("serial fixture");});
                serial=Program.HeartbeatTick(config,applied,net,()=>true,serial,()=>900,()=>null,line=>sent.Add(Json.Deserialize<Dictionary<string,object>>(line)));
                return 7;
            });
            Assert(result==7&&sent.Count==4&&(bool)sent[0]["success"]&&!(bool)sent[1]["success"]&&(bool)sent[2]["success"]&&(bool)sent[3]["success"],"Fresh next heartbeat recovers transient probe/send failure with clipboard off and no remap");
            Assert(Convert.ToInt64(sent[0]["serial"])==1000&&Convert.ToInt64(sent[1]["serial"])==1001&&Convert.ToInt64(sent[2]["serial"])==1002&&Convert.ToInt64(sent[3]["serial"])==1004&&serial==1004,"Serial advances even after clock rollback/send failure");
            bool available=false;var after=new Thread(()=>{using(var second=new Mutex(false,name)){available=second.WaitOne(0);if(available)second.ReleaseMutex();}});after.Start();Assert(after.Join(3000)&&available,"Owner releases only after resident work ends");
        }
        Assert(Program.OwnedReceipt(applied.Owned)==original&&applied.Success&&net.Adds==1&&net.Cancels==0,"Heartbeat failures never modify original state or network ownership");
    }
    static void NativeConnectionBufferRegression()
    {
        Type winNet=typeof(Program).GetNestedType("WinNet",BindingFlags.NonPublic);
        MethodInfo read=winNet.GetMethod("ResourceString",BindingFlags.Static|BindingFlags.NonPublic);
        IntPtr buffer=Marshal.AllocHGlobal(128);
        try
        {
            for(int index=0;index<128;index++)Marshal.WriteByte(buffer,index,65);
            char[] text=(Program.Server+"\\PC_test\0").ToCharArray();Marshal.Copy(text,0,IntPtr.Add(buffer,8),text.Length);
            Assert((string)read.Invoke(null,new object[]{IntPtr.Add(buffer,8),buffer,128})==Program.Server+"\\PC_test","Native enum buffer strings are decoded from their bounded Unicode span");
            Assert(read.Invoke(null,new object[]{IntPtr.Zero,buffer,128})==null,"Null LocalName is the deviceless native representation");
            foreach(IntPtr invalid in new[]{IntPtr.Subtract(buffer,2),IntPtr.Add(buffer,128),IntPtr.Add(buffer,9),IntPtr.Add(buffer,124)})
            {
                bool rejected=false;try{read.Invoke(null,new object[]{invalid,buffer,128});}catch(TargetInvocationException error){rejected=error.InnerException is FormatException;}
                Assert(rejected,"Malformed or unterminated native enum string must fail without reading beyond its buffer");
            }
        }
        finally{Marshal.FreeHGlobal(buffer);}
    }
    static void HandoverRegression()
    {
        var steps=new List<string>();int reads=0;
        Assert(Program.Handover(()=>{steps.Add("start");return true;},()=>++reads==2,()=>steps.Add("commit"),()=>steps.Add("stop"),()=>steps.Add("wait"),3),"Confirmed child must take over");
        Assert(String.Join(",",steps)=="start,wait,commit","Startup must change only after ACK");
        steps.Clear();Assert(!Program.Handover(()=>false,()=>true,()=>steps.Add("commit"),()=>steps.Add("stop"),()=>{},3)&&steps.Count==0,"Failed start must preserve startup without stopping another process");
        Assert(!Program.Handover(()=>{throw new IOException("start failed");},()=>true,()=>steps.Add("commit"),()=>steps.Add("stop"),()=>{},3)&&steps.Count==0,"Throwing start must preserve the old helper");
        steps.Clear();Assert(!Program.Handover(()=>true,()=>false,()=>steps.Add("commit"),()=>steps.Add("stop"),()=>{},3)&&String.Join(",",steps)=="stop","No ACK must preserve startup and stop only the child");
        steps.Clear();Assert(!Program.Handover(()=>true,()=>true,()=>{throw new IOException("shortcut failed");},()=>steps.Add("stop"),()=>{},3)&&String.Join(",",steps)=="stop","Failed shortcut replacement must stop the waiting child");
        string nonce=new string('a',32),hash=new string('B',64),receipt=Program.HandoffReceipt(nonce,hash,42,1234);
        Assert(Program.HandoffMatches(receipt,nonce,hash,42,1234),"Current child identity must confirm");
        Assert(!Program.HandoffMatches(receipt,new string('c',32),hash,42,1234)&&!Program.HandoffMatches(receipt,nonce,new string('C',64),42,1234),"Wrong nonce or SHA must never confirm");
        Assert(!Program.HandoffMatches(receipt,nonce,hash,43,1234)&&!Program.HandoffMatches(receipt,nonce,hash,42,1235)&&!Program.HandoffMatches(null,nonce,hash,42,1234),"Stale receipt and reused PID must never confirm");
        steps.Clear();Assert(!Program.Handover(()=>true,()=>Program.HandoffMatches(receipt,new string('c',32),hash,42,1234),()=>steps.Add("commit"),()=>steps.Add("stop"),()=>{},3)&&String.Join(",",steps)=="stop","Wrong-nonce ACK must time out without switching startup");
    }
    static void ExplorerLabelRegression()
    {
        Config config=Program.ParseConfig(ConfigText());
        var net=new FakeNet();Outcome outcome=Apply(config,new List<Mapping>(),net);
        string target=@"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2\##192.168.234.4#PC_work";
        var labels=Program.ExplorerLabels(config,outcome);
        Assert(labels.Count==1&&labels.ContainsKey(target)&&labels[target]=="Работа и 📁","Successful own mapping must receive its Unicode Explorer label at the fixed-server UNC key");
        outcome=Apply(config,new List<Mapping>(),new FakeNet{AddError=1219});Assert(Program.ExplorerLabels(config,outcome).Count==0,"Failed mapping must not label any Explorer target");
        config.Shares.Add(new Share{Name="PC_second",Label="Second",PreferredLetter="Y"});outcome=Apply(config,new List<Mapping>(),new FakeNet{AddError=5,AddFailOn=2});
        Assert(!outcome.Success&&Program.ExplorerLabels(config,outcome).Count==1&&Program.ExplorerLabels(config,outcome).ContainsKey(target),"Partial failure must label only the mapping actually created");
        outcome=new Outcome();outcome.Owned.Add(new Mapping{Name="PC_work",Letter="Z"});Assert(Program.ExplorerLabels(config,outcome).Count==0,"Previous owned receipt alone does not authorize relabelling");
        outcome.Mappings.Add(new Mapping{Name="PC_foreign",Letter="X"});Assert(Program.ExplorerLabels(config,outcome).Count==0,"Unknown share cannot acquire a label");
        foreach(string unsafeName in new[]{@"PC_work\other",@"..\foreign",@"\\foreign\share","PC_x#PC_other"})
        {
            config.Shares.Add(new Share{Name=unsafeName,Label="Unsafe",PreferredLetter=""});outcome.Mappings.Clear();outcome.Mappings.Add(new Mapping{Name=unsafeName,Letter="Z"});
            Assert(Program.ExplorerLabels(config,outcome).Count==0,"Malformed share name must not escape the fixed label namespace");
        }
        outcome.Mappings.Clear();outcome.Mappings.Add(new Mapping{Name="PC_work",Letter="C"});Assert(Program.ExplorerLabels(config,outcome).Count==0,"System drive cannot be a label target");
        outcome.Mappings.Clear();outcome.Mappings.Add(new Mapping{Name="PC_work",Letter=""});Assert(Program.ExplorerLabels(config,outcome).ContainsKey(target),"Successful deviceless mapping labels only its exact UNC share");
    }
    static int Main(string[] args)
    {
        try
        {
            if(args.Length==3&&args[0]=="--live-receipt")
            {
                Config fixture=Program.ParseConfig(ConfigText());fixture.ClipboardEnabled=args[1]=="on";var network=new FakeNet();Outcome mapped=Apply(fixture,new List<Mapping>(),network);
                Console.WriteLine(Program.LiveReceipt(fixture,Program.VerifyMappings(fixture,mapped,network,()=>true),Program.MaxReceiptSerial,args[2]));return 0;
            }
            if(args.Length>0&&File.Exists(args[0]))
            {
                var read=typeof(Program).GetMethod("ReadBounded",BindingFlags.Static|BindingFlags.NonPublic);
                if(args.Length==2&&args[1]=="--reject")
                {
                    bool oversized=false;try{read.Invoke(null,new object[]{args[0]});}catch(TargetInvocationException error){oversized=error.InnerException is FormatException;}
                    Assert(oversized,"Guest reader must reject more than 64KiB before parsing");return 0;
                }
                if(args.Length==2&&args[1]=="--legacy")
                {
                    Assert(new FileInfo(args[0]).Length<=32768,"Bootstrap must fit the installed helper's limit");
                    Config bootstrap=Program.ParseConfig((string)read.Invoke(null,new object[]{args[0]}));
                    Assert(bootstrap.Shares.Count==0,"Bootstrap only authorizes the helper upgrade; the new helper must load the full config");return 0;
                }
                var choose=typeof(Program).GetMethod("ConfigPath",BindingFlags.Static|BindingFlags.NonPublic);
                string selected=(string)choose.Invoke(null,new object[]{Path.GetDirectoryName(args[0])});
                string text=(string)read.Invoke(null,new object[]{selected});Config carried=Program.ParseConfig(text);
                Assert(carried.Shares.Count==64&&carried.Shares.All(share=>share.Label.Length-share.Label.Count(Char.IsLowSurrogate)==128),"64 Unicode labels must survive the actual carrier and bounded guest reader");
                if(args.Length==2&&args[1]=="--clipboard")Assert(carried.ClipboardEnabled&&carried.ClipboardNonce==new string('f',32)&&carried.Nonce==new string('c',32),"The current helper must load the private clipboard nonce from HOSTFIL2 while legacy bootstrap omits it");
                Reject(new string(' ',65537));Reject(ConfigText().Replace("Работа и 📁",new string('Я',129)));
                return 0;
            }
            bool rejected=false;try{Program.ParseConfig("{\"schema\":1,\"schema\":1}");}catch(FormatException){rejected=true;}
            Assert(rejected,"Duplicate JSON keys must be rejected before any mapping");
            string valid=ConfigText();Config config=Program.ParseConfig(valid);Assert(config.Shares.Count==1&&config.Shares[0].Label.Contains("Работа"),"Unicode share label must round-trip");
            foreach(string bad in new[]{valid.Replace("\"schema\":1","\"schema\":\"1\""),valid.Replace("\"schema\":1","\"schema\":1.0"),valid.Replace("\"schema\":1","\"schema\":true"),valid.Replace("\"schema\":1","\"schema\":1,\"schema\":1"),valid.Replace("\"name\":\"PC_work\"","\"name\":\"PC_work\",\"name\":\"PC_other\""),valid.Replace("\"username\":","\"unknown\":0,\"username\":"),valid.Replace("PC_work","\\\\host\\\\share"),valid.Replace("PC_work","PC_x:stream"),valid.Replace("WCP_session","WCP_x\\\\name"),valid.Replace("\"Z\"","\"C:\""),valid.Replace("\"Z\"","null"),valid.Replace("\"Z\"","\"z\""),valid.Replace("0123456789abcdefghijklmnopqrstuvwxyz_-","short"),valid.Replace("0123456789abcdef0123456789abcdef","not-a-nonce"),valid.Replace("}]}","}, {\"name\":\"PC_WORK\",\"label\":\"duplicate\",\"preferred_letter\":\"Y\"}]}"),valid.Replace("\"schema\":1","\"schema\":01"),valid+"x","{\"schema\":1,}",new string(' ',65537)})Reject(bad);
            var many=Json.Deserialize<Dictionary<string,object>>(valid);many["shares"]=Enumerable.Range(0,65).Select(index=>new{name="PC_"+index,label="share",preferred_letter=""}).ToArray();Reject(Json.Serialize(many));
            Assert(Program.ChooseLetter("Z",new[]{"C","Z"})=="Y"&&Program.ChooseLetter("C",new string[0])=="Z","Occupied preferred letter and C must be preserved");
            string[] full=Enumerable.Range('A',26).Select(value=>((char)value).ToString()).ToArray();Assert(Program.ChooseLetter("",full)=="","Full letters must fall back to UNC");
            Assert(Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"}}),"Expected isolated NIC must become ready");
            foreach(NicState bad in new[]{new NicState{Mac="525400770003",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"}},new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.0.0"}},new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"},HasDns=true},new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"},HasGateway=true}})Assert(!Program.NicReady(bad),"Unisolated NIC accepted");
            Assert(!Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.16"},Masks=new[]{"255.255.255.0"}}),"Bootstrap must require the exact static guest address, not any address in the subnet");
            Assert(!Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15","192.168.234.16"},Masks=new[]{"255.255.255.0","255.255.255.0"}}),"Extra IPv4 addresses must fail exact bootstrap validation");
            ClipboardConfigRegression();
            BootstrapRegression();
            HandoverRegression();
            LinkRegression();
            DisplayReceiptRegression();
            var modes=new List<int[]>{new[]{1280,800,32,60},new[]{1864,1048,32,60},new[]{1864,1048,16,75},new[]{1864,1048,32,75},new[]{800,600,32,60}};
            Assert(Program.NativeMode(modes)==3,"The desktop must take the largest true-colour adapter mode (the window-sized one), highest refresh on ties");
            Assert(Program.NativeMode(new List<int[]>{new[]{1024,768,16,60}})==-1&&Program.NativeMode(new List<int[]>())==-1,"No true-colour mode leaves the desktop unchanged");
            ExplorerLabelRegression();
            MappingJournalRegression();
            LiveProbeRegression();
            LiveReceiptRegression();
            HeartbeatOwnerRegression();
            NativeConnectionBufferRegression();
            FakeNet net=new FakeNet();net.Used.Add("C");Outcome outcome=Apply(config,new List<Mapping>(),net);Assert(outcome.Success&&outcome.Mappings.Single().Letter=="Z"&&net.Adds==1,"Normal temporary mapping");
            var second=new Share{Name="PC_second",Label="second",PreferredLetter="Z"};config.Shares.Add(second);net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net);Assert(outcome.Mappings[0].Letter=="Z"&&outcome.Mappings[1].Letter=="Y","Preferred-letter conflicts must pick a different free letter");config.Shares.RemoveAt(1);
            net=new FakeNet();foreach(string letter in full)net.Used.Add(letter);outcome=Apply(config,new List<Mapping>(),net);Assert(outcome.Success&&outcome.NeedsShortcut&&outcome.Mappings.Single().Letter=="","Full-letter UNC fallback");
            string wire=Program.Receipt(config,outcome),owned=Program.OwnedReceipt(outcome.Owned);Assert(!wire.Contains(config.Password)&&!wire.Contains(config.Username)&&!wire.Contains("192.168")&&!owned.Contains(config.Password)&&!owned.Contains("session_nonce"),"Receipt leaked credentials or endpoint");Assert(Program.ParseOwned(owned).Count==1,"Owned mappings must reload without credentials");
            foreach(string bad in new[]{"[{\"name\":\"PC_old\",\"letter\":\"C\"}]","[{\"name\":\"PC_old\",\"letter\":\"Z\",\"password\":\"never\"}]","[{\"name\":\"PC_old\",\"letter\":\"Z\"},{\"name\":\"PC_other\",\"letter\":\"Z\"}]","[{\"name\":\"PC_old\",\"letter\":\"Z\",\"letter\":\"Y\"}]"}){bool invalid=false;try{Program.ParseOwned(bad);}catch(FormatException){invalid=true;}Assert(invalid,"Unsafe owned receipt accepted");}
            var receipt=Json.Deserialize<Dictionary<string,object>>(wire);Assert(receipt.Count==5&&receipt["success"] is bool&&(int)receipt["schema"]==1&&(string)receipt["session_nonce"]==config.Nonce,"Wire receipt schema must be exact");
            Config empty=Program.ParseConfig(valid.Replace("[{\"name\":\"PC_work\",\"label\":\"Работа и 📁\",\"preferred_letter\":\"Z\"}]","[]"));
            net=new FakeNet();net.Connections["Z"]=Program.Server+"\\PC_old";outcome=Apply(empty,new List<Mapping>{new Mapping{Name="PC_old",Letter="Z"}},net,()=>{throw new Exception("Cleanup must not wait for NIC");});Assert(outcome.Success&&net.Cancels==1&&outcome.Owned.Count==0,"Own exact-UNC cleanup");
            net=new FakeNet();net.Connections["Z"]="\\\\foreign\\share";outcome=Apply(empty,new List<Mapping>{new Mapping{Name="PC_old",Letter="Z"}},net);Assert(net.Cancels==0&&net.Connections.ContainsKey("Z"),"Foreign mapping must survive");
            net=new FakeNet{CancelError=2401};net.Connections["Z"]=Program.Server+"\\PC_old";outcome=Apply(empty,new List<Mapping>{new Mapping{Name="PC_old",Letter="Z"}},net);Assert(!outcome.Success&&outcome.Code=="OWN_MAPPING_BUSY"&&outcome.Owned.Count==1,"Open-file cleanup must remain tracked and fail without force");
            net=new FakeNet();outcome=Apply(empty,new List<Mapping>{new Mapping{Name="PC_old",Letter=""}},net);Assert(outcome.Success&&outcome.Owned.Count==0&&net.Cancels==0,"Missing deviceless mapping clears its stale ownership without cancellation");
            net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net,null,true);Assert(!outcome.Success&&outcome.Code=="ORDINARY_TOKEN_REQUIRED"&&net.Calls==0,"Elevated token must not call WinNet");
            net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net,null,false,false);Assert(!outcome.Success&&net.Calls==0,"Noninteractive mapping must be refused");int checks=0;net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net,()=>{checks++;return false;});Assert(!outcome.Success&&outcome.Code=="NIC_NOT_READY"&&checks<=61&&net.Calls==0,"NIC wait must be bounded and precede network actions");
            net=new FakeNet{AddError=1219};outcome=Apply(config,new List<Mapping>(),net);Assert(!outcome.Success&&outcome.Code=="CREDENTIAL_CONFLICT"&&net.Cancels==0,"Credential conflict must not remove unknown sessions");
            Assert(Program.ShortcutMatches(Program.Server+"\\","",Program.Server,"")&&!Program.ShortcutMatches("\\\\other-server","",Program.Server,""),"Own UNC server shortcut must be recognized without accepting a foreign target");
            Assert(Program.ShortcutMatches(@"C:\Users\test\HostFilesGuest.exe","--apply",@"C:\Users\test\HostFilesGuest.exe","--apply")&&!Program.ShortcutMatches(@"C:\foreign.exe","--apply",@"C:\Users\test\HostFilesGuest.exe","--apply")&&!Program.ShortcutMatches(@"C:\Users\test\HostFilesGuest.exe","--install",@"C:\Users\test\HostFilesGuest.exe","--apply"),"Startup ownership must verify exact target and arguments");
            bool hostRefused=false;try{WindowsClaude.GuestEnvironment.Require();}catch(InvalidOperationException){hostRefused=true;}
            Assert(hostRefused,"This harness must run on the physical host, never execute native guest code in a guest");
            Assert(Program.Main(new[]{"--apply"})!=0&&Program.Main(new[]{"--configure-nic"})!=0&&Program.Main(new[]{"--install"})!=0,"Real host firmware guard must refuse mapping, configuration and installation");
            Console.WriteLine("PASS: strict bounded config; exact NIC and configuration boundary; static setup before link request, per-adapter marker; UAC/post-child verification; immutable upgrade and failure retention; letters/C/UNC; durable pre-add intent and failed rollback recovery; receipt-owned deviceless cleanup/manual mapping preservation; bounded native enum strings; fresh read-only mapping probes/recovery; exact schema2/clipboard status/monotonic serial; resident mutex ownership; ordinary token; receipt redaction; real host guard. No native network/UAC/shortcut operations executed.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

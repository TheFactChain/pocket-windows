using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Web.Script.Serialization;
using WindowsClaude.GuestFiles;
using Microsoft.Win32;

internal static class GuestFilesRegression
{
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer();
    static string ConfigText(){return "{\"schema\":1,\"session_nonce\":\"0123456789abcdef0123456789abcdef\",\"username\":\"WCP_session\",\"password\":\"0123456789abcdefghijklmnopqrstuvwxyz_-\",\"shares\":[{\"name\":\"PC_work\",\"label\":\"Работа и 📁\",\"preferred_letter\":\"Z\"}]}";}
    static void Reject(string text){bool rejected=false;try{Program.ParseConfig(text);}catch(FormatException){rejected=true;}Assert(rejected,"Invalid config accepted: "+text.Substring(0,Math.Min(60,text.Length)));}
    sealed class FakeNet : INetwork
    {
        internal readonly Dictionary<string,string> Connections=new Dictionary<string,string>();
        internal readonly HashSet<string> Used=new HashSet<string>();
        internal int Calls,Adds,Cancels;internal int AddError,CancelError,AddFailOn;
        public IEnumerable<string> UsedLetters(){Calls++;return Used.Concat(Connections.Keys).ToArray();}
        public int GetConnection(string letter,out string remote){Calls++;return Connections.TryGetValue(letter,out remote)?0:2250;}
        public int Add(string remote,string letter,string username,string password,uint flags){Calls++;Adds++;Assert(flags==4,"Mapping must use CONNECT_TEMPORARY only");Assert(remote.StartsWith(Program.Server+"\\PC_",StringComparison.Ordinal),"Mapping endpoint escaped");if(AddError!=0&&(AddFailOn==0||Adds==AddFailOn))return AddError;if(letter.Length>0)Connections[letter]=remote;return 0;}
        public int Cancel(string letter,uint flags,bool force){Calls++;Cancels++;Assert(flags==0&&!force,"Cleanup must not force open files or modify a persistent profile");if(CancelError!=0)return CancelError;Connections.Remove(letter);return 0;}
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
    static int Main()
    {
        try
        {
            bool rejected=false;try{Program.ParseConfig("{\"schema\":1,\"schema\":1}");}catch(FormatException){rejected=true;}
            Assert(rejected,"Duplicate JSON keys must be rejected before any mapping");
            string valid=ConfigText();Config config=Program.ParseConfig(valid);Assert(config.Shares.Count==1&&config.Shares[0].Label.Contains("Работа"),"Unicode share label must round-trip");
            foreach(string bad in new[]{valid.Replace("\"schema\":1","\"schema\":\"1\""),valid.Replace("\"schema\":1","\"schema\":1.0"),valid.Replace("\"schema\":1","\"schema\":true"),valid.Replace("\"schema\":1","\"schema\":1,\"schema\":1"),valid.Replace("\"name\":\"PC_work\"","\"name\":\"PC_work\",\"name\":\"PC_other\""),valid.Replace("\"username\":","\"unknown\":0,\"username\":"),valid.Replace("PC_work","\\\\host\\\\share"),valid.Replace("PC_work","PC_x:stream"),valid.Replace("WCP_session","WCP_x\\\\name"),valid.Replace("\"Z\"","\"C:\""),valid.Replace("\"Z\"","null"),valid.Replace("\"Z\"","\"z\""),valid.Replace("0123456789abcdefghijklmnopqrstuvwxyz_-","short"),valid.Replace("0123456789abcdef0123456789abcdef","not-a-nonce"),valid.Replace("}]}","}, {\"name\":\"PC_WORK\",\"label\":\"duplicate\",\"preferred_letter\":\"Y\"}]}"),valid.Replace("\"schema\":1","\"schema\":01"),valid+"x","{\"schema\":1,}",new string(' ',32769)})Reject(bad);
            var many=Json.Deserialize<Dictionary<string,object>>(valid);many["shares"]=Enumerable.Range(0,65).Select(index=>new{name="PC_"+index,label="share",preferred_letter=""}).ToArray();Reject(Json.Serialize(many));
            Assert(Program.ChooseLetter("Z",new[]{"C","Z"})=="Y"&&Program.ChooseLetter("C",new string[0])=="Z","Occupied preferred letter and C must be preserved");
            string[] full=Enumerable.Range('A',26).Select(value=>((char)value).ToString()).ToArray();Assert(Program.ChooseLetter("",full)=="","Full letters must fall back to UNC");
            Assert(Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"}}),"Expected isolated NIC must become ready");
            foreach(NicState bad in new[]{new NicState{Mac="525400770003",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"}},new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.0.0"}},new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"},HasDns=true},new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15"},Masks=new[]{"255.255.255.0"},HasGateway=true}})Assert(!Program.NicReady(bad),"Unisolated NIC accepted");
            Assert(!Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.16"},Masks=new[]{"255.255.255.0"}}),"Bootstrap must require the exact static guest address, not any address in the subnet");
            Assert(!Program.NicReady(new NicState{Mac="525400770004",Up=true,Addresses=new[]{"192.168.234.15","192.168.234.16"},Masks=new[]{"255.255.255.0","255.255.255.0"}}),"Extra IPv4 addresses must fail exact bootstrap validation");
            BootstrapRegression();
            LinkRegression();
            DisplayReceiptRegression();
            var modes=new List<int[]>{new[]{1280,800,32,60},new[]{1864,1048,32,60},new[]{1864,1048,16,75},new[]{1864,1048,32,75},new[]{800,600,32,60}};
            Assert(Program.NativeMode(modes)==3,"The desktop must take the largest true-colour adapter mode (the window-sized one), highest refresh on ties");
            Assert(Program.NativeMode(new List<int[]>{new[]{1024,768,16,60}})==-1&&Program.NativeMode(new List<int[]>())==-1,"No true-colour mode leaves the desktop unchanged");
            ExplorerLabelRegression();
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
            net=new FakeNet();outcome=Apply(empty,new List<Mapping>{new Mapping{Name="PC_old",Letter=""}},net);Assert(!outcome.Success&&outcome.Code=="UNLETTERED_CONNECTION_REMAINS"&&net.Cancels==0,"Unprovable deviceless cleanup must fail safely");
            net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net,null,true);Assert(!outcome.Success&&outcome.Code=="ORDINARY_TOKEN_REQUIRED"&&net.Calls==0,"Elevated token must not call WinNet");
            net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net,null,false,false);Assert(!outcome.Success&&net.Calls==0,"Noninteractive mapping must be refused");int checks=0;net=new FakeNet();outcome=Apply(config,new List<Mapping>(),net,()=>{checks++;return false;});Assert(!outcome.Success&&outcome.Code=="NIC_NOT_READY"&&checks<=61&&net.Calls==0,"NIC wait must be bounded and precede network actions");
            net=new FakeNet{AddError=1219};outcome=Apply(config,new List<Mapping>(),net);Assert(!outcome.Success&&outcome.Code=="CREDENTIAL_CONFLICT"&&net.Cancels==0,"Credential conflict must not remove unknown sessions");
            Assert(!Program.IsQemu("Physical manufacturer")&&Program.IsQemu("QEMU"),"QEMU guard classifier");
            Assert(Program.ShortcutMatches(Program.Server+"\\","",Program.Server,"")&&!Program.ShortcutMatches("\\\\other-server","",Program.Server,""),"Own UNC server shortcut must be recognized without accepting a foreign target");
            Assert(Program.ShortcutMatches(@"C:\Users\test\HostFilesGuest.exe","--apply",@"C:\Users\test\HostFilesGuest.exe","--apply")&&!Program.ShortcutMatches(@"C:\foreign.exe","--apply",@"C:\Users\test\HostFilesGuest.exe","--apply")&&!Program.ShortcutMatches(@"C:\Users\test\HostFilesGuest.exe","--install",@"C:\Users\test\HostFilesGuest.exe","--apply"),"Startup ownership must verify exact target and arguments");
            using(var bios=Registry.LocalMachine.OpenSubKey(@"HARDWARE\DESCRIPTION\System\BIOS",false))
            {Assert(bios==null||!Program.IsQemu(bios.GetValue("SystemManufacturer") as string),"This harness must run on the non-QEMU host, never execute native guest code in a guest");Assert(Program.Main(new[]{"--apply"})!=0&&Program.Main(new[]{"--configure-nic"})!=0&&Program.Main(new[]{"--install"})!=0,"Real host BIOS guard must refuse mapping, configuration and installation");}
            Console.WriteLine("PASS: strict bounded config; exact NIC and configuration boundary; static setup before link request, per-adapter marker; UAC/post-child verification; immutable upgrade and failure retention; letters/C/UNC; own cleanup/no-force; ordinary token; receipt redaction; real host guard. No native network/UAC/shortcut operations executed.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

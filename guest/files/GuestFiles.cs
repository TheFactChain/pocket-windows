using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Ports;
using System.Linq;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WindowsClaude.GuestFiles
{
    public sealed class Config { public string Nonce,Username,Password,ClipboardNonce;public bool ClipboardEnabled;public int ClipboardPort=27844;public readonly List<Share> Shares=new List<Share>(); }
    public sealed class Share { public string Name,Label,PreferredLetter; }
    public sealed class Mapping { public string Name,Letter; }
    public sealed class NetworkConnection { public string Remote,Letter; }
    public sealed class NicState { public string Id,Mac;public bool Up,HasGateway,HasDns;public string[] Addresses=new string[0],Masks=new string[0],Gateways=new string[0],DnsServers=new string[0]; }
    public interface INicConfiguration { NicState[] Read();void Configure(string id); }
    public sealed class Outcome
    {
        public string Code="OK";public bool NeedsShortcut;
        public readonly List<Mapping> Mappings=new List<Mapping>(),Owned=new List<Mapping>();
        public bool Success {get{return Code=="OK";}}
    }
    public interface INetwork
    {
        IEnumerable<string> UsedLetters();
        int Enumerate(out List<NetworkConnection> connections);
        int GetConnection(string letter,out string remote);
        int Add(string remote,string letter,string username,string password,uint flags);
        int Cancel(string letter,uint flags,bool force);
    }
    public static class Program
    {
        public const string Server=@"\\192.168.234.4";
        static readonly JavaScriptSerializer Json=new JavaScriptSerializer{MaxJsonLength=65536,RecursionLimit=8};
        static bool Matches(string value,string pattern){return value!=null&&Regex.IsMatch(value,"\\A"+pattern+"\\z",RegexOptions.CultureInvariant);}
        static FormatException Invalid(){return new FormatException("CONFIG_INVALID");}
        sealed class Reader
        {
            readonly string text;int offset;
            internal Reader(string input){if(input==null||Encoding.UTF8.GetByteCount(input)>65536)throw Invalid();text=input;}
            void Space(){while(offset<text.Length&&" \r\n\t".IndexOf(text[offset])>=0)offset++;}
            char Peek(){Space();return offset<text.Length?text[offset]:'\0';}
            void Take(char expected){if(Peek()!=expected)throw Invalid();offset++;}
            string String()
            {
                Space();int start=offset;Take('"');bool ended=false;
                while(offset<text.Length)
                {
                    char current=text[offset++];if(current=='"'){ended=true;break;}if(current<32)throw Invalid();
                    if(current=='\\')
                    {
                        if(offset>=text.Length)throw Invalid();char escape=text[offset++];
                        if(escape=='u'){for(int index=0;index<4;index++)if(offset>=text.Length||!Uri.IsHexDigit(text[offset++]))throw Invalid();}
                        else if("\"\\/bfnrt".IndexOf(escape)<0)throw Invalid();
                    }
                }
                if(!ended)throw Invalid();
                string value;try{value=Json.Deserialize<string>(text.Substring(start,offset-start));}catch(ArgumentException){throw Invalid();}
                for(int index=0;index<value.Length;index++)if(Char.IsSurrogate(value[index])){if(!Char.IsHighSurrogate(value[index])||index+1>=value.Length||!Char.IsLowSurrogate(value[++index]))throw Invalid();}
                return value;
            }
            object Value(int depth)
            {
                if(depth>4)throw Invalid();char current=Peek();
                if(current=='"')return String();
                if(current=='{')
                {
                    Take('{');var values=new Dictionary<string,object>(StringComparer.Ordinal);if(Peek()=='}'){offset++;return values;}
                    while(true){string key=String();Take(':');if(values.ContainsKey(key))throw Invalid();values.Add(key,Value(depth+1));if(values.Count>8)throw Invalid();if(Peek()=='}'){offset++;return values;}Take(',');}
                }
                if(current=='[')
                {
                    Take('[');var values=new List<object>();if(Peek()==']'){offset++;return values;}
                    while(true){values.Add(Value(depth+1));if(values.Count>64)throw Invalid();if(Peek()==']'){offset++;return values;}Take(',');}
                }
                if(current>='0'&&current<='9')
                {
                    int start=offset;offset++;if(current!='0')while(offset<text.Length&&text[offset]>='0'&&text[offset]<='9')offset++;
                    int number;if(!Int32.TryParse(text.Substring(start,offset-start),NumberStyles.None,CultureInfo.InvariantCulture,out number))throw Invalid();return number;
                }
                if(current=='t'&&offset+4<=text.Length&&text.Substring(offset,4)=="true"){offset+=4;return true;}
                if(current=='f'&&offset+5<=text.Length&&text.Substring(offset,5)=="false"){offset+=5;return false;}
                throw Invalid();
            }
            internal object Read(){object value=Value(0);Space();if(offset!=text.Length)throw Invalid();return value;}
        }
        static Dictionary<string,object> Object(object value,params string[] keys)
        {
            var item=value as Dictionary<string,object>;if(item==null||item.Count!=keys.Length||keys.Any(key=>!item.ContainsKey(key)))throw Invalid();return item;
        }
        static string Text(Dictionary<string,object> item,string key){string value=item[key] as string;if(value==null)throw Invalid();return value;}
        static bool Name(string name){return Matches(name,"PC_[A-Za-z0-9_]{1,48}");}
        public static Config ParseConfig(string text)
        {
            var raw=new Reader(text).Read() as Dictionary<string,object>;
            var values=raw!=null&&raw.ContainsKey("clipboard")?Object(raw,"schema","session_nonce","username","password","shares","clipboard"):Object(raw,"schema","session_nonce","username","password","shares");
            if(!(values["schema"] is int)||(int)values["schema"]!=1)throw Invalid();
            var config=new Config{Nonce=Text(values,"session_nonce"),Username=Text(values,"username"),Password=Text(values,"password")};
            if(!Matches(config.Nonce,"[A-Fa-f0-9]{32}")||!Matches(config.Username,"WCP_[A-Za-z0-9_]{1,32}")||!Matches(config.Password,"[A-Za-z0-9_-]{32,128}"))throw Invalid();
            if(values.ContainsKey("clipboard"))
            {
                var clipboard=Object(values["clipboard"],"enabled","port","nonce");
                if(!(clipboard["enabled"] is bool)||!(clipboard["port"] is int)||(int)clipboard["port"]!=27844)throw Invalid();
                config.ClipboardNonce=Text(clipboard,"nonce");
                if(!Matches(config.ClipboardNonce,"[a-f0-9]{32}")||String.Equals(config.ClipboardNonce,config.Nonce,StringComparison.OrdinalIgnoreCase))throw Invalid();
                config.ClipboardEnabled=(bool)clipboard["enabled"];
            }
            var shares=values["shares"] as List<object>;if(shares==null)throw Invalid();var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach(object entry in shares)
            {
                var item=Object(entry,"name","label","preferred_letter");var share=new Share{Name=Text(item,"name"),Label=Text(item,"label"),PreferredLetter=Text(item,"preferred_letter")};
                if(!Name(share.Name)||!names.Add(share.Name)||share.Label.Length-share.Label.Count(letter=>Char.IsLowSurrogate(letter))>128||share.Label.Any(letter=>Char.IsControl(letter))||!Matches(share.PreferredLetter,"[A-Z]?"))throw Invalid();config.Shares.Add(share);
            }
            return config;
        }
        public static List<Mapping> ParseOwned(string text)
        {
            var values=new Reader(text).Read() as List<object>;if(values==null)throw Invalid();var result=new List<Mapping>();var names=new HashSet<string>(StringComparer.OrdinalIgnoreCase);var letters=new HashSet<string>();
            foreach(object entry in values)
            {
                var item=Object(entry,"name","letter");var mapping=new Mapping{Name=Text(item,"name"),Letter=Text(item,"letter")};
                if(!Name(mapping.Name)||!names.Add(mapping.Name)||!Matches(mapping.Letter,"[A-Z]?")||mapping.Letter=="C"||(mapping.Letter.Length>0&&!letters.Add(mapping.Letter)))throw Invalid();result.Add(mapping);
            }
            return result;
        }
        public static string ChooseLetter(string preferred,IEnumerable<string> used)
        {
            var occupied=new HashSet<string>(used,StringComparer.OrdinalIgnoreCase);occupied.Add("C");
            if(Matches(preferred,"[A-Z]")&&!occupied.Contains(preferred))return preferred;
            for(char letter='Z';letter>='A';letter--)if(!occupied.Contains(letter.ToString()))return letter.ToString();return "";
        }
        public static bool NicReady(NicState nic)
        {
            return nic!=null&&nic.Mac=="525400770004"&&nic.Up&&!nic.HasGateway&&!nic.HasDns&&nic.Addresses!=null&&nic.Masks!=null&&nic.Addresses.Length==1&&nic.Masks.Length==1&&nic.Addresses[0]=="192.168.234.15"&&nic.Masks[0]=="255.255.255.0";
        }
        // Windows lists these unreachable site-local resolvers on any IPv6 adapter without its own DNS.
        public static bool DefaultIpv6Dns(string address)
        {
            string value=(address??"").Split('%')[0];System.Net.IPAddress parsed;
            return System.Net.IPAddress.TryParse(value,out parsed)&&new[]{"fec0:0:0:ffff::1","fec0:0:0:ffff::2","fec0:0:0:ffff::3"}.Any(item=>System.Net.IPAddress.Parse(item).Equals(parsed));
        }
        public static string Describe(NicState nic)
        {
            if(nic==null)return "адаптер папок ПК не найден";
            return "связь "+(nic.Up?"есть":"нет")+"; IPv4 "+(nic.Addresses.Length==0?"нет":String.Join(", ",nic.Addresses))+"; шлюз "+(nic.Gateways.Length==0?"нет":String.Join(", ",nic.Gateways))+"; DNS "+(nic.DnsServers.Length==0?"нет":String.Join(", ",nic.DnsServers));
        }
        public static void ConfigureNic(INicConfiguration network,bool elevated)
        {
            if(!elevated)throw new InvalidOperationException("ADMIN_TOKEN_REQUIRED");
            var matches=network.Read().Where(item=>item.Mac=="525400770004").ToArray();Guid id;
            if(matches.Length!=1||!Guid.TryParse(matches[0].Id,out id))throw new InvalidOperationException("ISOLATED_NIC_NOT_UNIQUE");
            // While the host keeps it unplugged Windows still stores this persistent setup and applies it on connect.
            if(!NicReady(matches[0]))network.Configure(id.ToString("B"));
        }
        public static void EnsureNic(Func<bool> ready,Func<int> configure,Action link,Action wait,int patience=0)
        {
            for(int attempt=0;attempt<=patience;attempt++){if(ready())return;if(attempt<patience)wait();}
            if(configure()!=0)throw new InvalidOperationException("NIC_CONFIGURATION_FAILED");
            link();
            for(int attempt=0;attempt<=30;attempt++){if(ready())return;if(attempt<30)wait();}
            throw new InvalidOperationException("NIC_NOT_READY");
        }
        public static string[] NicCommands(int ipv4Index,int? ipv6Index)
        {
            if(ipv4Index<=0||(ipv6Index.HasValue&&ipv6Index.Value<=0))throw new InvalidOperationException("ISOLATED_NIC_NOT_UNIQUE");
            string index=ipv4Index.ToString(CultureInfo.InvariantCulture);
            var commands=new List<string>{"interface ipv4 set address name="+index+" source=static address=192.168.234.15 mask=255.255.255.0 gateway=none store=persistent","interface ipv4 set dnsservers name="+index+" source=static address=none register=none validate=no"};
            if(ipv6Index.HasValue)commands.Add("interface ipv6 set dnsservers name="+ipv6Index.Value.ToString(CultureInfo.InvariantCulture)+" source=static address=none register=none validate=no");
            return commands.ToArray();
        }
        public static Outcome ApplyMappings(Config config,List<Mapping> previous,INetwork network,Func<bool> ready,Action wait,bool elevated,bool interactive,Action<List<Mapping>> persist)
        {
            var result=new Outcome();result.Owned.AddRange(previous);
            if(elevated){result.Code="ORDINARY_TOKEN_REQUIRED";return result;}if(!interactive){result.Code="INTERACTIVE_USER_REQUIRED";return result;}
            if(config.Shares.Count>0)
            {
                bool available=false;for(int attempt=0;attempt<=60;attempt++){if(ready()){available=true;break;}if(attempt<60)wait();}
                if(!available){result.Code="NIC_NOT_READY";return result;}
            }
            List<NetworkConnection> current;
            if(!Connections(network,out current)){result.Code="CONNECTION_INSPECTION_FAILED";return result;}
            foreach(Mapping old in previous)
            {
                bool connected;
                if(old.Letter.Length==0)
                {
                    if(!Connections(network,out current)){result.Code="CONNECTION_INSPECTION_FAILED";return result;}
                    connected=current.Any(item=>MatchesConnection(item,old));
                    if(connected&&UnownedLetter(current,old,result.Owned)){result.Code="UNOWNED_CONNECTION_REMAINS";return result;}
                }
                else
                {
                    string remote;int status=network.GetConnection(old.Letter,out remote);
                    if(status!=0&&status!=2250){result.Code="CONNECTION_INSPECTION_FAILED";return result;}
                    connected=status==0&&String.Equals(remote,Remote(old),StringComparison.OrdinalIgnoreCase);
                }
                if(connected)
                {
                    int cancelled=network.Cancel(old.Letter.Length==0?Remote(old):old.Letter,0,false);
                    if(cancelled!=0&&cancelled!=2250){result.Code="OWN_MAPPING_BUSY";return result;}
                }
                if(!PersistOwned(result,result.Owned.Where(item=>!System.Object.ReferenceEquals(item,old)).ToList(),persist))return result;
            }
            if(previous.Count>0&&!Connections(network,out current)){result.Code="CONNECTION_INSPECTION_FAILED";return result;}
            if(current.Any(item=>ReservedRemote(item.Remote))){result.Code="UNOWNED_CONNECTION_REMAINS";return result;}
            if(config.Shares.Count==0)return result;
            var used=new HashSet<string>(network.UsedLetters(),StringComparer.OrdinalIgnoreCase);used.Add("C");
            foreach(Share share in config.Shares)
            {
                string letter=ChooseLetter(share.PreferredLetter,used);var mapped=new Mapping{Name=share.Name,Letter=letter};
                if(!PersistOwned(result,result.Owned.Concat(new[]{mapped}).ToList(),persist))return result;
                int status;
                try{status=network.Add(Remote(mapped),letter,config.Username,config.Password,4);}
                catch{RollbackUncertain(result,mapped,network,persist);return result;}
                if(status!=0)
                {
                    if(PersistOwned(result,result.Owned.Where(item=>!System.Object.ReferenceEquals(item,mapped)).ToList(),persist))result.Code=status==1219?"CREDENTIAL_CONFLICT":"MAPPING_FAILED";
                    return result;
                }
                result.Mappings.Add(mapped);if(letter.Length>0)used.Add(letter);else result.NeedsShortcut=true;
            }
            return result;
        }
        static string Remote(Mapping mapping){return Server+"\\"+mapping.Name;}
        static bool MatchesConnection(NetworkConnection connection,Mapping mapping){return String.Equals(connection.Letter,mapping.Letter,StringComparison.OrdinalIgnoreCase)&&String.Equals(connection.Remote,Remote(mapping),StringComparison.OrdinalIgnoreCase);}
        static bool ReservedRemote(string remote){string prefix=Server+"\\PC_";return remote.StartsWith(prefix,StringComparison.OrdinalIgnoreCase)&&Matches(remote.Substring(prefix.Length),"[A-Za-z0-9_]{1,48}");}
        static bool UnownedLetter(IEnumerable<NetworkConnection> current,Mapping mapping,IEnumerable<Mapping> owned){return current.Any(item=>item.Letter.Length>0&&String.Equals(item.Remote,Remote(mapping),StringComparison.OrdinalIgnoreCase)&&!owned.Any(record=>MatchesConnection(item,record)));}
        static bool Connections(INetwork network,out List<NetworkConnection> current)
        {
            current=null;
            try{return network.Enumerate(out current)==0&&current!=null&&current.Count<=1024&&current.All(item=>item!=null&&!String.IsNullOrEmpty(item.Remote)&&item.Letter!=null&&Matches(item.Letter,"[A-Z]?"));}
            catch{return false;}
        }
        static bool PersistOwned(Outcome result,List<Mapping> owned,Action<List<Mapping>> persist)
        {
            try{persist(owned);result.Owned.Clear();result.Owned.AddRange(owned);return true;}
            catch{result.Code="OWN_RECEIPT_WRITE_FAILED";return false;}
        }
        static void RollbackUncertain(Outcome result,Mapping mapping,INetwork network,Action<List<Mapping>> persist)
        {
            result.Code="MAPPING_OUTCOME_UNKNOWN";
            try
            {
                List<NetworkConnection> current;if(!Connections(network,out current))return;
                bool connected;
                if(mapping.Letter.Length==0)
                {
                    connected=current.Any(item=>MatchesConnection(item,mapping));
                    if(connected&&UnownedLetter(current,mapping,result.Owned)){result.Code="UNOWNED_CONNECTION_REMAINS";return;}
                }
                else
                {
                    string remote;int status=network.GetConnection(mapping.Letter,out remote);
                    if(status!=0&&status!=2250)return;
                    if(status==0&&!String.Equals(remote,Remote(mapping),StringComparison.OrdinalIgnoreCase))return;
                    connected=status==0;
                }
                if(connected)
                {
                    int cancelled=network.Cancel(mapping.Letter.Length==0?Remote(mapping):mapping.Letter,0,false);
                    if(cancelled!=0&&cancelled!=2250){result.Code="OWN_MAPPING_BUSY";return;}
                }
                if(PersistOwned(result,result.Owned.Where(item=>!System.Object.ReferenceEquals(item,mapping)).ToList(),persist))result.Code="MAPPING_FAILED";
            }
            catch{result.Code="MAPPING_OUTCOME_UNKNOWN";}
        }
        public static string OwnedReceipt(IEnumerable<Mapping> mappings){return Json.Serialize(mappings.Select(item=>new{name=item.Name,letter=item.Letter}).ToArray());}
        public static Dictionary<string,string> ExplorerLabels(Config config,Outcome outcome)
        {
            var labels=new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase);
            foreach(Mapping mapping in outcome.Mappings)
            {
                if(!Name(mapping.Name)||!Matches(mapping.Letter,"[A-Z]?")||mapping.Letter=="C")continue;
                Share share=config.Shares.SingleOrDefault(item=>item.Name==mapping.Name);
                if(share==null||String.IsNullOrWhiteSpace(share.Label)||share.Label.Length-share.Label.Count(letter=>Char.IsLowSurrogate(letter))>128||share.Label.Any(Char.IsControl))continue;
                labels[@"Software\Microsoft\Windows\CurrentVersion\Explorer\MountPoints2\##192.168.234.4#"+share.Name]=share.Label;
            }
            return labels;
        }
        static void SetExplorerLabels(Config config,Outcome outcome)
        {
            RequireGuest();
            try{foreach(var label in ExplorerLabels(config,outcome))using(var key=Registry.CurrentUser.CreateSubKey(label.Key))key.SetValue("_LabelFromReg",label.Value,RegistryValueKind.String);}
            catch{if(outcome.Success)outcome.Code="EXPLORER_LABEL_FAILED";}
        }
        public static string Receipt(Config config,Outcome outcome){return Json.Serialize(new{schema=1,session_nonce=config==null?null:config.Nonce,success=outcome.Success,code=outcome.Code,mappings=outcome.Mappings.Select(item=>new{name=item.Name,letter=item.Letter}).ToArray()});}
        public const long MaxReceiptSerial=9007199254740991L;
        public static Outcome VerifyMappings(Config config,Outcome applied,INetwork network,Func<bool> ready)
        {
            if(applied==null)throw new ArgumentNullException("applied");
            var result=new Outcome{Code=applied.Code,NeedsShortcut=applied.NeedsShortcut};
            result.Mappings.AddRange(applied.Mappings.Select(item=>new Mapping{Name=item.Name,Letter=item.Letter}));
            result.Owned.AddRange(applied.Owned.Select(item=>new Mapping{Name=item.Name,Letter=item.Letter}));
            if(config==null){result.Code="CONFIG_INVALID";return result;}
            List<NetworkConnection> current;
            if(!Connections(network,out current)){result.Code="CONNECTION_INSPECTION_FAILED";return result;}
            try{if(config.Shares.Count>0&&!ready()){result.Code="NIC_NOT_READY";return result;}}
            catch(Exception){result.Code="NIC_NOT_READY";return result;}
            // Rebuild only the current report. The original apply result and durable
            // ownership stay untouched, so a later probe can recover without remap.
            result.Mappings.Clear();
            var expected=new HashSet<string>(config.Shares.Select(item=>item.Name),StringComparer.OrdinalIgnoreCase);
            var owned=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if(expected.Count!=config.Shares.Count||result.Owned.Count!=expected.Count||result.Owned.Any(item=>!Name(item.Name)||!Matches(item.Letter,"[A-BD-Z]?")||!expected.Contains(item.Name)||!owned.Add(item.Name)))
            {result.Code="OWN_RECEIPT_MISMATCH";return result;}
            result.Code="OK";
            foreach(Share share in config.Shares)
            {
                Mapping mapping=result.Owned.Single(item=>String.Equals(item.Name,share.Name,StringComparison.OrdinalIgnoreCase));
                int matches=current.Count(item=>MatchesConnection(item,mapping));
                if(matches!=1)
                {
                    result.Code=mapping.Letter.Length>0&&current.Any(item=>String.Equals(item.Letter,mapping.Letter,StringComparison.OrdinalIgnoreCase))?"MAPPING_CHANGED":"MAPPING_MISSING";
                    return result;
                }
                if(mapping.Letter.Length>0&&current.Count(item=>String.Equals(item.Letter,mapping.Letter,StringComparison.OrdinalIgnoreCase))!=1)
                {result.Code="MAPPING_CHANGED";return result;}
                result.Mappings.Add(new Mapping{Name=share.Name,Letter=mapping.Letter});
            }
            return result;
        }
        public static string LiveReceipt(Config config,Outcome outcome,long serial,string clipboardCode=null)
        {
            if(config==null||!Matches(config.Nonce,"[a-f0-9]{32}")||outcome==null)throw new ArgumentException("Invalid live receipt");
            if(serial<1||serial>MaxReceiptSerial)throw new ArgumentOutOfRangeException("serial");
            string status="off",code="OFF";
            if(config.ClipboardEnabled)
            {
                code=Matches(clipboardCode,"[A-Z_]{1,64}")?clipboardCode:"UNCONFIRMED";
                switch(code)
                {
                    case "READY":case "FILES_READY":case "CONNECTED":status="ready";break;
                    case "WAITING":case "RECONNECTING":case "NIC_UNAVAILABLE":status="connecting";break;
                    case "START_FAILED":case "PROTOCOL_FAILED":case "FILES_FAILED":case "CLIPBOARD_READ_FAILED":case "CLIPBOARD_LISTENER_FAILED":case "CLEANUP_FAILED":status="failed";break;
                    default:status="unconfirmed";break;
                }
            }
            string fileCode=Matches(outcome.Code,"[A-Z_]{1,64}")?outcome.Code:"APPLY_FAILED";
            return Json.Serialize(new{schema=2,session_nonce=config.Nonce,success=fileCode=="OK",code=fileCode,mappings=outcome.Mappings.Select(item=>new{name=item.Name,letter=item.Letter}).ToArray(),serial=serial,clipboard=new{status=status,code=code}});
        }
        public static long NextReceiptSerial(long previous,long clock)
        {
            if(previous<0||previous>=MaxReceiptSerial||clock<0||clock>MaxReceiptSerial)throw new ArgumentOutOfRangeException("serial");
            return Math.Max(previous+1,clock);
        }
        public static long HeartbeatTick(Config config,Outcome applied,INetwork network,Func<bool> ready,long previous,Func<long> clock,Func<string> clipboardCode,Action<string> send)
        {
            long serial=NextReceiptSerial(previous,clock());
            Outcome verified=VerifyMappings(config,applied,network,ready);string status=null;
            try{status=clipboardCode();}catch(Exception){}
            try{send(LiveReceipt(config,verified,serial,status));}catch(Exception){}
            return serial;
        }
        public static int WithApplyMutex(Mutex mutex,Func<int> work)
        {
            bool held;try{held=mutex.WaitOne(0);}catch(AbandonedMutexException){held=true;}
            if(!held)return 2;
            try{return work();}finally{mutex.ReleaseMutex();}
        }
        static void RequireGuest(){GuestEnvironment.Require();}
        static void NoReparse(string path){for(string item=Path.GetFullPath(path);!String.IsNullOrEmpty(item);item=Path.GetDirectoryName(item))if((File.Exists(item)||Directory.Exists(item))&&(File.GetAttributes(item)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("REPARSE_PATH_REFUSED");}
        static string ReadBounded(string path)
        {
            NoReparse(path);using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read)){if(stream.Length>65536)throw Invalid();using(var reader=new StreamReader(stream,new UTF8Encoding(false,true),true))return reader.ReadToEnd();}
        }
        static string mediumRoot;
        static string ConfigPath(string directory)
        {
            string current=Path.Combine(directory,"HOSTFIL2.JSN");
            return File.Exists(current)?current:Path.Combine(directory,"HOSTFILS.JSN");
        }
        static string FindConfig()
        {
            string found=null;
            foreach(var drive in DriveInfo.GetDrives())
            {
                if(!drive.IsReady||drive.VolumeLabel!="WCPFILES")continue;if(found!=null)throw new InvalidOperationException("AMBIGUOUS_MEDIUM");
                uint serial,length,flags;if(!GetVolumeInformation(drive.RootDirectory.FullName,null,0,out serial,out length,out flags,null,0))throw new InvalidOperationException("MEDIUM_INSPECTION_FAILED");
                if(!drive.DriveFormat.StartsWith("FAT",StringComparison.OrdinalIgnoreCase)||(flags&0x80000)==0)throw new InvalidOperationException("MEDIUM_NOT_READONLY_FAT");found=ConfigPath(drive.RootDirectory.FullName);mediumRoot=drive.RootDirectory.FullName;
            }
            if(found==null)throw new InvalidOperationException("MEDIUM_NOT_FOUND");return ReadBounded(found);
        }
        [StructLayout(LayoutKind.Sequential)]struct Luid {public uint Low;public int High;}
        [StructLayout(LayoutKind.Sequential)]struct TokenStatistics {public Luid TokenId,AuthenticationId;public long Expiration;public int Type,Level;public uint Charged,Available,Groups,Privileges;public Luid Modified;}
        static bool Elevated()
        {
            uint elevation;int length;if(!GetTokenInformation(WindowsIdentity.GetCurrent().Token,20,out elevation,4,out length))throw new InvalidOperationException("TOKEN_INSPECTION_FAILED");return elevation!=0;
        }
        static string DirectoryPath(){return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),@"WindowsClaude\HostFiles");}
        static string StatePath()
        {
            TokenStatistics stats;int length;if(!GetTokenStatistics(WindowsIdentity.GetCurrent().Token,10,out stats,Marshal.SizeOf(typeof(TokenStatistics)),out length))throw new InvalidOperationException("TOKEN_INSPECTION_FAILED");
            return Path.Combine(DirectoryPath(),"owned-"+unchecked((uint)stats.AuthenticationId.High).ToString("x8")+stats.AuthenticationId.Low.ToString("x8")+".json");
        }
        static void SaveOwned(string path,List<Mapping> owned)
        {
            NoReparse(path);Directory.CreateDirectory(Path.GetDirectoryName(path));NoReparse(path);string temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
            try{using(var stream=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None))using(var writer=new StreamWriter(stream,new UTF8Encoding(false)))writer.Write(OwnedReceipt(owned));NoReparse(path);if(File.Exists(path))File.Replace(temporary,path,null);else File.Move(temporary,path);}
            finally{if(File.Exists(temporary))File.Delete(temporary);}
        }
        sealed class WinNet : INetwork
        {
            public int Enumerate(out List<NetworkConnection> connections)
            {
                connections=new List<NetworkConnection>();IntPtr handle;int status=WNetOpenEnum(1,1,0,IntPtr.Zero,out handle);if(status!=0)return status;
                try
                {
                    int capacity=16384,entrySize=Marshal.SizeOf(typeof(EnumeratedResource));
                    for(int round=0;round<256;round++)
                    {
                        IntPtr buffer=Marshal.AllocHGlobal(capacity);
                        try
                        {
                            uint count=UInt32.MaxValue;int size=capacity;status=WNetEnumResource(handle,ref count,buffer,ref size);
                            if(status==259)return 0;
                            if(status==234){if(size<=capacity||size>1048576)return 13;capacity=size;continue;}
                            if(status!=0)return status;
                            if(count==0||count>1024||count>(uint)(capacity/entrySize)||connections.Count+count>1024)return 13;
                            for(int index=0;index<(int)count;index++)
                            {
                                var item=(EnumeratedResource)Marshal.PtrToStructure(IntPtr.Add(buffer,index*entrySize),typeof(EnumeratedResource));
                                string local=ResourceString(item.Local,buffer,capacity),remote=ResourceString(item.Remote,buffer,capacity);
                                if(String.IsNullOrEmpty(remote)||(!String.IsNullOrEmpty(local)&&!Matches(local,"[A-Za-z]:")))return 13;
                                connections.Add(new NetworkConnection{Letter=String.IsNullOrEmpty(local)?"":local.Substring(0,1).ToUpperInvariant(),Remote=remote});
                            }
                        }
                        catch(FormatException){return 13;}
                        finally{Marshal.FreeHGlobal(buffer);}
                    }
                    return 13;
                }
                finally{WNetCloseEnum(handle);}
            }
            static string ResourceString(IntPtr pointer,IntPtr buffer,int capacity)
            {
                if(pointer==IntPtr.Zero)return null;
                long offset=pointer.ToInt64()-buffer.ToInt64();if(offset<0||offset>=capacity||(offset&1)!=0)throw Invalid();
                int length=0,maximum=Math.Min((capacity-(int)offset)/2,32768);
                while(length<maximum&&Marshal.ReadInt16(pointer,length*2)!=0)length++;
                if(length==maximum)throw Invalid();return Marshal.PtrToStringUni(pointer,length);
            }
            public IEnumerable<string> UsedLetters()
            {
                var result=new HashSet<string>(Environment.GetLogicalDrives().Select(path=>path.Substring(0,1)),StringComparer.OrdinalIgnoreCase);
                for(char letter='A';letter<='Z';letter++){string remote;if(GetConnection(letter.ToString(),out remote)!=2250)result.Add(letter.ToString());}return result;
            }
            public int GetConnection(string letter,out string remote){var value=new StringBuilder(1024);int size=value.Capacity;int result=WNetGetConnection(letter+":",value,ref size);remote=result==0?value.ToString():null;return result;}
            public int Add(string remote,string letter,string username,string password,uint flags){var resource=new NetResource{Type=1,Local=letter.Length==0?null:letter+":",Remote=remote};return WNetAddConnection2(ref resource,password,username,flags);}
            public int Cancel(string target,uint flags,bool force){return WNetCancelConnection2(target.Length==1?target+":":target,flags,force);}
        }
        sealed class WindowsNic : INicConfiguration
        {
            public NicState[] Read()
            {
                return NetworkInterface.GetAllNetworkInterfaces().Select(adapter=>
                {
                    var ip=adapter.GetIPProperties();var ipv4=ip.UnicastAddresses.Where(item=>item.Address.AddressFamily==AddressFamily.InterNetwork).ToArray();
                    string[] gateways=ip.GatewayAddresses.Select(item=>item.Address.ToString()).ToArray(),dns=ip.DnsAddresses.Select(item=>item.ToString()).ToArray();
                    return new NicState{Id=adapter.Id,Mac=adapter.GetPhysicalAddress().ToString(),Up=adapter.OperationalStatus==OperationalStatus.Up,HasGateway=gateways.Length!=0,HasDns=dns.Any(item=>!DefaultIpv6Dns(item)),Addresses=ipv4.Select(item=>item.Address.ToString()).ToArray(),Masks=ipv4.Select(item=>item.IPv4Mask.ToString()).ToArray(),Gateways=gateways,DnsServers=dns};
                }).ToArray();
            }
            public void Configure(string id)
            {
                RequireGuest();
                var adapters=NetworkInterface.GetAllNetworkInterfaces().Where(item=>item.GetPhysicalAddress().ToString()=="525400770004").ToArray();
                if(adapters.Length!=1||!String.Equals(adapters[0].Id,id,StringComparison.OrdinalIgnoreCase))throw new InvalidOperationException("ISOLATED_NIC_NOT_UNIQUE");
                var ip=adapters[0].GetIPProperties();int? ipv6=adapters[0].Supports(NetworkInterfaceComponent.IPv6)?(int?)ip.GetIPv6Properties().Index:null;
                // Fixed commands affect only the validated isolated adapter; no shell or caller-supplied arguments.
                foreach(string command in NicCommands(ip.GetIPv4Properties().Index,ipv6))Netsh(command);
            }
        }
        static void Netsh(string arguments)
        {
            using(var process=Process.Start(new ProcessStartInfo(Path.Combine(Environment.SystemDirectory,"netsh.exe"),arguments){UseShellExecute=false,CreateNoWindow=true}))
            {if(process==null)throw new InvalidOperationException("NIC_CONFIGURATION_FAILED");if(!process.WaitForExit(30000)){process.Kill();process.WaitForExit();throw new InvalidOperationException("NIC_CONFIGURATION_FAILED");}if(process.ExitCode!=0)throw new InvalidOperationException("NIC_CONFIGURATION_FAILED");}
        }
        static string nicDetail;
        static bool Ready()
        {
            var matches=new WindowsNic().Read().Where(item=>item.Mac=="525400770004").ToArray();
            nicDetail=matches.Length==1?Describe(matches[0]):matches.Length==0?Describe(null):"найдено несколько адаптеров папок ПК";
            return matches.Length==1&&NicReady(matches[0]);
        }
        // Remembered per adapter after a verified static connection: a new or reset adapter stays unplugged
        // (no DHCP route) until the helper configures it again with administrator consent.
        public static string NicMarker(string directory,string id)
        {
            Guid guid;if(!Guid.TryParse(id,out guid))throw new InvalidOperationException("ISOLATED_NIC_NOT_UNIQUE");
            return Path.Combine(directory,"nic-"+guid.ToString("N")+".ok");
        }
        static string IsolatedNicId()
        {
            var matches=new WindowsNic().Read().Where(item=>item.Mac=="525400770004").ToArray();
            if(matches.Length!=1)throw new InvalidOperationException("ISOLATED_NIC_NOT_UNIQUE");return matches[0].Id;
        }
        static bool NicRemembered(){string path=NicMarker(DirectoryPath(),IsolatedNicId());NoReparse(path);return File.Exists(path)||NicStored();}
        // The image may ship with this adapter already set up: Windows keeps the static address even while the link is down.
        public static bool StoredNicMatches(object dhcp,string[] address,string[] mask,string[] gateway,string dns)
        {
            return dhcp!=null&&Convert.ToInt32(dhcp)==0&&address!=null&&address.Length==1&&address[0]=="192.168.234.15"&&mask!=null&&mask.Length==1&&mask[0]=="255.255.255.0"
                &&(gateway==null||gateway.All(item=>String.IsNullOrEmpty(item)))&&String.IsNullOrEmpty(dns);
        }
        static bool NicStored()
        {
            try
            {
                using(var key=Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces\"+IsolatedNicId(),false))
                    return key!=null&&StoredNicMatches(key.GetValue("EnableDHCP"),key.GetValue("IPAddress") as string[],key.GetValue("SubnetMask") as string[],key.GetValue("DefaultGateway") as string[],key.GetValue("NameServer") as string);
            }
            catch(Exception){return false;}
        }
        static void RememberNic()
        {
            string path=NicMarker(DirectoryPath(),IsolatedNicId());NoReparse(path);Directory.CreateDirectory(Path.GetDirectoryName(path));NoReparse(path);
            File.WriteAllText(path,"192.168.234.15/24 static, no gateway or DNS");
        }
        static void RequestLink(Config config)
        {
            try{SendReceipt(LinkRequest(config));}catch(Exception){throw new InvalidOperationException("FILE_CHANNEL_FAILED");}
        }
        public static string LinkRequest(Config config){return Json.Serialize(new{schema=1,session_nonce=config.Nonce,success=false,code="NIC_STATIC",mappings=new object[0]});}
        static void SendReceipt(string line)
        {
            using(var serial=new SerialPort("COM2",115200){WriteTimeout=3000,Encoding=new UTF8Encoding(false),NewLine="\n"}){serial.Open();serial.WriteLine(line);}
        }
        static int ElevateNic()
        {
            RequireGuest();string source=Assembly.GetExecutingAssembly().Location;NoReparse(source);
            try{using(var child=Process.Start(new ProcessStartInfo(source,"--configure-nic"){UseShellExecute=true,Verb="runas"})){if(child==null)return 1;child.WaitForExit();return child.ExitCode;}}
            catch(System.ComponentModel.Win32Exception){throw new InvalidOperationException("NIC_ADMIN_CANCELLED");}
        }
        static string Hash(string path){using(var algorithm=SHA256.Create())using(var stream=File.OpenRead(path))return BitConverter.ToString(algorithm.ComputeHash(stream));}
        public static bool ShortcutMatches(string actualTarget,string actualArguments,string target,string arguments)
        {
            if(actualArguments!=arguments)return false;
            if(target==Server)return String.Equals((actualTarget??"").TrimEnd('\\'),Server,StringComparison.OrdinalIgnoreCase);
            try{return String.Equals(Path.GetFullPath(actualTarget),Path.GetFullPath(target),StringComparison.OrdinalIgnoreCase);}catch(ArgumentException){return false;}
        }
        static bool OwnShortcut(string path,string target,string arguments)
        {
            NoReparse(path);dynamic shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));dynamic link=shell.CreateShortcut(path);
            try{return ShortcutMatches((string)link.TargetPath,(string)link.Arguments,target,arguments);}
            finally{Marshal.FinalReleaseComObject(link);Marshal.FinalReleaseComObject(shell);}
        }
        static void Shortcut(string path,string target,string arguments)
        {
            NoReparse(path);if(File.Exists(path)){if(!OwnShortcut(path,target,arguments))throw new InvalidOperationException("SHORTCUT_NAME_CONFLICT");return;}
            string temporary=Path.Combine(Path.GetDirectoryName(path),Guid.NewGuid().ToString("N")+".lnk");dynamic shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));dynamic link=shell.CreateShortcut(temporary);
            try{link.TargetPath=target;link.Arguments=arguments;link.Description="Pocket Windows: диски и папки ПК";link.Save();File.Move(temporary,path);}
            finally{Marshal.FinalReleaseComObject(link);Marshal.FinalReleaseComObject(shell);if(File.Exists(temporary))File.Delete(temporary);}
        }
        static void Install()
        {
            string directory=DirectoryPath(),source=Assembly.GetExecutingAssembly().Location;
            string startup=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Startup),"WindowsClaude Host Files.lnk");
            StageHelper(source,directory,destination=>StartupShortcut(startup,destination,directory));
        }
        public static bool StartupTargetMatches(string target,string arguments,string directory)
        {
            if(arguments!="--apply"||String.IsNullOrEmpty(target))return false;
            try{return String.Equals(Path.GetDirectoryName(Path.GetFullPath(target)),Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar),StringComparison.OrdinalIgnoreCase)&&Matches(Path.GetFileName(target),"HostFilesGuest(?:-[A-F0-9]{64})?\\.exe");}
            catch(ArgumentException){return false;}
        }
        public static void StageHelper(string source,string directory,Action<string> commitStartup)
        {
            NoReparse(source);NoReparse(directory);string hash=Hash(source).Replace("-","");
            string destination=Path.Combine(directory,"HostFilesGuest-"+hash+".exe");NoReparse(destination);
            Directory.CreateDirectory(directory);NoReparse(directory);
            if(File.Exists(destination)){if(Hash(destination).Replace("-","")!=hash)throw new InvalidOperationException("EXISTING_HELPER_CONFLICT");}
            else
            {
                string temporary=Path.Combine(directory,Guid.NewGuid().ToString("N")+".tmp");
                try{File.Copy(source,temporary,false);if(Hash(temporary).Replace("-","")!=hash)throw new InvalidOperationException("HELPER_COPY_FAILED");File.Move(temporary,destination);}
                finally{if(File.Exists(temporary))File.Delete(temporary);}
            }
            // Keep prior versions: the previous startup target and a running executable must survive failed upgrades.
            commitStartup(destination);
        }
        static void StartupShortcut(string path,string target,string directory)
        {
            NoReparse(path);bool exists=File.Exists(path);dynamic shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            string temporary=Path.Combine(Path.GetDirectoryName(path),Guid.NewGuid().ToString("N")+".lnk");dynamic link=null;
            try
            {
                if(exists)
                {
                    link=shell.CreateShortcut(path);
                    if(!StartupTargetMatches((string)link.TargetPath,(string)link.Arguments,directory))throw new InvalidOperationException("SHORTCUT_NAME_CONFLICT");
                    NoReparse((string)link.TargetPath);Marshal.FinalReleaseComObject(link);link=null;
                }
                link=shell.CreateShortcut(temporary);link.TargetPath=target;link.Arguments="--apply";link.Description="Pocket Windows: диски и папки ПК";link.Save();
                NoReparse(path);if(exists)File.Replace(temporary,path,null);else File.Move(temporary,path);
            }
            finally{if(link!=null)Marshal.FinalReleaseComObject(link);Marshal.FinalReleaseComObject(shell);if(File.Exists(temporary))File.Delete(temporary);}
        }
        static string Explanation(string code)
        {
            switch(code)
            {
                case "OK":return "Диски и папки ПК подключены. Они появятся в Проводнике. Подключение запускается автоматически при входе.";
                case "ORDINARY_TOKEN_REQUIRED":return "Помощник запущен от имени администратора, и диски не появятся в Проводнике. Выйди из учётной записи Windows и войди снова.";
                case "OWN_MAPPING_BUSY":return "Закрой файлы и окна папок ПК в Windows, затем повтори подключение. Открытые файлы принудительно не отключены.";
                case "UNOWNED_CONNECTION_REMAINS":return "Найдено подключение к папкам ПК без квитанции помощника. Отключи его вручную и повтори подключение. Оно не было изменено.";
                case "MAPPING_OUTCOME_UNKNOWN":return "Не удалось проверить результат подключения. Квитанция сохранена для следующей попытки. Чужие подключения не отключены.";
                case "OWN_RECEIPT_WRITE_FAILED":return "Не удалось сохранить квитанцию подключения. Проверь свободное место и доступ к профилю Windows, затем повтори попытку.";
                case "CONNECTION_INSPECTION_FAILED":return "Windows не предоставила полный список подключений. Повтори попытку; непроверенные подключения не изменены.";
                case "CREDENTIAL_CONFLICT":return "Windows уже подключена к ПК с другими учётными данными. Выйди из учётной записи Windows и войди снова. Чужие подключения не отключены.";
                case "NIC_NOT_READY":return "Изолированная сеть папок ПК не готова. Проверь подключение в панели и перезапусти Windows.";
                case "NIC_NOT_CONFIGURED":return "Отдельная сеть папок ПК ещё не настроена. Перезапусти Windows и подтверди запрос разрешения.";
                case "FILE_CHANNEL_FAILED":return "Не удалось сообщить панели о готовности сети папок ПК. Перезапусти Windows из панели.";
                case "NIC_ADMIN_CANCELLED":return "Для настройки отдельной сети папок ПК нужно разрешение администратора. Перезапусти Windows и подтверди запрос.";
                case "NIC_CONFIGURATION_FAILED":return "Не удалось настроить отдельный сетевой адаптер папок ПК. Перезапусти Windows.";
                case "EXPLORER_LABEL_FAILED":return "Папки ПК подключены, но Проводник не принял их понятные названия. Перезапусти Windows.";
                case "ISOLATED_NIC_NOT_UNIQUE":return "Не найден единственный отдельный сетевой адаптер папок ПК. Проверь конфигурацию виртуальной машины.";
                default:return "Подключение папок ПК не завершено. Код: "+code+". Передай этот код для диагностики.";
            }
        }
        static string FailureCode(Exception error)
        {return error is InvalidOperationException&&Matches(error.Message,"[A-Z_]{1,64}")?error.Message:error is FormatException?"CONFIG_INVALID":"APPLY_FAILED";}
        static bool InitialReceipt(Config config,Outcome outcome)
        {try{SendReceipt(Receipt(config,outcome));return true;}catch{return false;}}
        static void InitialMessage(Outcome outcome,bool installed)
        {
            string detail=outcome.Code=="NIC_NOT_READY"&&nicDetail!=null?Environment.NewLine+Environment.NewLine+"Состояние сети папок ПК: "+nicDetail+".":"";
            if(installed||!outcome.Success)MessageBox.Show(Explanation(outcome.Code)+detail,"Диски и папки ПК",MessageBoxButtons.OK,outcome.Success?MessageBoxIcon.Information:MessageBoxIcon.Warning);
        }
        static void Resident(Config config,Outcome applied,Thread clipboard)
        {
            var network=new WinNet();long serial=0;bool sending=false;
            Action tick=()=>
            {
                if(sending)return;sending=true;
                try{serial=HeartbeatTick(config,applied,network,Ready,serial,()=>DateTime.UtcNow.Ticks/TimeSpan.TicksPerMillisecond,
                    ()=>!config.ClipboardEnabled?null:clipboard==null||!clipboard.IsAlive?"START_FAILED":GuestClipboard.StatusCode,SendReceipt);}
                catch(Exception){} // A failed read/report expires on the host; it never triggers a remap.
                finally{sending=false;}
            };
            using(var timer=new System.Windows.Forms.Timer{Interval=3000})
            {
                timer.Tick+=delegate{tick();};tick();timer.Start();
                // This STA pump keeps one serial writer alive even with clipboard
                // disabled; its timer also runs inside the initial modal error box.
                InitialMessage(applied,false);Application.Run();
            }
        }
        [STAThread]
        public static int Main(string[] args)
        {
            try{RequireGuest();}catch{return 1;}
            if(args.Length==1&&args[0]=="--configure-nic")
            {
                try{ParseConfig(FindConfig());ConfigureNic(new WindowsNic(),Elevated());return 0;}catch{return 2;}
            }
            bool handoff=args.Length==3&&args[0]=="--apply"&&args[1]=="--handoff"&&Matches(args[2],"[a-f0-9]{32}");
            if(!handoff&&(args.Length!=1||(args[0]!="--install"&&args[0]!="--apply")))return 64;
            Config config=null;Outcome outcome=new Outcome();bool installed=false,acceptedHandoff=false;Thread clipboard=null;
            try
            {
                config=ParseConfig(FindConfig());
                if(Elevated())throw new InvalidOperationException("ORDINARY_TOKEN_REQUIRED");
                if(!Environment.UserInteractive||Process.GetCurrentProcess().SessionId==0)throw new InvalidOperationException("INTERACTIVE_USER_REQUIRED");
                // The panel ships each new helper on its medium: switch to it at logon, nothing to click.
                if(!handoff&&args[0]=="--apply"&&UpdateFromMedium())return 0;
                using(var mutex=new Mutex(false,@"Local\WindowsClaude.HostFiles."+WindowsIdentity.GetCurrent().User.Value))
                {
                    return WithApplyMutex(mutex,()=>
                    {
                        try
                        {
                            // No user-visible changes until the old helper committed this exact process.
                            if(handoff){if(!ConfirmHandoff(args[2]))return 2;acceptedHandoff=true;}
                            string displaySettings=ReadDisplaySettings();FitDisplay(RequestedSize(displaySettings));ReportDisplay(displaySettings);
                            if(config.Shares.Count>0||config.ClipboardEnabled)
                            {
                                bool remembered=NicRemembered();
                                if(remembered)RequestLink(config);
                                EnsureNic(Ready,ElevateNic,()=>RequestLink(config),()=>Thread.Sleep(1000),remembered?20:0);
                            }
                            if(args[0]=="--apply"&&config.ClipboardEnabled)clipboard=GuestClipboard.StartBackground(config.ClipboardNonce,config.ClipboardPort,Ready);
                            if(args[0]=="--install"){Install();installed=true;}
                            string state=StatePath();List<Mapping> previous;
                            try{previous=File.Exists(state)?ParseOwned(ReadBounded(state)):new List<Mapping>();}catch(FormatException){throw new InvalidOperationException("OWN_RECEIPT_INVALID");}
                            outcome=ApplyMappings(config,previous,new WinNet(),Ready,()=>Thread.Sleep(1000),false,true,owned=>SaveOwned(state,owned));
                            if(outcome.Success&&config.Shares.Count>0)RememberNic();
                            SetExplorerLabels(config,outcome);
                            if(outcome.NeedsShortcut)Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"Диски и папки ПК.lnk"),Server,"");
                        }
                        catch(Exception error){outcome.Code=FailureCode(error);}
                        if(handoff&&!acceptedHandoff)return 2;
                        bool sent=InitialReceipt(config,outcome);
                        if(args[0]=="--apply")Resident(config,outcome,clipboard);else InitialMessage(outcome,installed);
                        return !sent?3:outcome.Success?0:2;
                    });
                }
            }
            catch(Exception error){outcome.Code=FailureCode(error);}
            if(handoff&&!acceptedHandoff)return 2;
            // Initial config/token failures retain the old schema, but do not race
            // an already resident helper for COM2.
            using(var mutex=new Mutex(false,@"Local\WindowsClaude.HostFiles."+WindowsIdentity.GetCurrent().User.Value))
                return WithApplyMutex(mutex,()=>{bool sent=InitialReceipt(config,outcome);InitialMessage(outcome,installed);return !sent?3:2;});
        }
        public static int NativeMode(IList<int[]> modes)
        {
            // [width,height,bits,frequency]: the largest true-colour mode is the one QEMU's adapter offers for this window.
            int best=-1;
            for(int index=0;index<modes.Count;index++)
            {
                int[] mode=modes[index];if(mode[2]<32)continue;
                if(best<0||(long)mode[0]*mode[1]>(long)modes[best][0]*modes[best][1]||((long)mode[0]*mode[1]==(long)modes[best][0]*modes[best][1]&&mode[3]>modes[best][3]))best=index;
            }
            return best;
        }
        static DevMode NewMode(){return new DevMode{dmSize=(short)Marshal.SizeOf(typeof(DevMode))};}
        // What FitDisplay saw and did, reported when the desktop does not end at the requested size.
        static List<int[]> displayModes=new List<int[]>();static int[] displayLargest;static string displayStep="NOT_ATTEMPTED";
        static string ChangeResult(int result)
        {
            switch(result){case 0:return "OK";case 1:return "RESTART";case -1:return "FAILED";case -2:return "BADMODE";case -3:return "NOTUPDATED";case -4:return "BADFLAGS";case -5:return "BADPARAM";case -6:return "BADDUALVIEW";default:return "OTHER";}
        }
        public const int SafeWidth=1280,SafeHeight=800;
        public static int ModeIndex(IList<int[]> modes,int width,int height)
        {
            int found=-1;
            for(int index=0;index<modes.Count;index++)if(modes[index][0]==width&&modes[index][1]==height&&modes[index][2]>=32&&(found<0||modes[index][3]>modes[found][3]))found=index;
            return found;
        }
        public static int DisplayTarget(IList<int[]> modes,int[] requested)
        {
            // The window's size when Windows offers it; otherwise the standard safe mode, so the desktop stays readable.
            // Without the panel's request nothing is saved: the largest mode may be a non-standard size.
            if(requested==null)return -1;
            int index=ModeIndex(modes,requested[0],requested[1]);
            return index>=0?index:ModeIndex(modes,SafeWidth,SafeHeight);
        }
        static void FitDisplay(int[] requested)
        {
            try
            {
                var found=new List<DevMode>();var shapes=new List<int[]>();
                for(int index=0;index<4096;index++){DevMode mode=NewMode();if(!EnumDisplaySettings(null,index,ref mode))break;found.Add(mode);shapes.Add(new[]{mode.dmPelsWidth,mode.dmPelsHeight,mode.dmBitsPerPel,mode.dmDisplayFrequency});}
                int best=NativeMode(shapes),target=DisplayTarget(shapes,requested);DevMode current=NewMode();
                displayModes=shapes;displayLargest=best<0?null:shapes[best];
                if(target<0){displayStep=requested==null?"NO_REQUEST":"NO_MODE";return;}
                if(!EnumDisplaySettings(null,-1,ref current)){displayStep="CURRENT_UNKNOWN";return;}
                if(current.dmPelsWidth==shapes[target][0]&&current.dmPelsHeight==shapes[target][1]){displayStep="ALREADY";return;}
                DevMode mode_=found[target];
                // Saved, so Windows boots straight into it: the panel asks only standard sizes, which the firmware can set
                // again at the next start (a saved window-sized mode garbled the picture after a monitor change, 22.10).
                int saved=ChangeDisplaySettingsEx(null,ref mode_,IntPtr.Zero,1,IntPtr.Zero);
                displayStep="SAVE_"+ChangeResult(saved)+(saved==0?"":"_LIVE_"+ChangeResult(ChangeDisplaySettingsEx(null,ref mode_,IntPtr.Zero,0,IntPtr.Zero)));
            }
            catch(Exception){displayStep="ERROR";}  // Cosmetic: file access must not depend on the desktop size.
        }
        static string ReadDisplaySettings()
        {
            try
            {
                string found=null;
                foreach(var drive in DriveInfo.GetDrives()){if(!drive.IsReady||drive.VolumeLabel!="WCPSETTINGS")continue;if(found!=null)return null;found=Path.Combine(drive.RootDirectory.FullName,"SETTINGS.JSN");}
                return found==null?null:ReadBounded(found);
            }
            catch(Exception){return null;}
        }
        static int[] RequestedSize(string settings)
        {
            try{var values=Object(new Reader(settings).Read(),"schema","width","height","session_nonce");return values["width"] is int&&values["height"] is int?new[]{(int)values["width"],(int)values["height"]}:null;}
            catch(Exception){return null;}
        }
        public static string DisplayDetail(IList<int[]> modes,int[] largest,int width,int height,string step)
        {
            // e.g. REQ_UNLISTED_MAX_BELOW_ALREADY: Windows does not offer the adapter's new size at all.
            bool listed=modes.Any(mode=>mode[0]==width&&mode[1]==height&&mode[2]>=32);
            string max=largest==null?"NONE":largest[0]==width&&largest[1]==height?"SAME":(long)largest[0]*largest[1]>(long)width*height?"ABOVE":"BELOW";
            string detail="REQ_"+(listed?"LISTED":"UNLISTED")+"_MAX_"+max+"_"+step;
            return detail.Length>64?detail.Substring(0,64):detail;
        }
        public static string DisplayReceipt(string settings,int width,int height,string detail=null)
        {
            // The same COM1 receipt GuestSettings sends, so the panel confirms the size this helper applied.
            var values=Object(new Reader(settings).Read(),"schema","width","height","session_nonce");string nonce=Text(values,"session_nonce");
            if(!(values["schema"] is int)||(int)values["schema"]!=1||!(values["width"] is int)||!(values["height"] is int)||!Matches(nonce,"[a-f0-9]{32}"))throw Invalid();
            int requestedWidth=(int)values["width"],requestedHeight=(int)values["height"];
            string code=requestedWidth==width&&requestedHeight==height?"OK":"DISPLAY_APPLY_INCOMPLETE";
            string displayCode=code!="OK"&&Matches(detail,"[A-Z_]{1,64}")?detail:code;
            return Json.Serialize(new{schema=1,session_nonce=nonce,requested=new{width=requestedWidth,height=requestedHeight},current=new{width=width,height=height},success=code=="OK",code=code,display_code=displayCode});
        }
        static void ReportDisplay(string settings)
        {
            try
            {
                DevMode current=NewMode();int[] requested=RequestedSize(settings);
                if(settings==null||requested==null||!EnumDisplaySettings(null,-1,ref current))return;
                string detail=DisplayDetail(displayModes,displayLargest,requested[0],requested[1],displayStep);
                string line=DisplayReceipt(settings,current.dmPelsWidth,current.dmPelsHeight,detail);
                using(var serial=new SerialPort("COM1",115200){WriteTimeout=3000,Encoding=new UTF8Encoding(false),NewLine="\n"}){serial.Open();serial.WriteLine(line);}
            }
            catch(Exception){}  // Cosmetic: without it the panel shows the display as unconfirmed.
        }
        public static string HandoffReceipt(string nonce,string hash,int pid,long started)
        {
            return String.Join("|","1",nonce,hash,pid.ToString(CultureInfo.InvariantCulture),started.ToString(CultureInfo.InvariantCulture));
        }
        public static bool HandoffMatches(string receipt,string nonce,string hash,int pid,long started)
        {
            return receipt==HandoffReceipt(nonce,hash,pid,started);
        }
        public static bool Handover(Func<bool> start,Func<bool> ready,Action commit,Action stop,Action wait,int attempts)
        {
            bool started=false;
            try
            {
                started=start();if(!started)return false;
                for(int attempt=0;attempt<attempts;attempt++)
                {
                    if(ready()){commit();return true;}
                    if(attempt+1<attempts)wait();
                }
            }
            catch(Exception){} // Failed updates leave the installed helper responsible for this session.
            if(started)stop();
            return false;
        }
        static string HandoffPath(string nonce){return Path.Combine(DirectoryPath(),"handoff-"+nonce+".ready");}
        static string HandoffEvent(string nonce){return @"Local\WindowsClaude.HostFiles.Handoff."+nonce;}
        static bool ConfirmHandoff(string nonce)
        {
            string current=Assembly.GetExecutingAssembly().Location;
            if(!StartupTargetMatches(current,"--apply",DirectoryPath()))return false;
            using(var accepted=EventWaitHandle.OpenExisting(HandoffEvent(nonce)))
            using(var process=Process.GetCurrentProcess())
            {
                string path=HandoffPath(nonce);NoReparse(path);
                byte[] receipt=Encoding.UTF8.GetBytes(HandoffReceipt(nonce,Hash(current).Replace("-",""),process.Id,process.StartTime.ToUniversalTime().Ticks));
                using(var stream=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.Read))stream.Write(receipt,0,receipt.Length);
                return accepted.WaitOne(15000);
            }
        }
        static bool UpdateFromMedium()
        {
            Process child=null;string receipt=null;long started=0;bool committed=false,ownsContext=false;
            try
            {
                if(mediumRoot==null)return false;
                string current=Assembly.GetExecutingAssembly().Location,medium=Path.Combine(mediumRoot,"HOSTFILS.EXE");
                if(!File.Exists(medium)||Hash(medium)==Hash(current))return false;
                string directory=DirectoryPath(),startup=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Startup),"WindowsClaude Host Files.lnk"),staged=null;
                StageHelper(medium,directory,destination=>staged=destination);
                string nonce=Guid.NewGuid().ToString("N"),hash=Hash(staged).Replace("-","");receipt=HandoffPath(nonce);NoReparse(receipt);
                bool created;
                using(var accepted=new EventWaitHandle(false,EventResetMode.ManualReset,HandoffEvent(nonce),out created))
                {
                    if(!created||File.Exists(receipt))return false;
                    ownsContext=true;
                    committed=Handover(
                        ()=>{child=Process.Start(new ProcessStartInfo(staged,"--apply --handoff "+nonce){UseShellExecute=false});if(child==null)return false;started=child.StartTime.ToUniversalTime().Ticks;return true;},
                        ()=>
                        {
                            if(child.HasExited)return false;
                            try{NoReparse(receipt);return HandoffMatches(ReadBounded(receipt),nonce,hash,child.Id,started)&&!child.HasExited;}
                            catch(IOException){return false;}
                        },
                        ()=>{StartupShortcut(startup,staged,directory);accepted.Set();},
                        ()=>StopHandoff(child,started),()=>Thread.Sleep(100),100);
                    return committed;
                }
            }
            catch(Exception){return false;}
            finally
            {
                if(child!=null){if(!committed)StopHandoff(child,started);child.Dispose();}
                try{if(ownsContext){NoReparse(receipt);if(File.Exists(receipt))File.Delete(receipt);}}catch(Exception){}
            }
        }
        static void StopHandoff(Process child,long started)
        {
            try{if(child!=null&&started!=0&&!child.HasExited&&child.StartTime.ToUniversalTime().Ticks==started){child.Kill();child.WaitForExit(3000);}}catch(Exception){}
        }
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]
        struct DevMode
        {
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=32)]public string dmDeviceName;
            public short dmSpecVersion,dmDriverVersion,dmSize,dmDriverExtra;
            public int dmFields,dmPositionX,dmPositionY,dmDisplayOrientation,dmDisplayFixedOutput;
            public short dmColor,dmDuplex,dmYResolution,dmTTOption,dmCollate;
            [MarshalAs(UnmanagedType.ByValTStr,SizeConst=32)]public string dmFormName;
            public short dmLogPixels;
            public int dmBitsPerPel,dmPelsWidth,dmPelsHeight,dmDisplayFlags,dmDisplayFrequency,dmICMMethod,dmICMIntent,dmMediaType,dmDitherType,dmReserved1,dmReserved2,dmPanningWidth,dmPanningHeight;
        }
        [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern bool EnumDisplaySettings(string device,int mode,ref DevMode settings);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern int ChangeDisplaySettingsEx(string device,ref DevMode settings,IntPtr window,uint flags,IntPtr parameter);
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]struct NetResource {public uint Scope,Type,DisplayType,Usage;public string Local,Remote,Comment,Provider;}
        [StructLayout(LayoutKind.Sequential)]struct EnumeratedResource {public uint Scope,Type,DisplayType,Usage;public IntPtr Local,Remote,Comment,Provider;}
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetOpenEnum(uint scope,uint type,uint usage,IntPtr resource,out IntPtr handle);
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetEnumResource(IntPtr handle,ref uint count,IntPtr buffer,ref int size);
        [DllImport("mpr.dll")]static extern int WNetCloseEnum(IntPtr handle);
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetAddConnection2(ref NetResource resource,string password,string username,uint flags);
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetGetConnection(string local,StringBuilder remote,ref int length);
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetCancelConnection2(string name,uint flags,bool force);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool GetVolumeInformation(string root,StringBuilder name,uint size,out uint serial,out uint maximum,out uint flags,StringBuilder filesystem,uint capacity);
        [DllImport("advapi32.dll",SetLastError=true)]static extern bool GetTokenInformation(IntPtr token,int information,out uint elevation,int capacity,out int size);
        [DllImport("advapi32.dll",EntryPoint="GetTokenInformation",SetLastError=true)]static extern bool GetTokenStatistics(IntPtr token,int information,out TokenStatistics stats,int capacity,out int size);
    }
}

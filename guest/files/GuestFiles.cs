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

[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]

namespace WindowsClaude.GuestFiles
{
    public sealed class Config { public string Nonce,Username,Password;public readonly List<Share> Shares=new List<Share>(); }
    public sealed class Share { public string Name,Label,PreferredLetter; }
    public sealed class Mapping { public string Name,Letter; }
    public sealed class NicState { public string Id,Mac;public bool Up,HasGateway,HasDns;public string[] Addresses=new string[0],Masks=new string[0]; }
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
        int GetConnection(string letter,out string remote);
        int Add(string remote,string letter,string username,string password,uint flags);
        int Cancel(string letter,uint flags,bool force);
    }
    public static class Program
    {
        public const string Server=@"\\192.168.234.4";
        static readonly JavaScriptSerializer Json=new JavaScriptSerializer{MaxJsonLength=32768,RecursionLimit=8};
        static bool Matches(string value,string pattern){return value!=null&&Regex.IsMatch(value,"\\A"+pattern+"\\z",RegexOptions.CultureInvariant);}
        static FormatException Invalid(){return new FormatException("CONFIG_INVALID");}
        sealed class Reader
        {
            readonly string text;int offset;
            internal Reader(string input){if(input==null||Encoding.UTF8.GetByteCount(input)>32768)throw Invalid();text=input;}
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
            var values=Object(new Reader(text).Read(),"schema","session_nonce","username","password","shares");
            if(!(values["schema"] is int)||(int)values["schema"]!=1)throw Invalid();
            var config=new Config{Nonce=Text(values,"session_nonce"),Username=Text(values,"username"),Password=Text(values,"password")};
            if(!Matches(config.Nonce,"[A-Fa-f0-9]{32}")||!Matches(config.Username,"WCP_[A-Za-z0-9_]{1,32}")||!Matches(config.Password,"[A-Za-z0-9_-]{32,128}"))throw Invalid();
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
        public static void ConfigureNic(INicConfiguration network,bool elevated)
        {
            if(!elevated)throw new InvalidOperationException("ADMIN_TOKEN_REQUIRED");
            var matches=network.Read().Where(item=>item.Mac=="525400770004").ToArray();Guid id;
            if(matches.Length!=1||!Guid.TryParse(matches[0].Id,out id))throw new InvalidOperationException("ISOLATED_NIC_NOT_UNIQUE");
            if(!NicReady(matches[0]))network.Configure(id.ToString("B"));
        }
        public static void EnsureNic(Func<bool> ready,Func<int> configure,Action wait)
        {
            if(ready())return;if(configure()!=0)throw new InvalidOperationException("NIC_CONFIGURATION_FAILED");
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
            foreach(Mapping old in previous)
            {
                if(old.Letter.Length==0){result.Code="UNLETTERED_CONNECTION_REMAINS";continue;}
                string remote;int status=network.GetConnection(old.Letter,out remote);
                if(status!=0&&status!=2250){result.Code="CONNECTION_INSPECTION_FAILED";return result;}
                if(status==0&&String.Equals(remote,Server+"\\"+old.Name,StringComparison.OrdinalIgnoreCase))
                {if(network.Cancel(old.Letter,0,false)!=0){result.Code="OWN_MAPPING_BUSY";return result;}}
                result.Owned.Remove(old);try{persist(result.Owned);}catch{result.Code="OWN_RECEIPT_WRITE_FAILED";return result;}
            }
            if(!result.Success)return result;
            if(config.Shares.Count==0)return result;
            var used=new HashSet<string>(network.UsedLetters(),StringComparer.OrdinalIgnoreCase);used.Add("C");
            foreach(Share share in config.Shares)
            {
                string letter=ChooseLetter(share.PreferredLetter,used);int status=network.Add(Server+"\\"+share.Name,letter,config.Username,config.Password,4);
                if(status!=0){result.Code=status==1219?"CREDENTIAL_CONFLICT":"MAPPING_FAILED";return result;}
                var mapped=new Mapping{Name=share.Name,Letter=letter};result.Mappings.Add(mapped);result.Owned.Add(mapped);if(letter.Length>0)used.Add(letter);else result.NeedsShortcut=true;
                try{persist(result.Owned);}catch{result.Code="OWN_RECEIPT_WRITE_FAILED";return result;}
            }
            return result;
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
        public static bool IsQemu(string manufacturer){return String.Equals((manufacturer??"").Trim(),"QEMU",StringComparison.OrdinalIgnoreCase);}
        static void RequireGuest(){using(var bios=Registry.LocalMachine.OpenSubKey(@"HARDWARE\DESCRIPTION\System\BIOS",false))if(bios==null||!IsQemu(bios.GetValue("SystemManufacturer") as string))throw new InvalidOperationException("GUEST_GUARD_REFUSED");}
        static void NoReparse(string path){for(string item=Path.GetFullPath(path);!String.IsNullOrEmpty(item);item=Path.GetDirectoryName(item))if((File.Exists(item)||Directory.Exists(item))&&(File.GetAttributes(item)&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("REPARSE_PATH_REFUSED");}
        static string ReadBounded(string path)
        {
            NoReparse(path);using(var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read)){if(stream.Length>32768)throw Invalid();using(var reader=new StreamReader(stream,new UTF8Encoding(false,true),true))return reader.ReadToEnd();}
        }
        static string FindConfig()
        {
            string found=null;
            foreach(var drive in DriveInfo.GetDrives())
            {
                if(!drive.IsReady||drive.VolumeLabel!="WCPFILES")continue;if(found!=null)throw new InvalidOperationException("AMBIGUOUS_MEDIUM");
                uint serial,length,flags;if(!GetVolumeInformation(drive.RootDirectory.FullName,null,0,out serial,out length,out flags,null,0))throw new InvalidOperationException("MEDIUM_INSPECTION_FAILED");
                if(!drive.DriveFormat.StartsWith("FAT",StringComparison.OrdinalIgnoreCase)||(flags&0x80000)==0)throw new InvalidOperationException("MEDIUM_NOT_READONLY_FAT");found=Path.Combine(drive.RootDirectory.FullName,"HOSTFILS.JSN");
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
            public IEnumerable<string> UsedLetters()
            {
                var result=new HashSet<string>(Environment.GetLogicalDrives().Select(path=>path.Substring(0,1)),StringComparer.OrdinalIgnoreCase);
                for(char letter='A';letter<='Z';letter++){string remote;if(GetConnection(letter.ToString(),out remote)!=2250)result.Add(letter.ToString());}return result;
            }
            public int GetConnection(string letter,out string remote){var value=new StringBuilder(1024);int size=value.Capacity;int result=WNetGetConnection(letter+":",value,ref size);remote=result==0?value.ToString():null;return result;}
            public int Add(string remote,string letter,string username,string password,uint flags){var resource=new NetResource{Type=1,Local=letter.Length==0?null:letter+":",Remote=remote};return WNetAddConnection2(ref resource,password,username,flags);}
            public int Cancel(string letter,uint flags,bool force){return WNetCancelConnection2(letter+":",flags,force);}
        }
        sealed class WindowsNic : INicConfiguration
        {
            public NicState[] Read()
            {
                return NetworkInterface.GetAllNetworkInterfaces().Select(adapter=>
                {
                    var ip=adapter.GetIPProperties();var ipv4=ip.UnicastAddresses.Where(item=>item.Address.AddressFamily==AddressFamily.InterNetwork).ToArray();
                    return new NicState{Id=adapter.Id,Mac=adapter.GetPhysicalAddress().ToString(),Up=adapter.OperationalStatus==OperationalStatus.Up,HasGateway=ip.GatewayAddresses.Count!=0,HasDns=ip.DnsAddresses.Count!=0,Addresses=ipv4.Select(item=>item.Address.ToString()).ToArray(),Masks=ipv4.Select(item=>item.IPv4Mask.ToString()).ToArray()};
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
        static bool Ready()
        {
            var matches=new WindowsNic().Read().Where(item=>item.Mac=="525400770004").ToArray();return matches.Length==1&&NicReady(matches[0]);
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
            try{link.TargetPath=target;link.Arguments=arguments;link.Description="Windows Claude: диски и папки ПК";link.Save();File.Move(temporary,path);}
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
                link=shell.CreateShortcut(temporary);link.TargetPath=target;link.Arguments="--apply";link.Description="Windows Claude: диски и папки ПК";link.Save();
                NoReparse(path);if(exists)File.Replace(temporary,path,null);else File.Move(temporary,path);
            }
            finally{if(link!=null)Marshal.FinalReleaseComObject(link);Marshal.FinalReleaseComObject(shell);if(File.Exists(temporary))File.Delete(temporary);}
        }
        static string Explanation(string code)
        {
            switch(code)
            {
                case "OK":return "Диски и папки ПК подключены. Они появятся в Проводнике. Подключение запускается автоматически при входе.";
                case "ORDINARY_TOKEN_REQUIRED":return "Запусти RUNFILES.CMD обычным двойным щелчком, без «Запуск от имени администратора», чтобы диски появились в Проводнике.";
                case "OWN_MAPPING_BUSY":return "Закрой файлы и окна папок ПК в Windows, затем повтори подключение. Открытые файлы принудительно не отключены.";
                case "UNLETTERED_CONNECTION_REMAINS":return "Осталось временное подключение без буквы. Выйди из учётной записи Windows и войди снова, чтобы очистить его.";
                case "CREDENTIAL_CONFLICT":return "Windows уже подключена к ПК с другими учётными данными. Выйди из учётной записи Windows и войди снова. Чужие подключения не отключены.";
                case "NIC_NOT_READY":return "Изолированная сеть папок ПК не готова. Проверь подключение в панели и повтори запуск RUNFILES.CMD.";
                case "NIC_ADMIN_CANCELLED":return "Для настройки отдельной сети папок ПК нужно разрешение администратора. Повтори RUNFILES.CMD и подтверди запрос Windows.";
                case "NIC_CONFIGURATION_FAILED":return "Не удалось настроить отдельный сетевой адаптер папок ПК. Повтори RUNFILES.CMD.";
                case "EXPLORER_LABEL_FAILED":return "Папки ПК подключены, но Проводник не принял их понятные названия. Повтори RUNFILES.CMD.";
                case "ISOLATED_NIC_NOT_UNIQUE":return "Не найден единственный отдельный сетевой адаптер папок ПК. Проверь конфигурацию виртуальной машины.";
                default:return "Подключение папок ПК не завершено. Код: "+code+". Передай этот код для диагностики.";
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
            if(args.Length!=1||(args[0]!="--install"&&args[0]!="--apply"))return 64;
            Config config=null;Outcome outcome=new Outcome();bool installed=false;
            try
            {
                config=ParseConfig(FindConfig());
                if(Elevated())throw new InvalidOperationException("ORDINARY_TOKEN_REQUIRED");
                if(!Environment.UserInteractive||Process.GetCurrentProcess().SessionId==0)throw new InvalidOperationException("INTERACTIVE_USER_REQUIRED");
                using(var mutex=new Mutex(false,@"Local\WindowsClaude.HostFiles."+WindowsIdentity.GetCurrent().User.Value))
                {
                    bool held;try{held=mutex.WaitOne(0);}catch(AbandonedMutexException){held=true;}if(!held)throw new InvalidOperationException("APPLY_ALREADY_RUNNING");
                    try
                    {
                        if(args[0]=="--install"){if(config.Shares.Count>0)EnsureNic(Ready,ElevateNic,()=>Thread.Sleep(1000));Install();installed=true;}
                        string state=StatePath();List<Mapping> previous;
                        try{previous=File.Exists(state)?ParseOwned(ReadBounded(state)):new List<Mapping>();}catch(FormatException){throw new InvalidOperationException("OWN_RECEIPT_INVALID");}
                        outcome=ApplyMappings(config,previous,new WinNet(),Ready,()=>Thread.Sleep(1000),false,true,owned=>SaveOwned(state,owned));
                        SetExplorerLabels(config,outcome);
                        if(outcome.NeedsShortcut)Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),"Диски и папки ПК.lnk"),Server,"");
                    }
                    finally{mutex.ReleaseMutex();}
                }
            }
            catch(Exception error){outcome.Code=error is InvalidOperationException&&Matches(error.Message,"[A-Z_]{1,64}")?error.Message:error is FormatException?"CONFIG_INVALID":"APPLY_FAILED";}
            bool sent=true;try{using(var serial=new SerialPort("COM2",115200){WriteTimeout=3000,Encoding=new UTF8Encoding(false),NewLine="\n"}){serial.Open();serial.WriteLine(Receipt(config,outcome));}}catch{sent=false;}
            if(installed||!outcome.Success)MessageBox.Show(Explanation(outcome.Code),"Диски и папки ПК",MessageBoxButtons.OK,outcome.Success?MessageBoxIcon.Information:MessageBoxIcon.Warning);
            return !sent?3:outcome.Success?0:2;
        }
        [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]struct NetResource {public uint Scope,Type,DisplayType,Usage;public string Local,Remote,Comment,Provider;}
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetAddConnection2(ref NetResource resource,string password,string username,uint flags);
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetGetConnection(string local,StringBuilder remote,ref int length);
        [DllImport("mpr.dll",CharSet=CharSet.Unicode)]static extern int WNetCancelConnection2(string name,uint flags,bool force);
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern bool GetVolumeInformation(string root,StringBuilder name,uint size,out uint serial,out uint maximum,out uint flags,StringBuilder filesystem,uint capacity);
        [DllImport("advapi32.dll",SetLastError=true)]static extern bool GetTokenInformation(IntPtr token,int information,out uint elevation,int capacity,out int size);
        [DllImport("advapi32.dll",EntryPoint="GetTokenInformation",SetLastError=true)]static extern bool GetTokenStatistics(IntPtr token,int information,out TokenStatistics stats,int capacity,out int size);
    }
}

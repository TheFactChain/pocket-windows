using System;
using System.IO;
using System.Text;

internal static class ReleaseInfoRegression
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static string Metadata(string id="2026.10.23.1",string commit=null)
    {return "{\"schema\":1,\"release_id\":\""+id+"\",\"source_commit\":\""+(commit??new string('a',40))+"\"}";}
    [STAThread]
    static int Main(string[] args)
    {
        try
        {
            string root=Path.Combine(args[0],"release-info-fixture");Directory.CreateDirectory(root);
            string path=Path.Combine(root,"release-info.json");
            Assert(ReleaseInfo.Read(root).DisplayText=="Версия для разработки","Old bundles without metadata must remain usable");
            File.WriteAllText(path,Metadata(),new UTF8Encoding(false));var release=ReleaseInfo.Read(root);
            Assert(release.DisplayText=="Версия 2026.10.23.1"&&release.SourceCommit==new string('a',40),"Valid release metadata must be readable from an arbitrary root");
            foreach(string invalid in new[]{"null","{broken",Metadata().Replace("\"schema\":1","\"schema\":2"),Metadata().Replace("\"schema\":1","\"schema\":true"),
                Metadata("not-a-release"),Metadata(commit:new string('A',40)),Metadata(commit:new string('a',39)),Metadata().Replace("{","{\"extra\":1,"),
                Metadata().Replace("{","{\"schema\":1,"),Metadata().Replace("}",",}"),new string(' ',4097)+Metadata()})
            {
                File.WriteAllText(path,invalid,new UTF8Encoding(false));
                Assert(ReleaseInfo.Read(root).DisplayText=="Версия для разработки","Invalid metadata must fall back safely: "+invalid.Substring(0,Math.Min(50,invalid.Length)));
            }
            File.WriteAllBytes(path,new byte[]{0xff,0xfe,0xfa});
            Assert(ReleaseInfo.Read(root).ReleaseId==null,"Invalid UTF8 must fall back safely");
            Assert(ReleaseInfo.StartupBlockReason(root)==null,"A bundle without an update transaction must be allowed");
            string marker=Path.Combine(root,"state","app-update.json");Directory.CreateDirectory(Path.GetDirectoryName(marker));
            foreach(string value in new[]{"{unfinished","{}",""})
            {
                File.WriteAllText(marker,value);
                Assert(ReleaseInfo.StartupBlockReason(root).Contains("Обновление программы не завершено"),"Any unfinished transaction marker must block startup without parsing or running UI");
            }
            File.Delete(marker);Assert(ReleaseInfo.StartupBlockReason(root)==null,"Removing a completed marker must allow startup");
            Console.WriteLine("PASS: release metadata, strict validation/development fallback and pure update startup guard");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

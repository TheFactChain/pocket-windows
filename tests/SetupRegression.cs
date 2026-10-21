#if SETUP_TESTS
using System;
using System.IO;
using System.IO.Compression;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal static class SetupRegression
{
    private static int checks;
    private static void Assert(bool condition, string message)
    { if (!condition) throw new Exception(message); checks++; }
    private static void Refused(Action action, string message)
    { try { action(); } catch (Exception) { checks++; return; } throw new Exception(message); }
    private static string Hash(byte[] value)
    { using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(value)).Replace("-", "").ToLowerInvariant(); }
    private static byte[] Disk()
    { var bytes = new byte[104]; bytes[0]=81; bytes[1]=70; bytes[2]=73; bytes[3]=251; bytes[7]=3; bytes[31]=1; return bytes; }
    private static void CheckMetadataBounds(string root)
    {
        var readSmall=typeof(SetupInstaller).GetMethod("ReadSmall",System.Reflection.BindingFlags.Static|System.Reflection.BindingFlags.NonPublic);
        Assert(readSmall!=null,"metadata reader missing");
        foreach(int claimed in new [] {1,4097})
        {
            string file=Path.Combine(root,"metadata-"+claimed+".zip");
            using(var stream=File.Create(file)) using(var zip=new ZipArchive(stream,ZipArchiveMode.Create))
                using(var output=zip.CreateEntry("metadata",CompressionLevel.Optimal).Open())
                { byte[] bytes=new byte[4096]; output.Write(bytes,0,bytes.Length); }
            byte[] content=File.ReadAllBytes(file);
            int central=-1;
            for(int i=0;i<content.Length-46;i++)
                if(content[i]==80 && content[i+1]==75 && content[i+2]==1 && content[i+3]==2) {central=i;break;}
            Assert(central>=0,"central directory missing");
            Array.Copy(BitConverter.GetBytes(claimed),0,content,central+24,4);
            File.WriteAllBytes(file,content);
            using(var stream=File.OpenRead(file)) using(var zip=new ZipArchive(stream,ZipArchiveMode.Read))
            {
                var entry=zip.GetEntry("metadata");
                Assert(entry.Length==claimed,"forged central size not exposed");
                using(var actual=entry.Open()) using(var expanded=new MemoryStream())
                { actual.CopyTo(expanded); Assert(expanded.Length==4096,"fixture does not expose actual deflate bytes"); }
                Refused(delegate { readSmall.Invoke(null,new object[] {entry,claimed==1 ? 1024L : 8192L}); },
                    "metadata with forged size accepted: "+claimed);
            }
        }
    }
    private static string Fixture(string root, string label, string badPath, bool badHash, bool link, bool backing)
    {
        string file = Path.Combine(root, label + ".zip");
        var rows = new List<Dictionary<string, object>>();
        var files = new Dictionary<string, byte[]>();
        foreach (string name in SetupInstaller.Required) files[name] = name.EndsWith(".qcow2") ? Disk() : Encoding.UTF8.GetBytes("fixture: " + name);
        if (backing) files["images/workvm-empty.qcow2"][15] = 1;
        if (badPath != null) files[badPath] = Encoding.UTF8.GetBytes("must refuse");
        foreach (var item in files) rows.Add(new Dictionary<string,object> { {"path", item.Key}, {"bytes", item.Value.LongLength}, {"sha256", badHash ? new string('0',64) : Hash(item.Value)} });
        var manifest = new Dictionary<string, object> { {"schema",2}, {"kind","public_distribution"}, {"created_utc","fixture"},
            {"encrypted_profiles_included",false}, {"gateway_recovery_included",false}, {"credentials_portability","No connection profiles included."},
            {"vm_started_for_packaging",false}, {"second_pc_tested",false}, {"files",rows} };
        byte[] raw = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(manifest));
        var sums = new StringBuilder();
        foreach (var row in rows) sums.Append(row["sha256"]).Append("  ").Append(row["path"]).Append('\n');
        sums.Append(Hash(raw)).Append("  MANIFEST.json\n");
        using (var stream = File.Create(file)) using (var zip = new ZipArchive(stream, ZipArchiveMode.Create))
        {
            foreach (var item in files)
            {
                var entry = zip.CreateEntry("Windows-Claude/" + item.Key);
                if (link && item.Key == "Launcher.exe") entry.ExternalAttributes = unchecked((int)0xA1FF0000);
                using (var output = entry.Open()) output.Write(item.Value, 0, item.Value.Length);
            }
            using (var output = zip.CreateEntry("Windows-Claude/MANIFEST.json").Open()) output.Write(raw,0,raw.Length);
            byte[] sumBytes = Encoding.UTF8.GetBytes(sums.ToString());
            using (var output = zip.CreateEntry("Windows-Claude/MANIFEST.sha256").Open()) output.Write(sumBytes,0,sumBytes.Length);
        }
        using (var sha = SHA256.Create()) using (var input = File.OpenRead(file))
            File.WriteAllText(file + ".sha256", BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant() + "  " + Path.GetFileName(file) + "\n", new UTF8Encoding(false));
        return file;
    }
    public static int Main(string[] args)
    {
        if(args.Length==3 && args[0]=="--install-fixture")
        {
            try { SetupInstaller.Install(args[1],args[2],CancellationToken.None,delegate(string v) {}); return 0; }
            catch(Exception error) { Console.Error.WriteLine(error); return 1; }
        }
        string root = Path.Combine(Path.GetTempPath(), "setup regression " + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            CheckMetadataBounds(root);
            string zip = Fixture(root,"good",null,false,false,false), target = Path.Combine(root,"installed");
            SetupInstaller.Install(zip,target,CancellationToken.None,delegate(string value) {});
            Assert(File.Exists(Path.Combine(target,"Launcher.exe")),"Launcher missing; folders: " + String.Join(", ",Directory.GetDirectories(root)));
            Assert(File.ReadAllBytes(Path.Combine(target,"images/workvm-empty.qcow2")).Length == 104,"disk differs");
            File.WriteAllText(Path.Combine(target,"user-data.txt"),"preserve");
            Refused(delegate { SetupInstaller.Install(zip,target,CancellationToken.None,delegate(string v) {}); },"existing installation overwritten");
            Assert(File.ReadAllText(Path.Combine(target,"user-data.txt"))=="preserve","user data lost");
            string empty=Path.Combine(root,"existing-empty"); Directory.CreateDirectory(empty);
            Refused(delegate { SetupInstaller.Install(zip,empty,CancellationToken.None,delegate(string v) {}); },"existing empty directory replaced");
            string concurrent=Path.Combine(root,"concurrent"); bool created=false;
            Refused(delegate { SetupInstaller.Install(zip,concurrent,CancellationToken.None,delegate(string v) {
                if(!created && v.StartsWith("Распаковка")) { Directory.CreateDirectory(concurrent); File.WriteAllText(Path.Combine(concurrent,"preserve"),"other installer"); created=true; }
            }); },"concurrent destination overwritten");
            Assert(File.ReadAllText(Path.Combine(concurrent,"preserve"))=="other installer","concurrent data lost");
            foreach (string path in new [] {"../escape","runtime/python-3.12.14/../../escape","profiles/profiles.json","private/key","scripts/Build-Launcher.ps1","runtime/python-3.12.14/CON.txt","Launcher.EXE"})
            {
                string bad = Fixture(root,Guid.NewGuid().ToString("N"),path,false,false,false);
                string destination = Path.Combine(root,Guid.NewGuid().ToString("N"));
                Refused(delegate { SetupInstaller.Install(bad,destination,CancellationToken.None,delegate(string v) {}); },"unsafe member accepted: " + path);
                Assert(!Directory.Exists(destination),"failed archive published");
            }
            foreach (string failure in new [] {"hash","link","backing","sidecar","cancel"})
            {
                string bad = Fixture(root,failure,null,failure=="hash",failure=="link",failure=="backing");
                if (failure=="sidecar") File.WriteAllText(bad+".sha256", new string('0',64)+"  sidecar.zip\n");
                var cancel = new CancellationTokenSource();
                if (failure=="cancel") cancel.Cancel();
                string destination = Path.Combine(root,"failed-"+failure);
                Refused(delegate { SetupInstaller.Install(bad,destination,cancel.Token,delegate(string v) {}); },"invalid archive accepted: "+failure);
                Assert(!Directory.Exists(destination),"failed archive published");
                cancel.Dispose();
            }
            string midway = Path.Combine(root,"cancel-midway");
            string unrelated=Path.Combine(root,".windows-claude-setup-unrelated"); Directory.CreateDirectory(unrelated);
            File.WriteAllText(Path.Combine(unrelated,"preserve"),"not ours");
            using (var cancel = new CancellationTokenSource())
                Refused(delegate { SetupInstaller.Install(zip,midway,cancel.Token,delegate(string v) { if(v.StartsWith("Распаковка")) cancel.Cancel(); }); },"cancellation ignored");
            Assert(!Directory.Exists(midway),"cancelled extraction published");
            Assert(Directory.GetDirectories(root,".windows-claude-setup-*").Length==1,"own staging leaked");
            Assert(File.ReadAllText(Path.Combine(unrelated,"preserve"))=="not ours","unrelated staging deleted");
            Refused(delegate { SetupInstaller.CheckSpace(1024,1024); },"space precheck missing");
            Console.WriteLine("Setup regression: " + checks + " assertions OK");
            return 0;
        }
        catch(Exception error) { Console.Error.WriteLine(error); return 1; }
        finally { Directory.Delete(root,true); }
    }
}
#endif

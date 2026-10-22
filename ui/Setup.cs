#if SETUP || SETUP_TESTS
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32.SafeHandles;

internal static class SetupInstaller
{
    const string Prefix = "Windows-Claude/";
    internal static readonly string[] Required = new [] {
        "images/workvm-empty.qcow2", "state/workvm-vars.fd", "Launcher.exe", "config/runtime.json",
        "runtime/components-manifest.json", "runtime/components-recovery.zip", "gateway/packages/current.json",
        "runtime/python-3.12.14/python.exe", "runtime/qemu-11.1.0/qemu-system-x86_64.exe",
        "runtime/qemu-11.1.0/qemu-img.exe", "runtime/qemu-11.1.0/share/edk2-x86_64-code.fd",
        "scripts/portable_app.py", "scripts/launch_runtime.py", "scripts/control_guest_install.py",
        "scripts/runtime_network_plan.py", "scripts/runtime_config.py", "scripts/connection_profile.py",
        "scripts/profile_store.py", "scripts/gateway_controller.py", "scripts/gateway_package.py",
        "scripts/gateway_profile.py", "scripts/session_carrier.py", "scripts/file_snapshot.py",
        "scripts/runtime_logs.py", "scripts/Start-Runtime.ps1", "scripts/Stop-Runtime.ps1",
        "scripts/host_file_sharing.py", "scripts/file_share_carrier.py",
        "scripts/Prepare-Dependencies.ps1", "scripts/Prepare-Host.ps1" };

    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFile(string path, uint access, uint sharing, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateDirectory(string path, IntPtr security);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetFileInformationByHandle(SafeFileHandle handle, int informationClass, IntPtr information, uint length);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfoNative information);
    [StructLayout(LayoutKind.Sequential)]
    struct FileInfoNative
    {
        public uint Attributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Modified;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct RenameInfo { public uint Replace; public IntPtr Root; public uint Length; public char Name; }

    static void Fail(string message) { throw new InvalidDataException(message); }
    internal static bool SafeName(string name)
    {
        return !String.IsNullOrEmpty(name) && name.Split('/').All(part => part.Length>0 && part!="." && part!=".."
            && !part.EndsWith(".") && !part.EndsWith(" ") && !Regex.IsMatch(part,"[\\\\:<>\"|?*\x00-\x1f]")
            && !Regex.IsMatch(part,@"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)",RegexOptions.IgnoreCase));
    }
    static bool Allowed(string name)
    {
        return SafeName(name) && (Required.Contains(name) || name=="release-info.json" || name=="scripts/windows_guest_settings.py" || name=="scripts/windows_hardware.py" || name=="config/windows.json" || name=="config/viewer.json"
            || Regex.IsMatch(name,@"^runtime/host-files/(HostFiles\.exe|HostFilesGuest\.exe|SMBLibrary\.dll|SMBLibrary\.Win32\.dll|Utilities\.dll|SMBLibrary\.LICENSE\.txt|SMBLibrary-source\.zip|NOTICE\.txt|build-manifest\.json)$")
            || name=="runtime/gateway-recovery.json" || name=="runtime/gateway-recovery.zip"
            || name.StartsWith("runtime/python-3.12.14/",StringComparison.Ordinal)
            || name.StartsWith("runtime/qemu-11.1.0/",StringComparison.Ordinal)
            || Regex.IsMatch(name,@"^gateway/packages/releases/[a-z0-9][a-z0-9._-]{0,63}/(manifest.json|gateway.qcow2)$"));
    }
    static void Plain(string path)
    {
        var info = File.GetAttributes(path);
        if ((info & FileAttributes.ReparsePoint)!=0) Fail("Ссылки и reparse-пути запрещены: " + path);
    }
    static SafeFileHandle Pin(string path, bool rename)
    {
        Plain(path);
        var handle = CreateFile(path, rename ? 0x10080u : 0x80u, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        if (handle.IsInvalid) { handle.Dispose(); throw new Win32Exception(Marshal.GetLastWin32Error(),"Не удалось закрепить папку: " + path); }
        try { Plain(path); return handle; } catch { handle.Dispose(); throw; }
    }
    internal static List<SafeFileHandle> PinParents(string parent)
    {
        var paths = new Stack<string>();
        for (var item=new DirectoryInfo(parent); item!=null; item=item.Parent) paths.Push(item.FullName);
        var handles = new List<SafeFileHandle>();
        try { foreach(string path in paths) handles.Add(Pin(path,false)); return handles; }
        catch { foreach(var handle in handles) handle.Dispose(); throw; }
    }
    static FileStream LockedRead(string path)
    {
        var handle=CreateFile(path,0x80000000,1,IntPtr.Zero,3,0x00200000,IntPtr.Zero);
        try
        {
            FileInfoNative info;
            if(handle.IsInvalid || !GetFileInformationByHandle(handle,out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if((info.Attributes&0x410)!=0 || info.Links!=1) Fail("Нужен обычный файл без ссылок: "+path);
            return new FileStream(handle,FileAccess.Read);
        }
        catch { handle.Dispose(); throw; }
    }
    static string Digest(Stream stream, CancellationToken token, Action<string> progress, string phase)
    {
        using(var sha=SHA256.Create())
        {
            byte[] buffer=new byte[4*1024*1024]; int read; long count=0, next=0;
            while((read=stream.Read(buffer,0,buffer.Length))!=0)
            {
                token.ThrowIfCancellationRequested(); sha.TransformBlock(buffer,0,read,null,0); count+=read;
                if(count>=next) { progress(phase+": "+(count/(1024*1024))+" МиБ"); next=count+64L*1024*1024; }
            }
            sha.TransformFinalBlock(new byte[0],0,0);
            return BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant();
        }
    }
    static byte[] ReadSmall(ZipArchiveEntry entry, long limit)
    {
        if(entry.Length>limit) Fail("Слишком большой манифест.");
        using(var input=entry.Open()) using(var output=new MemoryStream())
        {
            byte[] buffer=new byte[8192]; int read;
            while((read=input.Read(buffer,0,(int)Math.Min(buffer.Length,limit-output.Length+1)))!=0)
            {
                if(output.Length+read>limit) Fail("Слишком большой манифест.");
                output.Write(buffer,0,read);
            }
            if(output.Length!=entry.Length) Fail("Размер манифеста не совпадает с ZIP-записью.");
            return output.ToArray();
        }
    }
    internal static void CheckSpace(long bytes, long available)
    { if(bytes<0 || available<bytes || available-bytes<256L*1024*1024) throw new IOException("Недостаточно места: нужны распакованные файлы и 256 МиБ запаса."); }
    static void StandaloneDisk(FileStream disk)
    {
        byte[] h=new byte[104]; disk.Position=0;
        if(disk.Read(h,0,h.Length)!=h.Length || h[0]!=81 || h[1]!=70 || h[2]!=73 || h[3]!=251
            || h[4]!=0 || h[5]!=0 || h[6]!=0 || (h[7]!=2 && h[7]!=3)
            || h.Skip(8).Take(12).Any(b=>b!=0) || h.Skip(24).Take(8).All(b=>b==0)
            || h.Skip(32).Take(4).Any(b=>b!=0) || (h[7]==3 && h.Skip(72).Take(8).Any(b=>b!=0)))
            Fail("Образ Windows/шлюза должен быть самостоятельным, чистым qcow2 без backing-файла.");
    }
    static void Publish(SafeFileHandle stage, string destination)
    {
        // Include the terminator required by the Win32 rename path conversion.
        byte[] name=Encoding.Unicode.GetBytes(@"\??\"+destination+"\0");
        int offset=Marshal.OffsetOf(typeof(RenameInfo),"Name").ToInt32();
        IntPtr buffer=Marshal.AllocHGlobal(offset+name.Length);
        try
        {
            var info=new RenameInfo { Replace=0, Root=IntPtr.Zero, Length=(uint)(name.Length-2) };
            Marshal.StructureToPtr(info,buffer,false); Marshal.Copy(name,0,IntPtr.Add(buffer,offset),name.Length);
            if(!SetFileInformationByHandle(stage,3,buffer,(uint)(offset+name.Length)))
            {
                int error=Marshal.GetLastWin32Error();
                throw new Win32Exception(error,"Не удалось опубликовать папку ("+error+"). Существующая установка не заменяется.");
            }
        }
        finally { Marshal.FreeHGlobal(buffer); }
    }
    static void DeleteHandle(SafeFileHandle handle)
    {
        IntPtr data=Marshal.AllocHGlobal(1);
        try { Marshal.WriteByte(data,1); if(!SetFileInformationByHandle(handle,4,data,1)) throw new Win32Exception(Marshal.GetLastWin32Error()); }
        finally { Marshal.FreeHGlobal(data); }
    }
    static void RemoveOwnTree(string root, SafeFileHandle rootHandle)
    {
        // Keep each directory pinned until deletion; never traverse a replacement link.
        foreach(string path in Directory.EnumerateFileSystemEntries(root))
        {
            var attributes=File.GetAttributes(path);
            if((attributes&FileAttributes.Directory)!=0 && (attributes&FileAttributes.ReparsePoint)==0)
            { using(var handle=Pin(path,true)) RemoveOwnTree(path,handle); }
            else
            {
                using(var handle=CreateFile(path,0x10000,0,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
                { if(handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error()); DeleteHandle(handle); }
            }
        }
        DeleteHandle(rootHandle);
    }
    internal static void Install(string zipPath, string destination, CancellationToken token, Action<string> progress)
    {
        zipPath=Path.GetFullPath(zipPath); destination=Path.GetFullPath(destination);
        string parent=Path.GetDirectoryName(destination);
        if(parent==null || !Directory.Exists(parent) || destination.StartsWith(@"\\",StringComparison.Ordinal)
            || !SafeName(Path.GetFileName(destination))) Fail("Выбери новую локальную папку внутри существующего каталога.");
        string stage=null; SafeFileHandle stagePin=null;
        var pins=PinParents(parent); var subdirectories=new List<SafeFileHandle>();
        try
        {
            if(File.Exists(destination) || Directory.Exists(destination)) Fail("Папка назначения уже существует. Выбери новую папку; пользовательские диски не заменяются.");
            token.ThrowIfCancellationRequested();
            foreach(string path in new [] {zipPath,zipPath+".sha256"})
                pins.AddRange(PinParents(Path.GetDirectoryName(path)));
            using(var input=LockedRead(zipPath))
            using(var sidecar=LockedRead(zipPath+".sha256"))
            {
                if(sidecar.Length>4096) Fail("Неверный файл SHA256.");
                string expected;
                using(var reader=new StreamReader(sidecar,Encoding.UTF8,true,4096,true)) expected=reader.ReadToEnd();
                var match=Regex.Match(expected,@"\A([a-f0-9]{64})  ([^\r\n]+)(\r?\n)?\z");
                if(!match.Success || match.Groups[2].Value!=Path.GetFileName(zipPath)) Fail("Неверный файл SHA256 или имя архива.");
                if(Digest(input,token,progress,"Проверка архива")!=match.Groups[1].Value) Fail("SHA256 архива не совпадает. Скачай комплект заново.");
                input.Position=0;
                using(var archive=new ZipArchive(input,ZipArchiveMode.Read,true))
                {
                    if(archive.Entries.Count<3 || archive.Entries.Count>40002) Fail("Неверное число файлов архива.");
                    var members=new Dictionary<string,ZipArchiveEntry>(StringComparer.OrdinalIgnoreCase);
                    foreach(var entry in archive.Entries)
                    {
                        string name=entry.FullName;
                        int kind=(entry.ExternalAttributes>>16)&0xF000;
                        if(!name.StartsWith(Prefix,StringComparison.Ordinal) || !SafeName(name) || members.ContainsKey(name)
                            || entry.Name.Length==0 || (kind!=0 && kind!=0x8000) || (entry.ExternalAttributes&0x400)!=0)
                            Fail("Небезопасный путь или ссылка в архиве.");
                        members.Add(name,entry);
                    }
                    ZipArchiveEntry manifestEntry=null, sumsEntry=null;
                    if(!members.TryGetValue(Prefix+"MANIFEST.json",out manifestEntry) || !members.TryGetValue(Prefix+"MANIFEST.sha256",out sumsEntry)) Fail("Отсутствует манифест архива.");
                    byte[] raw=ReadSmall(manifestEntry,16*1024*1024);
                    var serializer=new JavaScriptSerializer { MaxJsonLength=16*1024*1024, RecursionLimit=32 };
                    var manifest=serializer.DeserializeObject(Encoding.UTF8.GetString(raw)) as Dictionary<string,object>;
                    if(manifest==null || manifest.Count!=9 || !manifest.ContainsKey("files")
                        || Convert.ToInt32(manifest["schema"])!=2 || (string)manifest["kind"]!="public_distribution"
                        || !(manifest["encrypted_profiles_included"] is bool) || (bool)manifest["encrypted_profiles_included"]
                        || !(manifest["second_pc_tested"] is bool) || (bool)manifest["second_pc_tested"]
                        || !(manifest["vm_started_for_packaging"] is bool) || (bool)manifest["vm_started_for_packaging"]
                        || (string)manifest["credentials_portability"]!="No connection profiles included.") Fail("Нужен полный публичный архив версии 2 без профилей.");
                    var rows=manifest["files"] as object[];
                    if(rows==null || rows.Length+2!=members.Count) Fail("Состав архива отличается от манифеста.");
                    var wanted=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    var sums=new StringBuilder(); long total=0;
                    foreach(var value in rows)
                    {
                        var row=value as Dictionary<string,object>;
                        if(row==null || row.Count!=3 || !row.ContainsKey("path") || !row.ContainsKey("bytes") || !row.ContainsKey("sha256")) Fail("Неверная запись манифеста.");
                        string name=row["path"] as string, hash=row["sha256"] as string;
                        if(!Allowed(name) || !wanted.Add(name) || hash==null || !Regex.IsMatch(hash,@"\A[a-f0-9]{64}\z")
                            || !(row["bytes"] is int || row["bytes"] is long)) Fail("Недопустимый файл в публичном архиве.");
                        long size=Convert.ToInt64(row["bytes"]); ZipArchiveEntry member;
                        if(size<0 || !members.TryGetValue(Prefix+name,out member) || member.FullName!=Prefix+name || member.Length!=size) Fail("Размер или имя файла не совпадает с манифестом.");
                        total=checked(total+size); sums.Append(hash).Append("  ").Append(name).Append('\n');
                    }
                    if(Required.Any(name=>!wanted.Contains(name)) || wanted.Contains("runtime/gateway-recovery.json")!=wanted.Contains("runtime/gateway-recovery.zip")
                        || !(manifest["gateway_recovery_included"] is bool) || (bool)manifest["gateway_recovery_included"]!=wanted.Contains("runtime/gateway-recovery.zip")) Fail("Неполный комплект.");
                    using(var memory=new MemoryStream(raw)) sums.Append(Digest(memory,token,delegate(string v){},"Манифест")).Append("  MANIFEST.json\n");
                    if(Encoding.UTF8.GetString(ReadSmall(sumsEntry,16*1024*1024))!=sums.ToString()) Fail("Манифест SHA256 не совпадает.");
                    CheckSpace(total,new DriveInfo(Path.GetPathRoot(parent)).AvailableFreeSpace);
                    token.ThrowIfCancellationRequested();
                    string candidate=Path.Combine(parent,".windows-claude-setup-"+Guid.NewGuid().ToString("N"));
                    if(!CreateDirectory(candidate,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error(),"Не удалось создать временную папку.");
                    stage=candidate;
                    stagePin=Pin(stage,true);
                    var made=new HashSet<string>(StringComparer.OrdinalIgnoreCase) {stage};
                    foreach(var value in rows)
                    {
                        token.ThrowIfCancellationRequested();
                        var row=(Dictionary<string,object>)value; string name=(string)row["path"];
                        string output=Path.Combine(stage,name.Replace('/',Path.DirectorySeparatorChar));
                        var parents=new Stack<string>();
                        for(string dir=Path.GetDirectoryName(output); !made.Contains(dir); dir=Path.GetDirectoryName(dir)) parents.Push(dir);
                        foreach(string dir in parents)
                        {
                            if(!CreateDirectory(dir,IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error());
                            subdirectories.Add(Pin(dir,false)); made.Add(dir);
                        }
                        progress("Распаковка: "+name); token.ThrowIfCancellationRequested();
                        using(var source=members[Prefix+name].Open())
                        using(var file=new FileStream(output,FileMode.CreateNew,FileAccess.ReadWrite,FileShare.None))
                        using(var sha=SHA256.Create())
                        {
                            byte[] buffer=new byte[4*1024*1024]; int count; long written=0, next=64L*1024*1024;
                            while((count=source.Read(buffer,0,buffer.Length))!=0)
                            {
                                token.ThrowIfCancellationRequested(); written=checked(written+count);
                                if(written>Convert.ToInt64(row["bytes"])) Fail("Файл превысил заявленный размер.");
                                file.Write(buffer,0,count); sha.TransformBlock(buffer,0,count,null,0);
                                if(written>=next) { progress("Распаковка: "+name+" ("+(written/(1024*1024))+" МиБ)"); next=written+64L*1024*1024; }
                            }
                            sha.TransformFinalBlock(new byte[0],0,0);
                            if(written!=Convert.ToInt64(row["bytes"]) || BitConverter.ToString(sha.Hash).Replace("-", "").ToLowerInvariant()!=(string)row["sha256"]) Fail("SHA256 файла не совпадает: "+name);
                            if(name.EndsWith(".qcow2",StringComparison.Ordinal)) StandaloneDisk(file);
                            file.Flush(true);
                        }
                    }
                    token.ThrowIfCancellationRequested();
                    foreach(var handle in subdirectories) handle.Dispose(); subdirectories.Clear();
                    Publish(stagePin,destination); stagePin.Dispose(); stagePin=null; stage=null;
                }
            }
        }
        finally
        {
            foreach(var handle in subdirectories) handle.Dispose();
            try { if(stage!=null && stagePin!=null) RemoveOwnTree(stage,stagePin); }
            finally { if(stagePin!=null) stagePin.Dispose(); foreach(var handle in pins) handle.Dispose(); }
        }
    }
}

#if SETUP
internal sealed class PortableSetup : Form
{
    readonly TextBox folder=new TextBox { Dock=DockStyle.Top };
    readonly Label status=new Label { Dock=DockStyle.Top, AutoSize=true, ForeColor=NativeStyle.Ink };
    readonly Button install=new RoundedButton { Text="Установить", Primary=true, AutoSize=true, MinimumSize=new Size(120,44), Padding=new Padding(16,10,16,10) };
    readonly Button browse=new RoundedButton { Text="Выбрать папку", AutoSize=true, MinimumSize=new Size(120,44), Padding=new Padding(16,10,16,10) };
    readonly Button cancel=new RoundedButton { Text="Закрыть", AutoSize=true, MinimumSize=new Size(100,44), Padding=new Padding(16,10,16,10) };
    readonly CheckBox shortcut=new CheckBox { Text="Ярлык на рабочем столе", Checked=true, AutoSize=true };
    CancellationTokenSource cancellation;
    PortableSetup()
    {
        AutoScaleDimensions=new SizeF(96,96); AutoScaleMode=AutoScaleMode.Dpi;
        Font=new Font("Segoe UI",10); BackColor=NativeStyle.Background; ForeColor=NativeStyle.Ink;
        Text="Установка Windows Claude Portable"; ClientSize=new Size(740,480); MinimumSize=new Size(700,460);
        StartPosition=FormStartPosition.CenterScreen; Padding=new Padding(24);
        folder.Text=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Windows Claude Portable");
        var layout=new TableLayoutPanel { Dock=DockStyle.Fill, ColumnCount=1, RowCount=4, Margin=Padding.Empty };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var title=new Label { Text="Установка Windows Claude Portable", AutoSize=true, ForeColor=NativeStyle.Ink, Margin=new Padding(0,0,0,16) };
        var card=new RoundedFlowPanel { Dock=DockStyle.Fill, AutoSize=true, AutoSizeMode=AutoSizeMode.GrowAndShrink, FlowDirection=FlowDirection.TopDown, WrapContents=false, BackColor=Color.White, Padding=new Padding(16), Margin=new Padding(0,0,0,16) };
        var top=new Label { Text="Новая папка установки (существующие папки не заменяются):", AutoSize=true, ForeColor=NativeStyle.Ink, Margin=new Padding(0,0,0,10) };
        folder.Dock=DockStyle.None; folder.Margin=new Padding(0,0,0,12);
        shortcut.ForeColor=NativeStyle.Ink; shortcut.Margin=Padding.Empty;
        card.Controls.AddRange(new Control[] {top,folder,shortcut});
        card.SizeChanged+=delegate { int width=Math.Max(1,card.ClientSize.Width-card.Padding.Horizontal); top.MaximumSize=new Size(width,0); folder.Width=width; shortcut.MaximumSize=new Size(width,0); };
        var progress=new RoundedPanel { Dock=DockStyle.Fill, BackColor=Color.White, AutoScroll=true, Padding=new Padding(16), Margin=new Padding(0,0,0,16), MinimumSize=new Size(0,100) };
        progress.Controls.Add(status);
        progress.SizeChanged+=delegate { status.MaximumSize=new Size(Math.Max(1,progress.ClientSize.Width-progress.Padding.Horizontal-SystemInformation.VerticalScrollBarWidth),0); };
        var buttons=new FlowLayoutPanel { Dock=DockStyle.Fill, AutoSize=true, FlowDirection=FlowDirection.RightToLeft, WrapContents=false, Margin=Padding.Empty };
        buttons.Controls.AddRange(new Control[] {cancel,install,browse});
        layout.Controls.Add(title,0,0); layout.Controls.Add(card,0,1); layout.Controls.Add(progress,0,2); layout.Controls.Add(buttons,0,3);
        Controls.Add(layout);
        status.Text="Рядом с Setup.exe должны находиться Windows-Claude.zip и Windows-Claude.zip.sha256. Архив распакуется в новую папку.";
        browse.Click+=delegate { using(var picker=new FolderBrowserDialog { Description="Выбери родительскую папку; внутри будет создана Windows Claude Portable" }) if(picker.ShowDialog()==DialogResult.OK) folder.Text=Path.Combine(picker.SelectedPath,"Windows Claude Portable"); };
        cancel.Click+=delegate { if(cancellation!=null) { cancellation.Cancel(); status.Text="Отмена: ожидаю завершения текущего чтения…"; } else Close(); };
        FormClosing+=delegate(object sender,FormClosingEventArgs e) { if(cancellation!=null) { cancellation.Cancel(); e.Cancel=true; } };
        install.Click+=delegate { BeginInstall(); };
    }
    void BeginInstall()
    {
        string destination=folder.Text; bool makeShortcut=shortcut.Checked;
        cancellation=new CancellationTokenSource(); var token=cancellation.Token;
        install.Enabled=browse.Enabled=folder.Enabled=shortcut.Enabled=false; cancel.Text="Отмена";
        var thread=new Thread(delegate()
        {
            string result;
            try
            {
                SetupInstaller.Install(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"Windows-Claude.zip"),destination,token,
                    delegate(string value) { BeginInvoke((Action)delegate {status.Text=value;}); });
                result="Готово: "+destination+". Запусти Launcher.exe в этой папке.";
                if(makeShortcut) { try { MakeShortcut(Path.GetFullPath(destination)); } catch(Exception) { result+=" Ярлык не создан; открой Launcher.exe вручную."; } }
            }
            catch(OperationCanceledException) { result="Установка отменена. Папка назначения не опубликована."; }
            catch(Exception error) { result="Установка остановлена: "+error.Message; }
            BeginInvoke((Action)delegate { cancellation.Dispose(); cancellation=null; install.Enabled=browse.Enabled=folder.Enabled=shortcut.Enabled=true; cancel.Text="Закрыть"; status.Text=result; });
        });
        thread.IsBackground=true; thread.SetApartmentState(ApartmentState.STA); thread.Start();
    }
    static void MakeShortcut(string destination)
    {
        string desktop=Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
        string target=Path.Combine(desktop,"Windows Claude Portable.lnk"), temporary=Path.Combine(desktop,Guid.NewGuid().ToString("N")+".lnk");
        if(File.Exists(target)) throw new IOException("Ярлык уже существует.");
        object shell=null, link=null;
        var pins=SetupInstaller.PinParents(desktop);
        try
        {
            shell=Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            link=shell.GetType().InvokeMember("CreateShortcut",System.Reflection.BindingFlags.InvokeMethod,null,shell,new object[] {temporary});
            link.GetType().InvokeMember("TargetPath",System.Reflection.BindingFlags.SetProperty,null,link,new object[] {Path.Combine(destination,"Launcher.exe")});
            link.GetType().InvokeMember("WorkingDirectory",System.Reflection.BindingFlags.SetProperty,null,link,new object[] {destination});
            link.GetType().InvokeMember("Save",System.Reflection.BindingFlags.InvokeMethod,null,link,null);
            File.Move(temporary,target);
        }
        finally { if(link!=null) Marshal.FinalReleaseComObject(link); if(shell!=null) Marshal.FinalReleaseComObject(shell); if(File.Exists(temporary)) File.Delete(temporary); foreach(var handle in pins) handle.Dispose(); }
    }
    [STAThread] public static void Main() { Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false); Application.Run(new PortableSetup()); }
}
#endif
#endif

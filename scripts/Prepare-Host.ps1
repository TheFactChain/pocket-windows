param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Elevated,
    [string]$OperationId
)

$script:HostPreparationSourceRoot = Split-Path -Parent $PSScriptRoot

function Initialize-HostPreparationNative {
    if ('PortableHostPreparationNative' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public sealed class HostPreparationActiveLease : IDisposable {
    public readonly FileStream Stream;
    public readonly bool Created;
    public HostPreparationActiveLease(FileStream stream, bool created) { Stream=stream; Created=created; }
    public string Read() {
        Stream.Position=0;
        using(StreamReader reader=new StreamReader(Stream,new UTF8Encoding(false,true),true,1024,true)) { return reader.ReadToEnd(); }
    }
    public void Write(string value) {
        byte[] bytes=new UTF8Encoding(false,true).GetBytes(value);
        if(bytes.Length>8192) throw new IOException("Invalid active operation");
        Stream.Position=0;Stream.SetLength(0);Stream.Write(bytes,0,bytes.Length);Stream.Flush(true);
    }
    public void Dispose() { Stream.Dispose(); }
}
public static class PortableHostPreparationNative {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct VersionInfo { public int Size, Major, Minor, Build, Platform; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string Text; public ushort SPmajor, SPminor, Suite; public byte ProductType, Reserved; }
    [StructLayout(LayoutKind.Sequential)]
    struct FileInfo { public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write; public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow; }
    [StructLayout(LayoutKind.Sequential)]
    struct BootInfo { public Guid Identifier; public int FirmwareType; public ulong Flags; }
    [DllImport("ntdll.dll", CharSet=CharSet.Unicode)] static extern int RtlGetVersion(ref VersionInfo info);
    [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int kind, ref BootInfo info, uint size, out uint written);
    [DllImport("kernel32.dll")] static extern bool IsWow64Process2(IntPtr process, out ushort machine, out ushort native);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("WinHvPlatform.dll")] static extern int WHvGetCapability(int code, out int present, uint size, out uint written);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern uint GetWindowsDirectory(StringBuilder path, uint size);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CreateDirectory(string path, IntPtr security);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInfo info);
    public static bool Supported() {
        VersionInfo version = new VersionInfo(); version.Size = Marshal.SizeOf(version);
        ushort machine, native;
        return IntPtr.Size == 8 && RtlGetVersion(ref version) == 0 && version.Major == 10 && version.Build >= 22000 && version.ProductType == 1 && IsWow64Process2(GetCurrentProcess(), out machine, out native) && native == 0x8664;
    }
    public static bool Whpx() {
        try { int present; uint written; return WHvGetCapability(0, out present, 4, out written) == 0 && present != 0; }
        catch (DllNotFoundException) { return false; } catch (EntryPointNotFoundException) { return false; }
    }
    public static string BootId() {
        try {
            BootInfo info=new BootInfo();uint written;
            // SystemBootEnvironmentInformation contains the kernel boot GUID, not a wall-clock estimate.
            if(NtQuerySystemInformation(90,ref info,(uint)Marshal.SizeOf(info),out written)!=0 || written<16 || info.Identifier==Guid.Empty) return null;
            return info.Identifier.ToString("N");
        } catch(EntryPointNotFoundException) { return null; }
    }
    public static string WindowsDirectory() {
        StringBuilder path = new StringBuilder(32768);
        uint size = GetWindowsDirectory(path, (uint)path.Capacity);
        if (size == 0 || size >= path.Capacity) throw new IOException("System directory unavailable");
        return path.ToString();
    }
    public static SafeFileHandle PinDirectory(string path) {
        SafeFileHandle handle = CreateFile(path, 0x80, 3, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero);
        FileInfo info;
        if (handle.IsInvalid || !GetFileInformationByHandle(handle, out info) || (info.Attributes & 0x400) != 0 || (info.Attributes & 0x10) == 0) {
            handle.Dispose(); throw new IOException("Unsafe directory");
        }
        return handle;
    }
    public static void NewDirectory(string path) {
        if (!CreateDirectory(path, IntPtr.Zero)) throw new IOException("Operation directory already exists or is unavailable");
    }
    public static string ReadPlainFile(string path) {
        SafeFileHandle handle = CreateFile(path, 0x80000000, 0, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero);
        FileInfo info;
        if (handle.IsInvalid || !GetFileInformationByHandle(handle, out info) || (info.Attributes & 0x410) != 0 || info.Links != 1 || info.SizeHigh != 0 || info.SizeLow > 8192) {
            handle.Dispose(); throw new IOException("Invalid operation result");
        }
        using (FileStream stream = new FileStream(handle, FileAccess.Read))
        using (StreamReader reader = new StreamReader(stream, new UTF8Encoding(false, true), true)) { return reader.ReadToEnd(); }
    }
    public static HostPreparationActiveLease OpenActiveOperation(string path) {
        SafeFileHandle handle=CreateFile(path,0xC0000000,0,IntPtr.Zero,4,0x00200000,IntPtr.Zero);
        int error=Marshal.GetLastWin32Error();
        if(handle.IsInvalid) { handle.Dispose();throw new System.ComponentModel.Win32Exception(error); }
        FileInfo info;
        if(!GetFileInformationByHandle(handle,out info) || (info.Attributes&0x410)!=0 || info.Links!=1 || info.SizeHigh!=0 || info.SizeLow>8192) {
            handle.Dispose();throw new IOException("Unsafe active operation file");
        }
        return new HostPreparationActiveLease(new FileStream(handle,FileAccess.ReadWrite),error!=183);
    }
}
'@
}

function New-HostPreparationResult([string]$Code) {
    $messages = @{
        ready = 'This PC is ready to run the virtual machines.'
        restart_required = 'The virtualization component is enabled. Restart Windows when convenient, then check this PC again.'
        uac_cancelled = 'Administrator approval was cancelled. The PC preparation was not started.'
        outcome_unknown = 'Preparation is still pending or its outcome is unknown. Check again later. If this persists, restart this PC when convenient, then retry preparation. Windows will not restart automatically.'
        unsupported_host = 'Windows 11 x64 and Windows PowerShell 5.1 are required.'
        unsafe_path = 'The portable directory or operation files are unavailable or contain unsupported links.'
        dism_failed = 'Windows could not enable Hypervisor Platform. Check Windows component servicing and try again.'
        feature_unavailable = 'Hypervisor Platform is still unavailable. Check virtualization support and restart Windows if required.'
        invalid_result = 'The preparation result is missing or invalid. Check this PC again before continuing.'
        error = 'PC preparation could not be completed. Check permissions and retry diagnostics.'
    }
    if (-not $messages.ContainsKey($Code)) { $Code = 'error' }
    return [ordered]@{schema=1;ok=($Code -in @('ready','restart_required'));code=$Code;message=$messages[$Code];restart_required=($Code -eq 'restart_required')}
}

function Get-HostPreparationFacts {
    Initialize-HostPreparationNative
    return @{supported=($PSVersionTable.PSVersion.Major -eq 5 -and [PortableHostPreparationNative]::Supported());whpx=[PortableHostPreparationNative]::Whpx()}
}

function Get-HostPreparationBootId {
    try { Initialize-HostPreparationNative; return [PortableHostPreparationNative]::BootId() }
    catch { return $null }
}

function Test-HostPreparationAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { return ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
    finally { $identity.Dispose() }
}

function Open-HostPreparationPath([string]$Root, [string]$OperationId, [switch]$Create, [switch]$StateOnly) {
    Initialize-HostPreparationNative
    $handles = [Collections.Generic.List[IDisposable]]::new()
    try {
        if (-not [IO.Path]::IsPathRooted($Root) -or $Root.StartsWith('\\') -or @($Root.Split([char[]]'\/') | Where-Object {$_ -eq '..'}).Count) { throw 'Unsafe root' }
        $full = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        $expected = [IO.Path]::GetFullPath($script:HostPreparationSourceRoot).TrimEnd('\')
        if (-not $full.Equals($expected,[StringComparison]::OrdinalIgnoreCase) -or (-not $StateOnly -and $OperationId -cnotmatch '^[0-9a-f]{32}$')) { throw 'Unexpected operation' }
        $ancestors = [Collections.Generic.List[string]]::new()
        $current = $full
        while ($current) { $ancestors.Insert(0,$current);$current=[IO.Path]::GetDirectoryName($current) }
        foreach ($path in $ancestors) { $handles.Add([PortableHostPreparationNative]::PinDirectory($path)) }
        $state = Join-Path $full 'state'
        if ($Create -and -not [IO.Directory]::Exists($state)) { [PortableHostPreparationNative]::NewDirectory($state) }
        $handles.Add([PortableHostPreparationNative]::PinDirectory($state))
        if ($StateOnly) { return @{root=$full;state=$state;handles=$handles} }
        $operation = Join-Path $state ('host-prepare-'+$OperationId)
        if ($Create) { [PortableHostPreparationNative]::NewDirectory($operation) }
        $handles.Add([PortableHostPreparationNative]::PinDirectory($operation))
        return @{root=$full;operation=$operation;handles=$handles}
    } catch {
        foreach ($handle in $handles) { $handle.Dispose() }
        throw
    }
}

function Close-HostPreparationPath($Context) {
    if ($Context) { foreach ($handle in $Context.handles) { $handle.Dispose() } }
}

function Write-HostPreparationFile([string]$Path, $Value) {
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 5 -Compress))
    $file = [IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $file.Write($bytes,0,$bytes.Length);$file.Flush($true) } finally { $file.Dispose() }
}

function Test-HostPreparationFields($Value, [string[]]$Fields) {
    if ($null -eq $Value -or $Value -isnot [pscustomobject]) { return $false }
    $names = @($Value.PSObject.Properties.Name)
    return $names.Count -eq $Fields.Count -and @($names | Where-Object {$_ -cnotin $Fields}).Count -eq 0
}

function Read-HostPreparationResult($Context, [string]$OperationId) {
    try {
        $envelope = [PortableHostPreparationNative]::ReadPlainFile((Join-Path $Context.operation 'result.json')) | ConvertFrom-Json
        if (-not (Test-HostPreparationFields $envelope @('operation_id','result')) -or $envelope.operation_id -cne $OperationId) { throw 'Invalid envelope' }
        $result = $envelope.result
        if (-not (Test-HostPreparationFields $result @('schema','ok','code','message','restart_required')) -or $result.schema -isnot [int] -or $result.schema -ne 1 -or $result.ok -isnot [bool] -or $result.restart_required -isnot [bool]) { throw 'Invalid result' }
        $expected = New-HostPreparationResult $result.code
        if ($result.code -cne $expected.code -or $result.ok -ne $expected.ok -or $result.restart_required -ne $expected.restart_required -or $result.message -cne $expected.message) { throw 'Unexpected result' }
        return $expected
    } catch { return New-HostPreparationResult 'invalid_result' }
}

function Invoke-HostPreparationDism {
    Initialize-HostPreparationNative
    $dism = Join-Path ([PortableHostPreparationNative]::WindowsDirectory()) 'System32/dism.exe'
    $process = Start-Process -FilePath $dism -ArgumentList @('/Online','/Enable-Feature','/FeatureName:HypervisorPlatform','/All','/NoRestart') -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit(150000)) { return $null }
        return $process.ExitCode
    } finally { $process.Dispose() }
}

function Invoke-HostPreparationElevated([string]$Root, [string]$OperationId) {
    $context = $null
    try {
        $context = Open-HostPreparationPath -Root $Root -OperationId $OperationId
        $request = [PortableHostPreparationNative]::ReadPlainFile((Join-Path $context.operation 'request.json')) | ConvertFrom-Json
        if (-not (Test-HostPreparationFields $request @('schema','operation_id')) -or $request.schema -isnot [int] -or $request.schema -ne 1 -or $request.operation_id -cne $OperationId) { throw 'Invalid request' }
        if (-not (Test-HostPreparationAdministrator)) { throw 'Administrator required' }
        $facts = Get-HostPreparationFacts
        if (-not $facts.supported) { $result = New-HostPreparationResult 'unsupported_host' }
        elseif ($facts.whpx) { $result = New-HostPreparationResult 'ready' }
        else {
            try {
                $exitCode = Invoke-HostPreparationDism
                if ($null -eq $exitCode) { $result = New-HostPreparationResult 'outcome_unknown' }
                elseif ($exitCode -eq 3010) { $result = New-HostPreparationResult 'restart_required' }
                elseif ($exitCode -ne 0) { $result = New-HostPreparationResult 'dism_failed' }
                elseif ((Get-HostPreparationFacts).whpx) { $result = New-HostPreparationResult 'ready' }
                else { $result = New-HostPreparationResult 'feature_unavailable' }
            } catch { $result = New-HostPreparationResult 'dism_failed' }
        }
        Write-HostPreparationFile (Join-Path $context.operation 'result.json') @{operation_id=$OperationId;result=$result}
        return $result
    } catch { return New-HostPreparationResult 'unsafe_path' }
    finally { Close-HostPreparationPath $context }
}

function Start-ElevatedHostPreparation([string]$Root, [string]$OperationId) {
    Initialize-HostPreparationNative
    $powershell = Join-Path ([PortableHostPreparationNative]::WindowsDirectory()) 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $source = Join-Path $Root 'scripts/Prepare-Host.ps1'
    if ($Root.Contains('"')) { throw 'Invalid root' }
    $arguments = '-NoProfile -NonInteractive -File "'+$source+'" -Root "'+$Root+'" -Elevated -OperationId '+$OperationId
    $runner = [PowerShell]::Create()
    $null = $runner.AddScript('param($Executable,$Arguments) Start-Process -FilePath $Executable -ArgumentList $Arguments -Verb RunAs -WindowStyle Hidden -PassThru').AddArgument($powershell).AddArgument($arguments)
    $pending = $runner.BeginInvoke()
    # Disposing a pending RunAs invocation could wait on the unanswered UAC prompt.
    if (-not $pending.AsyncWaitHandle.WaitOne(30000)) { return $null }
    try {
        $processes = $runner.EndInvoke($pending)
        if ($runner.HadErrors) { throw $runner.Streams.Error[0].Exception }
        if ($processes.Count -ne 1) { throw 'No elevated process' }
        return $processes[0]
    } finally { $runner.Dispose() }
}

function Wait-HostPreparationProcess($Process) { return $Process.WaitForExit(150000) }

function Get-HostPreparationNativeError($Exception) {
    while ($Exception) {
        if ($Exception -is [ComponentModel.Win32Exception]) { return $Exception.NativeErrorCode }
        $Exception = $Exception.InnerException
    }
    return 0
}

function Invoke-HostPreparation([string]$Root) {
    $context = $null
    $stateContext = $null
    $active = $null
    try {
        $facts = Get-HostPreparationFacts
        if (-not $facts.supported) { return New-HostPreparationResult 'unsupported_host' }
        if ($facts.whpx) { return New-HostPreparationResult 'ready' }
        $bootId = Get-HostPreparationBootId
        if ($bootId -isnot [string] -or $bootId -cnotmatch '^[a-f0-9]{32}$') { $bootId = $null }
        try {
            $stateContext = Open-HostPreparationPath -Root $Root -StateOnly -Create
            try { $active = [PortableHostPreparationNative]::OpenActiveOperation((Join-Path $stateContext.state 'host-prepare-active.json')) }
            catch {
                if ((Get-HostPreparationNativeError $_.Exception) -in @(32,33)) { return New-HostPreparationResult 'outcome_unknown' }
                return New-HostPreparationResult 'unsafe_path'
            }
            if (-not $active.Created) {
                $previous = $active.Read() | ConvertFrom-Json
                $fields = @('schema','kind','operation_id')
                if ($previous.PSObject.Properties.Name -ccontains 'boot_id') { $fields += 'boot_id' }
                if (-not (Test-HostPreparationFields $previous $fields) -or $previous.schema -isnot [int] -or $previous.schema -ne 1 -or $previous.kind -cne 'host-preparation' -or $previous.operation_id -cnotmatch '^[0-9a-f]{32}$' -or ($null -ne $previous.boot_id -and ($previous.boot_id -isnot [string] -or $previous.boot_id -cnotmatch '^[a-f0-9]{32}$'))) { throw 'Invalid active operation' }
                $newBoot = $bootId -and $previous.boot_id -and $bootId -cne $previous.boot_id
                if (-not $newBoot) {
                    $context = Open-HostPreparationPath -Root $Root -OperationId $previous.operation_id
                    if (-not [IO.File]::Exists((Join-Path $context.operation 'result.json'))) { return New-HostPreparationResult 'outcome_unknown' }
                    $priorResult = Read-HostPreparationResult $context $previous.operation_id
                    if ($priorResult.code -notin @('uac_cancelled','dism_failed','feature_unavailable','unsupported_host','unsafe_path','error','ready')) { return $priorResult }
                    Close-HostPreparationPath $context
                    $context = $null
                }
            }
            $operationId = [Guid]::NewGuid().ToString('N')
            $context = Open-HostPreparationPath -Root $Root -OperationId $operationId -Create
            Write-HostPreparationFile (Join-Path $context.operation 'request.json') @{schema=1;operation_id=$operationId}
            # The exclusively opened pointer survives panel/process restarts; a torn write blocks a new launch.
            $active.Write((@{schema=1;kind='host-preparation';operation_id=$operationId;boot_id=$bootId} | ConvertTo-Json -Compress))
        } catch { return New-HostPreparationResult 'unsafe_path' }
        try { $process = Start-ElevatedHostPreparation -Root $context.root -OperationId $operationId }
        catch {
            if ((Get-HostPreparationNativeError $_.Exception) -eq 1223) {
                $cancelled = New-HostPreparationResult 'uac_cancelled'
                Write-HostPreparationFile (Join-Path $context.operation 'result.json') @{operation_id=$operationId;result=$cancelled}
                return $cancelled
            }
            return New-HostPreparationResult 'outcome_unknown'
        }
        if ($null -eq $process -or -not (Wait-HostPreparationProcess $process)) { return New-HostPreparationResult 'outcome_unknown' }
        return Read-HostPreparationResult $context $operationId
    } catch { return New-HostPreparationResult 'error' }
    finally {
        Close-HostPreparationPath $context
        if ($active) { $active.Dispose() }
        Close-HostPreparationPath $stateContext
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    if ($Elevated) { $result = Invoke-HostPreparationElevated -Root $Root -OperationId $OperationId }
    else { $result = Invoke-HostPreparation -Root $Root }
    $result | ConvertTo-Json -Compress
    if ($result.ok) { exit 0 } else { exit 1 }
}

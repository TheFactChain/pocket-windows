using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;
using SMBLibrary;

internal sealed partial class ConfinedFileStore
{
    private readonly object requestGate=new object();
    private readonly List<ChangeRequest> notifications=new List<ChangeRequest>();
    private const int MaxPendingNotifications=256;
    private sealed class ChangeRequest
    {
        internal OpenHandle Owner;
        internal SafeFileHandle Directory;
        internal ManualResetEvent Event;
        internal RegisteredWaitHandle Wait;
        internal IntPtr Buffer,Overlapped;
        internal int Size;
        internal bool Queued,Published,Completed,Cleanup;
        internal OnNotifyChangeCompleted Callback;
        internal object Context;
        internal void Dispose()
        {
            if(Wait!=null){Wait.Unregister(null);Wait=null;}
            if(Overlapped!=IntPtr.Zero){Marshal.FreeHGlobal(Overlapped);Overlapped=IntPtr.Zero;}
            if(Buffer!=IntPtr.Zero){Marshal.FreeHGlobal(Buffer);Buffer=IntPtr.Zero;}
            if(Directory!=null)Directory.Dispose();if(Event!=null)Event.Dispose();
        }
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct ChangeOverlapped
    {
        internal UIntPtr Internal,InternalHigh;
        internal uint Offset,OffsetHigh;
        internal IntPtr Event;
    }
    [DllImport("kernel32.dll",SetLastError=true)]
    private static extern bool ReadDirectoryChangesW(SafeFileHandle directory,IntPtr buffer,uint size,bool tree,uint filter,IntPtr bytes,IntPtr overlapped,IntPtr completion);
    [DllImport("kernel32.dll",SetLastError=true)]
    private static extern bool CancelIoEx(SafeFileHandle directory,IntPtr overlapped);
    [DllImport("kernel32.dll",SetLastError=true)]
    private static extern bool GetOverlappedResult(SafeFileHandle directory,IntPtr overlapped,out uint bytes,bool wait);
    private static NTStatus Win32Status(int error)
    {
        switch(error)
        {
            case 0:return NTStatus.STATUS_SUCCESS;
            case 1:return NTStatus.STATUS_INVALID_DEVICE_REQUEST;
            case 2:return NTStatus.STATUS_OBJECT_NAME_NOT_FOUND;
            case 3:return NTStatus.STATUS_OBJECT_PATH_NOT_FOUND;
            case 5:return NTStatus.STATUS_ACCESS_DENIED;
            case 6:return NTStatus.STATUS_INVALID_HANDLE;
            case 8:case 14:case 1450:return NTStatus.STATUS_INSUFFICIENT_RESOURCES;
            case 32:return NTStatus.STATUS_SHARING_VIOLATION;
            case 33:return NTStatus.STATUS_FILE_LOCK_CONFLICT;
            case 50:return NTStatus.STATUS_NOT_SUPPORTED;
            case 87:return NTStatus.STATUS_INVALID_PARAMETER;
            case 111:case 234:return NTStatus.STATUS_BUFFER_OVERFLOW;
            case 123:case 206:return NTStatus.STATUS_OBJECT_NAME_INVALID;
            case 145:return NTStatus.STATUS_DIRECTORY_NOT_EMPTY;
            case 183:return NTStatus.STATUS_OBJECT_NAME_COLLISION;
            case 267:return NTStatus.STATUS_NOT_A_DIRECTORY;
            case 995:return NTStatus.STATUS_CANCELLED;
            case 996:case 997:return NTStatus.STATUS_PENDING;
            case 1022:return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            case 1168:return NTStatus.STATUS_NOT_FOUND;
            default:return (NTStatus)0xC0000001; // STATUS_UNSUCCESSFUL: an unknown Win32 error must remain a failure.
        }
    }

    public NTStatus NotifyChange(out object request,object handle,NotifyChangeFilter filter,bool tree,int size,OnNotifyChangeCompleted callback,object context)
    {
        request=null;if(size<=0||size>65536||callback==null)return NTStatus.STATUS_INVALID_PARAMETER;
        ChangeRequest pending=null;
        NTStatus status=UseHandle(handle,native=>BeginNotification((OpenHandle)handle,filter,tree,size,callback,context,out pending));
        request=pending;return status;
    }
    private NTStatus BeginNotification(OpenHandle owner,NotifyChangeFilter filter,bool tree,int size,OnNotifyChangeCompleted callback,object context,out ChangeRequest pending)
    {
        pending=null;FileIdentity original;
        if(!GetFileInformationByHandle((IntPtr)owner.Native,out original))return Win32Status(Marshal.GetLastWin32Error());
        if((original.Attributes&0x10)==0)return NTStatus.STATUS_NOT_A_DIRECTORY;
        FileInformation access;NTStatus status=inner.GetFileInformation(out access,owner.Native,FileInformationClass.FileAccessInformation);
        if(status!=NTStatus.STATUS_SUCCESS)return status;
        if((((uint)((FileAccessInformation)access).AccessFlags)&1)==0)return NTStatus.STATUS_ACCESS_DENIED;
        List<SafeFileHandle> pins;status=PinParents(owner.Path,out pins);
        try
        {
            if(status!=NTStatus.STATUS_SUCCESS)return status;
            ChangeRequest operation=new ChangeRequest{Owner=owner,Size=size,Callback=callback,Context=context};
            bool waiterRegistered=false;
            try
            {
                operation.Directory=PinFile(Path.Combine(root,owner.Path),0x81,7,IntPtr.Zero,3,0x42200000,IntPtr.Zero);
                if(operation.Directory.IsInvalid)return Win32Status(Marshal.GetLastWin32Error());
                FileIdentity reopened;
                if(!GetFileInformationByHandle(operation.Directory.DangerousGetHandle(),out reopened))return Win32Status(Marshal.GetLastWin32Error());
                if((reopened.Attributes&0x400)!=0||(reopened.Attributes&0x10)==0||reopened.Volume!=original.Volume||reopened.IndexHigh!=original.IndexHigh||reopened.IndexLow!=original.IndexLow)return NTStatus.STATUS_ACCESS_DENIED;
                operation.Event=new ManualResetEvent(false);operation.Buffer=Marshal.AllocHGlobal(size);operation.Overlapped=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(ChangeOverlapped)));
                Marshal.StructureToPtr(new ChangeOverlapped{Event=operation.Event.SafeWaitHandle.DangerousGetHandle()},operation.Overlapped,false);
                lock(requestGate)
                {
                    if(notifications.Count>=MaxPendingNotifications)return NTStatus.STATUS_INSUFFICIENT_RESOURCES;
                    // Registered waits share CLR wait threads; the gate publishes I/O before its completion callback.
                    operation.Wait=ThreadPool.RegisterWaitForSingleObject(operation.Event,(value,timedOut)=>CompleteNotification((ChangeRequest)value),operation,Timeout.Infinite,true);
                    waiterRegistered=true;
                    bool initiated=ReadDirectoryChangesW(operation.Directory,operation.Buffer,(uint)size,tree,(uint)filter,IntPtr.Zero,operation.Overlapped,IntPtr.Zero);
                    int error=initiated?0:Marshal.GetLastWin32Error();
                    if(!initiated&&error!=997){operation.Event.Set();return Win32Status(error);}
                    operation.Queued=true;notifications.Add(operation);operation.Published=true;pending=operation;return NTStatus.STATUS_PENDING;
                }
            }
            catch
            {
                if(waiterRegistered)lock(requestGate){if(operation.Queued)CancelIoEx(operation.Directory,operation.Overlapped);else operation.Event.Set();}
                throw;
            }
            finally{if(!waiterRegistered)operation.Dispose();}
        }
        finally{Release(pins);}
    }
    private void CompleteNotification(ChangeRequest operation)
    {
        NTStatus status=NTStatus.STATUS_CANCELLED;byte[] data=new byte[0];bool deliver;
        lock(requestGate)
        {
            deliver=operation.Published;
            try
            {
                if(operation.Queued)
                {
                    uint count;
                    if(!GetOverlappedResult(operation.Directory,operation.Overlapped,out count,false))status=Win32Status(Marshal.GetLastWin32Error());
                    else if(count==0)status=NTStatus.STATUS_NOTIFY_ENUM_DIR;
                    else if(count>operation.Size)status=NTStatus.STATUS_DATA_ERROR;
                    else{data=new byte[count];Marshal.Copy(operation.Buffer,data,0,(int)count);status=NTStatus.STATUS_SUCCESS;}
                    if(operation.Cleanup){status=NTStatus.STATUS_NOTIFY_CLEANUP;data=new byte[0];}
                }
            }
            finally{operation.Completed=true;}
        }
        try
        {
            if(status==NTStatus.STATUS_SUCCESS)status=FilterNotification(operation.Directory.DangerousGetHandle(),data,out data);
        }
        finally
        {
            lock(requestGate)
            {
                // Keep filtering completions within the same pending-resource bound.
                if(operation.Cleanup){status=NTStatus.STATUS_NOTIFY_CLEANUP;data=new byte[0];}
                notifications.Remove(operation);operation.Dispose();
            }
        }
        if(deliver)operation.Callback(status,data,operation.Context);
    }

    private sealed class NotifyEntry
    {
        internal uint Action;
        internal string Name;
        internal byte[] Bytes;
    }
    private enum NotifyVisibility { Allowed,Protected,Uncertain }
    private NTStatus FilterNotification(IntPtr watched,byte[] input,out byte[] output)
    {
        output=new byte[0];List<SafeFileHandle> rootPins=null,watchPins=null;
        try
        {
            if(input==null||input.Length<12||input.Length>65536)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            var entries=new List<NotifyEntry>();var utf16=new UnicodeEncoding(false,false,true);int offset=0;
            while(true)
            {
                if(entries.Count>=4096||input.Length-offset<12)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                uint next=BitConverter.ToUInt32(input,offset),action=BitConverter.ToUInt32(input,offset+4),length=BitConverter.ToUInt32(input,offset+8);
                if(action<1||action>5||length==0||(length&1)!=0||length>(uint)(input.Length-offset-12))return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                string name=utf16.GetString(input,offset+12,(int)length),relative;
                if(name[0]=='\\'||!ValidPath(name,out relative)||name.Split('\\').Length>256)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                entries.Add(new NotifyEntry{Action=action,Name=name,Bytes=utf16.GetBytes(name)});
                int size=12+(int)length;
                if(next==0)
                {if(input.Length-offset-size>3)return NTStatus.STATUS_NOTIFY_ENUM_DIR;break;}
                if(next!=(uint)((size+3)&~3)||next>(uint)(input.Length-offset-12))return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                offset+=(int)next;
            }
            if(PinParents("",out rootPins)!=NTStatus.STATUS_SUCCESS||CheckHandle(watched)!=NTStatus.STATUS_SUCCESS)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            string canonicalRoot=HandlePath(rootPins[rootPins.Count-1].DangerousGetHandle()),current=HandlePath(watched);
            string prefix=canonicalRoot.EndsWith("\\",StringComparison.Ordinal)?canonicalRoot:canonicalRoot+"\\";
            if(!String.Equals(current,canonicalRoot,StringComparison.OrdinalIgnoreCase)&&!current.StartsWith(prefix,StringComparison.OrdinalIgnoreCase))return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            string watchRelative=String.Equals(current,canonicalRoot,StringComparison.OrdinalIgnoreCase)?"":current.Substring(prefix.Length);
            if(watchRelative.Split('\\').Length>256||PinParents(Path.Combine(watchRelative,"__notify_pin__"),out watchPins)!=NTStatus.STATUS_SUCCESS)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            FileIdentity observed,pinned;
            if(!GetFileInformationByHandle(watched,out observed)||!GetFileInformationByHandle(watchPins[watchPins.Count-1].DangerousGetHandle(),out pinned)||Key(observed)!=Key(pinned)||!String.Equals(current,HandlePath(watched),StringComparison.OrdinalIgnoreCase))return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            var visible=new List<NotifyEntry>();
            for(int index=0;index<entries.Count;index++)
            {
                NotifyEntry entry=entries[index];
                if(entry.Action==5)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                if(entry.Action==4)
                {
                    if(index+1>=entries.Count||entries[index+1].Action!=5)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                    NotifyEntry renamed=entries[++index];
                    NotifyVisibility oldSide=NotificationVisibility(watchRelative,current,entry.Name,true),newSide=NotificationVisibility(watchRelative,current,renamed.Name,false);
                    if(oldSide==NotifyVisibility.Uncertain||newSide==NotifyVisibility.Uncertain)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                    if(oldSide==NotifyVisibility.Protected||newSide==NotifyVisibility.Protected)continue;
                    visible.Add(entry);visible.Add(renamed);
                }
                else
                {
                    NotifyVisibility visibility=NotificationVisibility(watchRelative,current,entry.Name,false);
                    if(visibility==NotifyVisibility.Uncertain)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
                    if(visibility==NotifyVisibility.Allowed)visible.Add(entry);
                }
            }
            if(visible.Count==0)return NTStatus.STATUS_NOTIFY_ENUM_DIR;
            using(var stream=new MemoryStream(input.Length))using(var writer=new BinaryWriter(stream))
            {
                for(int index=0;index<visible.Count;index++)
                {
                    NotifyEntry entry=visible[index];int size=12+entry.Bytes.Length,padded=(size+3)&~3;bool last=index==visible.Count-1;
                    writer.Write(last?0:padded);writer.Write(entry.Action);writer.Write(entry.Bytes.Length);writer.Write(entry.Bytes);
                    if(!last)for(int pad=size;pad<padded;pad++)writer.Write((byte)0);
                }
                output=stream.ToArray();return NTStatus.STATUS_SUCCESS;
            }
        }
        catch(IOException){return NTStatus.STATUS_NOTIFY_ENUM_DIR;}
        catch(ArgumentException){return NTStatus.STATUS_NOTIFY_ENUM_DIR;}
        finally{if(watchPins!=null)Release(watchPins);if(rootPins!=null)Release(rootPins);}
    }
    private NotifyVisibility NotificationVisibility(string watchRelative,string current,string name,bool allowMissingOld)
    {
        string relative=Path.Combine(watchRelative,name);List<SafeFileHandle> pins=null;
        if(relative.Split('\\').Length>256)return NotifyVisibility.Uncertain;
        if(ProtectedPath(Path.Combine(current,name)))return NotifyVisibility.Protected;
        try
        {
            if(PinParents(relative,out pins)!=NTStatus.STATUS_SUCCESS)return NotifyVisibility.Uncertain;
            string expected=Path.Combine(HandlePath(pins[pins.Count-1].DangerousGetHandle()),Path.GetFileName(name));
            if(ProtectedPath(expected))return NotifyVisibility.Protected;
            using(SafeFileHandle entry=PinFile(Path.Combine(root,relative),0x80,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero))
            {
                if(entry.IsInvalid)return allowMissingOld&&Marshal.GetLastWin32Error()==2?NotifyVisibility.Allowed:NotifyVisibility.Uncertain;
                string canonical=HandlePath(entry.DangerousGetHandle());FileIdentity identity;
                if(!GetFileInformationByHandle(entry.DangerousGetHandle(),out identity))return NotifyVisibility.Uncertain;
                if(ProtectedPath(canonical)||protectedFolders.Contains(Key(identity)))return NotifyVisibility.Protected;
                if(CheckHandle(entry.DangerousGetHandle())!=NTStatus.STATUS_SUCCESS||!String.Equals(canonical,expected,StringComparison.OrdinalIgnoreCase))return NotifyVisibility.Uncertain;
                return NotifyVisibility.Allowed;
            }
        }
        finally{if(pins!=null)Release(pins);}
    }
    public NTStatus Cancel(object request)
    {
        ChangeRequest operation=request as ChangeRequest;if(operation==null)return NTStatus.STATUS_INVALID_HANDLE;
        lock(requestGate)
        {
            if(operation.Completed||!notifications.Contains(operation))return NTStatus.STATUS_NOT_FOUND;
            return CancelIoEx(operation.Directory,operation.Overlapped)?NTStatus.STATUS_SUCCESS:Win32Status(Marshal.GetLastWin32Error());
        }
    }
    private void CancelNotifications(OpenHandle owner)
    {
        lock(requestGate)
        {
            foreach(ChangeRequest operation in notifications)
                if(Object.ReferenceEquals(operation.Owner,owner)){operation.Cleanup=true;CancelIoEx(operation.Directory,operation.Overlapped);}
        }
    }
}

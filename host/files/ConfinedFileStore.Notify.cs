using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32.SafeHandles;
using SMBLibrary;

internal sealed partial class ConfinedFileStore
{
    private readonly object requestGate=new object();
    private readonly List<ChangeRequest> notifications=new List<ChangeRequest>();
    private sealed class ChangeRequest
    {
        internal OpenHandle Owner;
        internal SafeFileHandle Directory;
        internal ManualResetEvent Event;
        internal IntPtr Buffer,Overlapped;
        internal int Size;
        internal bool Queued,Published,Completed,Cleanup;
        internal OnNotifyChangeCompleted Callback;
        internal object Context;
        internal void Dispose()
        {
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
            bool workerStarted=false;
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
                    // Start the waiter before issuing I/O, but publish only after I/O has actually been initiated.
                    Thread worker=new Thread(()=>CompleteNotification(operation)){IsBackground=true,Name="Host files change notification"};worker.Start();workerStarted=true;
                    bool initiated=ReadDirectoryChangesW(operation.Directory,operation.Buffer,(uint)size,tree,(uint)filter,IntPtr.Zero,operation.Overlapped,IntPtr.Zero);
                    int error=initiated?0:Marshal.GetLastWin32Error();
                    if(!initiated&&error!=997){operation.Event.Set();return Win32Status(error);}
                    operation.Queued=true;notifications.Add(operation);operation.Published=true;pending=operation;return NTStatus.STATUS_PENDING;
                }
            }
            catch
            {
                if(workerStarted)lock(requestGate){if(operation.Queued)CancelIoEx(operation.Directory,operation.Overlapped);else operation.Event.Set();}
                throw;
            }
            finally{if(!workerStarted)operation.Dispose();}
        }
        finally{Release(pins);}
    }
    private void CompleteNotification(ChangeRequest operation)
    {
        operation.Event.WaitOne();NTStatus status=NTStatus.STATUS_CANCELLED;byte[] data=new byte[0];bool deliver;
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
            finally{operation.Completed=true;notifications.Remove(operation);operation.Dispose();}
        }
        if(deliver)operation.Callback(status,data,operation.Context);
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

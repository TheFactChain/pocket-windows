using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

internal static class SliderInteractionRegression
{
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left,Top,Right,Bottom; }
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateDesktop(string name,IntPtr device,IntPtr mode,int flags,uint access,IntPtr security);
    [DllImport("user32.dll",SetLastError=true)] static extern bool SetThreadDesktop(IntPtr desktop);
    [DllImport("user32.dll")] static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll")] static extern IntPtr GetCapture();
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr window,StringBuilder name,int capacity);
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window,int message,IntPtr wParam,IntPtr lParam);
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window,int message,IntPtr wParam,ref Rect rect);
    static void Assert(bool condition,string message) { if(!condition)throw new Exception(message); }
    static IntPtr PointAt(int x,int y) { return new IntPtr((y<<16)|(x&65535)); }
    static Dictionary<string,object> Values(int memory) { return new Dictionary<string,object>{{"schema",1},{"work_memory_mib",memory},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}}; }
    static void Run()
    {
        Application.EnableVisualStyles();
        using(Form host=new Form { ClientSize=new Size(760,640),ShowInTaskbar=false })
        using(CompositedPanel pages=new CompositedPanel { Dock=DockStyle.Fill })
        using(SettingsEditor editor=new SettingsEditor { Dock=DockStyle.Fill })
        {
            host.Controls.Add(pages);pages.Controls.Add(editor);
            editor.LoadValues(Values(5000),false);editor.LoadWindowsValues(null);editor.LoadViewerValues(null);editor.LoadFileSharingValues(null);
            editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",10000},{"logical_processors",8}});
            host.Show();Application.DoEvents();
            Dictionary<string,object> value;
            Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==5000&&!editor.Dirty,"Legacy MiB value changed on load/host refresh");
            editor.Fields["work_memory_mib"].Text="2147483647";Assert(!editor.TryGetValue(out value),"Invalid high memory accepted");
            editor.Fields["work_memory_mib"].Text="0";Assert(!editor.TryGetValue(out value),"Invalid low memory accepted");
            editor.CancelButton.PerformClick();Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==5000,"Cancel lost non-step memory");
            editor.LoadValues(Values(4096),false);
            TrackBar slider=editor.MemorySlider;StringBuilder name=new StringBuilder(256);GetClassName(slider.Handle,name,256);
            Assert(name.ToString().IndexOf("msctls_trackbar32",StringComparison.OrdinalIgnoreCase)>=0,"Expected native Windows trackbar");
            Rect thumb=new Rect(),channel=new Rect();SendMessage(slider.Handle,0x419,IntPtr.Zero,ref thumb);SendMessage(slider.Handle,0x41A,IntPtr.Zero,ref channel);
            int y=(thumb.Top+thumb.Bottom)/2;IntPtr button=new IntPtr(1);
            SendMessage(slider.Handle,0x201,button,PointAt((thumb.Left+thumb.Right)/2,y));
            try
            {
                Assert(GetCapture()==slider.Handle,"Native thumb did not capture mouse");
                SendMessage(slider.Handle,0x200,button,PointAt(channel.Right+100,y));
                Assert(slider.Value==slider.Maximum,"Native drag did not reach maximum before release");
                Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==slider.Value*SettingsEditor.MemoryStepMiB,"Draft not updated during held drag");
                SendMessage(slider.Handle,0x200,button,PointAt(channel.Left-100,y));
                Assert(slider.Value==slider.Minimum,"Captured drag outside control did not reach minimum");
            }
            finally { SendMessage(slider.Handle,0x202,IntPtr.Zero,PointAt(channel.Left,y)); }
            Assert(GetCapture()!=slider.Handle,"Native thumb did not release capture");
            slider.Focus();slider.Value=4096/SettingsEditor.MemoryStepMiB;
            SendMessage(slider.Handle,0x100,new IntPtr((int)Keys.Right),IntPtr.Zero);SendMessage(slider.Handle,0x101,new IntPtr((int)Keys.Right),IntPtr.Zero);
            Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==4352,"Native arrow must change memory by 256 MiB");
            SendMessage(slider.Handle,0x100,new IntPtr((int)Keys.PageUp),IntPtr.Zero);SendMessage(slider.Handle,0x101,new IntPtr((int)Keys.PageUp),IntPtr.Zero);
            Assert(editor.TryGetValue(out value)&&Math.Abs((int)value["work_memory_mib"]-4352)==1024,"Native page key must change memory by 1 GiB");
            editor.CancelButton.PerformClick();Assert(slider.Value*SettingsEditor.MemoryStepMiB==4096&&!editor.Dirty,"Cancel did not restore memory");
            Label label=(Label)typeof(SettingsEditor).GetField("memoryGib",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(editor);
            using(Font scaled=new Font(label.Font.FontFamily,label.Font.SizeInPoints*1.5f,label.Font.Style))
            {
                label.Font=scaled;Size slot=label.Size;slider.Value=slider.Maximum-1;
                Assert(label.Size==slot&&label.Width>=TextRenderer.MeasureText(label.Text,label.Font).Width,"Scaled fractional label clipped or resized");
                Assert(slider.AccessibleDescription.Contains(label.Text),"Accessible description lost current memory");
            }
            Console.WriteLine("PASS native trackbar class, captured drag before release, both edges, release, native keys, MiB draft/save validation, legacy/cancel, scaled label and accessibility");
        }
    }
    static int Main()
    {
        IntPtr desktop=CreateDesktop("WindowsClaudeSliderTest-"+Guid.NewGuid().ToString("N"),IntPtr.Zero,IntPtr.Zero,0,0x1FF,IntPtr.Zero);
        if(desktop==IntPtr.Zero){Console.Error.WriteLine(new Win32Exception(Marshal.GetLastWin32Error()));return 1;}
        try { if(!SetThreadDesktop(desktop))throw new Win32Exception(Marshal.GetLastWin32Error());Run();return 0; }
        catch(Exception error) { Console.Error.WriteLine(error);return 1; }
        finally { CloseDesktop(desktop); }
    }
}

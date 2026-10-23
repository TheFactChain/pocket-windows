using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Reflection;
using System.Windows.Forms;

internal static class SliderInteractionRegression
{
    static void Watch(Control control,Action tick) { control.Layout+=delegate { tick(); }; foreach(Control child in control.Controls)Watch(child,tick); }
    [STAThread]
    static int Main()
    {
        Application.EnableVisualStyles();
        using (Form host = new Form { ClientSize = new Size(760,640), ShowInTaskbar = false, StartPosition = FormStartPosition.Manual, Location = new Point(-30000,-30000) })
        using (CompositedPanel pages = new CompositedPanel { Dock = DockStyle.Fill })
        using (SettingsEditor editor = new SettingsEditor { Dock = DockStyle.Fill })
        {
            host.Controls.Add(pages); pages.Controls.Add(editor);
            editor.LoadValues(new Dictionary<string,object>{{"schema",1},{"work_memory_mib",4096},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}},false);
            editor.LoadWindowsValues(null); editor.LoadViewerValues(null); editor.LoadFileSharingValues(null);
            host.Show(); Application.DoEvents();
            int layouts=0;
            Watch(editor,delegate { layouts++; });
            MethodInfo move=typeof(Slider).GetMethod("OnMouseMove",BindingFlags.Instance|BindingFlags.NonPublic);
            Stopwatch timer=Stopwatch.StartNew();
            for(int i=0;i<180;i++)
            {
                move.Invoke(editor.MemorySlider,new object[]{new MouseEventArgs(MouseButtons.Left,0,10+(i*37)%(editor.MemorySlider.Width-20),12,0)});
                Application.DoEvents();
            }
            Console.WriteLine("180 drag moves: "+timer.ElapsedMilliseconds+"ms; editor layouts: "+layouts);
            Dictionary<string,object> value;
            if(!editor.TryGetValue(out value)||(int)value["work_memory_mib"]!=editor.MemorySlider.Value)throw new Exception("Dragged memory was not preserved for Save");
            // Allow the first dirty-state transition, but no relayout on each of the 180 moves.
            if(layouts>30)throw new Exception("Memory drag repeatedly relayouts the entire settings page: "+layouts);
            editor.MemorySlider.Value=4096;
            typeof(Slider).GetMethod("OnKeyDown",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(editor.MemorySlider,new object[]{new KeyEventArgs(Keys.Right)});
            if(editor.MemorySlider.Value!=4352)throw new Exception("Keyboard step changed");
            editor.CancelButton.PerformClick();
            if(editor.MemorySlider.Value!=4096||editor.Dirty)throw new Exception("Cancel did not restore memory");
            Label label=(Label)typeof(SettingsEditor).GetField("memoryGib",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(editor);
            label.Font=new Font(label.Font.FontFamily,label.Font.SizeInPoints*1.5f,label.Font.Style);
            Size slot=label.Size;
            editor.MemorySlider.Value=31744;
            if(label.Size!=slot||label.Width<TextRenderer.MeasureText(label.Text,label.Font).Width||label.Height<TextRenderer.MeasureText(label.Text,label.Font).Height)throw new Exception("Scaled memory label clips or changes size");
        }
        return 0;
    }
}

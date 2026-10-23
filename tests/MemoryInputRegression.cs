using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows.Forms;

internal static class MemoryInputRegression
{
    static void Assert(bool condition,string message) { if(!condition)throw new Exception(message); }
    static IEnumerable<Control> Tree(Control root) { yield return root;foreach(Control child in root.Controls)foreach(Control item in Tree(child))yield return item; }
    static Dictionary<string,object> Values(int memory) { return new Dictionary<string,object>{{"schema",1},{"work_memory_mib",memory},{"gateway_memory_mib",1536},{"vcpus",2},{"lan_port",42345}}; }
    [STAThread] static int Main()
    {
        try
        {
            foreach(string culture in new[]{"ru-RU","en-US"})
            using(SettingsEditor editor=new SettingsEditor())
            {
                CultureInfo.CurrentCulture=CultureInfo.GetCultureInfo(culture);
                editor.LoadValues(Values(5000),false);editor.LoadWindowsValues(null);editor.LoadViewerValues(null);editor.LoadFileSharingValues(null);
                Dictionary<string,object> value;
                Assert(!Tree(editor).OfType<TrackBar>().Any(),"Memory slider must be removed");
                Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==5000&&!editor.Dirty,"Legacy MiB changed on load");
                editor.MemoryInput.Text="4";Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==4096&&editor.Dirty,"Whole GiB input failed");
                foreach(string text in new[]{"3,75","3.75"}){editor.MemoryInput.Text=text;Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==3840,"Decimal separator failed: "+culture+text);}
                editor.MemoryInput.Text="3.7";Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==3789,"Fractional GiB must round to nearest MiB");
                foreach(string text in new[]{"","0","0.9999","32.0001","33","abc","1,2.3","9999999999999999999999999999999999999999"})
                {editor.MemoryInput.Text=text;Assert(!editor.TryGetValue(out value)&&editor.Error.Text.Contains("ГиБ"),"Invalid memory accepted: "+text);}
                editor.LoadHostValues(new Dictionary<string,object>{{"ram_total_mib",16384},{"ram_available_mib",10000},{"logical_processors",8}});
                Assert(editor.MemoryInput.Text.StartsWith("999"),"Host refresh replaced draft");
                editor.CancelButton.PerformClick();Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==5000&&!editor.Dirty,"Cancel lost legacy value");
                foreach(string text in new[]{"1","32"}){editor.MemoryInput.Text=text;Assert(editor.TryGetValue(out value),"Boundary rejected");}
                editor.MemoryInput.Text="4.5";Assert(editor.TryGetValue(out value),"Save input failed");editor.LoadValues(value,false);
                Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==4608&&(int)value["gateway_memory_mib"]==1536&&(int)value["lan_port"]==42345&&!editor.Dirty,"Reload changed memory or hidden settings");
                editor.RecommendedButton.PerformClick();Assert(editor.TryGetValue(out value)&&(int)value["work_memory_mib"]==7424,"Recommendations lost numeric binding");
                editor.Fields["work_memory_mib"].Enabled=false;Assert(!editor.MemoryInput.Enabled,"Input ignored disabled state");
                Assert(editor.MemoryInput.AccessibleName.Contains("ГиБ"),"Input units inaccessible");
            }
            Console.WriteLine("PASS numeric GiB: no trackbar, ru/en comma/dot, fractions/rounding, invalid/range, legacy, cancel, reload, recommendations, disabled/accessibility");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

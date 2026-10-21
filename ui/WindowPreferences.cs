using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Security;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed partial class MainForm
{
    private bool panelPreferencesLoaded;
    private bool rememberPanelBounds;
    private bool panelWasMaximized;

    private static bool PanelInteger(Dictionary<string,object> values,string key,int minimum,int maximum,out int number)
    {
        object value;number=0;
        if(values==null||!values.TryGetValue(key,out value)||!(value is int))return false;
        number=(int)value;return number>=minimum&&number<=maximum;
    }
    private static bool PanelStateError(Exception error)
    {
        return error is IOException||error is UnauthorizedAccessException||error is SecurityException||error is ArgumentException||error is InvalidOperationException;
    }
    internal void ApplyViewerPreferences(Dictionary<string,object> settings)
    {
        int schema;object remember,fullscreen;
        if(!PanelInteger(settings,"schema",1,1,out schema)||!settings.TryGetValue("remember_window",out remember)||!(remember is bool)
            ||!settings.TryGetValue("start_fullscreen",out fullscreen)||!(fullscreen is bool))return;
        rememberPanelBounds=(bool)remember;
        if(panelPreferencesLoaded)return;
        panelPreferencesLoaded=true;panelWasMaximized=WindowState==FormWindowState.Maximized;
        Resize+=delegate{if(WindowState!=FormWindowState.Minimized)panelWasMaximized=WindowState==FormWindowState.Maximized;};
        if(!rememberPanelBounds)return;
        try
        {
            string path=Path.Combine(root,"config","window-state.json");
            if(!File.Exists(path)||new FileInfo(path).Length>4096)return;
            var state=new JavaScriptSerializer{MaxJsonLength=4096}.Deserialize<Dictionary<string,object>>(File.ReadAllText(path));
            int x,y,width,height;object maximized;
            if(!PanelInteger(state,"schema",1,1,out schema)||!PanelInteger(state,"x",-1000000,1000000,out x)||!PanelInteger(state,"y",-1000000,1000000,out y)
                ||!PanelInteger(state,"width",320,32768,out width)||!PanelInteger(state,"height",240,32768,out height)
                ||!state.TryGetValue("maximized",out maximized)||!(maximized is bool))return;
            Rectangle work=Screen.FromRectangle(new Rectangle(x,y,width,height)).WorkingArea;
            MinimumSize=new Size(Math.Min(MinimumSize.Width,work.Width),Math.Min(MinimumSize.Height,work.Height));
            width=Math.Min(work.Width,Math.Max(MinimumSize.Width,width));height=Math.Min(work.Height,Math.Max(MinimumSize.Height,height));
            x=Math.Max(work.Left,Math.Min(x,work.Right-width));y=Math.Max(work.Top,Math.Min(y,work.Bottom-height));
            StartPosition=FormStartPosition.Manual;WindowState=FormWindowState.Normal;Bounds=new Rectangle(x,y,width,height);
            panelWasMaximized=(bool)maximized;if(panelWasMaximized)WindowState=FormWindowState.Maximized;
        }
        catch(Exception error){if(!PanelStateError(error))throw;}
    }
    internal void SavePanelBounds()
    {
        if(!panelPreferencesLoaded||!rememberPanelBounds)return;
        string temporary=null;
        try
        {
            Rectangle bounds=WindowState==FormWindowState.Normal?Bounds:RestoreBounds;
            if(bounds.Width<320||bounds.Height<240||bounds.Width>32768||bounds.Height>32768)return;
            var state=new Dictionary<string,object>{{"schema",1},{"x",bounds.X},{"y",bounds.Y},{"width",bounds.Width},{"height",bounds.Height},
                {"maximized",WindowState==FormWindowState.Maximized||(WindowState==FormWindowState.Minimized&&panelWasMaximized)}};
            string path=Path.Combine(root,"config","window-state.json");Directory.CreateDirectory(Path.GetDirectoryName(path));
            temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";File.WriteAllText(temporary,new JavaScriptSerializer().Serialize(state));
            if(File.Exists(path))File.Replace(temporary,path,null);else File.Move(temporary,path);
        }
        catch(Exception error){if(!PanelStateError(error))throw;}
        finally
        {
            if(temporary!=null)try{File.Delete(temporary);}catch(Exception error){if(!PanelStateError(error))throw;}
        }
    }
}

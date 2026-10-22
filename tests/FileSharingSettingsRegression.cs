using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;

internal static class FileSharingSettingsRegression
{
    static void Assert(bool condition,string message){if(!condition)throw new Exception(message);}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control item in Tree(child))yield return item;}
    static Dictionary<string,object> Get(SettingsEditor editor)
    {
        object[] args={null};
        Assert((bool)typeof(SettingsEditor).GetMethod("TryGetFileSharingValue",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(editor,args),"Valid file settings rejected: "+editor.Error.Text);
        return (Dictionary<string,object>)args[0];
    }
    static bool Save(SettingsEditor editor){object[] args={null};return (bool)typeof(SettingsEditor).GetMethod("TryGetFileSharingValue",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(editor,args);}
    static void Load(SettingsEditor editor,string mode,string[] folders){typeof(SettingsEditor).GetMethod("LoadFileSharingValues",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(editor,new object[]{mode==null?null:new Dictionary<string,object>{{"schema",1},{"mode",mode},{"folders",folders}}});}
    static bool Add(SettingsEditor editor,string path){return (bool)typeof(SettingsEditor).GetMethod("AddFileSharingFolder",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(editor,new object[]{path});}
    [STAThread] static int Main()
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            Assert(typeof(SettingsEditor).GetMethod("LoadFileSharingValues",BindingFlags.Instance|BindingFlags.NonPublic)!=null,"Missing separate file sharing settings contract");
            using(SettingsEditor editor=new SettingsEditor())
            {
                editor.ShowSection(2);
                Load(editor,null,null);var value=Get(editor);Assert((string)value["mode"]=="off"&&((string[])value["folders"]).Length==0&&value.Count==3,"Defaults must be off with separate schema");
                ComboBox mode=Tree(editor).OfType<ComboBox>().Single(item=>item.AccessibleName=="Доступ к файлам ПК");
                ListBox folders=Tree(editor).OfType<ListBox>().Single();
                mode.SelectedIndex=1;Assert(!Save(editor)&&editor.Error.Visible,"Selected mode needs a folder");
                Assert(Add(editor,@"C:\Work"),"Absolute local folder rejected");Assert(!Add(editor,@"c:\work\"),"Case/trailing-separator duplicate accepted");
                Assert(!Add(editor,@"\\server\share")&&!Add(editor,@"relative")&&!Add(editor,@"C:relative")&&!Add(editor,@"C:\bad*name"),"Unsafe path accepted");
                mode.SelectedIndex=0;Assert((string)Get(editor)["mode"]=="all"&&folders.Items.Count==1,"All mode must retain selected folders");
                mode.SelectedIndex=2;Assert((string)Get(editor)["mode"]=="off"&&folders.Items.Count==1,"Off must retain folders");
                mode.SelectedIndex=1;Assert((string)Get(editor)["mode"]=="selected"&&folders.Items.Count==1,"Returning to selected lost folder");
                Load(editor,"selected",new[]{@"C:\Original"});folders.Items.Add(@"D:\Other");mode.SelectedIndex=0;editor.CancelButton.PerformClick();
                Assert((string)Get(editor)["mode"]=="selected"&&folders.Items.Count==1&&(string)folders.Items[0]==@"C:\Original","Cancel failed to restore snapshot");
                Load(editor,"selected",Enumerable.Range(0,64).Select(index=>@"C:\Folder"+index).ToArray());Assert(Save(editor)&&!Add(editor,@"D:\Extra"),"64 folder limit failed");
                folders.Items.Add(@"D:\Extra");Assert(!Save(editor),"Oversized loaded list accepted");
                Load(editor,"selected",new[]{@"\\server\share"});Assert(!Save(editor),"Loaded UNC path accepted");
                Load(editor,"bad",new string[0]);Assert(!Save(editor),"Unknown mode accepted");
                Load(editor,"selected",new[]{@"C:\Work",@"c:\WORK\"});Assert(!Save(editor),"Loaded duplicates accepted");
                Load(editor,"selected",new[]{@"C:\Work"});folders.SelectedIndex=0;Tree(editor).OfType<Button>().Single(button=>button.Text=="Удалить").PerformClick();Assert(folders.Items.Count==0&&!Save(editor),"Remove failed");
            }
            Console.WriteLine("PASS: file sharing defaults, modes, retained folders, validation, duplicate rejection, 64 limit, removal and Cancel.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}
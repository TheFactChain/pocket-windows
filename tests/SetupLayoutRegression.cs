using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;

internal static class SetupLayoutRegression
{
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static IEnumerable<Control> Tree(Control root){yield return root;foreach(Control child in root.Controls)foreach(Control item in Tree(child))yield return item;}
    static Point Position(Control item,Control root){Point point=Point.Empty;for(;item!=root;item=item.Parent)point.Offset(item.Location);return point;}
    static void Layout(Form form)
    {
        typeof(Control).GetMethod("CreateControl",BindingFlags.Instance|BindingFlags.NonPublic,null,new[]{typeof(bool)},null).Invoke(form,new object[]{true});
        for(int i=0;i<4;i++){form.PerformLayout();foreach(Control item in Tree(form).Reverse())item.PerformLayout();}
    }
    [STAThread] static int Main(string[] args)
    {
        try
        {
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);
            foreach(float scale in new[]{1f} /* laid out at 100% only; Windows scales the window */)
            using(Form form=(Form)Activator.CreateInstance(typeof(PortableSetup),true))
            {
                Assert(form.Font.Name=="Segoe UI"&&Math.Abs(form.Font.SizeInPoints-10)<.1,"Setup must share the 10pt Segoe UI baseline");
                Assert(form.BackColor==Color.FromArgb(242,242,247),"Setup must share the light background");
                Layout(form);form.Font=new Font("Segoe UI",10*scale);form.ClientSize=new Size(740,520);Layout(form);
                var buttons=Tree(form).OfType<Button>().ToArray();
                Assert(buttons.Length==3&&buttons.All(button=>button.GetType().Name=="RoundedButton"),"All setup actions must use shared rounded buttons");
                var bounds=buttons.Select(button=>new Rectangle(Position(button,form),button.Size)).ToArray();
                for(int i=0;i<buttons.Length;i++)
                {
                    Assert(form.ClientRectangle.Contains(bounds[i]),"Setup action is outside the window at font scale "+scale);
                    Assert(buttons[i].Width>=buttons[i].GetPreferredSize(Size.Empty).Width,"Setup action text is clipped at font scale "+scale);
                    for(int j=0;j<i;j++)Assert(!bounds[i].IntersectsWith(bounds[j]),"Setup actions overlap");
                }
                foreach(Label label in Tree(form).OfType<Label>())
                {
                    Assert(label.ForeColor==Color.FromArgb(28,28,30)||label.ForeColor==Color.FromArgb(110,110,115),"Setup label must share readable ink");
                    if(label.Text.Contains("существующие папки")||label.Text.Contains("Рядом с Setup.exe"))
                    {
                        Size text=TextRenderer.MeasureText(label.Text,label.Font,new Size(label.Width,0),TextFormatFlags.WordBreak);
                        Assert(label.Height>=text.Height,"Setup folder guidance is clipped at font scale "+scale);
                    }
                }
                if(args.Length>0)
                {
                    Directory.CreateDirectory(args[0]);
                    using(Bitmap image=new Bitmap(form.Width,form.Height))
                    {form.DrawToBitmap(image,new Rectangle(Point.Empty,form.Size));image.Save(Path.Combine(args[0],"setup-font"+(int)(scale*100)+".png"),ImageFormat.Png);}
                }
            }
            Console.WriteLine("PASS: hidden setup layout at font100/125/150/200; shared palette and buttons; wrapped guidance; visible actions.");return 0;
        }
        catch(Exception error){Console.Error.WriteLine(error);return 1;}
    }
}

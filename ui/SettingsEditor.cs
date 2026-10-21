using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Linq;
using System.Windows.Forms;

internal sealed class SettingsEditor : UserControl
{
    internal readonly Dictionary<string, TextBox> Fields = new Dictionary<string, TextBox>();
    internal readonly ComboBox Resolution = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, BackColor = NativeStyle.Background, AccessibleName = "Разрешение рабочего стола Windows", AccessibleDescription = "Размер рабочего стола Windows, применяется после следующего запуска.", Dock = DockStyle.Top, Margin = Padding.Empty };
    internal readonly Label Error = new Label { Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, Visible = false, ForeColor = Color.FromArgb(175,59,48), Margin = new Padding(0,4,0,4) };
    internal readonly Button SaveButton = new RoundedButton { Text = "Сохранить", Primary = true, AutoSize = true, Padding = new Padding(18,8,18,8), Margin = new Padding(8,0,0,0) };
    internal readonly Button CancelButton = new RoundedButton { Text = "Отменить", AutoSize = true, Padding = new Padding(18,8,18,8), Margin = Padding.Empty };
    private readonly string[] keys = { "work_memory_mib", "vcpus" };
    private readonly string[] labels = { "Память Windows", "Процессоры каждой VM" };
    private readonly int[] minimum = {1024,1};
    private readonly int[] maximum = {32768,32};
    private Dictionary<string,object> original;
    private Dictionary<string,object> originalWindows;
    private readonly int[,] dimensions = {{800,600},{1024,768},{1280,720},{1280,800},{1366,768},{1600,900},{1920,1080}};
    private readonly Label diskCapacity = new Label { Text = "80 ГиБ", AutoSize = true, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleRight, Margin = new Padding(0,12,0,12) };
    private readonly Label notice = new Label { Text = "Изменения применяются при следующем запуске Windows.", Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, ForeColor = NativeStyle.Muted, Margin = new Padding(0,6,0,12) };

    internal SettingsEditor()
    {
        Dock = DockStyle.Fill;
        BackColor = NativeStyle.Background;
        Font = new Font("Segoe UI",10);
        TableLayoutPanel root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Padding = new Padding(2), Margin = Padding.Empty };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        root.Controls.Add(new Label { Text = "Настройки запуска", AutoSize = true, Font = new Font("Segoe UI",13,FontStyle.Bold), Margin = new Padding(0,0,0,2) },0,0);
        root.Controls.Add(notice,0,1);
        Panel scroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Margin = Padding.Empty };
        RoundedPanel group = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = Color.White, Padding = new Padding(16,10,16,10), Margin = Padding.Empty };
        TableLayoutPanel rows = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = keys.Length+2, Margin = Padding.Empty };
        rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,62));
        rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,38));
        for(int index=0;index<keys.Length;index++)
        {
            rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            AddCaption(rows,index,index==0?"Память Windows":"Процессоры",index==0?"1024 МиБ = 1 ГиБ оперативной памяти.":"Число выделенных виртуальных процессоров.");
            RoundedPanel field = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = NativeStyle.Background, Padding = new Padding(12,8,12,8), Margin = new Padding(0,5,0,5) };
            TableLayoutPanel content = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty, BackColor = field.BackColor };
            content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            content.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            TextBox input = new TextBox { Font = new Font("Segoe UI",9.5f), BorderStyle = BorderStyle.None, Anchor = AnchorStyles.Left|AnchorStyles.Right, TextAlign = HorizontalAlignment.Right, BackColor = field.BackColor, Margin = Padding.Empty, AccessibleName = labels[index] };
            Fields.Add(keys[index],input);
            content.Controls.Add(input,0,0);
            if(keys[index]=="work_memory_mib")content.Controls.Add(new Label { Text = "МиБ", Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, ForeColor = NativeStyle.Muted, Margin = new Padding(5,0,0,0) },1,0);
            field.Controls.Add(content);
            rows.Controls.Add(field,1,index);
        }
        rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        AddCaption(rows,2,"Разрешение","Размер рабочего стола Windows.");
        RoundedPanel resolutionBox = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = NativeStyle.Background, Padding = new Padding(10,8,10,8), Margin = new Padding(0,5,0,5) };
        TableLayoutPanel resolutionContent = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = 1, Margin = Padding.Empty, BackColor = resolutionBox.BackColor };
        resolutionContent.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        RowStyle resolutionRow=new RowStyle(SizeType.Absolute,Resolution.Height);resolutionContent.RowStyles.Add(resolutionRow);
        Resolution.SizeChanged+=delegate{resolutionRow.Height=Resolution.Height;};
        for(int index=0;index<dimensions.GetLength(0);index++)Resolution.Items.Add(dimensions[index,0]+" × "+dimensions[index,1]);
        Resolution.SelectedIndex=2;
        resolutionContent.Controls.Add(Resolution,0,0);resolutionBox.Controls.Add(resolutionContent);rows.Controls.Add(resolutionBox,1,2);
        rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        AddCaption(rows,3,"Диск Windows","Файл находится в папке portable.");
        rows.Controls.Add(diskCapacity,1,3);
        rows.Paint += delegate(object sender,PaintEventArgs args) {
            int position=0;int[] heights=rows.GetRowHeights();
            using(Pen line=new Pen(Color.FromArgb(237,237,241)))
                for(int index=0;index<heights.Length-1;index++){position+=heights[index];args.Graphics.DrawLine(line,0,position,rows.Width,position);}
        };
        group.Controls.Add(rows);scroll.Controls.Add(group);root.Controls.Add(scroll,0,2);root.Controls.Add(Error,0,3);
        FlowLayoutPanel footer = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.RightToLeft, WrapContents = false, Margin = new Padding(0,12,0,0) };
        footer.Controls.Add(SaveButton);footer.Controls.Add(CancelButton);root.Controls.Add(footer,0,4);
        CancelButton.Click += delegate {if(original!=null)LoadValues(original,false);if(originalWindows!=null)LoadWindowsValues(originalWindows);};
        root.SizeChanged += delegate { int width=Math.Max(100,root.ClientSize.Width-root.Padding.Horizontal);notice.MaximumSize=Error.MaximumSize=new Size(width,0); };
        Controls.Add(root);
    }
    private static void AddCaption(TableLayoutPanel rows,int row,string title,string explanation)
    {
        TableLayoutPanel caption = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 1, RowCount = 2, Margin = new Padding(0,8,12,8) };
        caption.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));caption.RowStyles.Add(new RowStyle(SizeType.AutoSize));caption.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Label heading = new Label { Text = title, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9.5f), Margin = Padding.Empty };
        Label help = new Label { Text = explanation, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",8.5f), ForeColor = NativeStyle.Muted, Margin = new Padding(0,4,0,0) };
        caption.Controls.Add(heading,0,0);caption.Controls.Add(help,0,1);
        caption.SizeChanged+=delegate{heading.MaximumSize=help.MaximumSize=new Size(Math.Max(100,caption.ClientSize.Width),0);};
        rows.Controls.Add(caption,0,row);
    }
    internal void LoadWindowsValues(Dictionary<string,object> settings,Dictionary<string,object> disk = null)
    {
        originalWindows=settings==null?new Dictionary<string,object>{{"schema",1},{"width",1280},{"height",720}}:new Dictionary<string,object>(settings);
        Resolution.SelectedIndex=-1;int width,height;
        if(originalWindows.ContainsKey("width")&&originalWindows.ContainsKey("height")
            &&Int32.TryParse(Convert.ToString(originalWindows["width"],CultureInfo.InvariantCulture),out width)
            &&Int32.TryParse(Convert.ToString(originalWindows["height"],CultureInfo.InvariantCulture),out height))
            for(int index=0;index<dimensions.GetLength(0);index++)if(dimensions[index,0]==width&&dimensions[index,1]==height)Resolution.SelectedIndex=index;
        if(Resolution.SelectedIndex<0){Error.Text="Сохранённое разрешение не поддерживается. Выбери значение из списка.";Error.Visible=true;}
        long bytes;
        if(disk!=null&&disk.ContainsKey("virtual_bytes")&&Int64.TryParse(Convert.ToString(disk["virtual_bytes"],CultureInfo.InvariantCulture),out bytes)&&bytes>0)
            diskCapacity.Text=((decimal)bytes/(1024L*1024*1024)).ToString("0.#",CultureInfo.CurrentCulture)+" ГиБ";
    }
    internal bool TryGetWindowsValue(out Dictionary<string,object> value)
    {
        value=null;
        if(originalWindows==null){Error.Text="Сначала загрузи настройки Windows.";Error.Visible=true;return false;}
        int index=Resolution.SelectedIndex;
        if(index<0||index>=dimensions.GetLength(0)){Error.Text="Выбери разрешение рабочего стола из списка.";Error.Visible=true;return false;}
        value=new Dictionary<string,object>{{"schema",1},{"width",dimensions[index,0]},{"height",dimensions[index,1]}};
        Error.Text="";Error.Visible=false;return true;
    }
    internal void LoadValues(Dictionary<string,object> settings,bool recovery)
    {
        original=new Dictionary<string,object>(settings);
        foreach(string key in keys)Fields[key].Text=Convert.ToString(settings[key],CultureInfo.InvariantCulture);
        Error.Visible = recovery;
        Error.Text=recovery?"Файл настроек повреждён. При сохранении исходный файл останется в резервной копии.":"";
    }
    internal bool TryGetValue(out Dictionary<string,object> value)
    {
        if(original==null){value=null;Error.Text="Сначала загрузи настройки.";Error.Visible=true;return false;}
        value=new Dictionary<string,object>(original);value["schema"]=1;int errors=0;string message="";TextBox first=null;
        for(int index=0;index<keys.Length;index++)
        {
            TextBox field=Fields[keys[index]];int number;
            bool valid=Int32.TryParse(field.Text.Trim(),NumberStyles.None,CultureInfo.InvariantCulture,out number)&&number>=minimum[index]&&number<=maximum[index];
            field.ForeColor=valid?NativeStyle.Ink:Color.FromArgb(175,59,48);
            if(valid)value[keys[index]]=number;
            else {errors++;if(first==null){first=field;message=labels[index]+": целое "+minimum[index]+"–"+maximum[index]+".";}}
        }
        Error.Visible = errors > 0;
        Error.Text=message+(errors>1?" Проверь ещё полей: "+(errors-1)+".":"");
        if(errors==0)return true;
        value=null;first.Focus();first.SelectAll();return false;
    }
}

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal sealed class SettingsEditor : UserControl
{
    internal readonly Dictionary<string, TextBox> Fields = new Dictionary<string, TextBox>();
    internal readonly ComboBox Resolution = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, BackColor = NativeStyle.Background, AccessibleName = "Разрешение рабочего стола Windows", AccessibleDescription = "Размер рабочего стола Windows, применяется после следующего запуска.", Dock = DockStyle.Top, Margin = Padding.Empty };
    internal readonly Label Error = new Label { Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, Visible = false, ForeColor = Color.FromArgb(175,59,48), Margin = new Padding(0,4,0,4) };
    internal readonly Button SaveButton = new RoundedButton { Text = "Сохранить", Primary = true, AutoSize = true, Padding = new Padding(18,8,18,8), Margin = new Padding(8,0,0,0) };
    internal readonly Button CancelButton = new RoundedButton { Text = "Отменить", AutoSize = true, Padding = new Padding(18,8,18,8), Margin = Padding.Empty };
    internal readonly CheckBox StartFullscreen = new CheckBox { Text = "Открывать Windows на весь экран", Dock = DockStyle.Top, Margin = new Padding(0,8,0,4) };
    internal readonly CheckBox RememberWindow = new CheckBox { Text = "Запоминать размер и положение панели", Dock = DockStyle.Top, Margin = new Padding(0,4,0,8) };
    internal readonly Button RecommendedButton = new RoundedButton { Text = "Рекомендуемые", AutoSize = true, Dock = DockStyle.Top, Padding = new Padding(8,6,8,6), Margin = new Padding(0,8,0,8), Enabled = false };
    private readonly string[] keys = { "work_memory_mib", "vcpus" };
    private readonly string[] labels = { "Память Windows", "Процессоры каждой VM" };
    private readonly int[] minimum = {1024,1};
    private readonly int[] maximum = {32768,32};
    private Dictionary<string,object> original;
    private Dictionary<string,object> originalWindows;
    private Dictionary<string,object> originalViewer;
    private Dictionary<string,object> originalFileSharing;
    private readonly ComboBox fileSharingMode = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, Dock = DockStyle.Top, BackColor = NativeStyle.Background, AccessibleName = "Доступ к файлам ПК", Margin = new Padding(0,8,0,8) };
    private readonly ListBox sharedFolders = new ListBox { Dock = DockStyle.Top, IntegralHeight = false, Height = 100, DrawMode = DrawMode.OwnerDrawFixed, BorderStyle = BorderStyle.None, BackColor = NativeStyle.Background, AccessibleName = "Выбранные папки ПК", Margin = new Padding(0,0,0,8) };
    private readonly List<RoundedButton> modeButtons = new List<RoundedButton>();
    private int hostTotal = -1, hostAvailable = -1, hostProcessors = -1;
    private Label memoryGib;
    private readonly Label hostMemory = new Label { Text = "Данные памяти ПК пока недоступны.", AutoSize = true, Dock = DockStyle.Top, ForeColor = NativeStyle.Muted, Margin = new Padding(0,8,12,8) };
    private readonly int[,] dimensions = {{800,600},{1024,768},{1280,720},{1280,800},{1366,768},{1440,900},{1600,900},{1680,1050},{1920,1080},{1920,1200},{2560,1440}};
    private readonly Label diskCapacity = new Label { Text = "80 ГиБ", AutoSize = true, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleRight, Margin = new Padding(0,12,0,12) };
    private readonly Label notice = new Label { Text = "Изменения применяются при следующем запуске Windows.", Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, ForeColor = NativeStyle.Muted, Margin = new Padding(0,6,0,12) };

    internal SettingsEditor()
    {
        Dock = DockStyle.Fill;
        BackColor = NativeStyle.Background;
        Font = new Font("Segoe UI",10);
        TableLayoutPanel root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6, Padding = new Padding(2), Margin = Padding.Empty };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for(int index=0;index<6;index++)root.RowStyles.Add(index==3?new RowStyle(SizeType.Percent,100):new RowStyle(SizeType.AutoSize));
        // Title and the "next start" note share one line: vertical space goes to the section itself.
        TableLayoutPanel heading = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2, RowCount = 1, Margin = new Padding(0,0,0,8) };
        heading.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));heading.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));heading.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        heading.Controls.Add(new Label { Text = "Настройки", AutoSize = true, Font = new Font("Segoe UI",13,FontStyle.Bold), Margin = new Padding(0,0,16,0) },0,0);
        notice.TextAlign = ContentAlignment.MiddleRight;notice.Margin = Padding.Empty;notice.Text = "Действует со следующего запуска";
        heading.Controls.Add(notice,1,0);
        root.Controls.Add(heading,0,0);
        // Three short sections instead of one long scrolling form; each fits the window on its own.
        RoundedPanel tabs = new RoundedPanel { Dock = DockStyle.Top, Height = 44, BackColor = Color.FromArgb(227,227,232), Padding = new Padding(3), Margin = new Padding(0,0,0,6), AccessibleName = "Разделы настроек", AccessibleRole = AccessibleRole.PageTabList };
        TableLayoutPanel tabRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, Margin = Padding.Empty, BackColor = tabs.BackColor };
        for(int index=0;index<3;index++)tabRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,33.33f));
        tabRow.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        string[] sectionNames={"Ресурсы","Экран","Файлы ПК"};
        for(int index=0;index<3;index++)
        {
            int section=index;
            RoundedButton tab=new RoundedButton { Text = sectionNames[index], Segment = true, CenterText = true, Dock = DockStyle.Fill, Margin = Padding.Empty, AccessibleName = sectionNames[index] };
            tab.Click+=delegate{ShowSection(section);};sectionButtons.Add(tab);tabRow.Controls.Add(tab,index,0);
        }
        tabs.Controls.Add(tabRow);
        tabs.FontChanged+=delegate{tabs.Height=TextRenderer.MeasureText("Ресурсы",tabs.Font).Height+16;};
        root.Controls.Add(tabs,0,2);
        Panel scroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true, Margin = Padding.Empty };
        TableLayoutPanel resources = SectionRows(), display = SectionRows();
        for(int index=0;index<keys.Length;index++)
        {
            resources.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Label help=AddCaption(resources,index,index==0?"Память Windows":"Процессоры",index==0?"1024 МиБ = 1 ГиБ оперативной памяти.":"Число выделенных виртуальных процессоров.");
            if(index==0)memoryGib=help;
            RoundedPanel field = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = NativeStyle.Background, Padding = new Padding(12,6,12,6), Margin = new Padding(0,4,0,4) };
            TableLayoutPanel content = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty, BackColor = field.BackColor };
            content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            content.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            content.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            TextBox input = new TextBox { Font = new Font("Segoe UI",9.5f), BorderStyle = BorderStyle.None, Anchor = AnchorStyles.Left|AnchorStyles.Right, TextAlign = HorizontalAlignment.Right, BackColor = field.BackColor, Margin = Padding.Empty, AccessibleName = labels[index] };
            Fields.Add(keys[index],input);
            if(index==0)input.TextChanged+=delegate{int mib;memoryGib.Text=Int32.TryParse(input.Text,NumberStyles.None,CultureInfo.InvariantCulture,out mib)&&mib>=0?((decimal)mib/1024).ToString("0.##",CultureInfo.CurrentCulture)+" ГиБ оперативной памяти.":"Введи память в МиБ (1024 МиБ = 1 ГиБ).";};
            content.Controls.Add(input,0,0);
            if(keys[index]=="work_memory_mib")content.Controls.Add(new Label { Text = "МиБ", Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, ForeColor = NativeStyle.Muted, Margin = new Padding(5,0,0,0) },1,0);
            field.Controls.Add(content);
            resources.Controls.Add(field,1,index);
        }
        resources.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        AddCaption(resources,2,"Диск Windows (внутри папки программы)","");
        resources.Controls.Add(diskCapacity,1,2);
        resources.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        TableLayoutPanel hostResources=new TableLayoutPanel{Dock=DockStyle.Top,AutoSize=true,ColumnCount=2,RowCount=1,Margin=Padding.Empty};hostResources.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));hostResources.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));hostResources.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        hostResources.Controls.Add(hostMemory,0,0);hostResources.Controls.Add(RecommendedButton,1,0);resources.Controls.Add(hostResources,0,3);resources.SetColumnSpan(hostResources,2);
        hostResources.Layout+=delegate{hostMemory.MaximumSize=new Size(Math.Max(100,hostResources.ClientSize.Width-RecommendedButton.GetPreferredSize(Size.Empty).Width-hostMemory.Margin.Horizontal-RecommendedButton.Margin.Horizontal),0);};
        RecommendedButton.Click+=delegate{ApplyRecommendedValues();};
        display.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        AddCaption(display,0,"Размер экрана Windows","«Как экран ПК» убирает чёрные полосы в окне и на весь экран.");
        RoundedPanel resolutionBox = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = NativeStyle.Background, Padding = new Padding(10,8,10,8), Margin = new Padding(0,5,0,5) };
        TableLayoutPanel resolutionContent = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = 1, Margin = Padding.Empty, BackColor = resolutionBox.BackColor };
        resolutionContent.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        RowStyle resolutionRow=new RowStyle(SizeType.Absolute,Resolution.Height);resolutionContent.RowStyles.Add(resolutionRow);
        Resolution.SizeChanged+=delegate{resolutionRow.Height=Resolution.Height;};
        // First choice: the PC monitor's proportions, so Windows never shows black bars.
        Resolution.Items.Add("Как экран ПК (рекомендуется)");
        for(int index=0;index<dimensions.GetLength(0);index++)Resolution.Items.Add(dimensions[index,0]+" × "+dimensions[index,1]);
        Resolution.SelectedIndex=0;
        resolutionContent.Controls.Add(Resolution,0,0);resolutionBox.Controls.Add(resolutionContent);display.Controls.Add(resolutionBox,1,0);
        display.RowStyles.Add(new RowStyle(SizeType.AutoSize));display.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        display.Controls.Add(StartFullscreen,0,1);display.SetColumnSpan(StartFullscreen,2);
        display.Controls.Add(RememberWindow,0,2);display.SetColumnSpan(RememberWindow,2);
        foreach(CheckBox option in new[]{StartFullscreen,RememberWindow})
        {
            option.SizeChanged+=delegate{FitViewerOption(option);};option.FontChanged+=delegate{FitViewerOption(option);};
        }
        FlowLayoutPanel folderActions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Margin = Padding.Empty };
        TableLayoutPanel files = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 6, Margin = Padding.Empty };
        files.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for(int index=0;index<6;index++)files.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Label filesHeading = new Label { Text = "Доступ к файлам ПК", AutoSize = true, Dock = DockStyle.Top, Margin = Padding.Empty };
        // The three modes as one row of buttons; the hidden list box stays the single source of the choice.
        RoundedPanel modes = new RoundedPanel { Dock = DockStyle.Top, Height = 40, BackColor = Color.FromArgb(227,227,232), Padding = new Padding(3), Margin = new Padding(0,6,0,6), AccessibleName = "Доступ к файлам ПК: режим", AccessibleRole = AccessibleRole.PageTabList };
        TableLayoutPanel modeRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, Margin = Padding.Empty, BackColor = modes.BackColor };
        string[] modeNames={"Выключено","Выбранные папки","Все диски ПК"};int[] modeIndex={2,1,0};
        for(int index=0;index<3;index++)
        {
            int choice=modeIndex[index];modeRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,33.33f));
            RoundedButton button=new RoundedButton { Text = modeNames[index], Segment = true, CenterText = true, Dock = DockStyle.Fill, Margin = Padding.Empty, Tag = choice, AccessibleName = modeNames[index] };
            button.Click+=delegate{fileSharingMode.SelectedIndex=choice;};modeButtons.Add(button);modeRow.Controls.Add(button,index,0);
        }
        modeRow.RowStyles.Add(new RowStyle(SizeType.Percent,100));modes.Controls.Add(modeRow);
        modes.FontChanged+=delegate{modes.Height=TextRenderer.MeasureText("Выключено",modes.Font).Height+18;};
        fileSharingMode.Visible=false;
        sharedFolders.DrawItem+=delegate(object sender,DrawItemEventArgs args){DrawFolder(args);};
        sharedFolders.FontChanged+=delegate{sharedFolders.ItemHeight=TextRenderer.MeasureText("Ag",sharedFolders.Font).Height+10;sharedFolders.Height=sharedFolders.ItemHeight*3+2;};
        Label filesNotice = new Label { AutoSize = true, Dock = DockStyle.Top, ForeColor = NativeStyle.Muted, Margin = new Padding(0,8,0,0) };
        fileSharingMode.SelectedIndexChanged+=delegate{
            foreach(RoundedButton button in modeButtons)button.Selected=(int)button.Tag==fileSharingMode.SelectedIndex;
            sharedFolders.Visible=folderActions.Visible=fileSharingMode.SelectedIndex==1;
            filesNotice.Text=fileSharingMode.SelectedIndex==0
                ?"Запись из Windows меняет любые файлы ПК, кроме программы и автозагрузки."
                :"Запись из Windows меняет настоящие файлы ПК.";};
        fileSharingMode.Items.AddRange(new object[]{"Все доступные диски ПК","Только выбранные папки","Выключено"});fileSharingMode.SelectedIndex=2;
        Button addFolder = new RoundedButton { Text = "Добавить папку", AutoSize = true, Padding = new Padding(10,6,10,6), Margin = new Padding(0,0,8,0) };
        Button removeFolder = new RoundedButton { Text = "Удалить", AutoSize = true, Padding = new Padding(10,6,10,6), Margin = Padding.Empty, Enabled = false };
        folderActions.Controls.Add(addFolder);folderActions.Controls.Add(removeFolder);
        addFolder.Click+=delegate{using(FolderBrowserDialog dialog=new FolderBrowserDialog { Description = "Выбери папку ПК для доступа из Windows", ShowNewFolderButton = false })if(dialog.ShowDialog(this)==DialogResult.OK)AddFileSharingFolder(dialog.SelectedPath);};
        removeFolder.Click+=delegate{if(sharedFolders.SelectedIndex>=0)sharedFolders.Items.RemoveAt(sharedFolders.SelectedIndex);};
        sharedFolders.SelectedIndexChanged+=delegate{removeFolder.Enabled=sharedFolders.SelectedIndex>=0;};
        files.Controls.Add(modes,0,1);files.Controls.Add(fileSharingMode,0,2);files.Controls.Add(sharedFolders,0,3);files.Controls.Add(folderActions,0,4);files.Controls.Add(filesNotice,0,5);
        files.SizeChanged+=delegate{filesHeading.MaximumSize=filesNotice.MaximumSize=new Size(Math.Max(100,files.ClientSize.Width),0);};
        foreach(TableLayoutPanel section in new[]{resources,display,files})
        {
            RoundedPanel group = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = Color.White, Padding = new Padding(16,10,16,10), Margin = Padding.Empty };
            group.Controls.Add(section);sections.Add(group);scroll.Controls.Add(group);
        }
        ShowSection(0);
        root.Controls.Add(scroll,0,3);root.Controls.Add(Error,0,4);
        FlowLayoutPanel footer = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.RightToLeft, WrapContents = false, Margin = new Padding(0,12,0,0) };
        footer.Controls.Add(SaveButton);footer.Controls.Add(CancelButton);root.Controls.Add(footer,0,5);
        CancelButton.Click += delegate {if(original!=null)LoadValues(original,false);if(originalWindows!=null)LoadWindowsValues(originalWindows);if(originalViewer!=null)LoadViewerValues(originalViewer);if(originalFileSharing!=null)LoadFileSharingValues(originalFileSharing);};
        root.SizeChanged += delegate { int width=Math.Max(100,root.ClientSize.Width-root.Padding.Horizontal);notice.MaximumSize=new Size(Math.Max(100,width/2),0);Error.MaximumSize=new Size(width,0); };
        Controls.Add(root);
    }
    private readonly List<RoundedPanel> sections = new List<RoundedPanel>();
    private readonly List<RoundedButton> sectionButtons = new List<RoundedButton>();
    internal int Section { get; private set; }
    internal void ShowSection(int section)
    {
        if(section<0||section>=sections.Count)return;
        Section=section;
        for(int index=0;index<sections.Count;index++){sections[index].Visible=index==section;sectionButtons[index].Selected=index==section;}
    }
    private void DrawFolder(DrawItemEventArgs args)
    {
        if(args.Index<0)return;
        bool selected=(args.State&DrawItemState.Selected)!=0;
        using(Brush fill=new SolidBrush(selected?Color.FromArgb(220,234,248):NativeStyle.Background))args.Graphics.FillRectangle(fill,args.Bounds);
        int size=Math.Max(12,args.Bounds.Height-18);Rectangle icon=new Rectangle(args.Bounds.Left+8,args.Bounds.Top+(args.Bounds.Height-size)/2,size+4,size);
        using(Brush folder=new SolidBrush(Color.FromArgb(224,161,0)))using(System.Drawing.Drawing2D.GraphicsPath shape=NativeStyle.Rounded(icon,3)){args.Graphics.SmoothingMode=System.Drawing.Drawing2D.SmoothingMode.AntiAlias;args.Graphics.FillPath(folder,shape);}
        Rectangle text=new Rectangle(icon.Right+10,args.Bounds.Top,args.Bounds.Width-icon.Right-14,args.Bounds.Height);
        TextRenderer.DrawText(args.Graphics,sharedFolders.Items[args.Index] as string,sharedFolders.Font,text,NativeStyle.Ink,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.PathEllipsis);
    }
    private static TableLayoutPanel SectionRows()
    {
        TableLayoutPanel rows = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, Margin = Padding.Empty };
        rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,62));rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,38));
        rows.Paint += delegate(object sender,PaintEventArgs args) {
            int position=0;int[] heights=rows.GetRowHeights();
            using(Pen line=new Pen(Color.FromArgb(237,237,241)))
                for(int index=0;index<heights.Length-1;index++){position+=heights[index];args.Graphics.DrawLine(line,0,position,rows.Width,position);}
        };
        return rows;
    }
    private static Label AddCaption(TableLayoutPanel rows,int row,string title,string explanation)
    {
        TableLayoutPanel caption = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 1, RowCount = 2, Margin = new Padding(0,6,12,6) };
        caption.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));caption.RowStyles.Add(new RowStyle(SizeType.AutoSize));caption.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Label heading = new Label { Text = title, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9.5f), Margin = Padding.Empty };
        Label help = new Label { Text = explanation, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",8.5f), ForeColor = NativeStyle.Muted, Margin = new Padding(0,4,0,0) };
        caption.Controls.Add(heading,0,0);caption.Controls.Add(help,0,1);help.Visible=explanation.Length>0;
        caption.SizeChanged+=delegate{heading.MaximumSize=help.MaximumSize=new Size(Math.Max(100,caption.ClientSize.Width),0);};
        rows.Controls.Add(caption,0,row);
        return help;
    }
    private static void FitViewerOption(CheckBox option)
    {
        option.Height=TextRenderer.MeasureText(option.Text,option.Font,new Size(Math.Max(1,option.Width-32),0),TextFormatFlags.WordBreak).Height+8;
    }
    private bool FileSharingError(string message)
    {
        Error.Text=message;Error.Visible=true;return false;
    }
    private static string LocalFolderPath(string path)
    {
        if(String.IsNullOrWhiteSpace(path)||path.Length<3||!Char.IsLetter(path[0])||path[1]!=':'||(path[2]!='\\'&&path[2]!='/')||path.Substring(2).IndexOf(':')>=0||path.IndexOfAny(new[]{'*','?','"','<','>','|'})>=0)return null;
        try { string full=Path.GetFullPath(path);return full.Length>3?full.TrimEnd('\\','/'):full; }
        catch(ArgumentException){return null;}catch(NotSupportedException){return null;}catch(PathTooLongException){return null;}
    }
    private bool AddFileSharingFolder(string path)
    {
        string folder=LocalFolderPath(path);
        if(folder==null)return FileSharingError("Выбери абсолютный путь к локальной папке ПК. Сетевые пути UNC недоступны.");
        if(sharedFolders.Items.Cast<string>().Any(item=>String.Equals(LocalFolderPath(item),folder,StringComparison.OrdinalIgnoreCase)))return FileSharingError("Эта папка уже добавлена.");
        if(sharedFolders.Items.Count>=64)return FileSharingError("Можно выбрать не более 64 папок.");
        sharedFolders.Items.Add(folder);Error.Text="";Error.Visible=false;return true;
    }
    internal void LoadFileSharingValues(Dictionary<string,object> settings)
    {
        object mode,folders;
        string selectedMode=settings==null?"off":settings.TryGetValue("mode",out mode)?mode as string:null;
        sharedFolders.Items.Clear();
        if(settings!=null&&settings.TryGetValue("folders",out folders)&&folders is System.Collections.IEnumerable&&!(folders is string))
            foreach(object folder in (System.Collections.IEnumerable)folders)sharedFolders.Items.Add(folder as string??"");
        fileSharingMode.SelectedIndex=Array.IndexOf(new[]{"all","selected","off"},selectedMode);
        originalFileSharing=new Dictionary<string,object>{{"schema",1},{"mode",selectedMode},{"folders",sharedFolders.Items.Cast<string>().ToArray()}};
    }
    internal bool TryGetFileSharingValue(out Dictionary<string,object> value)
    {
        value=null;
        if(fileSharingMode.SelectedIndex<0)return FileSharingError("Выбери режим доступа к файлам ПК.");
        if(sharedFolders.Items.Count>64)return FileSharingError("Можно выбрать не более 64 папок.");
        if(fileSharingMode.SelectedIndex==1&&sharedFolders.Items.Count==0)return FileSharingError("Добавь хотя бы одну папку для выбранного режима доступа.");
        List<string> folders=new List<string>();
        foreach(string path in sharedFolders.Items)
        {
            string folder=LocalFolderPath(path);
            if(folder==null)return FileSharingError("Папки должны иметь абсолютные локальные пути. Сетевые пути UNC недоступны.");
            if(folders.Contains(folder,StringComparer.OrdinalIgnoreCase))return FileSharingError("Удали повторяющиеся папки из списка.");
            folders.Add(folder);
        }
        value=new Dictionary<string,object>{{"schema",1},{"mode",new[]{"all","selected","off"}[fileSharingMode.SelectedIndex]},{"folders",folders.ToArray()}};
        Error.Text="";Error.Visible=false;return true;
    }
    internal void LoadViewerValues(Dictionary<string,object> settings)
    {
        originalViewer=new Dictionary<string,object>{{"schema",1},{"start_fullscreen",settings!=null&&settings.ContainsKey("start_fullscreen")&&settings["start_fullscreen"] is bool&&(bool)settings["start_fullscreen"]},{"remember_window",settings==null||!settings.ContainsKey("remember_window")||!(settings["remember_window"] is bool)||(bool)settings["remember_window"]}};
        StartFullscreen.Checked=(bool)originalViewer["start_fullscreen"];RememberWindow.Checked=(bool)originalViewer["remember_window"];
    }
    internal bool TryGetViewerValue(out Dictionary<string,object> value)
    {
        value=null;
        if(originalViewer==null){Error.Text="Сначала загрузи настройки панели.";Error.Visible=true;return false;}
        value=new Dictionary<string,object>{{"schema",1},{"start_fullscreen",StartFullscreen.Checked},{"remember_window",RememberWindow.Checked}};
        return true;
    }
    private static int ResourceValue(Dictionary<string,object> values,string key)
    {
        int number;return values!=null&&values.ContainsKey(key)&&Int32.TryParse(Convert.ToString(values[key],CultureInfo.InvariantCulture),NumberStyles.None,CultureInfo.InvariantCulture,out number)&&number>=0?number:-1;
    }
    internal void LoadHostValues(Dictionary<string,object> host)
    {
        hostTotal=ResourceValue(host,"ram_total_mib");hostAvailable=ResourceValue(host,"ram_available_mib");hostProcessors=ResourceValue(host,"logical_processors");
        hostMemory.Text=hostTotal>0&&hostAvailable>=0?"Память ПК: доступно "+((decimal)hostAvailable/1024).ToString("0.#",CultureInfo.CurrentCulture)+" из "+((decimal)hostTotal/1024).ToString("0.#",CultureInfo.CurrentCulture)+" ГиБ.":"Данные памяти ПК пока недоступны.";
        UpdateRecommendations();
    }
    private void UpdateRecommendations()
    {
        RecommendedButton.Enabled=hostTotal>0&&hostAvailable>=0&&hostProcessors>0&&ResourceValue(original,"gateway_memory_mib")>0;
    }
    private void ApplyRecommendedValues()
    {
        if(!RecommendedButton.Enabled)return;
        long memory=Math.Min(8192,Math.Min(hostTotal/2,(long)hostAvailable-ResourceValue(original,"gateway_memory_mib")-1024));memory=memory/256*256;
        if(memory<1024){Error.Text="Недостаточно свободной памяти: нужны минимум 1 ГиБ для Windows, память шлюза и 1 ГиБ для ПК. Освободи память и обнови данные ПК.";Error.Visible=true;return;}
        Fields["work_memory_mib"].Text=memory.ToString(CultureInfo.InvariantCulture);Fields["vcpus"].Text=Math.Min(4,Math.Max(1,hostProcessors/2)).ToString(CultureInfo.InvariantCulture);
        Error.Text="";Error.Visible=false;
    }
    internal void LoadWindowsValues(Dictionary<string,object> settings,Dictionary<string,object> disk = null)
    {
        originalWindows=settings==null?new Dictionary<string,object>{{"schema",1},{"width",1280},{"height",720},{"fit",true}}:new Dictionary<string,object>(settings);
        Resolution.SelectedIndex=-1;int width,height;object fit;
        if(!originalWindows.TryGetValue("fit",out fit)||!(fit is bool)||(bool)fit)Resolution.SelectedIndex=0;else
        if(originalWindows.ContainsKey("width")&&originalWindows.ContainsKey("height")
            &&Int32.TryParse(Convert.ToString(originalWindows["width"],CultureInfo.InvariantCulture),out width)
            &&Int32.TryParse(Convert.ToString(originalWindows["height"],CultureInfo.InvariantCulture),out height))
            for(int index=0;index<dimensions.GetLength(0);index++)if(dimensions[index,0]==width&&dimensions[index,1]==height)Resolution.SelectedIndex=index+1;
        if(Resolution.SelectedIndex<0){Error.Text="Сохранённое разрешение не поддерживается. Выбери значение из списка.";Error.Visible=true;}
        long bytes;
        if(disk!=null&&disk.ContainsKey("virtual_bytes")&&Int64.TryParse(Convert.ToString(disk["virtual_bytes"],CultureInfo.InvariantCulture),out bytes)&&bytes>0)
            diskCapacity.Text=((decimal)bytes/(1024L*1024*1024)).ToString("0.#",CultureInfo.CurrentCulture)+" ГиБ";
    }
    internal bool TryGetWindowsValue(out Dictionary<string,object> value)
    {
        value=null;
        if(originalWindows==null){Error.Text="Сначала загрузи настройки Windows.";Error.Visible=true;return false;}
        int index=Resolution.SelectedIndex-1;
        if(index<-1||index>=dimensions.GetLength(0)){Error.Text="Выбери разрешение рабочего стола из списка.";Error.Visible=true;return false;}
        if(index<0)
        {
            // Keep the saved fixed size as the fallback for when "as the PC screen" is switched off again.
            int width=1280,height=720;bool known=Int32.TryParse(Convert.ToString(originalWindows.ContainsKey("width")?originalWindows["width"]:null,CultureInfo.InvariantCulture),out width)&&Int32.TryParse(Convert.ToString(originalWindows.ContainsKey("height")?originalWindows["height"]:null,CultureInfo.InvariantCulture),out height)&&Enumerable.Range(0,dimensions.GetLength(0)).Any(item=>dimensions[item,0]==width&&dimensions[item,1]==height);
            value=new Dictionary<string,object>{{"schema",1},{"width",known?width:1280},{"height",known?height:720},{"fit",true}};
        }
        else value=new Dictionary<string,object>{{"schema",1},{"width",dimensions[index,0]},{"height",dimensions[index,1]},{"fit",false}};
        Error.Text="";Error.Visible=false;return true;
    }
    internal void LoadValues(Dictionary<string,object> settings,bool recovery)
    {
        original=new Dictionary<string,object>(settings);
        UpdateRecommendations();
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

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
    private readonly ListBox sharedFolders = new ListBox { Dock = DockStyle.Top, IntegralHeight = false, Height = 100, HorizontalScrollbar = true, BorderStyle = BorderStyle.FixedSingle, AccessibleName = "Выбранные папки ПК", Margin = new Padding(0,0,0,8) };
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
        TableLayoutPanel rows = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 2, RowCount = 8, Margin = Padding.Empty };
        rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,62));
        rows.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,38));
        for(int index=0;index<keys.Length;index++)
        {
            rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            Label help=AddCaption(rows,index,index==0?"Память Windows":"Процессоры",index==0?"1024 МиБ = 1 ГиБ оперативной памяти.":"Число выделенных виртуальных процессоров.");
            if(index==0)memoryGib=help;
            RoundedPanel field = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = NativeStyle.Background, Padding = new Padding(12,8,12,8), Margin = new Padding(0,5,0,5) };
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
            rows.Controls.Add(field,1,index);
        }
        rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        AddCaption(rows,2,"Разрешение","Размер рабочего стола Windows.");
        RoundedPanel resolutionBox = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, BackColor = NativeStyle.Background, Padding = new Padding(10,8,10,8), Margin = new Padding(0,5,0,5) };
        TableLayoutPanel resolutionContent = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = 1, Margin = Padding.Empty, BackColor = resolutionBox.BackColor };
        resolutionContent.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        RowStyle resolutionRow=new RowStyle(SizeType.Absolute,Resolution.Height);resolutionContent.RowStyles.Add(resolutionRow);
        Resolution.SizeChanged+=delegate{resolutionRow.Height=Resolution.Height;};
        // First choice: the PC monitor's proportions, so Windows never shows black bars.
        Resolution.Items.Add("Как экран ПК (рекомендуется)");
        for(int index=0;index<dimensions.GetLength(0);index++)Resolution.Items.Add(dimensions[index,0]+" × "+dimensions[index,1]);
        Resolution.SelectedIndex=0;
        resolutionContent.Controls.Add(Resolution,0,0);resolutionBox.Controls.Add(resolutionContent);rows.Controls.Add(resolutionBox,1,2);
        rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        AddCaption(rows,3,"Диск Windows","Файл находится в папке portable.");
        rows.Controls.Add(diskCapacity,1,3);
        for(int index=4;index<7;index++)rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        TableLayoutPanel hostResources=new TableLayoutPanel{Dock=DockStyle.Top,AutoSize=true,ColumnCount=2,RowCount=1,Margin=Padding.Empty};hostResources.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));hostResources.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));hostResources.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        hostResources.Controls.Add(hostMemory,0,0);hostResources.Controls.Add(RecommendedButton,1,0);rows.Controls.Add(hostResources,0,4);rows.SetColumnSpan(hostResources,2);
        hostResources.Layout+=delegate{hostMemory.MaximumSize=new Size(Math.Max(100,hostResources.ClientSize.Width-RecommendedButton.GetPreferredSize(Size.Empty).Width-hostMemory.Margin.Horizontal-RecommendedButton.Margin.Horizontal),0);};
        rows.Controls.Add(StartFullscreen,0,5);rows.SetColumnSpan(StartFullscreen,2);
        rows.Controls.Add(RememberWindow,0,6);rows.SetColumnSpan(RememberWindow,2);
        foreach(CheckBox option in new[]{StartFullscreen,RememberWindow})
        {
            option.SizeChanged+=delegate{FitViewerOption(option);};option.FontChanged+=delegate{FitViewerOption(option);};
        }
        RecommendedButton.Click+=delegate{ApplyRecommendedValues();};
        rows.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        TableLayoutPanel files = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 6, Margin = new Padding(0,12,0,0) };
        files.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for(int index=0;index<6;index++)files.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Label filesHeading = new Label { Text = "Доступ к файлам ПК", AutoSize = true, Dock = DockStyle.Top, Margin = Padding.Empty };
        Label filesNotice = new Label { AutoSize = true, Dock = DockStyle.Top, ForeColor = NativeStyle.Muted, Margin = new Padding(0,8,0,8) };
        Label filesGuide = new Label { Text = "Первое подключение: в Windows открой диск WCPFILES, запусти RUNFILES.CMD и подтверди запрос администратора. Потом диски ПК подключаются сами при входе.", AutoSize = true, Dock = DockStyle.Top, ForeColor = NativeStyle.Muted, Margin = new Padding(0,0,0,8) };
        fileSharingMode.SelectedIndexChanged+=delegate{
            filesNotice.Text=fileSharingMode.SelectedIndex==0
                ?"Запись из Windows меняет настоящие файлы ПК: с режимом всех дисков программы в Windows смогут изменить любой файл, доступный твоей учётной записи. Папка программы и автозагрузка ПК защищены. Настройки применяются при следующем запуске Windows."
                :"Запись из Windows меняет настоящие файлы ПК. Папка программы и автозагрузка ПК из Windows недоступны. Настройки применяются при следующем запуске Windows.";};
        fileSharingMode.Items.AddRange(new object[]{"Все доступные диски ПК","Только выбранные папки","Выключено"});fileSharingMode.SelectedIndex=2;
        FlowLayoutPanel folderActions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Margin = Padding.Empty };
        Button addFolder = new RoundedButton { Text = "Добавить папку", AutoSize = true, Padding = new Padding(10,6,10,6), Margin = new Padding(0,0,8,0) };
        Button removeFolder = new RoundedButton { Text = "Удалить", AutoSize = true, Padding = new Padding(10,6,10,6), Margin = Padding.Empty, Enabled = false };
        folderActions.Controls.Add(addFolder);folderActions.Controls.Add(removeFolder);
        addFolder.Click+=delegate{using(FolderBrowserDialog dialog=new FolderBrowserDialog { Description = "Выбери папку ПК для доступа из Windows", ShowNewFolderButton = false })if(dialog.ShowDialog(this)==DialogResult.OK)AddFileSharingFolder(dialog.SelectedPath);};
        removeFolder.Click+=delegate{if(sharedFolders.SelectedIndex>=0)sharedFolders.Items.RemoveAt(sharedFolders.SelectedIndex);};
        sharedFolders.SelectedIndexChanged+=delegate{removeFolder.Enabled=sharedFolders.SelectedIndex>=0;};
        files.Controls.Add(filesHeading,0,0);files.Controls.Add(fileSharingMode,0,1);files.Controls.Add(sharedFolders,0,2);files.Controls.Add(folderActions,0,3);files.Controls.Add(filesNotice,0,4);files.Controls.Add(filesGuide,0,5);
        files.SizeChanged+=delegate{filesHeading.MaximumSize=filesNotice.MaximumSize=filesGuide.MaximumSize=new Size(Math.Max(100,files.ClientSize.Width),0);};
        sharedFolders.FontChanged+=delegate{sharedFolders.Height=sharedFolders.ItemHeight*4+6;};
        rows.Controls.Add(files,0,7);rows.SetColumnSpan(files,2);
        rows.Paint += delegate(object sender,PaintEventArgs args) {
            int position=0;int[] heights=rows.GetRowHeights();
            using(Pen line=new Pen(Color.FromArgb(237,237,241)))
                for(int index=0;index<heights.Length-1;index++){position+=heights[index];args.Graphics.DrawLine(line,0,position,rows.Width,position);}
        };
        group.Controls.Add(rows);scroll.Controls.Add(group);root.Controls.Add(scroll,0,2);root.Controls.Add(Error,0,3);
        FlowLayoutPanel footer = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.RightToLeft, WrapContents = false, Margin = new Padding(0,12,0,0) };
        footer.Controls.Add(SaveButton);footer.Controls.Add(CancelButton);root.Controls.Add(footer,0,4);
        CancelButton.Click += delegate {if(original!=null)LoadValues(original,false);if(originalWindows!=null)LoadWindowsValues(originalWindows);if(originalViewer!=null)LoadViewerValues(originalViewer);if(originalFileSharing!=null)LoadFileSharingValues(originalFileSharing);};
        root.SizeChanged += delegate { int width=Math.Max(100,root.ClientSize.Width-root.Padding.Horizontal);notice.MaximumSize=Error.MaximumSize=new Size(width,0); };
        Controls.Add(root);
    }
    private static Label AddCaption(TableLayoutPanel rows,int row,string title,string explanation)
    {
        TableLayoutPanel caption = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 1, RowCount = 2, Margin = new Padding(0,8,12,8) };
        caption.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));caption.RowStyles.Add(new RowStyle(SizeType.AutoSize));caption.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Label heading = new Label { Text = title, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9.5f), Margin = Padding.Empty };
        Label help = new Label { Text = explanation, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",8.5f), ForeColor = NativeStyle.Muted, Margin = new Padding(0,4,0,0) };
        caption.Controls.Add(heading,0,0);caption.Controls.Add(help,0,1);
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

using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal sealed class SettingsEditor : UserControl
{
    internal readonly Dictionary<string, TextBox> Fields = new Dictionary<string, TextBox>();
    internal readonly ComboBox Resolution = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, BackColor = NativeStyle.Background, AccessibleName = "Разрешение рабочего стола Windows", AccessibleDescription = "Размер рабочего стола Windows, применяется после следующего запуска.", Dock = DockStyle.Top, Margin = Padding.Empty };
    internal readonly Label Error = new Label { Font = new Font("Segoe UI",9), AutoSize = true, Dock = DockStyle.Fill, Visible = false, ForeColor = Color.FromArgb(175,59,48), Margin = new Padding(0,8,0,0) };
    internal readonly Button SaveButton = new RoundedButton { Text = "Сохранить", Hint = "Ctrl+S", Primary = true, AutoSize = true, Padding = new Padding(18,8,18,8), Margin = new Padding(10,0,0,0) };
    internal readonly Button CancelButton = new RoundedButton { Text = "Отменить", AutoSize = true, Padding = new Padding(16,8,16,8), Margin = Padding.Empty };
    internal readonly CheckBox StartFullscreen = new CheckBox { Text = "Открывать Windows на весь экран", Dock = DockStyle.Top, Font = new Font("Segoe UI Semibold",10), Margin = Padding.Empty };
    internal readonly CheckBox RememberWindow = new CheckBox { Text = "Запоминать размер и положение панели", Dock = DockStyle.Top, Font = new Font("Segoe UI Semibold",10), Margin = Padding.Empty };
    internal readonly Button RecommendedButton = new RoundedButton { Text = "Поставить рекомендуемые значения", AutoSize = true, Anchor = AnchorStyles.Left, Padding = new Padding(16,8,16,8), Margin = new Padding(0,14,0,0), Enabled = false };
    internal readonly TextBox MemoryInput = new TextBox { Width = 130, TextAlign = HorizontalAlignment.Right, Font = new Font("Segoe UI Semibold",13), Anchor = AnchorStyles.Right, Margin = Padding.Empty, AccessibleName = "Память Windows в ГиБ", AccessibleDescription = "Введи число от 1 до 32. Дробную часть можно отделить запятой или точкой." };
    private readonly string[] keys = { "work_memory_mib", "vcpus" };
    private readonly string[] labels = { "Память Windows", "Процессоры каждой VM" };
    private readonly int[] minimum = {1024,1};
    private readonly int[] maximum = {32768,32};
    private Dictionary<string,object> original;
    private Dictionary<string,object> originalWindows;
    private Dictionary<string,object> originalViewer;
    private Dictionary<string,object> originalFileSharing;
    private int loadedResolution = -1;
    private bool syncing;
    private Control content;
    private readonly Label loading = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter, Text = "Загружаем настройки…", AccessibleName = "Состояние загрузки настроек", Visible = false };
    internal bool Loading { get; private set; }
    internal bool HasValues { get { return original != null; } }
    internal void SetLoading(bool value, string message = null)
    {
        loading.Text = message ?? "Загружаем настройки…";
        if (Loading == value) return;
        SuspendLayout();
        try
        {
            Loading = value;
            Control next = value ? (Control)loading : content;
            next.Visible = true; next.BringToFront();
            (value ? content : loading).Visible = false;
        }
        finally { ResumeLayout(true); }
    }
    private readonly ComboBox fileSharingMode = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, FlatStyle = FlatStyle.Flat, Dock = DockStyle.Top, BackColor = NativeStyle.Background, AccessibleName = "Доступ к файлам ПК", Margin = new Padding(0,8,0,8) };
    private readonly ListBox sharedFolders = new ListBox { Dock = DockStyle.Top, IntegralHeight = false, Height = 100, DrawMode = DrawMode.OwnerDrawFixed, BorderStyle = BorderStyle.None, BackColor = Color.White, AccessibleName = "Выбранные папки ПК", Margin = Padding.Empty, AllowDrop = true };
    private readonly List<RoundedButton> modeButtons = new List<RoundedButton>();
    private int hostTotal = -1, hostAvailable = -1, hostProcessors = -1;
    private readonly Label hostMemory = new Label { Text = "Данные памяти ПК пока недоступны.", AutoSize = true, Anchor = AnchorStyles.None, TextAlign = ContentAlignment.MiddleCenter, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = new Padding(8,0,8,0) };
    private Label cpuHelp;
    private readonly int[,] dimensions = {{800,600},{1024,768},{1280,720},{1280,800},{1366,768},{1440,900},{1600,900},{1680,1050},{1920,1080},{1920,1200},{2560,1440}};
    private readonly Label diskCapacity = new Label { Text = "80 ГиБ", AutoSize = true, Anchor = AnchorStyles.Right, Margin = Padding.Empty };
    private readonly Label notice = new Label { Text = "Применяется при следующем запуске Windows", Font = new Font("Segoe UI",9), AutoSize = true, ForeColor = NativeStyle.Muted, Margin = Padding.Empty };
    private readonly Pill dirty = new Pill { Text = "Есть несохранённые изменения", AutoSize = true, Anchor = AnchorStyles.Left, BackColor = Color.White, ForeColor = Color.FromArgb(138,90,0), Dot = Color.FromArgb(224,161,0), Padding = new Padding(16,4,0,4), Margin = Padding.Empty, Visible = false };

    internal SettingsEditor()
    {
        Section = -1;
        DoubleBuffered = true;
        Dock = DockStyle.Fill;
        BackColor = NativeStyle.Background;
        Font = new Font("Segoe UI",10);
        TableLayoutPanel root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5, Margin = Padding.Empty };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for(int index=0;index<5;index++)root.RowStyles.Add(index==2?new RowStyle(SizeType.Percent,100):new RowStyle(SizeType.AutoSize));
        root.Controls.Add(NativeStyle.PageHeader(root,"Настройки",notice),0,0);
        // Three short sections instead of one long scrolling form; each fits the window on its own.
        RoundedPanel tabs = new RoundedPanel { Dock = DockStyle.Top, Height = 40, Radius = 11, BackColor = Color.FromArgb(227,227,232), Padding = new Padding(3), Margin = new Padding(0,0,0,16), AccessibleName = "Разделы настроек", AccessibleRole = AccessibleRole.PageTabList };
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
        tabs.FontChanged+=delegate{tabs.Height=TextRenderer.MeasureText("Ресурсы",tabs.Font).Height+20;};
        root.Controls.Add(tabs,0,1);
        Panel scroll = new BufferedPanel { Dock = DockStyle.Fill, AutoScroll = true, Margin = Padding.Empty };

        // Resources: numeric GiB input, CPU stepper, read-only disk; stored memory stays in MiB.
        for(int index=0;index<keys.Length;index++)
        {
            TextBox input = new TextBox { Font = new Font("Segoe UI Semibold",10), BorderStyle = BorderStyle.None, TextAlign = HorizontalAlignment.Center, BackColor = Color.White, Width = 44, Anchor = AnchorStyles.None, Margin = Padding.Empty, AccessibleName = labels[index] };
            Fields.Add(keys[index],input);
            input.TextChanged+=delegate{UpdateDirty();};
        }
        TextBox memory=Fields["work_memory_mib"],cpu=Fields["vcpus"];
        memory.Visible=false;
        memory.TextChanged+=delegate{
            if(syncing)return;
            int mib;syncing=true;
            MemoryInput.Text=Int32.TryParse(memory.Text,NumberStyles.None,CultureInfo.InvariantCulture,out mib)?Gib(mib,"0.##########"):"";
            syncing=false;};
        MemoryInput.TextChanged+=delegate{
            if(syncing)return;
            decimal gib;bool valid=Decimal.TryParse(MemoryInput.Text.Trim().Replace(',','.'),NumberStyles.AllowDecimalPoint,CultureInfo.InvariantCulture,out gib)&&gib>=1&&gib<=32;
            syncing=true;
            memory.Text=valid?Decimal.Round(gib*1024,0,MidpointRounding.AwayFromZero).ToString(CultureInfo.InvariantCulture):"";
            syncing=false;};
        memory.EnabledChanged+=delegate{MemoryInput.Enabled=memory.Enabled;};
        Label unit = new Label { Text = "ГиБ", AutoSize = true, Anchor = AnchorStyles.Left, Margin = new Padding(8,0,0,0) };
        TableLayoutPanel memoryRow = Stack(Line(0,Title("Память Windows"),Line(-1,MemoryInput,unit)),hostMemory,memory);
        hostMemory.Anchor=AnchorStyles.Left;hostMemory.TextAlign=ContentAlignment.MiddleLeft;hostMemory.Margin=new Padding(0,8,0,0);
        memoryRow.SizeChanged+=delegate{hostMemory.MaximumSize=new Size(Math.Max(100,memoryRow.ClientSize.Width),0);};
        RoundedPanel stepper = new RoundedPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Anchor = AnchorStyles.Right, Radius = 10, BackColor = Color.FromArgb(209,209,214), Padding = new Padding(1), Margin = Padding.Empty };
        RoundedButton fewer = StepButton("−","Меньше процессоров"), more = StepButton("+","Больше процессоров");
        TableLayoutPanel stepperRow = Line(-1,fewer,cpu,more);stepperRow.BackColor=Color.White;stepperRow.Dock=DockStyle.None;stepperRow.Location=new Point(1,1);stepper.Controls.Add(stepperRow);
        fewer.Click+=delegate{StepCpu(-1);};more.Click+=delegate{StepCpu(1);};
        cpu.EnabledChanged+=delegate{fewer.Enabled=more.Enabled=cpu.Enabled;};
        Control cpuCaption=Caption("Процессоры","Ядра у Windows.",out cpuHelp);
        Label diskHelp;
        TableLayoutPanel resources = Stack(Card(Rows(memoryRow,Line(0,cpuCaption,stepper),Line(0,Caption("Диск Windows","Файл внутри папки программы.",out diskHelp),diskCapacity))),RecommendedButton);
        RecommendedButton.Click+=delegate{ApplyRecommendedValues();};

        // Display: the fixed desktop size, how the window opens, and a picture of the chosen proportions.
        RoundedPanel resolutionBox = new RoundedPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Anchor = AnchorStyles.Right, Radius = 10, BackColor = NativeStyle.Background, Padding = new Padding(10,8,10,8), Margin = Padding.Empty };
        TableLayoutPanel resolutionContent = new TableLayoutPanel { Location = new Point(10,8), AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = 1, Margin = Padding.Empty, BackColor = resolutionBox.BackColor };
        resolutionContent.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute,200));
        RowStyle resolutionRow=new RowStyle(SizeType.Absolute,Resolution.Height);resolutionContent.RowStyles.Add(resolutionRow);
        Resolution.SizeChanged+=delegate{resolutionRow.Height=Resolution.Height;};
        // Standard sizes only: Windows saves its mode, and the firmware restores only standard ones at the next start.
        for(int index=0;index<dimensions.GetLength(0);index++)Resolution.Items.Add(dimensions[index,0]+" × "+dimensions[index,1]+(dimensions[index,0]==1920&&dimensions[index,1]==1080?" (рекомендуется)":""));
        Resolution.SelectedIndex=DefaultResolution;
        Resolution.SelectedIndexChanged+=delegate{UpdateDirty();};
        resolutionContent.Controls.Add(Resolution,0,0);resolutionBox.Controls.Add(resolutionContent);
        Label unused;
        Control fullscreenOption=Option(StartFullscreen,"Переключение в любой момент: Ctrl+Alt+F"),rememberOption=Option(RememberWindow,"Панель откроется там же, где была закрыта");
        foreach(CheckBox option in new[]{StartFullscreen,RememberWindow})
        {
            option.SizeChanged+=delegate{FitViewerOption(option);};option.FontChanged+=delegate{FitViewerOption(option);};
            option.CheckedChanged+=delegate{UpdateDirty();};
        }
        Panel preview = new Panel { Size = new Size(144,90), Anchor = AnchorStyles.Left, BackColor = Color.White, Margin = new Padding(0,0,18,0) };
        preview.Paint+=delegate(object sender,PaintEventArgs args){DrawPreview(args.Graphics,preview.ClientRectangle);};
        Resolution.SelectedIndexChanged+=delegate{preview.Invalidate();};
        Label previewText = new Label { Text = "Окно сохраняет пропорции рабочего стола и уменьшается, если не помещается на мониторе. Его можно перемещать и сворачивать. Полный экран: Ctrl+Alt+F.", AutoSize = true, Anchor = AnchorStyles.Left, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = Padding.Empty };
        TableLayoutPanel previewRow = Line(1,preview,previewText);
        previewRow.SizeChanged+=delegate{previewText.MaximumSize=new Size(Math.Max(100,previewRow.ClientSize.Width-preview.Width-preview.Margin.Horizontal),0);};
        RoundedPanel previewCard=Card(previewRow);previewCard.Margin=new Padding(0,14,0,0);previewCard.Padding=new Padding(18,14,18,14);
        TableLayoutPanel display = Stack(Card(Rows(Line(0,Caption("Размер экрана Windows","Рабочий стол внутри Windows; рекомендуется 1920 × 1080.",out unused),resolutionBox),fullscreenOption,rememberOption)),previewCard);

        // Files: the three modes as cards; the hidden list box stays the single source of the choice.
        RoundedPanel modes = new RoundedPanel { Dock = DockStyle.Top, Height = 64, Radius = 11, BackColor = Color.FromArgb(227,227,232), Padding = new Padding(3), Margin = new Padding(0,0,0,14), AccessibleName = "Доступ к файлам ПК: режим", AccessibleRole = AccessibleRole.PageTabList };
        TableLayoutPanel modeRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, Margin = Padding.Empty, BackColor = modes.BackColor };
        string[] modeNames={"Выключено","Выбранные папки","Все диски ПК"},modeNotes={"Windows не видит файлы ПК","Только папки из списка","Запись меняет любые файлы ПК"};int[] modeIndex={2,1,0};
        for(int index=0;index<3;index++)
        {
            int choice=modeIndex[index];modeRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,33.33f));
            RoundedButton button=new RoundedButton { Text = modeNames[index], Note = modeNotes[index], Segment = true, Dock = DockStyle.Fill, Margin = Padding.Empty, Tag = choice, AccessibleName = modeNames[index], AccessibleDescription = modeNotes[index] };
            button.Click+=delegate{fileSharingMode.SelectedIndex=choice;};modeButtons.Add(button);modeRow.Controls.Add(button,index,0);
        }
        modeRow.RowStyles.Add(new RowStyle(SizeType.Percent,100));modes.Controls.Add(modeRow);
        modes.FontChanged+=delegate{modes.Height=modeButtons[0].GetPreferredSize(Size.Empty).Height+8;};
        fileSharingMode.Visible=false;
        sharedFolders.DrawItem+=delegate(object sender,DrawItemEventArgs args){DrawFolder(args);};
        sharedFolders.FontChanged+=delegate{sharedFolders.ItemHeight=TextRenderer.MeasureText("Ag",sharedFolders.Font).Height*2+12;sharedFolders.Height=sharedFolders.ItemHeight*3+2;};
        sharedFolders.DragEnter+=delegate(object sender,DragEventArgs args){args.Effect=args.Data.GetDataPresent(DataFormats.FileDrop)?DragDropEffects.Link:DragDropEffects.None;};
        // Each dropped path goes through the same validation as the folder dialog; the first refusal stays visible.
        sharedFolders.DragDrop+=delegate(object sender,DragEventArgs args){
            string[] paths=args.Data.GetData(DataFormats.FileDrop) as string[];if(paths==null)return;string refusal=null;
            foreach(string path in paths)if(!AddFileSharingFolder(path)&&refusal==null)refusal=Error.Text;
            if(refusal!=null)FileSharingError(refusal);};
        Label filesNotice = new Label { AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = new Padding(0,12,0,0) };
        Label firstRun = new Label { Text = "Первое подключение: в Windows открой диск WCPFILES, запусти RUNFILES.CMD и подтверди запрос. Дальше папки подключаются сами при входе.", AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = new Padding(0,6,0,0) };
        Button addFolder = new RoundedButton { Text = "Добавить папку", AutoSize = true, Padding = new Padding(12,6,12,6), Margin = new Padding(0,0,8,0) };
        Button removeFolder = new RoundedButton { Text = "Убрать", AutoSize = true, Padding = new Padding(12,6,12,6), Margin = new Padding(0,0,12,0), Enabled = false };
        Label dropHint = new Label { Text = "или перетащи папку в список", AutoSize = true, Anchor = AnchorStyles.Left, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = Padding.Empty };
        TableLayoutPanel folderActions = Line(2,addFolder,removeFolder,dropHint);folderActions.Margin=new Padding(0,10,0,0);
        RoundedPanel folderCard=Card(Stack(sharedFolders,folderActions));folderCard.Padding=new Padding(12,10,12,12);
        fileSharingMode.SelectedIndexChanged+=delegate{
            foreach(RoundedButton button in modeButtons)button.Selected=(int)button.Tag==fileSharingMode.SelectedIndex;
            folderCard.Visible=fileSharingMode.SelectedIndex==1;firstRun.Visible=fileSharingMode.SelectedIndex!=2;
            filesNotice.Text=fileSharingMode.SelectedIndex==2
                ?"Windows не видит файлы ПК. Папка программы и автозагрузка ПК недоступны из Windows в любом режиме."
                :fileSharingMode.SelectedIndex==0
                ?"Запись из Windows меняет любые файлы ПК, кроме программы и автозагрузки."
                :"Запись из Windows меняет настоящие файлы ПК. Папка программы и автозагрузка ПК всегда защищены.";
            UpdateDirty();};
        fileSharingMode.Items.AddRange(new object[]{"Все доступные диски ПК","Только выбранные папки","Выключено"});fileSharingMode.SelectedIndex=2;
        addFolder.Click+=delegate{using(FolderBrowserDialog dialog=new FolderBrowserDialog { Description = "Выбери папку ПК для доступа из Windows", ShowNewFolderButton = false })if(dialog.ShowDialog(this)==DialogResult.OK)AddFileSharingFolder(dialog.SelectedPath);};
        removeFolder.Click+=delegate{if(sharedFolders.SelectedIndex>=0){sharedFolders.Items.RemoveAt(sharedFolders.SelectedIndex);UpdateDirty();}};
        sharedFolders.SelectedIndexChanged+=delegate{removeFolder.Enabled=sharedFolders.SelectedIndex>=0;};
        fileControls.Add(modes);fileControls.Add(folderCard);
        TableLayoutPanel files = Stack(modes,fileSharingMode,folderCard,filesNotice,firstRun);
        files.SizeChanged+=delegate{filesNotice.MaximumSize=firstRun.MaximumSize=new Size(Math.Max(100,files.ClientSize.Width),0);};

        foreach(TableLayoutPanel section in new[]{resources,display,files}){sections.Add(section);scroll.Controls.Add(section);}
        ShowSection(0);
        root.Controls.Add(scroll,0,2);root.Controls.Add(Error,0,3);
        // The action bar: unsaved-changes note on the left, Cancel and Save on the right.
        TableLayoutPanel actions = Line(0,dirty,CancelButton,SaveButton);actions.BackColor=Color.White;
        RoundedPanel footer=Card(actions);footer.Padding=new Padding(16,10,12,10);footer.Margin=new Padding(0,12,0,0);
        root.Controls.Add(footer,0,4);
        CancelButton.Click += delegate {if(original!=null)LoadValues(original,false);if(originalWindows!=null)LoadWindowsValues(originalWindows);if(originalViewer!=null)LoadViewerValues(originalViewer);if(originalFileSharing!=null)LoadFileSharingValues(originalFileSharing);};
        root.SizeChanged += delegate { int width=Math.Max(100,root.ClientSize.Width);notice.MaximumSize=new Size(Math.Max(100,width/2),0);Error.MaximumSize=new Size(width,0); };
        content=root;Controls.Add(root);Controls.Add(loading);
    }
    private readonly List<Control> fileControls = new List<Control>();
    // Locked with the other fields while an operation may reload them, so a drop or mode change is not lost.
    internal void SetFilesEnabled(bool enabled) { foreach(Control control in fileControls)control.Enabled=enabled;sharedFolders.AllowDrop=enabled; }
    internal void SetEditingEnabled(bool enabled)
    {
        foreach(Control section in sections)section.Enabled=enabled;
        SetFilesEnabled(enabled);
    }
    private readonly List<Control> sections = new List<Control>();
    private readonly List<RoundedButton> sectionButtons = new List<RoundedButton>();
    internal int Section { get; private set; }
    internal void ShowSection(int section)
    {
        if(section<0||section>=sections.Count||section==Section)return;
        Control parent=sections[section].Parent;parent.SuspendLayout();
        try
        {
            Section=section;
            sections[section].Visible=true;sections[section].BringToFront();
            for(int index=0;index<sections.Count;index++)if(index!=section)sections[index].Visible=false;
            for(int index=0;index<sections.Count;index++)sectionButtons[index].Selected=index==section;
        }
        finally { parent.ResumeLayout(true); }
    }
    // Layout helpers: a vertical stack, one line with a stretching cell, a white card, rows with separators.
    private static TableLayoutPanel Stack(params Control[] items)
    {
        TableLayoutPanel stack = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = 1, RowCount = items.Length, Margin = Padding.Empty };
        stack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        for(int index=0;index<items.Length;index++){stack.RowStyles.Add(new RowStyle(SizeType.AutoSize));stack.Controls.Add(items[index],0,index);}
        return stack;
    }
    private static TableLayoutPanel Line(int fill,params Control[] cells)
    {
        TableLayoutPanel line = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, ColumnCount = cells.Length, RowCount = 1, Margin = Padding.Empty };
        for(int index=0;index<cells.Length;index++){line.ColumnStyles.Add(index==fill?new ColumnStyle(SizeType.Percent,100):new ColumnStyle(SizeType.AutoSize));line.Controls.Add(cells[index],index,0);}
        line.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        return line;
    }
    private static RoundedPanel Card(Control content)
    {
        RoundedPanel card = new RoundedPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, Radius = 14, BackColor = Color.White, Padding = new Padding(22,4,22,4), Margin = Padding.Empty };
        content.Dock=DockStyle.Top;card.Controls.Add(content);
        return card;
    }
    private static TableLayoutPanel Rows(params Control[] rows)
    {
        foreach(Control row in rows)row.Margin=new Padding(0,14,0,14);
        TableLayoutPanel stack=Stack(rows);stack.BackColor=Color.White;
        stack.Paint += delegate(object sender,PaintEventArgs args) {
            int position=0;int[] heights=stack.GetRowHeights();
            using(Pen line=new Pen(Color.FromArgb(237,237,241)))
                for(int index=0;index<heights.Length-1;index++){position+=heights[index];args.Graphics.DrawLine(line,0,position,stack.Width,position);}
        };
        return stack;
    }
    private static Label Title(string text) { return new Label { Text = text, AutoSize = true, Anchor = AnchorStyles.Left, Font = new Font("Segoe UI Semibold",10), Margin = Padding.Empty }; }
    private static Control Caption(string title,string explanation,out Label help)
    {
        Label heading=Title(title);heading.Dock=DockStyle.Top;
        Label text = new Label { Text = explanation, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = new Padding(0,2,0,0) };
        TableLayoutPanel caption=Stack(heading,text);caption.Margin=new Padding(0,0,16,0);caption.Anchor=AnchorStyles.Left|AnchorStyles.Right;caption.Dock=DockStyle.None;
        caption.SizeChanged+=delegate{heading.MaximumSize=text.MaximumSize=new Size(Math.Max(100,caption.ClientSize.Width),0);};
        help=text;return caption;
    }
    private static Control Option(CheckBox option,string explanation)
    {
        Label text = new Label { Text = explanation, AutoSize = true, Dock = DockStyle.Top, Font = new Font("Segoe UI",9), ForeColor = NativeStyle.Muted, Margin = new Padding(20,0,0,0) };
        TableLayoutPanel stack=Stack(option,text);
        stack.SizeChanged+=delegate{text.MaximumSize=new Size(Math.Max(100,stack.ClientSize.Width-20),0);};
        return stack;
    }
    private static RoundedButton StepButton(string text,string name)
    {
        return new RoundedButton { Text = text, CenterText = true, Size = new Size(40,36), Font = new Font("Segoe UI",12), Margin = Padding.Empty, AccessibleName = name };
    }
    private static string Gib(long mib,string format) { return ((decimal)mib/1024).ToString(format,CultureInfo.CurrentCulture); }
    private int CpuLimit { get { return hostProcessors>0?Math.Min(maximum[1],hostProcessors):maximum[1]; } }
    private void StepCpu(int delta)
    {
        int count;
        if(!Int32.TryParse(Fields["vcpus"].Text.Trim(),NumberStyles.None,CultureInfo.InvariantCulture,out count))count=1;
        Fields["vcpus"].Text=Math.Max(minimum[1],Math.Min(CpuLimit,count+delta)).ToString(CultureInfo.InvariantCulture);
    }
    private void DrawPreview(Graphics graphics,Rectangle bounds)
    {
        int index=Resolution.SelectedIndex,width=index>=0?dimensions[index,0]:1920,height=index>=0?dimensions[index,1]:1080;
        int frameWidth=bounds.Width-4,frameHeight=Math.Min(bounds.Height-4,frameWidth*height/width);frameWidth=frameHeight*width/height;
        Rectangle frame=new Rectangle(bounds.Left+(bounds.Width-frameWidth)/2,bounds.Top+(bounds.Height-frameHeight)/2,frameWidth,frameHeight);
        graphics.Clear(Color.White);graphics.SmoothingMode=SmoothingMode.AntiAlias;
        using(GraphicsPath shape=NativeStyle.Rounded(frame,8))using(Brush fill=new SolidBrush(Color.FromArgb(232,241,250)))using(Pen pen=new Pen(NativeStyle.Accent,1.5f)){graphics.FillPath(fill,shape);graphics.DrawPath(pen,shape);}
        TextRenderer.DrawText(graphics,width+" × "+height,Font,frame,NativeStyle.Accent,TextFormatFlags.HorizontalCenter|TextFormatFlags.VerticalCenter|TextFormatFlags.NoPrefix);
    }
    private void DrawFolder(DrawItemEventArgs args)
    {
        if(args.Index<0)return;
        bool selected=(args.State&DrawItemState.Selected)!=0;
        using(Brush fill=new SolidBrush(selected?Color.FromArgb(220,234,248):Color.White))args.Graphics.FillRectangle(fill,args.Bounds);
        int size=TextRenderer.MeasureText("Ag",sharedFolders.Font).Height;Rectangle icon=new Rectangle(args.Bounds.Left+6,args.Bounds.Top+(args.Bounds.Height-size)/2,size+4,size);
        using(Brush folder=new SolidBrush(Color.FromArgb(224,161,0)))using(GraphicsPath shape=NativeStyle.Rounded(icon,3)){args.Graphics.SmoothingMode=SmoothingMode.AntiAlias;args.Graphics.FillPath(folder,shape);}
        string path=sharedFolders.Items[args.Index] as string??"",name=Path.GetFileName(path.TrimEnd('\\','/'));
        int line=(args.Bounds.Height-8)/2;Rectangle text=new Rectangle(icon.Right+12,args.Bounds.Top+4,args.Bounds.Width-icon.Right-16,line);
        using(Font bold=new Font(sharedFolders.Font,FontStyle.Bold))TextRenderer.DrawText(args.Graphics,String.IsNullOrEmpty(name)?path:name,bold,text,NativeStyle.Ink,TextFormatFlags.Left|TextFormatFlags.Bottom|TextFormatFlags.EndEllipsis|TextFormatFlags.NoPrefix);
        text.Offset(0,line);
        TextRenderer.DrawText(args.Graphics,path,sharedFolders.Font,text,NativeStyle.Muted,TextFormatFlags.Left|TextFormatFlags.Top|TextFormatFlags.PathEllipsis|TextFormatFlags.NoPrefix);
        if(args.Index<sharedFolders.Items.Count-1)using(Pen separator=new Pen(Color.FromArgb(237,237,241)))args.Graphics.DrawLine(separator,text.Left,args.Bounds.Bottom-1,args.Bounds.Right,args.Bounds.Bottom-1);
    }
    private static void FitViewerOption(CheckBox option)
    {
        option.Height=TextRenderer.MeasureText(option.Text,option.Font,new Size(Math.Max(1,option.Width-32),0),TextFormatFlags.WordBreak).Height+8;
    }
    // Any loaded value edited and not yet saved; Cancel and Save both bring this back to false.
    internal bool Dirty
    {
        get
        {
            if(original!=null&&keys.Any(key=>Fields[key].Text.Trim()!=Convert.ToString(original[key],CultureInfo.InvariantCulture)))return true;
            if(originalWindows!=null&&Resolution.SelectedIndex!=loadedResolution)return true;
            if(originalViewer!=null&&(StartFullscreen.Checked!=(bool)originalViewer["start_fullscreen"]||RememberWindow.Checked!=(bool)originalViewer["remember_window"]))return true;
            if(originalFileSharing==null)return false;
            string mode=fileSharingMode.SelectedIndex>=0?new[]{"all","selected","off"}[fileSharingMode.SelectedIndex]:null;
            return mode!=originalFileSharing["mode"] as string||!((string[])originalFileSharing["folders"]).SequenceEqual(sharedFolders.Items.Cast<string>());
        }
    }
    private void UpdateDirty() { dirty.Visible=Dirty; }
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
        sharedFolders.Items.Add(folder);Error.Text="";Error.Visible=false;UpdateDirty();return true;
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
        UpdateDirty();
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
        UpdateDirty();
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
        int total=ResourceValue(host,"ram_total_mib"),available=ResourceValue(host,"ram_available_mib"),processors=ResourceValue(host,"logical_processors");
        if(hostTotal==total&&hostAvailable==available&&hostProcessors==processors)return;
        hostTotal=total;hostAvailable=available;hostProcessors=processors;
        UpdateRecommendations();
    }
    // Half the PC memory, at most 8 GiB, leaving the gateway and 1 GiB free; -1 without host evidence.
    private long RecommendedMemory()
    {
        int gateway=ResourceValue(original,"gateway_memory_mib");
        if(hostTotal<=0||hostAvailable<0||gateway<=0)return -1;
        long memory=Math.Min(8192,Math.Min(hostTotal/2,(long)hostAvailable-gateway-1024));return memory/256*256;
    }
    private void UpdateRecommendations()
    {
        RecommendedButton.Enabled=hostTotal>0&&hostAvailable>=0&&hostProcessors>0&&ResourceValue(original,"gateway_memory_mib")>0;
        long recommended=RecommendedMemory();
        string free=hostTotal>0&&hostAvailable>=0?"на ПК свободно "+Gib(hostAvailable,"0.#")+" из "+Gib(hostTotal,"0.#")+" ГиБ":null;
        hostMemory.Text=free==null?"Данные памяти ПК пока недоступны.":recommended>=1024?"Рекомендуется "+Gib(recommended,"0.##")+" ГиБ, "+free:"Н"+free.Substring(1);
        cpuHelp.Text=hostProcessors>0?"Ядра у Windows; у ПК "+hostProcessors+" логических":"Ядра у Windows.";

    }
    private void ApplyRecommendedValues()
    {
        if(!RecommendedButton.Enabled)return;
        long memory=RecommendedMemory();
        if(memory<1024){Error.Text="Недостаточно свободной памяти: нужны минимум 1 ГиБ для Windows, память шлюза и 1 ГиБ для ПК. Освободи память и обнови данные ПК.";Error.Visible=true;return;}
        Fields["work_memory_mib"].Text=memory.ToString(CultureInfo.InvariantCulture);Fields["vcpus"].Text=Math.Min(4,Math.Max(1,hostProcessors/2)).ToString(CultureInfo.InvariantCulture);
        Error.Text="";Error.Visible=false;
    }
    int DefaultResolution{get{return Enumerable.Range(0,dimensions.GetLength(0)).First(item=>dimensions[item,0]==1920&&dimensions[item,1]==1080);}}
    internal void LoadWindowsValues(Dictionary<string,object> settings,Dictionary<string,object> disk = null)
    {
        originalWindows=settings==null?new Dictionary<string,object>{{"schema",1},{"width",1920},{"height",1080},{"fit",false}}:new Dictionary<string,object>(settings);
        Resolution.SelectedIndex=-1;int width,height;object fit;
        // The old "as the PC screen" choice is gone; the backend already reads it as the default size.
        if(originalWindows.TryGetValue("fit",out fit)&&fit is bool&&(bool)fit)Resolution.SelectedIndex=DefaultResolution;else
        if(originalWindows.ContainsKey("width")&&originalWindows.ContainsKey("height")
            &&Int32.TryParse(Convert.ToString(originalWindows["width"],CultureInfo.InvariantCulture),out width)
            &&Int32.TryParse(Convert.ToString(originalWindows["height"],CultureInfo.InvariantCulture),out height))
            for(int index=0;index<dimensions.GetLength(0);index++)if(dimensions[index,0]==width&&dimensions[index,1]==height)Resolution.SelectedIndex=index;
        if(Resolution.SelectedIndex<0){Error.Text="Сохранённое разрешение не поддерживается. Выбери значение из списка.";Error.Visible=true;}
        long bytes;
        if(disk!=null&&disk.ContainsKey("virtual_bytes")&&Int64.TryParse(Convert.ToString(disk["virtual_bytes"],CultureInfo.InvariantCulture),out bytes)&&bytes>0)
            diskCapacity.Text=((decimal)bytes/(1024L*1024*1024)).ToString("0.#",CultureInfo.CurrentCulture)+" ГиБ";
        loadedResolution=Resolution.SelectedIndex;UpdateDirty();
    }
    internal bool TryGetWindowsValue(out Dictionary<string,object> value)
    {
        value=null;
        if(originalWindows==null){Error.Text="Сначала загрузи настройки Windows.";Error.Visible=true;return false;}
        int index=Resolution.SelectedIndex;
        if(index<0||index>=dimensions.GetLength(0)){Error.Text="Выбери разрешение рабочего стола из списка.";Error.Visible=true;return false;}
        value=new Dictionary<string,object>{{"schema",1},{"width",dimensions[index,0]},{"height",dimensions[index,1]},{"fit",false}};
        Error.Text="";Error.Visible=false;return true;
    }
    internal void LoadValues(Dictionary<string,object> settings,bool recovery)
    {
        original=new Dictionary<string,object>(settings);
        foreach(string key in keys)Fields[key].Text=Convert.ToString(settings[key],CultureInfo.InvariantCulture);
        UpdateRecommendations();
        Error.Visible = recovery;
        Error.Text=recovery?"Файл настроек повреждён. При сохранении исходный файл останется в резервной копии.":"";
        UpdateDirty();
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
            else {errors++;if(first==null){first=field;message=index==0?"Память Windows: введи число от 1 до 32 ГиБ, например 4 или 3,5.":labels[index]+": целое "+minimum[index]+"–"+maximum[index]+".";}}
        }
        Error.Visible = errors > 0;
        Error.Text=message+(errors>1?" Проверь ещё полей: "+(errors-1)+".":"");
        MemoryInput.ForeColor=Fields["work_memory_mib"].ForeColor;
        if(errors==0)return true;
        value=null;
        TextBox target=first.Visible?first:MemoryInput;target.Focus();target.SelectAll();return false;
    }
}

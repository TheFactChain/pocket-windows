using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal sealed class ConnectionEditor : UserControl
{
    // The hidden combo boxes stay the single source of the choice; the list and the segment buttons mirror them.
    internal readonly ComboBox Profiles = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Visible = false, AccessibleName = "Сохранённые подключения" };
    internal readonly ListBox ProfileList = new ListBox { Dock = DockStyle.Fill, IntegralHeight = false, DrawMode = DrawMode.OwnerDrawFixed, BorderStyle = BorderStyle.None, BackColor = NativeStyle.Background, Visible = false, AccessibleName = "Сохранённые подключения" };
    internal readonly Dictionary<string,TextBox> Fields = new Dictionary<string,TextBox>();
    internal readonly ComboBox Protocol = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Visible = false, AccessibleName = "Протокол" };
    internal readonly CheckBox ProxyAuth = new CheckBox { Text = "Нужны логин и пароль", AutoSize = true };
    internal readonly CheckBox Obfuscation = new CheckBox { Text = "Включить Salamander", AutoSize = true };
    internal readonly Label State = new Label { AutoSize = true, Dock = DockStyle.Fill, ForeColor = NativeStyle.Muted, Visible = false };
    internal readonly Label Error = new Label { AutoSize = true, Visible = false, Dock = DockStyle.Fill, ForeColor = Color.FromArgb(175,59,48) };
    internal readonly Button SaveButton = Button("Сохранить",true);
    internal readonly Button SelectButton = Button("Выбрать",false);
    internal readonly Button TestButton = Button("Проверить",false);
    internal readonly Button NewButton = Button("+ Новое",false);
    internal readonly Button ImportButton = Button("Импорт…",false);
    internal readonly Button ExportButton = Button("Экспорт…",false);
    internal string ProfileId;
    internal event Action<string> ProfilePicked;
    private bool binding;
    private bool busy;
    private bool needsCredentials;
    private bool hadCredentials;
    private string loadedProtocol;
    private string loadedObfuscation;
    private bool testAvailable;
    private readonly CheckBox advanced = new CheckBox { Text = "Дополнительные параметры", AutoSize = true, ForeColor = NativeStyle.Accent, Margin = new Padding(0,2,0,0) };
    private readonly ColumnStyle listWidth = new ColumnStyle(SizeType.Absolute,0);
    private readonly List<RoundedButton> protocolButtons = new List<RoundedButton>();
    private readonly TableLayoutPanel body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty };
    private bool syncing;
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr SendMessage(IntPtr window, int message, IntPtr wide, string text);
    private readonly TableLayoutPanel fields = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2, Margin = Padding.Empty };
    private readonly Dictionary<string,Control[]> rows = new Dictionary<string,Control[]>();
    private readonly List<RoundedPanel> inputBoxes = new List<RoundedPanel>();
    private static Button Button(string text,bool primary){return new RoundedButton{Text=text,Primary=primary,AutoSize=true,Padding=new Padding(12,8,12,8),Margin=new Padding(0,0,6,0)};}
    private sealed class Entry
    {
        internal Dictionary<string,object> Value;
        public override string ToString(){return Text(Value,"name","Без названия")+(Flag(Value,"selected")?" (выбрано)":"");}
    }
    private static string Text(Dictionary<string,object> item,string key,string fallback){return item.ContainsKey(key)&&item[key]!=null?Convert.ToString(item[key]):fallback;}
    private static bool Flag(Dictionary<string,object> item,string key){return item.ContainsKey(key)&&item[key] is bool&&(bool)item[key];}
    internal ConnectionEditor()
    {
        Dock=DockStyle.Fill;Font=new Font("Segoe UI",10);BackColor=NativeStyle.Background;
        State.TextChanged+=delegate{State.Visible=!String.IsNullOrEmpty(State.Text);};
        Error.TextChanged+=delegate{Error.Visible=!String.IsNullOrEmpty(Error.Text);};
        TableLayoutPanel root=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=1,RowCount=2,Margin=Padding.Empty};root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        root.RowStyles.Add(new RowStyle(SizeType.AutoSize));root.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        root.Controls.Add(NativeStyle.PageHeader(root,"Подключение",ImportButton,NewButton),0,0);
        // Body: saved connections on the left (only when there are any), the open one on the right.
        body.ColumnStyles.Add(listWidth);body.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));body.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        ProfileList.Margin=new Padding(0,0,12,0);body.Controls.Add(ProfileList,0,0);
        TableLayoutPanel editor=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=1,RowCount=5,Margin=Padding.Empty};RowStyle cardRow=new RowStyle(SizeType.Absolute,0);editor.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
        editor.RowStyles.Add(new RowStyle(SizeType.AutoSize));editor.RowStyles.Add(cardRow);editor.RowStyles.Add(new RowStyle(SizeType.AutoSize));editor.RowStyles.Add(new RowStyle(SizeType.AutoSize));editor.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        State.Margin=new Padding(0,0,0,8);editor.Controls.Add(State,0,0);
        // The card scrolls only as a last resort, with very large text in a small window.
        Panel scroll=new Panel{Dock=DockStyle.Fill,AutoScroll=true,BackColor=NativeStyle.Background,Margin=Padding.Empty};
        RoundedPanel card=new RoundedPanel{Dock=DockStyle.Top,AutoSize=true,AutoSizeMode=AutoSizeMode.GrowAndShrink,Padding=new Padding(16,14,16,6),BackColor=Color.White,Margin=Padding.Empty};
        fields.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));fields.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));
        for(int row=0;row<10;row++)fields.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        RoundedPanel protocols=new RoundedPanel{Dock=DockStyle.Top,Height=40,BackColor=Color.FromArgb(227,227,232),Padding=new Padding(3),Margin=Padding.Empty,AccessibleName="Протокол",AccessibleRole=AccessibleRole.PageTabList};
        TableLayoutPanel protocolRow=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=4,RowCount=1,Margin=Padding.Empty,BackColor=protocols.BackColor};protocolRow.RowStyles.Add(new RowStyle(SizeType.Percent,100));
        string[] protocolNames={"Hysteria2","SOCKS5","HTTP","HTTPS"};
        for(int index=0;index<4;index++)
        {
            int choice=index;protocolRow.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,25));
            RoundedButton button=new RoundedButton{Text=protocolNames[index],Segment=true,CenterText=true,Dock=DockStyle.Fill,Margin=Padding.Empty,AccessibleName=protocolNames[index]};
            button.Click+=delegate{Protocol.SelectedIndex=choice;};protocolButtons.Add(button);protocolRow.Controls.Add(button,index,0);
        }
        protocols.Controls.Add(protocolRow);inputBoxes.Add(protocols);
        AddControl("protocol","Тип подключения",protocols,0,0,true);
        AddField("name","Название",false,"Любое удобное имя, например «Мой сервер».",0,1,true);
        AddField("host","Адрес сервера",false,"Например proxy.example.com или 203.0.113.10, без https:// и пути.",0,2,false);AddField("port","Порт",false,"Число из настроек поставщика, например 443.",1,2,false);
        AddField("auth","Пароль Hysteria2",true,"Пароль подключения, выданный поставщиком сервера.",0,3,true);AddControl("proxy_auth","",ProxyAuth,0,4,true);AddField("username","Логин",false,"Имя пользователя из настроек прокси.",0,5,false);AddField("password","Пароль прокси",true,"Пароль пользователя прокси.",1,5,false);
        AddControl("advanced","",advanced,0,6,true);AddField("tls_server_name","Имя TLS (SNI)",false,"Необязательно. Заполни, только если поставщик указал отдельное имя TLS.",0,7,true);
        AddControl("obfs","",Obfuscation,0,8,true);AddField("obfs_password","Пароль маскировки",true,"Отдельный пароль Salamander из настроек сервера.",0,9,true);
        card.Controls.Add(fields);scroll.Controls.Add(card);editor.Controls.Add(scroll,0,1);
        Error.Margin=new Padding(0,8,0,0);editor.Controls.Add(Error,0,2);
        FlowLayoutPanel footer=new FlowLayoutPanel{Dock=DockStyle.Fill,AutoSize=true,WrapContents=true,Margin=new Padding(0,10,0,0)};foreach(Button button in new[]{SaveButton,SelectButton,TestButton,ExportButton})footer.Controls.Add(button);editor.Controls.Add(footer,0,3);
        body.Controls.Add(editor,1,0);root.Controls.Add(body,0,1);
        // The buttons follow the card directly; the card takes only the height it needs, and scrolls only when even that does not fit.
        editor.Layout+=delegate{
            int width=editor.ClientSize.Width;Func<Control,int> need=item=>item.GetPreferredSize(new Size(Math.Max(1,width-item.Margin.Horizontal),0)).Height+item.Margin.Vertical;
            int other=need(footer)+(String.IsNullOrEmpty(State.Text)?0:need(State))+(String.IsNullOrEmpty(Error.Text)?0:need(Error));
            int wanted=Math.Max(0,Math.Min(card.GetPreferredSize(new Size(width,0)).Height,editor.ClientSize.Height-other));
            if(cardRow.Height!=wanted)cardRow.Height=wanted;
        };
        StyleCombo(Profiles);StyleCombo(Protocol);Protocol.SizeChanged+=delegate{FitInputHeights();};Protocol.FontChanged+=delegate{FitInputHeights();};FitInputHeights();
        Protocol.Items.AddRange(new object[]{"Hysteria2","SOCKS5","HTTP","HTTPS CONNECT"});Protocol.SelectedIndexChanged+=delegate{for(int index=0;index<protocolButtons.Count;index++)protocolButtons[index].Selected=index==Protocol.SelectedIndex;UpdateRows();};
        Obfuscation.CheckedChanged+=delegate{UpdateRows();};ProxyAuth.CheckedChanged+=delegate{UpdateRows();};advanced.CheckedChanged+=delegate{UpdateRows();};
        Profiles.SelectedIndexChanged+=delegate{
            syncing=true;try{ProfileList.SelectedIndex=Profiles.SelectedIndex;}finally{syncing=false;}
            if(!binding&&Profiles.SelectedItem is Entry&&ProfilePicked!=null)ProfilePicked(Text(((Entry)Profiles.SelectedItem).Value,"profile_id",""));
        };
        ProfileList.SelectedIndexChanged+=delegate{if(!syncing&&ProfileList.SelectedIndex>=0&&ProfileList.SelectedIndex!=Profiles.SelectedIndex)Profiles.SelectedIndex=ProfileList.SelectedIndex;};
        ProfileList.DrawItem+=delegate(object sender,DrawItemEventArgs args){DrawProfile(args);};
        ProfileList.FontChanged+=delegate{ProfileList.ItemHeight=Math.Min(255,ProfileList.Font.Height*2+26);FitList();};ProfileList.ItemHeight=ProfileList.Font.Height*2+26;
        body.SizeChanged+=delegate{FitList();};
        editor.SizeChanged+=delegate{State.MaximumSize=Error.MaximumSize=new Size(Math.Max(100,editor.ClientSize.Width),0);};
        NewButton.Click+=delegate{NewProfile();};Controls.Add(root);Controls.Add(Profiles);Controls.Add(Protocol);NewProfile();
    }
    private void FitList()
    {
        int wanted=ProfileList.Items.Count==0?0:Math.Min(body.ClientSize.Width*34/100,Math.Max(190,ProfileList.Font.Height*12));
        ProfileList.Visible=wanted>0;if(listWidth.Width!=wanted)listWidth.Width=wanted;
    }
    private static string ProtocolName(string value)
    {
        int index=Array.IndexOf(new[]{"hysteria2","socks5","http","https"},value);return index<0?value:new[]{"Hysteria2","SOCKS5","HTTP","HTTPS"}[index];
    }
    private void DrawProfile(DrawItemEventArgs args)
    {
        using(Brush back=new SolidBrush(ProfileList.BackColor))args.Graphics.FillRectangle(back,args.Bounds);
        if(args.Index<0||args.Index>=ProfileList.Items.Count)return;
        Dictionary<string,object> value=((Entry)ProfileList.Items[args.Index]).Value;
        Rectangle card=new Rectangle(args.Bounds.X,args.Bounds.Y+3,args.Bounds.Width-1,args.Bounds.Height-6);
        if((args.State&DrawItemState.Selected)!=0)
        {
            args.Graphics.SmoothingMode=System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            using(System.Drawing.Drawing2D.GraphicsPath shape=NativeStyle.Rounded(card,10))using(Brush fill=new SolidBrush(Color.White))args.Graphics.FillPath(fill,shape);
            using(Brush mark=new SolidBrush(NativeStyle.Accent))args.Graphics.FillRectangle(mark,card.X+1,card.Y+10,3,card.Height-20);
        }
        bool used=Flag(value,"selected"),locked=Flag(value,"needs_credentials");
        string state=locked?"нужен пароль":used?(Flag(value,"verified")?"используется, проверено":"используется"):"не выбрано";
        TextFormatFlags flags=TextFormatFlags.Left|TextFormatFlags.EndEllipsis|TextFormatFlags.NoPrefix|TextFormatFlags.SingleLine;
        int line=ProfileList.Font.Height,top=card.Y+(card.Height-line*2-4)/2;
        using(Font bold=new Font(ProfileList.Font,FontStyle.Bold))TextRenderer.DrawText(args.Graphics,Text(value,"name","Без названия"),bold,new Rectangle(card.X+14,top,card.Width-24,line+2),NativeStyle.Ink,flags);
        Color tone=locked?Color.FromArgb(175,59,48):used?Color.FromArgb(30,107,52):NativeStyle.Muted;
        TextRenderer.DrawText(args.Graphics,ProtocolName(Text(value,"protocol",""))+", "+state,ProfileList.Font,new Rectangle(card.X+14,top+line+4,card.Width-24,line+2),tone,flags);
    }
    // A grey hint inside an empty password box instead of a second caption line.
    private static void Cue(TextBox box,string text)
    {
        box.Tag=text;if(box.IsHandleCreated)SendMessage(box.Handle,0x1501,(IntPtr)1,text);
    }
    private void StyleCombo(ComboBox combo)
    {
        combo.DrawMode=DrawMode.OwnerDrawFixed;combo.FlatStyle=FlatStyle.Flat;combo.BackColor=NativeStyle.Background;combo.ForeColor=NativeStyle.Ink;combo.ItemHeight=combo.Font.Height;
        combo.MinimumSize=new Size(0,combo.PreferredHeight);combo.Dock=DockStyle.Top;
        combo.FontChanged+=delegate{combo.ItemHeight=combo.Font.Height;combo.MinimumSize=new Size(0,combo.PreferredHeight);};
        combo.HandleCreated+=delegate{combo.MinimumSize=new Size(0,combo.PreferredHeight);};
        combo.DrawItem+=delegate(object sender,DrawItemEventArgs args)
        {
            bool selected=(args.State&DrawItemState.Selected)!=0&&(args.State&DrawItemState.ComboBoxEdit)==0;
            using(SolidBrush brush=new SolidBrush(selected?SystemColors.Highlight:combo.BackColor))args.Graphics.FillRectangle(brush,args.Bounds);
            string value=args.Index>=0?combo.GetItemText(combo.Items[args.Index]):combo.Text;
            if(String.IsNullOrEmpty(value))value=combo==Profiles?"Новый профиль":"Выбери протокол";
            TextRenderer.DrawText(args.Graphics,value,combo.Font,new Rectangle(args.Bounds.X+2,args.Bounds.Y,Math.Max(1,args.Bounds.Width-4),args.Bounds.Height),selected?SystemColors.HighlightText:combo.ForeColor,TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
            args.DrawFocusRectangle();
        };
    }
    private RoundedPanel InputBox(Control input)
    {
        RoundedPanel box=new RoundedPanel{Dock=DockStyle.Top,Height=44,Padding=new Padding(10,6,10,6),BackColor=NativeStyle.Background,Margin=Padding.Empty};
        TableLayoutPanel content=new TableLayoutPanel{Dock=DockStyle.Fill,ColumnCount=1,RowCount=1,Margin=Padding.Empty,BackColor=box.BackColor};content.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));content.RowStyles.Add(new RowStyle(SizeType.Percent,100));input.Dock=DockStyle.None;input.Anchor=AnchorStyles.Left|AnchorStyles.Right;input.Margin=Padding.Empty;content.Controls.Add(input,0,0);box.Controls.Add(content);inputBoxes.Add(box);return box;
    }
    private void FitInputHeights()
    {
        int height=Math.Max(Protocol.Height,Fields.ContainsKey("name")?Fields["name"].PreferredHeight:Font.Height)+12;
        foreach(RoundedPanel box in inputBoxes)if(box.Height!=height)box.Height=height;
    }
    private void AddField(string key,string title,bool secret,string help,int column,int row,bool wide)
    {
        TextBox field=new TextBox{BorderStyle=BorderStyle.None,BackColor=NativeStyle.Background,UseSystemPasswordChar=secret,AccessibleName=title,AccessibleDescription=help,Margin=Padding.Empty};
        Fields.Add(key,field);field.FontChanged+=delegate{FitInputHeights();};field.HandleCreated+=delegate{if(field.Tag is string)Cue(field,(string)field.Tag);};AddControl(key,title,InputBox(field),column,row,wide);
    }
    private void AddControl(string key,string title,Control control,int column,int row,bool wide)
    {
        TableLayoutPanel cell=new TableLayoutPanel{Dock=DockStyle.Top,AutoSize=true,AutoSizeMode=AutoSizeMode.GrowAndShrink,ColumnCount=1,RowCount=3,Margin=new Padding(column==0?0:8,0,wide?0:column==0?8:0,8)};cell.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));for(int index=0;index<3;index++)cell.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Label label=new Label{Text=title,AutoSize=true,Dock=DockStyle.Top,Margin=new Padding(0,0,0,String.IsNullOrEmpty(title)?0:4),Visible=!String.IsNullOrEmpty(title)};
        cell.Controls.Add(label,0,0);cell.Controls.Add(control,0,1);fields.Controls.Add(cell,column,row);if(wide)fields.SetColumnSpan(cell,2);rows[key]=new Control[]{label,control,cell};
    }
    private void UpdateRows()
    {
        if(binding)return;
        bool hy=Protocol.SelectedIndex==0;bool tls=hy||Protocol.SelectedIndex==3;
        bool preserve=CanReuseCredentials;
        Cue(Fields["auth"],preserve?"Сохранён. Оставь пустым, чтобы не менять":"");
        Cue(Fields["password"],preserve?"Сохранён. Оставь пустым, чтобы не менять":"");
        SetRowVisibility("advanced",tls);SetRowVisibility("tls_server_name",tls&&advanced.Checked);
        SetRowVisibility("obfs",hy&&advanced.Checked);SetRowVisibility("auth",hy);
        SetRowVisibility("obfs_password",hy&&advanced.Checked&&Obfuscation.Checked);SetRowVisibility("proxy_auth",!hy);
        SetRowVisibility("username",!hy&&ProxyAuth.Checked);SetRowVisibility("password",!hy&&ProxyAuth.Checked);
        rows["protocol"][0].Text=hy?"Тип подключения: TCP и UDP":"Тип подключения: только TCP, без UDP";
    }
    private void SetRowVisibility(string key,bool visible)
    {
        rows[key][0].Visible=visible&&!String.IsNullOrEmpty(rows[key][0].Text);rows[key][1].Visible=rows[key][2].Visible=visible;
    }
    internal void NewProfile()
    {
        binding=true;
        try
        {
            ProfileId=null;needsCredentials=true;hadCredentials=false;ProxyAuth.Checked=false;advanced.Checked=false;
            Profiles.SelectedIndex=-1;
            foreach(TextBox field in Fields.Values)field.Clear();Fields["name"].Text="Моё подключение";Fields["port"].Text="443";Protocol.SelectedIndex=0;Obfuscation.Checked=false;Error.Text="";State.Text="";
        }
        finally{binding=false;}
        UpdateRows();SetBusy(busy);
    }

    internal void LoadProfiles(object collection)
    {
        binding=true;Profiles.Items.Clear();ProfileList.Items.Clear();IEnumerable values=collection as IEnumerable;
        if(values!=null)foreach(object value in values){Dictionary<string,object> item=value as Dictionary<string,object>;if(item!=null){Entry entry=new Entry{Value=item};Profiles.Items.Add(entry);ProfileList.Items.Add(entry);}}
        if(Profiles.Items.Count>0)
        {
            int selected=0;for(int index=0;index<Profiles.Items.Count;index++)if(Flag(((Entry)Profiles.Items[index]).Value,"selected"))selected=index;
            for(int index=0;index<Profiles.Items.Count;index++)if(Text(((Entry)Profiles.Items[index]).Value,"profile_id","")==ProfileId){selected=index;break;}
            Profiles.SelectedIndex=selected;
        }
        FitList();ProfileList.Invalidate();
        binding=false;
    }
    internal string PickedId {get{return Profiles.SelectedItem is Entry?Text(((Entry)Profiles.SelectedItem).Value,"profile_id",""):null;}}
    internal Dictionary<string,object> PickedView {get{return Profiles.SelectedItem is Entry?((Entry)Profiles.SelectedItem).Value:null;}}
    internal void RestoreLoadedSelection()
    {
        bool wasBinding=binding;binding=true;
        try
        {
            Profiles.SelectedIndex=-1;
            for(int index=0;index<Profiles.Items.Count;index++)
                if(Text(((Entry)Profiles.Items[index]).Value,"profile_id","")==ProfileId){Profiles.SelectedIndex=index;break;}
        }
        finally{binding=wasBinding;}
    }
    private bool CanReuseCredentials
    {
        get{return ProfileId!=null&&hadCredentials&&!needsCredentials&&Protocol.SelectedIndex>=0
            &&loadedProtocol==new[]{"hysteria2","socks5","http","https"}[Protocol.SelectedIndex]
            &&loadedObfuscation==(Protocol.SelectedIndex==0&&Obfuscation.Checked?"salamander":"");}
    }
    internal void LoadView(Dictionary<string,object> view)
    {
        bool wasBinding=binding;binding=true;
        try
        {
            ProfileId=Text(view,"profile_id",null);needsCredentials=Flag(view,"needs_credentials");hadCredentials=Flag(view,"auth_present");ProxyAuth.Checked=hadCredentials;
            loadedProtocol=Text(view,"protocol","hysteria2");loadedObfuscation=Text(view,"obfs","");
            foreach(TextBox field in Fields.Values)field.Clear();
            foreach(string key in new[]{"name","host","port","tls_server_name"})Fields[key].Text=Text(view,key,"");
            string protocol=Text(view,"protocol","hysteria2");Protocol.SelectedIndex=Array.IndexOf(new[]{"hysteria2","socks5","http","https"},protocol);if(Protocol.SelectedIndex<0)Protocol.SelectedIndex=0;
            Obfuscation.Checked=Text(view,"obfs","")=="salamander";
            advanced.Checked=Obfuscation.Checked||!String.IsNullOrWhiteSpace(Fields["tls_server_name"].Text);
            State.Text=needsCredentials?"Нужно ввести пароль заново для этого пользователя Windows.":Flag(view,"selected")?"Выбрано для следующего запуска Windows, работающий сеанс не меняется.":"Сохранено. Профиль ещё не выбран для запуска.";
            State.AccessibleName=Text(view,"name","Профиль");
            State.Text+=Flag(view,"verified")?" Проверка подключения пройдена.":" Сеть не проверена.";
            Error.Text="";
        }
        finally{binding=wasBinding;}
        UpdateRows();SetBusy(busy);
    }
    internal void LoadImported(Dictionary<string,object> profile)
    {
        binding=true;
        try
        {
            LoadView(profile);ProfileId=null;Dictionary<string,object> credentials=profile.ContainsKey("credentials")?profile["credentials"] as Dictionary<string,object>:null;
            if(credentials!=null)foreach(string key in new[]{"auth","obfs_password","username","password"})if(credentials.ContainsKey(key))Fields[key].Text=Convert.ToString(credentials[key]);
            needsCredentials=Protocol.SelectedIndex==0;RestoreLoadedSelection();State.Text="Импорт заполнен в редакторе. Проверь поля, введи пароль и сохрани.";
        }
        finally{binding=false;}
        UpdateRows();SetBusy(busy);
    }

    internal bool TrySaveRequest(out Dictionary<string,object> request)
    {
        request=null;int port;
        if(Protocol.SelectedIndex<0||Protocol.SelectedIndex>=4){Error.Text="Выбери тип подключения.";return false;}
        if(String.IsNullOrWhiteSpace(Fields["name"].Text)||String.IsNullOrWhiteSpace(Fields["host"].Text)){Error.Text="Заполни название и адрес сервера.";return false;}
        if(!Int32.TryParse(Fields["port"].Text,NumberStyles.None,CultureInfo.InvariantCulture,out port)||port<1||port>65535){Error.Text="Порт должен быть целым числом от 1 до 65535.";return false;}
        bool hy=Protocol.SelectedIndex==0;
        Dictionary<string,object> profile=new Dictionary<string,object>{{"name",Fields["name"].Text.Trim()},{"protocol",new[]{"hysteria2","socks5","http","https"}[Protocol.SelectedIndex]},{"host",Fields["host"].Text.Trim()},{"port",port}};
        if((hy||Protocol.SelectedIndex==3)&&!String.IsNullOrWhiteSpace(Fields["tls_server_name"].Text))profile["tls_server_name"]=Fields["tls_server_name"].Text.Trim();
        if(hy&&Obfuscation.Checked)profile["obfs"]="salamander";
        if(hy)
        {
            if(Fields["auth"].Text.Length>0)
            {
                Dictionary<string,object> secret=new Dictionary<string,object>{{"auth",Fields["auth"].Text}};
                if(Obfuscation.Checked){if(Fields["obfs_password"].Text.Length==0){Error.Text="Введи пароль маскировки.";return false;}secret["obfs_password"]=Fields["obfs_password"].Text;}
                profile["credentials"]=secret;
            }
            else if(Fields["obfs_password"].Text.Length>0){Error.Text="Для смены маскировки введи и пароль Hysteria2.";return false;}
            else if(!CanReuseCredentials){Error.Text="Введи пароль Hysteria2. После смены типа подключения или маскировки пароль нужен заново.";return false;}
        }
        else if(!ProxyAuth.Checked)profile["credentials"]=new Dictionary<string,object>();
        else if(Fields["username"].Text.Length>0||Fields["password"].Text.Length>0)
        {
            if(Fields["username"].Text.Length==0||Fields["password"].Text.Length==0){Error.Text="Для авторизации нужны и логин, и пароль.";return false;}
            profile["credentials"]=new Dictionary<string,object>{{"username",Fields["username"].Text},{"password",Fields["password"].Text}};
        }
        else if(!CanReuseCredentials){Error.Text="Введи логин и пароль для авторизации. После смены типа подключения пароль нужен заново.";return false;}
        request=new Dictionary<string,object>{{"profile",profile}};if(ProfileId!=null)request["profile_id"]=ProfileId;Error.Text="";return true;
    }
    internal void SetTestAvailable(bool value){testAvailable=value;SetBusy(busy);}
    internal void SetBusy(bool value)
    {
        busy=value;
        foreach(Button button in new[]{SaveButton,NewButton,ImportButton})button.Enabled=!value;
        SelectButton.Enabled=ExportButton.Enabled=!value&&ProfileId!=null;TestButton.Enabled=!value&&ProfileId!=null&&testAvailable;
        Profiles.Enabled=ProfileList.Enabled=!value;foreach(RoundedButton button in protocolButtons)button.Enabled=!value;
        foreach(TextBox field in Fields.Values)field.Enabled=!value;
        Protocol.Enabled=ProxyAuth.Enabled=Obfuscation.Enabled=advanced.Enabled=!value;
    }
}

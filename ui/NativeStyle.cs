using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;

internal static class NativeStyle
{
    internal static readonly Color Background = Color.FromArgb(242, 242, 247);
    internal static readonly Color Accent = Color.FromArgb(36, 129, 204);
    internal static readonly Color Ink = Color.FromArgb(28, 28, 30);
    internal static readonly Color Muted = Color.FromArgb(110, 110, 115);
    internal static readonly Color Sidebar = Color.FromArgb(235, 235, 240);
    internal static readonly Color NavInk = Color.FromArgb(31, 111, 178);

    internal static GraphicsPath Rounded(Rectangle bounds, int radius)
    {
        GraphicsPath path = new GraphicsPath();
        int diameter = Math.Min(radius * 2, Math.Min(bounds.Width, bounds.Height));
        if (diameter < 2) { path.AddRectangle(bounds); return path; }
        path.AddArc(bounds.Left, bounds.Top, diameter, diameter, 180, 90);
        path.AddArc(bounds.Right - diameter, bounds.Top, diameter, diameter, 270, 90);
        path.AddArc(bounds.Right - diameter, bounds.Bottom - diameter, diameter, diameter, 0, 90);
        path.AddArc(bounds.Left, bounds.Bottom - diameter, diameter, diameter, 90, 90);
        path.CloseFigure();
        return path;
    }

    internal static void Surface(Control control, Graphics graphics, int radius)
    {
        graphics.Clear(control.Parent == null ? Background : control.Parent.BackColor);
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using (GraphicsPath shape = Rounded(new Rectangle(0, 0, control.Width - 1, control.Height - 1), radius))
        using (Brush fill = new SolidBrush(control.BackColor)) graphics.FillPath(fill, shape);
    }

    // Page title on the left, actions on the right. The menu already names the page,
    // so with very large text the title gives way instead of pushing the actions off screen.
    internal static Panel PageHeader(Control owner, string title, params Control[] actions)
    {
        Panel header = new Panel { Dock = DockStyle.Top, Height = 40, Margin = new Padding(0, 0, 0, 14) };
        Label caption = new Label { Text = title, AutoSize = true, Font = new Font("Segoe UI", 15, FontStyle.Bold), Margin = Padding.Empty };
        FlowLayoutPanel tools = new FlowLayoutPanel { AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = true, Margin = Padding.Empty };
        foreach (Control button in actions) { button.Margin = new Padding(6, 0, 0, 0); tools.Controls.Add(button); }
        header.Controls.Add(caption); header.Controls.Add(tools);
        bool captionVisible = true;
        // Decided in the owner's layout, before the header itself is laid out.
        owner.Layout += delegate
        {
            int width = owner.ClientSize.Width; Size wanted = tools.GetPreferredSize(Size.Empty);
            // Actions wider than the page wrap onto a second line rather than leave the window.
            if (wanted.Width > width) { wanted = tools.GetPreferredSize(new Size(Math.Max(1, width), 0)); wanted.Width = Math.Min(wanted.Width, width); }
            int height = Math.Max(caption.PreferredHeight, wanted.Height);
            header.Height = height; tools.Bounds = new Rectangle(Math.Max(0, width - wanted.Width), (height - wanted.Height) / 2, wanted.Width, wanted.Height);
            caption.Location = new Point(0, (height - caption.PreferredHeight) / 2);
            bool visible = caption.PreferredWidth + wanted.Width + 16 <= width;
            if (captionVisible != visible) { captionVisible = visible; caption.Visible = visible; }
        };
        return header;
    }

    // A filled circle with a check mark (passed), an exclamation mark (failed) or a grey check (not checked yet).
    internal static void StatusMark(Graphics graphics, Rectangle bounds, bool? passed)
    {
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        Color fill = passed == null ? Color.FromArgb(174, 174, 178) : passed.Value ? Color.FromArgb(46, 158, 79) : Color.FromArgb(214, 110, 40);
        using (Brush brush = new SolidBrush(fill)) graphics.FillEllipse(brush, bounds);
        float x = bounds.X, y = bounds.Y, size = bounds.Width;
        using (Pen pen = new Pen(Color.White, Math.Max(2f, size / 9f)) { StartCap = LineCap.Round, EndCap = LineCap.Round, LineJoin = LineJoin.Round })
        {
            if (passed != false) graphics.DrawLines(pen, new[] { new PointF(x + size * 0.28f, y + size * 0.52f), new PointF(x + size * 0.44f, y + size * 0.67f), new PointF(x + size * 0.73f, y + size * 0.36f) });
            else { graphics.DrawLine(pen, x + size / 2, y + size * 0.26f, x + size / 2, y + size * 0.55f); graphics.DrawLine(pen, x + size / 2, y + size * 0.73f, x + size / 2, y + size * 0.74f); }
        }
    }
}

internal sealed class BufferedPanel : Panel
{
    internal BufferedPanel() { DoubleBuffered = true; ResizeRedraw = true; }
}

internal sealed class CompositedPanel : Panel
{
    internal CompositedPanel() { DoubleBuffered = true; ResizeRedraw = true; }
    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams parameters = base.CreateParams;
            // DoubleBuffered alone does not buffer the native child windows of a page.
            parameters.ExStyle |= 0x02000000; // WS_EX_COMPOSITED
            return parameters;
        }
    }
}

internal sealed class RoundedButton : Button
{
    internal bool Primary;
    internal bool Segment;
    internal bool CenterText;
    // A sidebar item: the selected one is accent and bold, with its key hint on the right.
    internal bool Nav;
    internal string Hint;
    // A secondary action that ends something (shutting Windows down): red text.
    internal bool Danger;
    internal AppGlyph? Glyph;
    // A second muted line under the text (choice cards such as the file access modes).
    internal string Note;
    private Font boldFont, hintFont;
    private Font BoldFont { get { if (boldFont == null || boldFont.Size != Font.Size) boldFont = new Font(Font, FontStyle.Bold); return boldFont; } }
    private Font HintFont { get { if (hintFont == null || hintFont.Size != Font.Size * 0.85f) hintFont = new Font(Font.FontFamily, Font.Size * 0.85f); return hintFont; } }
    protected override void Dispose(bool disposing) { if (disposing) { if (boldFont != null) boldFont.Dispose(); if (hintFont != null) hintFont.Dispose(); } base.Dispose(disposing); }
    private int HintWidth { get { return String.IsNullOrEmpty(Hint) ? 0 : TextRenderer.MeasureText(Hint, HintFont).Width + 8; } }
    public override Size GetPreferredSize(Size proposedSize)
    {
        Size content = ContentPreferredSize;
        return new Size(Math.Max(MinimumSize.Width, content.Width), Math.Max(MinimumSize.Height, content.Height));
    }
    internal Size ContentPreferredSize
    {
        get
        {
            Size text = TextRenderer.MeasureText(Text, Nav || Note != null ? BoldFont : Font, Size.Empty, TextFormatFlags.SingleLine);
            if (Note != null)
            {
                Size note = TextRenderer.MeasureText(Note, HintFont, Size.Empty, TextFormatFlags.SingleLine);
                return new Size(Math.Max(text.Width, note.Width) + 28, text.Height + note.Height + 18);
            }
            return new Size(text.Width + Math.Max(24, Padding.Horizontal) + (Glyph.HasValue ? 40 : 0) + HintWidth,
                text.Height + Padding.Vertical);
        }
    }
    private bool selected;
    internal bool Selected
    {
        get { return selected; }
        set
        {
            if (selected == value) return;
            selected = value;
            Invalidate();
            AccessibilityNotifyClients(AccessibleEvents.StateChange, -1);
            AccessibilityNotifyClients(value ? AccessibleEvents.Selection : AccessibleEvents.SelectionRemove, -1);
        }
    }
    protected override AccessibleObject CreateAccessibilityInstance() { return Segment ? new SegmentAccessibleObject(this) : base.CreateAccessibilityInstance(); }
    private sealed class SegmentAccessibleObject : Control.ControlAccessibleObject
    {
        private readonly RoundedButton button;
        internal SegmentAccessibleObject(RoundedButton owner) : base(owner) { button = owner; }
        public override AccessibleRole Role { get { return AccessibleRole.PageTab; } }
        public override string DefaultAction { get { return "Выбрать"; } }
        public override void DoDefaultAction() { button.PerformClick(); }
        public override AccessibleStates State
        {
            get
            {
                AccessibleStates state = base.State;
                if (!button.Segment) return state;
                state |= AccessibleStates.Selectable;
                return button.Selected ? state | AccessibleStates.Selected : state & ~AccessibleStates.Selected;
            }
        }
    }
    private bool hover;
    private bool pressed;
    internal RoundedButton()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        Cursor = Cursors.Hand;
        UseVisualStyleBackColor = false;
    }
    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { pressed = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }
    protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
    protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
    protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }
    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(Parent == null ? NativeStyle.Background : Parent.BackColor);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        Color fill = Segment ? (Selected ? Color.White : Parent.BackColor) : Primary ? NativeStyle.Accent : Color.White;
        Color text = Segment ? (Nav && Selected ? NativeStyle.NavInk : NativeStyle.Ink) : Primary ? Color.White : Danger ? Color.FromArgb(179, 38, 30) : NativeStyle.Accent;
        if (!Enabled) { fill = Segment ? fill : Color.FromArgb(229, 229, 234); text = NativeStyle.Muted; }
        else if (pressed) fill = ControlPaint.Dark(fill, 0.08f);
        else if (hover) fill = Primary ? ControlPaint.Light(fill, 0.08f) : Color.FromArgb(246, 247, 249);
        if (SystemInformation.HighContrast)
        {
            fill = Selected || Primary ? SystemColors.Highlight : SystemColors.Control;
            text = !Enabled ? SystemColors.GrayText : Selected || Primary ? SystemColors.HighlightText : SystemColors.ControlText;
        }
        using (GraphicsPath shape = NativeStyle.Rounded(new Rectangle(1, 1, Width - 3, Height - 3), Segment ? 9 : 13))
        using (Brush brush = new SolidBrush(fill)) e.Graphics.FillPath(brush, shape);
        if (Note != null)
        {
            int title = TextRenderer.MeasureText("Ag", BoldFont).Height, note = TextRenderer.MeasureText("Ag", HintFont).Height, top = Math.Max(4, (Height - title - note - 2) / 2);
            const TextFormatFlags flags = TextFormatFlags.Left | TextFormatFlags.Top | TextFormatFlags.EndEllipsis | TextFormatFlags.NoPrefix;
            TextRenderer.DrawText(e.Graphics, Text, BoldFont, new Rectangle(14, top, Width - 28, title), text, flags);
            TextRenderer.DrawText(e.Graphics, Note, HintFont, new Rectangle(14, top + title + 2, Width - 28, note), SystemInformation.HighContrast || !Enabled ? text : NativeStyle.Muted, flags);
            if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -5, -5), text, fill);
            return;
        }
        if (Glyph.HasValue) AppIcons.Draw(e.Graphics, Glyph.Value, new Rectangle(14, (Height - 20) / 2, 20, 20), text);
        int textLeft = Glyph.HasValue ? (Nav ? 38 : 42) : CenterText ? 0 : 12, hint = HintWidth;
        if (hint > 0) TextRenderer.DrawText(e.Graphics, Hint, HintFont, new Rectangle(Width - hint - 4, 4, hint, Height - 8),
            SystemInformation.HighContrast ? text : Primary && Enabled ? Color.FromArgb(205, 226, 245) : Color.FromArgb(142, 142, 147), TextFormatFlags.Right | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPrefix);
        TextRenderer.DrawText(e.Graphics, Text, Nav && Selected ? BoldFont : Font, new Rectangle(textLeft, 4, Width - textLeft - (hint > 0 ? hint : CenterText ? 0 : 12), Height - 8), text,
            (Segment && !CenterText ? TextFormatFlags.Left : TextFormatFlags.HorizontalCenter) | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | (Nav ? TextFormatFlags.NoPadding : 0) | (ShowKeyboardCues ? 0 : TextFormatFlags.HidePrefix));
        if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -7, -7), text, fill);
    }
}

// A rounded chip: state badge, launch step or key cap. An optional dot sits before the text.
internal sealed class Pill : Label
{
    internal Color? Dot;
    // A step number or mark in a small circle before the text (launch steps).
    internal string Badge;
    internal int Radius = 10;
    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        if (String.IsNullOrEmpty(Badge)) return;
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        Rectangle circle = new Rectangle(Padding.Left - 30, (Height - 22) / 2, 22, 22);
        using (Pen pen = new Pen(ForeColor, 1.5f)) e.Graphics.DrawEllipse(pen, circle);
        using (Font small = new Font(Font.FontFamily, 8f))
            TextRenderer.DrawText(e.Graphics, Badge, small, circle, ForeColor, TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPadding);
    }
    internal Pill() { SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnPaintBackground(PaintEventArgs e)
    {
        e.Graphics.Clear(Parent == null ? NativeStyle.Background : Parent.BackColor);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using (GraphicsPath shape = NativeStyle.Rounded(new Rectangle(0, 0, Width - 1, Height - 1), Math.Min(Radius, Height / 2)))
        using (Brush fill = new SolidBrush(BackColor)) e.Graphics.FillPath(fill, shape);
        if (Dot.HasValue) using (Brush dot = new SolidBrush(Dot.Value)) e.Graphics.FillEllipse(dot, Padding.Left - 14, (Height - 8) / 2, 8, 8);
    }
}

internal sealed class RoundedPanel : Panel
{
    internal int Radius = 16;
    internal RoundedPanel() { SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (Width < 2 || Height < 2) return;
        Region previous = Region;
        using (GraphicsPath path = NativeStyle.Rounded(ClientRectangle, Radius)) Region = new Region(path);
        if (previous != null) previous.Dispose();
    }
    protected override void OnPaintBackground(PaintEventArgs e) { NativeStyle.Surface(this, e.Graphics, Radius); }
}

// A horizontal slider in the panel style: thin track, accent fill, round thumb; arrows move by Step.
internal sealed class Slider : Control
{
    internal int Minimum, Maximum = 100, Step = 1;
    private int value;
    internal event EventHandler ValueChanged;
    internal Slider()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.Selectable, true);
        TabStop = true; Cursor = Cursors.Hand; AccessibleRole = AccessibleRole.Slider;
    }
    internal int Value
    {
        get { return value; }
        set
        {
            int next = Math.Max(Minimum, Math.Min(Maximum, value));
            if (next == this.value) return;
            this.value = next; Invalidate(); AccessibilityNotifyClients(AccessibleEvents.ValueChange, -1);
            if (ValueChanged != null) ValueChanged(this, EventArgs.Empty);
        }
    }
    private int Snap(long raw) { return Minimum + (int)Math.Round((raw - Minimum) / (double)Step) * Step; }
    private void SetFromX(int x) { double ratio = Math.Max(0, Math.Min(1, (x - 10) / (double)Math.Max(1, Width - 20))); Value = Snap(Minimum + (long)Math.Round(ratio * (Maximum - Minimum))); }
    protected override void OnMouseDown(MouseEventArgs e) { Focus(); if (e.Button == MouseButtons.Left) SetFromX(e.X); base.OnMouseDown(e); }
    protected override void OnMouseMove(MouseEventArgs e) { if (e.Button == MouseButtons.Left) SetFromX(e.X); base.OnMouseMove(e); }
    protected override bool IsInputKey(Keys keyData)
    {
        switch (keyData) { case Keys.Left: case Keys.Right: case Keys.Up: case Keys.Down: case Keys.Home: case Keys.End: case Keys.PageUp: case Keys.PageDown: return true; }
        return base.IsInputKey(keyData);
    }
    protected override void OnKeyDown(KeyEventArgs e)
    {
        int page = Step * 4;
        switch (e.KeyCode)
        {
            case Keys.Left: case Keys.Down: Value = Snap(Value - Step); break;
            case Keys.Right: case Keys.Up: Value = Snap(Value + Step); break;
            case Keys.PageDown: Value = Snap(Value - page); break;
            case Keys.PageUp: Value = Snap(Value + page); break;
            case Keys.Home: Value = Minimum; break;
            case Keys.End: Value = Maximum; break;
            default: base.OnKeyDown(e); return;
        }
        e.Handled = true;
    }
    protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
    protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }
    protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
    protected override void OnPaint(PaintEventArgs e)
    {
        e.Graphics.Clear(Parent == null ? NativeStyle.Background : Parent.BackColor);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        int middle = Height / 2, width = Math.Max(1, Width - 20);
        int thumb = 10 + (int)Math.Round(width * (Maximum > Minimum ? (value - Minimum) / (double)(Maximum - Minimum) : 0));
        Color fill = Enabled ? NativeStyle.Accent : Color.FromArgb(174, 174, 178);
        using (GraphicsPath track = NativeStyle.Rounded(new Rectangle(10, middle - 2, width, 4), 2)) using (Brush brush = new SolidBrush(Color.FromArgb(209, 209, 214))) e.Graphics.FillPath(brush, track);
        using (GraphicsPath done = NativeStyle.Rounded(new Rectangle(10, middle - 2, Math.Max(1, thumb - 10), 4), 2)) using (Brush brush = new SolidBrush(fill)) e.Graphics.FillPath(brush, done);
        Rectangle knob = new Rectangle(thumb - 9, middle - 9, 18, 18);
        using (Brush brush = new SolidBrush(Color.White)) e.Graphics.FillEllipse(brush, knob);
        using (Pen pen = new Pen(Focused && ShowFocusCues ? NativeStyle.Accent : Color.FromArgb(199, 199, 204), Focused && ShowFocusCues ? 2f : 1f)) e.Graphics.DrawEllipse(pen, knob);
    }
    protected override AccessibleObject CreateAccessibilityInstance() { return new SliderAccessibleObject(this); }
    private sealed class SliderAccessibleObject : Control.ControlAccessibleObject
    {
        internal SliderAccessibleObject(Slider owner) : base(owner) { }
        public override string Value { get { return Owner.Text; } }
    }
}

internal sealed class RoundedFlowPanel : FlowLayoutPanel
{
    internal RoundedFlowPanel() { SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnPaintBackground(PaintEventArgs e) { NativeStyle.Surface(this, e.Graphics, 16); }
}


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
}

internal sealed class RoundedButton : Button
{
    internal bool Primary;
    internal bool Segment;
    internal AppGlyph? Glyph;
    public override Size GetPreferredSize(Size proposedSize)
    {
        Size text = TextRenderer.MeasureText(Text, Font, Size.Empty, TextFormatFlags.SingleLine);
        return new Size(Math.Max(MinimumSize.Width,text.Width + Math.Max(24,Padding.Horizontal) + (Glyph.HasValue ? 40 : 0)),
            Math.Max(MinimumSize.Height,text.Height + Padding.Vertical));
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
        Color text = Segment ? NativeStyle.Ink : Primary ? Color.White : NativeStyle.Accent;
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
        if (Glyph.HasValue) AppIcons.Draw(e.Graphics, Glyph.Value, new Rectangle(14, (Height - 20) / 2, 20, 20), text);
        int textLeft = Glyph.HasValue ? 42 : 12;
        TextRenderer.DrawText(e.Graphics, Text, Font, new Rectangle(textLeft, 4, Width - textLeft - 12, Height - 8), text,
            (Segment ? TextFormatFlags.Left : TextFormatFlags.HorizontalCenter) | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | (ShowKeyboardCues ? 0 : TextFormatFlags.HidePrefix));
        if (Focused && ShowFocusCues) ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -7, -7), text, fill);
    }
}

internal sealed class RoundedPanel : Panel
{
    internal RoundedPanel() { SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (Width < 2 || Height < 2) return;
        Region previous = Region;
        using (GraphicsPath path = NativeStyle.Rounded(ClientRectangle, 16)) Region = new Region(path);
        if (previous != null) previous.Dispose();
    }
    protected override void OnPaintBackground(PaintEventArgs e) { NativeStyle.Surface(this, e.Graphics, 16); }
}

internal sealed class RoundedFlowPanel : FlowLayoutPanel
{
    internal RoundedFlowPanel() { SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnPaintBackground(PaintEventArgs e) { NativeStyle.Surface(this, e.Graphics, 16); }
}


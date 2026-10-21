using System;
using System.Drawing;
using System.Drawing.Drawing2D;

internal enum AppGlyph { Play, Settings, Diagnostics, Repair, Home, Network }

internal static class AppIcons
{
    internal static void Draw(Graphics graphics, AppGlyph glyph, Rectangle bounds, Color color)
    {
        if (bounds.Width <= 0 || bounds.Height <= 0) return;
        GraphicsState state = graphics.Save();
        try
        {
            float size = Math.Min(bounds.Width, bounds.Height);
            graphics.TranslateTransform(bounds.Left + (bounds.Width - size) / 2f, bounds.Top + (bounds.Height - size) / 2f);
            graphics.ScaleTransform(size / 24f, size / 24f);
            graphics.SmoothingMode = SmoothingMode.AntiAlias;
            graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
            using (Pen pen = new Pen(color, 1.8f))
            {
                pen.StartCap = pen.EndCap = LineCap.Round;
                pen.LineJoin = LineJoin.Round;
                switch (glyph)
                {
                    case AppGlyph.Play:
                        graphics.DrawPolygon(pen, new[] { new PointF(8, 4.5f), new PointF(20, 12), new PointF(8, 19.5f) });
                        break;
                    case AppGlyph.Settings:
                        PointF[] teeth = new PointF[32];
                        for (int index = 0; index < teeth.Length; index++)
                        {
                            double angle = index * Math.PI / 16 - Math.PI / 16;
                            float radius = index % 4 < 2 ? 9.5f : 7.6f;
                            teeth[index] = new PointF(12 + radius * (float)Math.Cos(angle), 12 + radius * (float)Math.Sin(angle));
                        }
                        graphics.DrawPolygon(pen, teeth);
                        graphics.DrawEllipse(pen, 8.6f, 8.6f, 6.8f, 6.8f);
                        break;
                    case AppGlyph.Diagnostics:
                        graphics.DrawEllipse(pen, 3, 3, 18, 18);
                        graphics.DrawLines(pen, new[] { new PointF(7.5f, 12), new PointF(10.6f, 15.1f), new PointF(16.5f, 9) });
                        break;
                    case AppGlyph.Repair:
                        graphics.DrawLines(pen, new[] {
                            new PointF(14, 4), new PointF(11.8f, 6.2f), new PointF(11.8f, 10.1f),
                            new PointF(4.1f, 17.8f), new PointF(6.2f, 19.9f), new PointF(13.9f, 12.2f),
                            new PointF(17.8f, 12.2f), new PointF(20, 10), new PointF(16.2f, 10),
                            new PointF(14, 7.8f), new PointF(14, 4)
                        });
                        break;
                    case AppGlyph.Home:
                        graphics.DrawLines(pen, new[] {
                            new PointF(3, 10.5f), new PointF(12, 3.5f), new PointF(21, 10.5f),
                            new PointF(18.5f, 10.5f), new PointF(18.5f, 20), new PointF(14, 20),
                            new PointF(14, 14), new PointF(10, 14), new PointF(10, 20),
                            new PointF(5.5f, 20), new PointF(5.5f, 10.5f), new PointF(3, 10.5f)
                        });
                        break;
                    case AppGlyph.Network:
                        graphics.DrawLine(pen, 12, 9, 12, 13);
                        graphics.DrawLine(pen, 6, 13, 18, 13);
                        graphics.DrawLine(pen, 6, 13, 6, 16);
                        graphics.DrawLine(pen, 18, 13, 18, 16);
                        graphics.DrawRectangle(pen, 8, 3, 8, 6);
                        graphics.DrawRectangle(pen, 3, 16, 6, 5);
                        graphics.DrawRectangle(pen, 15, 16, 6, 5);
                        break;
                }
            }
        }
        finally { graphics.Restore(state); }
    }
}

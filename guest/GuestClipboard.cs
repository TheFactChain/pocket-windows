using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.IO.Ports;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace WindowsClaude
{
    // Shared text clipboard with the host over the guest's third serial port (COM3). Same protocol as scripts/clipboard_bridge.py:
    // one JSON line per copy {"v":1,"n":session nonce,"t":"text","z":1,"d":base64(raw deflate of UTF-8)}.
    // The emulated UART moves about 11 KB/s, so copies are capped; a copy marked as sensitive is never sent.
    public static class GuestClipboard
    {
        public const int MaxChars = 256 * 1024, MaxLine = 1024 * 1024;
        const string Exclude = "ExcludeClipboardContentFromMonitorProcessing";

        public static string Encode(string nonce, string text)
        {
            if (String.IsNullOrEmpty(text) || text.Length > MaxChars) return null;
            byte[] raw = Encoding.UTF8.GetBytes(text);
            using (var packed = new MemoryStream())
            {
                using (var deflate = new DeflateStream(packed, CompressionLevel.Optimal, true)) deflate.Write(raw, 0, raw.Length);
                return new JavaScriptSerializer { MaxJsonLength = MaxLine * 2 }.Serialize(new Dictionary<string, object>
                    { { "v", 1 }, { "n", nonce }, { "t", "text" }, { "z", 1 }, { "d", Convert.ToBase64String(packed.ToArray()) } });
            }
        }

        // The text of a valid line of this session; anything unexpected is dropped.
        public static string Decode(string line, string nonce)
        {
            try
            {
                if (line == null || line.Length > MaxLine) return null;
                var message = new JavaScriptSerializer { MaxJsonLength = MaxLine * 2 }.DeserializeObject(line) as Dictionary<string, object>;
                if (message == null || Number(message, "v") != 1 || Str(message, "t") != "text" || Str(message, "n") != nonce
                    || (Number(message, "z") != 0 && Number(message, "z") != 1) || Str(message, "d") == null) return null;
                byte[] raw = Convert.FromBase64String(Str(message, "d"));
                if (Number(message, "z") == 1)
                {
                    using (var inflate = new DeflateStream(new MemoryStream(raw), CompressionMode.Decompress))
                    using (var output = new MemoryStream())
                    {
                        var buffer = new byte[16384]; int read;
                        while ((read = inflate.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            output.Write(buffer, 0, read);
                            if (output.Length > MaxChars * 4) return null;
                        }
                        raw = output.ToArray();
                    }
                }
                string text = new UTF8Encoding(false, true).GetString(raw);
                return text.Length > 0 && text.Length <= MaxChars ? text : null;
            }
            catch (Exception) { return null; }
        }

        static int Number(Dictionary<string, object> message, string key)
        {
            object value;
            return message.TryGetValue(key, out value) && value is int ? (int)value : -1;
        }

        static string Str(Dictionary<string, object> message, string key)
        {
            object value;
            return message.TryGetValue(key, out value) ? value as string : null;
        }

        // The port QEMU adds for the clipboard is the first one besides COM1 (display) and COM2 (file receipts).
        public static string FindPort()
        {
            return SerialPort.GetPortNames().Where(name => name != "COM1" && name != "COM2")
                .OrderBy(name => name.Length).ThenBy(name => name, StringComparer.Ordinal).FirstOrDefault();
        }

        // Runs on its own thread for the whole session; the helper process stays alive while it does.
        public static Thread StartBackground(string nonce)
        {
            var thread = new Thread(() => { try { Run(nonce); } catch (Exception) { } }) { IsBackground = true, Name = "clipboard" };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            return thread;
        }

        static void Run(string nonce)
        {
            string name = null;
            // Windows names a new port a moment after the device appears.
            for (int attempt = 0; attempt < 30 && name == null; attempt++) { name = FindPort(); if (name == null) Thread.Sleep(2000); }
            if (name == null) return;
            var port = new SerialPort(name, 115200) { ReadTimeout = SerialPort.InfiniteTimeout, WriteTimeout = 5000, NewLine = "\n", Encoding = new UTF8Encoding(false), DtrEnable = true, RtsEnable = true };
            port.Open();
            using (var window = new Listener(nonce, port))
            {
                var reader = new Thread(() => window.ReadLoop()) { IsBackground = true, Name = "clipboard-reader" };
                reader.Start();
                Application.Run();
            }
        }

        sealed class Listener : Form
        {
            readonly string nonce; readonly SerialPort port; readonly object gate = new object();
            string applied, sent;

            [DllImport("user32.dll", SetLastError = true)] static extern bool AddClipboardFormatListener(IntPtr hwnd);

            internal Listener(string nonce, SerialPort port)
            {
                this.nonce = nonce; this.port = port;
                ShowInTaskbar = false; FormBorderStyle = FormBorderStyle.None; StartPosition = FormStartPosition.Manual;
                Location = new Point(-32000, -32000); Size = new Size(1, 1); Opacity = 0;
                CreateHandle();
            }

            protected override void SetVisibleCore(bool value) { base.SetVisibleCore(false); }

            protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); AddClipboardFormatListener(Handle); }

            protected override void WndProc(ref Message m)
            {
                if (m.Msg == 0x031D) Changed();
                base.WndProc(ref m);
            }

            static string ReadText()
            {
                for (int attempt = 0; attempt < 10; attempt++)
                {
                    try
                    {
                        IDataObject data = Clipboard.GetDataObject();
                        if (data == null || data.GetDataPresent(Exclude) || !data.GetDataPresent(DataFormats.UnicodeText)) return null;
                        return data.GetData(DataFormats.UnicodeText) as string;
                    }
                    catch (ExternalException) { Thread.Sleep(30); }
                }
                return null;
            }

            // Our own write comes back as a change; the text we applied last is recognised and not sent again.
            void Changed()
            {
                string text = ReadText();
                if (String.IsNullOrEmpty(text) || text == applied || text == sent) return;
                string line = Encode(nonce, text);
                if (line == null) return;
                sent = text;
                try { lock (gate) port.WriteLine(line); } catch (Exception) { }
            }

            internal void ReadLoop()
            {
                while (true)
                {
                    try
                    {
                        string text = Decode(port.ReadLine(), nonce);
                        if (text != null) BeginInvoke((Action)(() => Apply(text)));
                    }
                    catch (Exception) { Thread.Sleep(500); }
                }
            }

            void Apply(string text)
            {
                if (text == applied) return;
                applied = text; sent = null;
                for (int attempt = 0; attempt < 10; attempt++)
                {
                    try { Clipboard.SetText(text, TextDataFormat.UnicodeText); return; }
                    catch (ExternalException) { Thread.Sleep(30); }
                }
            }
        }
    }
}

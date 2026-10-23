using System;
using System.IO;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Windows;

namespace WindowsClaude.Launcher
{
    public partial class App : Application
    {
        [STAThread]
        public static int Main(string[] args)
        {
            AppDomain.CurrentDomain.AssemblyResolve += LoadEmbedded;
            try
            {
                string blocked = ReleaseInfo.StartupBlockReason(Bridge.Root);
                if (blocked != null) { MessageBox.Show(blocked, "Windows Claude"); return 1; }
                return RunApp();
            }
            catch (Exception error)
            {
                if (args.Length > 0) { Console.Error.WriteLine(error.GetType().Name + ": " + error.Message); return 1; }
                MessageBox.Show("Не удалось открыть панель. Проверь целостность комплекта и .NET Framework 4.8.", "Windows Claude");
                return 1;
            }
        }

        // Kept out of Main so WPF-UI types are resolved only after the handler above is installed.
        [MethodImpl(MethodImplOptions.NoInlining)]
        static int RunApp()
        {
            App app = new App();
            app.InitializeComponent();
            return app.Run(new MainWindow());
        }

        static Assembly LoadEmbedded(object sender, ResolveEventArgs args)
        {
            if (new AssemblyName(args.Name).Name != "Wpf.Ui") return null;
            using (Stream stream = typeof(App).Assembly.GetManifestResourceStream("Wpf.Ui.dll"))
            {
                if (stream == null) return null;
                byte[] bytes = new byte[stream.Length];
                stream.Read(bytes, 0, bytes.Length);
                return Assembly.Load(bytes);
            }
        }
    }
}

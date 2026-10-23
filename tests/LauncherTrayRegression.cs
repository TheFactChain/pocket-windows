using System;
using Microsoft.Win32;

internal static class LauncherTrayRegression
{
    static int checks;
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); checks++; }

    static void Make(RegistryKey root, string name, string path, int? promoted)
    {
        using (RegistryKey key = root.CreateSubKey(name))
        {
            key.SetValue("ExecutablePath", path);
            if (promoted.HasValue) key.SetValue("IsPromoted", promoted.Value, RegistryValueKind.DWord);
        }
    }

    static object Read(RegistryKey root, string name)
    {
        using (RegistryKey key = root.OpenSubKey(name)) return key.GetValue("IsPromoted");
    }

    static int Main()
    {
        // Machines that run need no attention, whatever the transitional state names are.
        Assert(AttentionPolicy.Classify(true, "running", "running", false) == null, "running machines are fine");
        Assert(AttentionPolicy.Classify(true, "starting", "reconnecting", false) == null, "start and reconnect are not failures");
        Assert(AttentionPolicy.Classify(true, "stopping", "running", false) == null, "a stop in progress is not a failure");
        Assert(AttentionPolicy.Classify(true, "stopped", "stopped", false) == "vm-attention", "stopped machines need attention");
        Assert(AttentionPolicy.Classify(true, "running", "connection-failed", false) == "vm-attention", "a gateway without its server needs attention");
        Assert(AttentionPolicy.Classify(true, "running", "unknown", false) == "state-unknown", "unreadable VM state is its own reason");
        Assert(AttentionPolicy.Classify(false, "running", "running", false) == "status-unreadable", "an unreadable poll is its own reason");
        Assert(AttentionPolicy.Classify(false, "unknown", "unknown", true) == "outcome-unknown", "an unconfirmed operation wins");

        // The reported flicker: one noisy poll must not reopen the panel; a confirmed problem must.
        Assert(!AttentionPolicy.ShouldReopen(null, 99), "nothing to report never reopens");
        Assert(!AttentionPolicy.ShouldReopen("status-unreadable", 1) && !AttentionPolicy.ShouldReopen("status-unreadable", 3), "transient poll noise stays in the tray");
        Assert(AttentionPolicy.ShouldReopen("status-unreadable", 4), "a lasting unreadable status reopens the panel");
        Assert(!AttentionPolicy.ShouldReopen("state-unknown", 3) && AttentionPolicy.ShouldReopen("state-unknown", 4), "unknown state needs four polls");
        Assert(!AttentionPolicy.ShouldReopen("vm-attention", 1) && AttentionPolicy.ShouldReopen("vm-attention", 2), "a stopped or failed machine needs two polls");
        Assert(AttentionPolicy.ShouldReopen("outcome-unknown", 1), "an unconfirmed operation reopens at once");

        // Tray promotion only touches the entry of this executable and respects an explicit choice.
        string sub = @"Software\WindowsClaudeTrayTest-" + Guid.NewGuid().ToString("N");
        try
        {
            using (RegistryKey root = Registry.CurrentUser.CreateSubKey(sub))
            {
                Make(root, "own", @"E:\Copy\Launcher.exe", null);
                Make(root, "chosen", @"E:\Other\Launcher.exe", 0);
                Make(root, "stranger", @"E:\Third\Launcher.exe", null);
                Assert(TrayPromotion.Promote(root, @"e:\copy\LAUNCHER.exe"), "the entry is found regardless of case");
                Assert(Convert.ToInt32(Read(root, "own")) == 1, "the icon of this Launcher is promoted");
                Assert(Read(root, "stranger") == null, "other executables are untouched");
                Assert(TrayPromotion.Promote(root, @"E:\Other\Launcher.exe") && Convert.ToInt32(Read(root, "chosen")) == 0, "an explicit choice of the user is kept");
                Assert(!TrayPromotion.Promote(root, @"E:\Nowhere\Launcher.exe"), "an unknown executable is reported");
            }
        }
        finally { Registry.CurrentUser.DeleteSubKeyTree(sub, false); }
        Console.WriteLine("PASS launcher tray: " + checks + " checks");
        return 0;
    }
}

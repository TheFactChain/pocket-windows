using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using WindowsClaude.GuestFiles;

internal static class GuestPowerDefaultsRegression
{
    static readonly string[] Commands = {
        "/change monitor-timeout-ac 0", "/change monitor-timeout-dc 0",
        "/change standby-timeout-ac 0", "/change standby-timeout-dc 0"
    };
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); }
    static void Apply(string directory, Action<string> run)
    {
        var method = typeof(Program).GetMethod("ApplyPowerDefaults", BindingFlags.Public | BindingFlags.Static);
        Assert(method != null, "Guest helper must apply persistent display and sleep defaults");
        try { method.Invoke(null, new object[] { directory, run }); }
        catch (TargetInvocationException error) { throw error.InnerException; }
    }
    static int Main()
    {
        string root = Path.Combine(Path.GetTempPath(), "guest-power-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            string first = Path.Combine(root, "first");
            var calls = new List<string>();
            Apply(first, calls.Add);
            Assert(calls.SequenceEqual(Commands), "Both AC/DC display and sleep timers must become Never");
            Assert(Directory.GetFiles(first).Length == 1, "Successful defaults need one persistent marker");
            Apply(first, command => { throw new Exception("Later user preferences must not be reset"); });

            for (int failAt = 0; failAt < Commands.Length; failAt++)
            {
                string directory = Path.Combine(root, "failed-" + failAt);
                int step = 0; bool failed = false;
                try { Apply(directory, command => { if (step++ == failAt) throw new IOException("fixture refusal"); }); }
                catch (IOException) { failed = true; }
                Assert(failed && Directory.GetFiles(directory).Length == 0, "A failed command must not mark defaults applied");
                calls.Clear(); Apply(directory, calls.Add);
                Assert(calls.SequenceEqual(Commands) && Directory.GetFiles(directory).Length == 1, "Failure must allow a complete retry");
            }
            string collision = Path.Combine(root, "collision"), foreign = null;
            int commands = 0; bool refused = false;
            try { Apply(collision, command => { if (++commands == 4) { foreign = Path.Combine(collision, "power-defaults-v1.ok"); File.WriteAllText(foreign, "foreign marker"); } }); }
            catch (IOException) { refused = true; }
            Assert(refused && File.ReadAllText(foreign) == "foreign marker", "Marker commit must never overwrite an existing file");
            Console.WriteLine("PASS: persistent power defaults, preserved user choice, four failure/retry paths and marker collision; no powercfg executed");
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        finally { Directory.Delete(root, true); }
    }
}

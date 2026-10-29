using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;
using WindowsClaude;

internal static class ClipboardStageRegression
{
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = ClipboardStage.MaxLeaseBytes, RecursionLimit = 8 };
    static int Main(string[] args)
    {
        try
        {
            Console.SetIn(new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true)));
            Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
            Console.SetError(new StreamWriter(Console.OpenStandardError(), new UTF8Encoding(false)) { AutoFlush = true });
            string operation = args[0], parent = Path.GetFullPath(args[1]), boot = args[2] == "-" ? null : args[2];
            if (operation == "cleanup")
            {
                object value = Json.DeserializeObject(Console.In.ReadToEnd());
                string[] current = value == null ? null : ((object[])value).Cast<string>().ToArray();
                var result = new ClipboardStage.Cache(parent, boot).Cleanup(current);
                Console.WriteLine(Json.Serialize(new { removed = result.Removed, retained = result.Retained, failed = result.Failed, cleanup_unconfirmed = result.CleanupUnconfirmed })); return 0;
            }
            if (operation == "resume")
            {
                ClipboardFiles.Receiver.ResumeCleanup(parent, Json.Deserialize<Dictionary<string, object>>(Console.In.ReadToEnd())); return 0;
            }
            if (operation == "send")
            {
                var paths = Json.Deserialize<string[]>(Console.In.ReadToEnd());
                foreach (var frame in new ClipboardFiles.Sender(paths, new string('a', 32)).Frames()) Console.WriteLine(Json.Serialize(frame)); return 0;
            }
            using (var receiver = new ClipboardFiles.Receiver(parent, new string('a', 32)))
            {
                string line; while ((line = Console.ReadLine()) != null) receiver.Accept(Json.Deserialize<Dictionary<string, object>>(line));
                if (operation == "export")
                { var lease = receiver.ExportLease(boot); receiver.Park(); Console.WriteLine(Json.Serialize(lease)); return 0; }
                if (operation == "prepare-foreign-marker") File.WriteAllText(receiver.Stage + ".lease.json", "foreign marker");
                var ticket = new ClipboardStage.Cache(parent, boot).Prepare(receiver);
                var result = new { stage = ticket.Stage, roots = ticket.Roots, marker = ticket.Marker };
                if (operation == "park-after-dispose") receiver.Dispose();
                if (operation == "abort-foreign-marker") { File.Delete(ticket.Marker); File.WriteAllText(ticket.Marker, "foreign marker"); ticket.Abort(); }
                else if (operation == "abort") ticket.Abort();
                else if (operation == "park-marker-missing") { File.Delete(ticket.Marker); ticket.Park(); }
                else if (operation == "park" || operation == "park-after-dispose") ticket.Park(); else throw new IOException("Unknown operation");
                Console.WriteLine(Json.Serialize(result)); return 0;
            }
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 2; }
    }
}

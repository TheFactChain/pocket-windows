using System;
using System.Text;
using WindowsClaude;

internal static class GuestClipboardRegression
{
    static int checks;
    static void Assert(bool value, string message) { if (!value) throw new Exception(message); checks++; }

    static int Main()
    {
        string nonce = new string('a', 32);
        // Text, including Cyrillic and characters outside the BMP, survives the round trip.
        foreach (string text in new[] { "hello", "Привет, мир", "emoji \U0001F600 and tabs\t\r\nlines", new string('x', 50000) })
            Assert(GuestClipboard.Decode(GuestClipboard.Encode(nonce, text), nonce) == text, "round trip of " + text.Length + " chars");
        Assert(GuestClipboard.Encode(nonce, "") == null && GuestClipboard.Encode(nonce, null) == null, "nothing to send for an empty copy");
        Assert(GuestClipboard.Encode(nonce, new string('y', GuestClipboard.MaxChars + 1)) == null, "oversized copies are not sent");
        // Only lines of this session are accepted.
        string line = GuestClipboard.Encode(nonce, "secret");
        Assert(GuestClipboard.Decode(line, new string('b', 32)) == null, "another session's line is dropped");
        Assert(GuestClipboard.Decode("not json", nonce) == null && GuestClipboard.Decode("{}", nonce) == null && GuestClipboard.Decode(null, nonce) == null, "garbage is dropped");
        Assert(GuestClipboard.Decode("{\"v\":2,\"n\":\"" + nonce + "\",\"t\":\"text\",\"z\":0,\"d\":\"aGk=\"}", nonce) == null, "unknown version is dropped");
        Assert(GuestClipboard.Decode("{\"v\":1,\"n\":\"" + nonce + "\",\"t\":\"image\",\"z\":0,\"d\":\"aGk=\"}", nonce) == null, "other types are dropped");
        Assert(GuestClipboard.Decode("{\"v\":1,\"n\":\"" + nonce + "\",\"t\":\"text\",\"z\":0,\"d\":\"aGk=\"}", nonce) == "hi", "uncompressed text is accepted");
        Assert(GuestClipboard.Decode("{\"v\":1,\"n\":\"" + nonce + "\",\"t\":\"text\",\"z\":0,\"d\":\"/w==\"}", nonce) == null, "invalid UTF-8 is dropped");
        Assert(GuestClipboard.Decode("{\"v\":1,\"n\":\"" + nonce + "\",\"t\":\"text\",\"z\":1,\"d\":\"!!!\"}", nonce) == null, "invalid base64 is dropped");
        // A small line that inflates to far more than the cap is refused.
        string bomb = GuestClipboardBomb(nonce);
        Assert(GuestClipboard.Decode(bomb, nonce) == null, "a decompression bomb is refused");
        Console.WriteLine("PASS guest clipboard: " + checks + " checks");
        return 0;
    }

    static string GuestClipboardBomb(string nonce)
    {
        using (var packed = new System.IO.MemoryStream())
        {
            using (var deflate = new System.IO.Compression.DeflateStream(packed, System.IO.Compression.CompressionMode.Compress, true))
            {
                var block = new byte[1 << 20];
                for (int i = 0; i < 8; i++) deflate.Write(block, 0, block.Length);
            }
            return "{\"v\":1,\"n\":\"" + nonce + "\",\"t\":\"text\",\"z\":1,\"d\":\"" + Convert.ToBase64String(packed.ToArray()) + "\"}";
        }
    }
}

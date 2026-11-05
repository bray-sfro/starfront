// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using System.Text.Json;
using NUnit.Framework;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

/// <summary>
/// The shared strip vectors, <c>docs/api/examples/strip-vectors.json</c>: input headers and the exact bytes a strip must
/// produce. The standalone uploader's tests load the same file. The server dedupes on the SHA-256 of the uploaded bytes, so a
/// frame uploaded by both clients must be stripped to identical bytes; a rule that changes in one client fails the other's
/// tests. The file's <c>builders</c> comment says how inputs and expected outputs are built from the JSON. Skips, with a
/// reason, when the file is not in the checkout.
/// </summary>
[TestFixture]
public class StripVectorsTests
{
    private static JsonDocument Load()
    {
        var path = RepoPaths.Find(Path.Combine("docs", "api", "examples", "strip-vectors.json"));
        if (path is null) Assert.Ignore($"docs/api/examples/strip-vectors.json is not reachable from {AppContext.BaseDirectory}");
        return JsonDocument.Parse(File.ReadAllText(path!));
    }

    public static IEnumerable<TestCaseData> FitsCases() => Cases("fits");
    public static IEnumerable<TestCaseData> XisfCases() => Cases("xisf");

    private static IEnumerable<TestCaseData> Cases(string kind)
    {
        // Cases are enumerated from the file when it can be found; with no file there is one case that skips.
        var path = RepoPaths.Find(Path.Combine("docs", "api", "examples", "strip-vectors.json"));
        if (path is null) { yield return new TestCaseData("(file not found)").SetName($"{kind}: strip-vectors.json not reachable"); yield break; }
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        foreach (var v in doc.RootElement.GetProperty(kind).EnumerateArray())
            yield return new TestCaseData(v.GetProperty("name").GetString()).SetName($"{kind}: {v.GetProperty("name").GetString()}");
    }

    private static byte[] Hex(string? s)
    {
        if (string.IsNullOrEmpty(s)) return Array.Empty<byte>();
        return Convert.FromHexString(s);
    }

    private static string? Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;

    private static StripOptions Options(JsonElement v) =>
        new() { IncludeHorizonCoordinates = !(v.TryGetProperty("options", out var o) && o.TryGetProperty("include_horizon", out var h) && h.ValueKind == JsonValueKind.False) };

    private static string[] Removed(JsonElement v) =>
        v.TryGetProperty("removed", out var r) ? r.EnumerateArray().Select(x => x.GetString()!).ToArray() : Array.Empty<string>();

    private static void AssertSame(string name, byte[] got, byte[] want)
    {
        if (got.AsSpan().SequenceEqual(want)) return;
        var at = 0;
        while (at < got.Length && at < want.Length && got[at] == want[at]) at++;
        string Window(byte[] b)
        {
            var from = Math.Min(Math.Max(at - 40, 0), b.Length);
            var to = Math.Min(at + 80, b.Length);
            return Encoding.Latin1.GetString(b, from, to - from);
        }
        Assert.Fail($"{name}: the stripped bytes differ (got {got.Length} bytes, want {want.Length}); first difference at byte {at}\n got:  {Window(got)}\n want: {Window(want)}");
    }

    // ---- FITS ----------------------------------------------------------------------------------------------------------

    private static byte[] FitsFile(JsonElement hdus, Func<int, JsonElement, IEnumerable<string>> cardsOf, string trailingHex)
    {
        var ms = new MemoryStream();
        var h = 0;
        foreach (var hdu in hdus.EnumerateArray())
        {
            var header = new List<byte>();
            foreach (var c in cardsOf(h, hdu))
            {
                Assert.That(c.Length, Is.LessThanOrEqualTo(80), "card longer than 80: " + c);
                header.AddRange(Encoding.Latin1.GetBytes(c.PadRight(80)));
            }
            header.AddRange(Encoding.Latin1.GetBytes("END".PadRight(80)));
            while (header.Count % 2880 != 0) header.Add((byte)' ');
            ms.Write(header.ToArray());
            // The data unit is the same length in the input and the expected output, so it is read from the input HDU.
            var n = hdus[h].GetProperty("data_len").GetInt32();
            var data = new byte[(n + 2879) / 2880 * 2880];
            for (var i = 0; i < n; i++) data[i] = (byte)((i * 7 + 3 + h) % 256);
            ms.Write(data);
            h++;
        }
        ms.Write(Hex(trailingHex));
        return ms.ToArray();
    }

    private static IEnumerable<string> CardsOf(JsonElement e) => e.GetProperty("cards").EnumerateArray().Select(c => c.GetString()!);

    [TestCaseSource(nameof(FitsCases))]
    public void Fits_vectors_are_stripped_to_exactly_the_expected_bytes(string name)
    {
        using var doc = Load();
        var v = doc.RootElement.GetProperty("fits").EnumerateArray().Single(x => x.GetProperty("name").GetString() == name);
        var hdus = v.GetProperty("input").GetProperty("hdus");
        var trailing = Str(v.GetProperty("input"), "trailing_hex") ?? "";
        var truncate = v.TryGetProperty("truncate_bytes", out var t) ? t.GetInt32() : 0;
        var input = FitsFile(hdus, (_, h) => CardsOf(h), trailing);
        input = input[..^truncate];

        var expected = v.GetProperty("expected");
        var output = new MemoryStream();
        if (expected.TryGetProperty("error", out _))
        {
            Assert.Throws<FitsFormatException>(() => FitsHeaderStripper.Strip(new MemoryStream(input), output, Options(v)), $"{name}: the strip must be refused");
            return;
        }
        var result = FitsHeaderStripper.Strip(new MemoryStream(input), output, Options(v));

        byte[] want;
        if (expected.TryGetProperty("same_as_input", out _)) want = input;
        else
        {
            var expHdus = expected.GetProperty("hdus");
            want = FitsFile(hdus, (h, _) => CardsOf(expHdus[h]), Str(expected, "trailing_hex") ?? trailing);
            want = want[..^truncate];
        }
        AssertSame(name, output.ToArray(), want);
        Assert.That(result.RemovedKeywords, Is.EqualTo(Removed(v)), $"{name}: removed keywords");
    }

    // ---- XISF ----------------------------------------------------------------------------------------------------------

    private static byte[] XisfFile(JsonElement v, string? header, string pad, string dataHex, int headerLen)
    {
        var sig = Str(v.GetProperty("input"), "signature") ?? "XISF0100";
        var body = new List<byte>(Str(v.GetProperty("input"), "header_hex") is { } hx && header is null ? Hex(hx) : Encoding.UTF8.GetBytes(header ?? ""));
        while (body.Count < headerLen) body.Add(pad == "nul" ? (byte)0 : (byte)' ');
        var ms = new MemoryStream();
        ms.Write(Encoding.ASCII.GetBytes(sig));
        ms.Write(BitConverter.GetBytes((uint)headerLen)); // little-endian on every platform this runs on
        ms.Write(new byte[4]);
        ms.Write(body.ToArray());
        ms.Write(Hex(dataHex));
        return ms.ToArray();
    }

    [TestCaseSource(nameof(XisfCases))]
    public void Xisf_vectors_are_stripped_to_exactly_the_expected_bytes(string name)
    {
        using var doc = Load();
        var v = doc.RootElement.GetProperty("xisf").EnumerateArray().Single(x => x.GetProperty("name").GetString() == name);
        var inp = v.GetProperty("input");
        var headerLen = inp.TryGetProperty("header_len", out var hl) ? hl.GetInt32() : 2048;
        var pad = Str(inp, "pad") ?? "space";
        var input = XisfFile(v, Str(inp, "header"), pad, Str(inp, "data_hex") ?? "", headerLen);

        var expected = v.GetProperty("expected");
        var output = new MemoryStream();
        if (expected.TryGetProperty("error", out _))
        {
            Assert.Throws<FitsFormatException>(() => XisfHeaderStripper.Strip(new MemoryStream(input), output, Options(v)), $"{name}: the strip must be refused");
            return;
        }
        var result = XisfHeaderStripper.Strip(new MemoryStream(input), output, Options(v));
        var want = expected.TryGetProperty("same_as_input", out _)
            ? input
            : XisfFile(v, Str(expected, "header"), Str(expected, "pad") ?? "space", Str(expected, "data_hex") ?? "", headerLen);
        AssertSame(name, output.ToArray(), want);
        Assert.That(result.RemovedKeywords, Is.EqualTo(Removed(v)), $"{name}: removed names");
    }

    [Test]
    public void The_vector_file_is_not_truncated()
    {
        using var doc = Load();
        Assert.That(doc.RootElement.GetProperty("fits").GetArrayLength(), Is.GreaterThanOrEqualTo(10));
        Assert.That(doc.RootElement.GetProperty("xisf").GetArrayLength(), Is.GreaterThanOrEqualTo(8));
    }
}

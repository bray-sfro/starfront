// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using System.Xml;

namespace RapidStackerCollab.Core.Fits;

/// <summary>What an XISF strip did.</summary>
/// <param name="RemovedKeywords">The <c>FITSKeyword</c> names and <c>Property</c> ids that were removed.</param>
/// <param name="ScrubbedBlocks">Attached data blocks of removed properties that were overwritten with zeros.</param>
public sealed record XisfStripResult(IReadOnlyList<string> RemovedKeywords, int ScrubbedBlocks, long BytesWritten)
{
    public int Removed => RemovedKeywords.Count;
}

/// <summary>
/// Writes a copy of a monolithic XISF file with the observer's location removed (plan §8.1, §10), the XISF counterpart of
/// <see cref="FitsHeaderStripper"/>.
/// <para>
/// <b>Why this is tractable.</b> Every data block is addressed by an <i>absolute</i> file position
/// (<c>location="attachment:pos:size"</c>), so shortening the XML header would move the blocks. Instead the declared header
/// length is <b>kept</b>: the removed elements are cut out of the XML <i>text</i> (everything else in the header stays byte for
/// byte) and the header is padded back to its original length (with NUL bytes if the header already contains NULs, as PixInsight writes it, otherwise blanks), so every position in the file stays valid and
/// everything after the header is copied verbatim. If the cut header is somehow longer than the original (it cannot be: text is
/// only removed) or is no longer well-formed XML, the strip fails instead of writing a damaged file.
/// </para>
/// <para>
/// Removed: <c>FITSKeyword</c> elements that <see cref="FitsHeaderStripper.ShouldStrip"/> names (the same list as for FITS) and
/// <c>Property</c> elements whose id begins <c>Observation:Location:</c> or <c>Observation:Observer</c>. A removed property whose
/// value lives in an attached block would leave the value in the file, so that block is overwritten with zeros in the copy.
/// The source file is opened read-only and never changed; the output depends only on the input.
/// </para>
/// </summary>
public static class XisfHeaderStripper
{
    private const int MaxHeaderBytes = 16 * 1024 * 1024;
    private static readonly byte[] Signature = Encoding.ASCII.GetBytes("XISF0100");
    private static readonly string[] PropertyPrefixes = { "Observation:Location:", "Observation:Observer" };

    public static bool ShouldStripProperty(string? id) =>
        id is not null && PropertyPrefixes.Any(p => StartsWithAsciiIgnoreCase(id, p));

    // ASCII-only case folding, so the answer cannot depend on the runtime's Unicode tables (the uploader does the same).
    private static bool StartsWithAsciiIgnoreCase(string s, string prefix)
    {
        if (s.Length < prefix.Length) return false;
        for (var i = 0; i < prefix.Length; i++)
        {
            var a = s[i];
            var b = prefix[i];
            if (a is >= 'A' and <= 'Z') a = (char)(a + 32);
            if (b is >= 'A' and <= 'Z') b = (char)(b + 32);
            if (a != b) return false;
        }
        return true;
    }

    private static readonly char[] TrailingPadding = { '\0', ' ', '\n', '\r', '\t' };

    // The header is followed by NUL or blank padding, which is not XML; the offsets of the part that is XML are unchanged.
    private static XmlReader CreateReader(string xml) =>
        XmlReader.Create(new StringReader(xml.TrimEnd(TrailingPadding)), new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null });

    /// <summary>Write the stripped copy of <paramref name="sourcePath"/> to a new file <paramref name="destPath"/> (which must not exist).</summary>
    public static XisfStripResult StripToFile(string sourcePath, string destPath, StripOptions? options = null)
    {
        if (string.Equals(Path.GetFullPath(sourcePath), Path.GetFullPath(destPath), StringComparison.Ordinal))
            throw new ArgumentException("The destination must not be the source: the original is never rewritten.", nameof(destPath));
        using var src = new FileStream(sourcePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 16, FileOptions.SequentialScan);
        var dst = new FileStream(destPath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 16, FileOptions.SequentialScan);
        try
        {
            var result = Strip(src, dst, options ?? StripOptions.Default);
            dst.Flush(true);
            return result;
        }
        catch
        {
            dst.Dispose();
            try { File.Delete(destPath); } catch (IOException) { } catch (UnauthorizedAccessException) { }
            throw;
        }
        finally
        {
            dst.Dispose();
        }
    }

    public static XisfStripResult Strip(Stream source, Stream destination, StripOptions options)
    {
        var fixedPart = new byte[16];
        if (ReadFully(source, fixedPart) != 16 || !fixedPart.AsSpan(0, 8).SequenceEqual(Signature))
            throw new FitsFormatException("Not an XISF file: the XISF0100 signature is missing.");
        var length = BitConverter.ToUInt32(fixedPart, 8);
        if (!BitConverter.IsLittleEndian) length = System.Buffers.Binary.BinaryPrimitives.ReverseEndianness(length);
        if (length == 0 || length > MaxHeaderBytes) throw new FitsFormatException("The XISF header length is implausible.");
        var raw = new byte[length];
        if (ReadFully(source, raw) != raw.Length) throw new FitsFormatException("The file ends inside the XISF header.");

        string xml;
        try { xml = new UTF8Encoding(false, true).GetString(raw); }
        catch (ArgumentException) { throw new FitsFormatException("The XISF header is not UTF-8."); }
        var cuts = FindCuts(xml, options, out var removed, out var scrub);
        var cutXml = Apply(xml, cuts);
        Verify(cutXml, options);

        var body = Encoding.UTF8.GetBytes(cutXml);
        if (body.Length > raw.Length) throw new FitsFormatException("Internal error: the stripped XISF header is longer than the original.");

        // The removed text is made up with padding in the file's own style: NUL when the header already contains NULs
        // (PixInsight pads that way), blanks otherwise. The uploader makes the same choice, so both write the same bytes.
        var padByte = Array.IndexOf(raw, (byte)0) >= 0 ? (byte)0 : (byte)' ';
        destination.Write(fixedPart, 0, 16);
        destination.Write(body, 0, body.Length);
        var pad = new byte[raw.Length - body.Length];
        Array.Fill(pad, padByte);
        destination.Write(pad, 0, pad.Length);

        var written = 16L + raw.Length;
        var scrubbed = CopyRest(source, destination, 16L + raw.Length, scrub, ref written);
        return new XisfStripResult(removed, scrubbed, written);
    }

    // ---- finding what to cut ---------------------------------------------------------------------------------------

    private readonly record struct Cut(int Start, int End);

    private static List<Cut> FindCuts(string xml, StripOptions options, out List<string> removed, out List<(long Pos, long Size)> scrub)
    {
        removed = new List<string>();
        scrub = new List<(long, long)>();
        var cuts = new List<Cut>();
        var lineStarts = LineStarts(xml);
        try
        {
            using var reader = CreateReader(xml);
            var info = (IXmlLineInfo)reader;
            var skipUntil = -1;
            while (reader.Read())
            {
                if (reader.NodeType != XmlNodeType.Element) continue;
                var open = OffsetOf(lineStarts, info.LineNumber, info.LinePosition) - 1; // LinePosition is the first char of the name; '<' precedes it
                if (open < skipUntil) continue;
                string? label = null;
                if (reader.LocalName == "FITSKeyword")
                {
                    var name = reader.GetAttribute("name");
                    if (name is not null && FitsHeaderStripper.ShouldStrip(name, options)) label = name.Trim();
                }
                else if (reader.LocalName == "Property")
                {
                    var id = reader.GetAttribute("id");
                    if (ShouldStripProperty(id)) label = id;
                }
                if (label is null) continue;

                var location = reader.GetAttribute("location");
                var qname = reader.Name;
                var end = ElementEnd(xml, open, qname);
                cuts.Add(new Cut(open, end));
                skipUntil = end;
                removed.Add(label);
                if (TryAttachment(location, out var pos, out var size)) scrub.Add((pos, size));
            }
        }
        catch (XmlException ex)
        {
            throw new FitsFormatException("The XISF header is not valid XML: " + ex.Message);
        }
        return cuts;
    }

    private static string Apply(string xml, List<Cut> cuts)
    {
        if (cuts.Count == 0) return xml;
        var sb = new StringBuilder(xml.Length);
        var at = 0;
        foreach (var c in cuts.OrderBy(c => c.Start))
        {
            sb.Append(xml, at, c.Start - at);
            at = c.End;
        }
        sb.Append(xml, at, xml.Length - at);
        return sb.ToString();
    }

    /// <summary>Re-read the cut header: it must still be well-formed and must carry nothing that was meant to go.</summary>
    private static void Verify(string xml, StripOptions options)
    {
        try
        {
            using var reader = CreateReader(xml);
            while (reader.Read())
            {
                if (reader.NodeType != XmlNodeType.Element) continue;
                if (reader.LocalName == "FITSKeyword" && reader.GetAttribute("name") is { } n && FitsHeaderStripper.ShouldStrip(n, options))
                    throw new FitsFormatException("Internal error: a location keyword survived the XISF strip.");
                if (reader.LocalName == "Property" && ShouldStripProperty(reader.GetAttribute("id")))
                    throw new FitsFormatException("Internal error: a location property survived the XISF strip.");
            }
        }
        catch (XmlException ex)
        {
            throw new FitsFormatException("The stripped XISF header would not be valid XML (" + ex.Message + "); the file was not written.");
        }
    }

    /// <summary>End (exclusive) of the element that starts at <paramref name="open"/>: the end of an empty element's tag, or of its closing tag.</summary>
    private static int ElementEnd(string xml, int open, string qualifiedName)
    {
        var tagEnd = EndOfTag(xml, open);
        if (xml[tagEnd - 2] == '/') return tagEnd; // <a ... />
        var close = xml.IndexOf("</" + qualifiedName, tagEnd, StringComparison.Ordinal);
        if (close < 0) throw new FitsFormatException("The XISF header has an unterminated element.");
        return EndOfTag(xml, close);
    }

    /// <summary>Index just after the '>' that closes the tag starting at <paramref name="open"/>, skipping '>' inside quoted attribute values.</summary>
    private static int EndOfTag(string xml, int open)
    {
        char quote = '\0';
        for (var i = open + 1; i < xml.Length; i++)
        {
            var c = xml[i];
            if (quote != '\0') { if (c == quote) quote = '\0'; }
            else if (c is '"' or '\'') quote = c;
            else if (c == '>') return i + 1;
        }
        throw new FitsFormatException("The XISF header has an unterminated tag.");
    }

    private static List<int> LineStarts(string text)
    {
        // XmlReader counts "\r\n", "\n" and a lone "\r" as one line break each.
        var starts = new List<int> { 0 };
        for (var i = 0; i < text.Length; i++)
        {
            if (text[i] == '\r')
            {
                if (i + 1 < text.Length && text[i + 1] == '\n') i++;
                starts.Add(i + 1);
            }
            else if (text[i] == '\n') starts.Add(i + 1);
        }
        return starts;
    }

    private static int OffsetOf(List<int> lineStarts, int line, int position) => lineStarts[line - 1] + position - 1;

    private static bool TryAttachment(string? location, out long pos, out long size)
    {
        pos = size = 0;
        if (location is null) return false;
        var parts = location.Split(':');
        // A leading '+' is accepted, as the uploader's integer parser does: the more a removed property's block is scrubbed, the better.
        return parts.Length >= 3 && parts[0] == "attachment"
            && TryUnsigned(parts[1], out pos)
            && TryUnsigned(parts[2], out size);
    }

    // Digits with at most one leading '+', the shape the uploader's integer parser accepts.
    private static bool TryUnsigned(string text, out long value) =>
        long.TryParse(text.StartsWith('+') ? text[1..] : text, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out value);

    // ---- copying the rest of the file ------------------------------------------------------------------------------

    private static int CopyRest(Stream source, Stream destination, long position, List<(long Pos, long Size)> scrub, ref long written)
    {
        var ranges = scrub.Where(r => r.Size > 0 && r.Pos >= 0).OrderBy(r => r.Pos).ToList();
        var buffer = new byte[1 << 16];
        var scrubbed = new HashSet<(long, long)>();
        int n;
        while ((n = source.Read(buffer, 0, buffer.Length)) > 0)
        {
            var chunkStart = position;
            foreach (var r in ranges)
            {
                var from = Math.Max(r.Pos, chunkStart);
                var to = Math.Min(r.Pos + r.Size, chunkStart + n);
                if (from >= to) continue;
                Array.Clear(buffer, (int)(from - chunkStart), (int)(to - from));
                scrubbed.Add(r);
            }
            destination.Write(buffer, 0, n);
            position += n;
            written += n;
        }
        return scrubbed.Count;
    }

    private static int ReadFully(Stream s, byte[] buffer)
    {
        var n = 0;
        while (n < buffer.Length)
        {
            var got = s.Read(buffer, n, buffer.Length - n);
            if (got <= 0) break;
            n += got;
        }
        return n;
    }
}

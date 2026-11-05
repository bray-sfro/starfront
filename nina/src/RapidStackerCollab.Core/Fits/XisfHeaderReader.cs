// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using System.Xml;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Fits;

/// <summary>
/// Reads the metadata of a monolithic XISF file (signature <c>XISF0100</c>, then a little-endian header length, then an
/// XML header) into the same <see cref="InitHeader"/> as <see cref="FitsHeaderReader"/>: the <c>FITSKeyword</c> elements
/// of the first <c>Image</c>, plus geometry and sample format for <c>NAXIS1</c>, <c>NAXIS2</c> and <c>BITPIX</c>.
/// </summary>
/// <remarks>
/// Reading is all this does. <b>Stripping an XISF is not implemented</b>: the image data block is addressed by an
/// absolute byte position in the XML (<c>location="attachment:pos:size"</c>), so removing keywords moves it and every
/// position has to be recomputed. Until that exists the uploader refuses to send an XISF while stripping is on, and
/// stripping is on by default (plan §8.1). The XML is parsed with DTDs prohibited and no resolver.
/// </remarks>
public static class XisfHeaderReader
{
    private const int MaxHeaderBytes = 16 * 1024 * 1024;
    private static readonly byte[] Signature = Encoding.ASCII.GetBytes("XISF0100");

    /// <summary>True when the file starts with the XISF signature.</summary>
    public static bool IsXisf(string path)
    {
        try
        {
            using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var sig = new byte[Signature.Length];
            return FitsHeader.ReadFully(s, sig) == sig.Length && sig.AsSpan().SequenceEqual(Signature);
        }
        catch (IOException)
        {
            return false;
        }
    }

    public static InitHeader Read(string path)
    {
        using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        return Read(s);
    }

    public static InitHeader Read(Stream stream) => FitsHeaderReader.FromHeader(ReadCards(stream));

    /// <summary>The <c>FITSKeyword</c> cards of the first image (plus synthesised <c>NAXIS1</c>, <c>NAXIS2</c> and <c>BITPIX</c>), for a caller that needs a keyword the allow-list does not carry.</summary>
    public static FitsHeader ReadCards(Stream stream)
    {
        var fixedPart = new byte[16];
        if (FitsHeader.ReadFully(stream, fixedPart) != 16 || !fixedPart.AsSpan(0, 8).SequenceEqual(Signature))
            throw new FitsFormatException("Not an XISF file: the XISF0100 signature is missing.");
        var length = BitConverter.ToUInt32(fixedPart, 8);
        if (!BitConverter.IsLittleEndian) length = System.Buffers.Binary.BinaryPrimitives.ReverseEndianness(length);
        if (length == 0 || length > MaxHeaderBytes) throw new FitsFormatException("The XISF header length is implausible.");
        var xml = new byte[length];
        if (FitsHeader.ReadFully(stream, xml) != xml.Length) throw new FitsFormatException("The file ends inside the XISF header.");

        var cards = new List<FitsCard>();
        var settings = new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, IgnoreComments = true };
        try
        {
            using var reader = XmlReader.Create(new MemoryStream(xml), settings);
            var inImage = false;
            var imageDepth = -1;
            var sawImage = false;
            while (reader.Read())
            {
                if (reader.NodeType == XmlNodeType.EndElement && inImage && reader.Depth == imageDepth)
                {
                    inImage = false;
                    continue;
                }
                if (reader.NodeType != XmlNodeType.Element) continue;
                if (reader.LocalName == "Image" && !sawImage)
                {
                    sawImage = true;
                    inImage = !reader.IsEmptyElement;
                    imageDepth = reader.Depth;
                    AddGeometry(cards, reader.GetAttribute("geometry"), reader.GetAttribute("sampleFormat"));
                }
                else if (reader.LocalName == "FITSKeyword" && inImage)
                {
                    var name = reader.GetAttribute("name");
                    if (string.IsNullOrWhiteSpace(name)) continue;
                    var (value, quoted) = Unquote(reader.GetAttribute("value"));
                    cards.Add(FitsCard.Synthesize(name.Trim(), value, quoted, reader.GetAttribute("comment")));
                }
            }
            if (!sawImage) throw new FitsFormatException("The XISF header has no Image element.");
        }
        catch (XmlException ex)
        {
            throw new FitsFormatException("The XISF header is not valid XML: " + ex.Message);
        }
        return new FitsHeader(cards, 0);
    }

    // PixInsight stores FITS string values with their apostrophes; N.I.N.A. and others may not. Accept both.
    private static (string? Value, bool Quoted) Unquote(string? raw)
    {
        if (raw is null) return (null, false);
        var t = raw.Trim();
        if (t.Length >= 2 && t[0] == '\'' && t[^1] == '\'')
            return (t[1..^1].Replace("''", "'").TrimEnd(), true);
        return (t, !double.TryParse(t, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out _) && t is not ("T" or "F"));
    }

    private static void AddGeometry(List<FitsCard> cards, string? geometry, string? sampleFormat)
    {
        if (geometry is not null)
        {
            var parts = geometry.Split(':');
            if (parts.Length >= 2 && int.TryParse(parts[0], out var w) && int.TryParse(parts[1], out var h))
            {
                cards.Add(FitsCard.Synthesize("NAXIS1", w.ToString(System.Globalization.CultureInfo.InvariantCulture), false));
                cards.Add(FitsCard.Synthesize("NAXIS2", h.ToString(System.Globalization.CultureInfo.InvariantCulture), false));
            }
        }
        var bitpix = sampleFormat switch
        {
            "UInt8" => 8, "UInt16" => 16, "UInt32" => 32, "Float32" => -32, "Float64" => -64, _ => (int?)null,
        };
        if (bitpix is { } b) cards.Add(FitsCard.Synthesize("BITPIX", b.ToString(System.Globalization.CultureInfo.InvariantCulture), false));
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;

namespace RapidStackerCollab.Core.Fits;

/// <summary>Which privacy cards <see cref="FitsHeaderStripper"/> removes.</summary>
public sealed class StripOptions
{
    /// <summary>
    /// Also remove horizon coordinates (<c>CENTALT</c>, <c>CENTAZ</c>, <c>OBJCTALT</c>, <c>OBJCTAZ</c>, <c>ALTITUDE</c>,
    /// <c>AZIMUTH</c>, <c>AIRMASS</c>). Altitude and azimuth of a known RA/Dec at a known time fix the observer's
    /// position exactly, so leaving them defeats removing <c>SITELAT</c>/<c>SITELONG</c>. On by default.
    /// </summary>
    public bool IncludeHorizonCoordinates { get; init; } = true;

    public static StripOptions Default { get; } = new();
}

/// <summary>What a strip did.</summary>
public sealed record StripResult(IReadOnlyList<string> RemovedKeywords, int HduCount, long BytesWritten)
{
    public int CardsRemoved => RemovedKeywords.Count;
}

/// <summary>
/// Writes a copy of a FITS file with the observer-location cards removed, for upload (plan §8.1, §10). Only header
/// blocks are rewritten (2880-byte records, one END card, blank padding); every data unit and every card that is kept
/// is copied byte for byte. The source is opened read-only and is never modified, and the output depends only on the
/// input, so a resumed upload hashes to the same SHA-256 as the one that was interrupted.
/// </summary>
/// <remarks>
/// Cards are matched by keyword with spaces, hyphens and underscores ignored, in every HDU. A card is removed whether or
/// not it carries a value (a blank <c>SITELAT =</c> is removed too), and a <c>HIERARCH</c> card matches by its whole long
/// keyword or by its last one or two words (<c>HIERARCH ESO TEL GEOLAT</c>). <c>COMMENT</c>/<c>HISTORY</c> free text is not
/// inspected. These rules are held identical to the standalone uploader's by the shared vectors in
/// <c>docs/api/examples/strip-vectors.json</c>: a frame stripped by either client has the same bytes, so the same SHA-256.
/// <c>DATE-LOC</c> (local time of the exposure, which together with <c>DATE-OBS</c> reveals the UTC offset) is deliberately kept: it is not one of
/// the cards the plan names, and removing it is a one-line change in <see cref="LocationKeywords"/>.
/// </remarks>
public static class FitsHeaderStripper
{
    /// <summary>The location and observer cards that are always removed (compared after <see cref="Normalise"/>).</summary>
    public static readonly IReadOnlyList<string> LocationKeywords = new[]
    {
        // The four the plan and the contract name.
        "SITELAT", "SITELONG", "SITEELEV", "OBSERVER",
        // Variants written by other capture software and observatory conventions.
        "SITELON", "SITEALT", "SITE", "SITENAME", "OBSERVAT",
        "LAT-OBS", "LONG-OBS", "LON-OBS", "ALT-OBS", "ELEV-OBS", "LATOBS", "LONGOBS", "LONOBS", "ALTOBS",
        "OBS-LAT", "OBS-LONG", "OBS-LON", "OBS-ALT", "OBS-ELEV", "OBSLAT", "OBSLONG", "OBSLON", "OBSALT", "OBSELEV",
        "OBSGEO-X", "OBSGEO-Y", "OBSGEO-Z", "OBSGEO-L", "OBSGEO-B", "OBSGEO-H",
        "GEOLAT", "GEOLON", "GEOLONG", "GEOELEV", "GEOALT",
        "LATITUDE", "LONGITUD", "LONGITUDE", "ELEVATIO", "ELEVATION",
    };

    /// <summary>Horizon-coordinate cards removed when <see cref="StripOptions.IncludeHorizonCoordinates"/> is on.</summary>
    public static readonly IReadOnlyList<string> HorizonKeywords = new[]
    {
        "CENTALT", "CENTAZ", "OBJCTALT", "OBJCTAZ", "ALTITUDE", "AZIMUTH", "AIRMASS",
    };

    private static readonly HashSet<string> LocationSet = new(LocationKeywords.Select(Normalise), StringComparer.Ordinal);
    private static readonly HashSet<string> HorizonSet = new(HorizonKeywords.Select(Normalise), StringComparer.Ordinal);

    /// <summary>Upper-case with spaces, hyphens and underscores removed: <c>LAT-OBS</c>, <c>LAT_OBS</c> and <c>HIERARCH LAT OBS</c> all match.</summary>
    public static string Normalise(string keyword) =>
        new(keyword.Where(c => c is not (' ' or '-' or '_')).Select(char.ToUpperInvariant).ToArray());

    /// <summary>
    /// Whether a card with this keyword is an observer-location (or, by default, horizon) card. <c>HIERARCH</c> keywords are
    /// made of words (<c>ESO TEL GEOLAT</c>): they match as a whole and by their last one or two words, so a
    /// convention-prefixed location card goes too.
    /// </summary>
    public static bool ShouldStrip(string keyword, StripOptions? options = null)
    {
        options ??= StripOptions.Default;
        if (Hit(keyword, options)) return true;
        var words = keyword.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (words.Length < 2) return false;
        return Hit(words[^1], options) || (words.Length > 2 && Hit(words[^2] + " " + words[^1], options));
    }

    private static bool Hit(string keyword, StripOptions options)
    {
        var k = Normalise(keyword);
        return LocationSet.Contains(k) || (options.IncludeHorizonCoordinates && HorizonSet.Contains(k));
    }

    /// <summary>Write the stripped copy of <paramref name="sourcePath"/> to a new file <paramref name="destPath"/> (which must not exist).</summary>
    public static StripResult StripToFile(string sourcePath, string destPath, StripOptions? options = null)
    {
        if (string.Equals(Path.GetFullPath(sourcePath), Path.GetFullPath(destPath), StringComparison.Ordinal))
            throw new ArgumentException("The destination must not be the source: the original is never rewritten.", nameof(destPath));
        using var src = new FileStream(sourcePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 16, FileOptions.SequentialScan);
        // CreateNew: never overwrite (or later delete) a file this call did not create.
        var dst = new FileStream(destPath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1 << 16, FileOptions.SequentialScan);
        try
        {
            using (dst) return Strip(src, dst, options);
        }
        catch
        {
            try { File.Delete(destPath); } catch { /* best effort: the half-written copy is ours */ }
            throw;
        }
    }

    /// <summary><paramref name="source"/> must be seekable; it is read, never written.</summary>
    public static StripResult Strip(Stream source, Stream dest, StripOptions? options = null)
    {
        options ??= StripOptions.Default;
        if (!source.CanSeek) throw new ArgumentException("The source stream must be seekable.", nameof(source));
        var removed = new List<string>();
        var hdus = 0;
        long written = 0;
        var buffer = new byte[1 << 16];

        while (true)
        {
            var start = source.Position;
            var header = TryReadHdu(source);
            if (header is null)
            {
                if (hdus == 0) throw new FitsFormatException("Not a FITS file: it does not start with SIMPLE.");
                source.Position = start;
                written += CopyAll(source, dest, buffer); // clean EOF copies nothing; trailing junk is kept verbatim
                break;
            }
            hdus++;

            var kept = new List<FitsCard>(header.Cards.Count);
            var changed = false;
            var dropContinuations = false;
            foreach (var card in header.Cards)
            {
                if (!card.IsEnd && ShouldStrip(card.Keyword, options))
                {
                    removed.Add(card.Keyword);
                    changed = true;
                    // A long string continues on CONTINUE cards when it ends in '&'; they go with it.
                    dropContinuations = card.IsString && card.Value is { } sv && sv.EndsWith('&');
                    continue;
                }
                if (dropContinuations && card.Keyword == "CONTINUE")
                {
                    changed = true;
                    dropContinuations = card.Value is { } v && v.EndsWith('&') || IsContinuedText(card);
                    continue;
                }
                dropContinuations = false;
                kept.Add(card);
            }

            if (!changed)
            {
                dest.Write(header.Raw, 0, header.Raw.Length);
                written += header.Raw.Length;
            }
            else
            {
                var cardBytes = kept.Count * FitsCard.Length;
                var total = (cardBytes + FitsHeader.BlockSize - 1) / FitsHeader.BlockSize * FitsHeader.BlockSize;
                var block = new byte[total];
                Array.Fill(block, (byte)' ');
                for (var i = 0; i < kept.Count; i++)
                    Buffer.BlockCopy(kept[i].Raw, 0, block, i * FitsCard.Length, FitsCard.Length);
                dest.Write(block, 0, block.Length);
                written += block.Length;
            }

            written += CopyDataUnit(source, dest, header, buffer);
        }
        dest.Flush();
        return new StripResult(removed, hdus, written);
    }

    // CONTINUE cards carry no "= " so Value is null; whether they continue further is in the raw text before the closing quote.
    private static bool IsContinuedText(FitsCard card)
    {
        var text = card.ToString();
        var close = text.LastIndexOf('\'');
        return close > 0 && text[..close].EndsWith('&');
    }

    // Reads the next HDU header, or returns null when what follows is not an HDU (clean EOF or trailing bytes).
    private static FitsHeader? TryReadHdu(Stream source)
    {
        var start = source.Position;
        var probe = new byte[8];
        var got = FitsHeader.ReadFully(source, probe);
        source.Position = start;
        if (got < 8) return null;
        var word = Encoding.ASCII.GetString(probe);
        if (word != "SIMPLE  " && word != "XTENSION") return null;
        return FitsHeader.Read(source);
    }

    private static long CopyDataUnit(Stream source, Stream dest, FitsHeader header, byte[] buffer)
    {
        var dataLength = header.DataLength();
        var padded = header.PaddedDataLength();
        long remaining = padded;
        long copied = 0;
        while (remaining > 0)
        {
            var n = source.Read(buffer, 0, (int)Math.Min(buffer.Length, remaining));
            if (n == 0) break;
            dest.Write(buffer, 0, n);
            remaining -= n;
            copied += n;
        }
        // A writer may omit the final padding; a data unit shorter than its declared length is truncation.
        if (copied < dataLength) throw new FitsFormatException("The file ends inside a data unit.");
        return copied;
    }

    private static long CopyAll(Stream source, Stream dest, byte[] buffer)
    {
        long total = 0;
        int n;
        while ((n = source.Read(buffer, 0, buffer.Length)) > 0)
        {
            dest.Write(buffer, 0, n);
            total += n;
        }
        return total;
    }
}

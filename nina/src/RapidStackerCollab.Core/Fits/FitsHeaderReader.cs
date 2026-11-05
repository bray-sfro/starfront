// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Fits;

/// <summary>
/// Turns a FITS primary header into the <see cref="InitHeader"/> allow-list of <c>frames/init</c>, and nothing else.
/// Location and observer cards (<c>SITELAT</c>, <c>OBSERVER</c> ...) are never read, so they cannot leak into a request
/// even by mistake. String lengths are clamped to the contract's limits so a long free-text card cannot turn a good
/// frame into a <c>validation_failed</c>.
/// </summary>
public static class FitsHeaderReader
{
    public static InitHeader Read(string path)
    {
        using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.SequentialScan);
        return Read(s);
    }

    public static InitHeader Read(Stream stream) => FromHeader(ReadCards(stream));

    /// <summary>The primary header with all its cards, for a caller that needs a keyword the allow-list does not carry (<c>CCD-TEMP</c>).</summary>
    public static FitsHeader ReadCards(Stream stream)
    {
        var header = FitsHeader.Read(stream) ?? throw new FitsFormatException("The file is empty.");
        if (!header.IsPrimary) throw new FitsFormatException("The file does not start with a primary HDU.");
        return header;
    }

    public static InitHeader FromHeader(FitsHeader h)
    {
        var o = new InitHeader
        {
            Filter = Str(h, "FILTER", 80),
            ImageTyp = Str(h, "IMAGETYP", 40),
            ExpTime = Num(h, "EXPTIME") ?? Num(h, "EXPOSURE"),
            Gain = Num(h, "GAIN"),
            Offset = Num(h, "OFFSET"),
            XBinning = Int(h, "XBINNING"),
            YBinning = Int(h, "YBINNING"),
            Instrume = Str(h, "INSTRUME", 80),
            Telescop = Str(h, "TELESCOP", 80),
            FocalLen = Num(h, "FOCALLEN"),
            XPixSz = Num(h, "XPIXSZ"),
            YPixSz = Num(h, "YPIXSZ"),
            BayerPat = Str(h, "BAYERPAT", 8),
            DateObs = Str(h, "DATE-OBS", 40),
            Object = Str(h, "OBJECT", 80),
            Ra = Coordinate(h, "RA"),
            Dec = Coordinate(h, "DEC"),
            ObjctRa = Str(h, "OBJCTRA", 40),
            ObjctDec = Str(h, "OBJCTDEC", 40),
            PixScale = Num(h, "PIXSCALE"),
            SetTemp = Num(h, "SET-TEMP"),
            CalStat = Str(h, "CALSTAT", 16),
            Naxis1 = Int(h, "NAXIS1") is { } n1 and >= 1 ? n1 : null,
            Naxis2 = Int(h, "NAXIS2") is { } n2 and >= 1 ? n2 : null,
            BitPix = Int(h, "BITPIX"),
        };
        if (o.XBinning is < 1) o.XBinning = null;
        if (o.YBinning is < 1) o.YBinning = null;
        return o;
    }

    private static string? Str(FitsHeader h, string key, int max)
    {
        var v = h.GetString(key)?.Trim();
        if (string.IsNullOrEmpty(v)) return null;
        return v.Length <= max ? v : v[..max];
    }

    private static double? Num(FitsHeader h, string key) => h.GetDouble(key);

    private static int? Int(FitsHeader h, string key) => h.GetInt(key);

    // RA and DEC are degrees as numbers in N.I.N.A.'s files; some writers use sexagesimal strings. Both are allowed.
    private static object? Coordinate(FitsHeader h, string key)
    {
        var card = h.Get(key);
        if (card is null) return null;
        if (!card.IsString && card.TryGetDouble(out var d)) return d;
        var s = card.Value?.Trim();
        if (string.IsNullOrEmpty(s)) return null;
        return card.TryGetDouble(out var parsed) ? parsed : (s.Length <= 40 ? s : s[..40]);
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using System.Text;

namespace RapidStackerCollab.Core.Tests.Support;

/// <summary>Writes small, valid FITS files (several HDUs, any BITPIX/NAXIS/PCOUNT) with deterministic data units.</summary>
public sealed class FitsBuilder
{
    private readonly List<Hdu> hdus = new();

    private sealed record Hdu(string? Xtension, int Bitpix, int[] Naxis, int Pcount, int Gcount, List<string> Cards, byte[] Data);

    public static string Card(string keyword, string valuePart, string? comment = null)
    {
        var kw = keyword.PadRight(8);
        var body = valuePart;
        if (comment is not null) body += " / " + comment;
        return (kw + "= " + body).PadRight(80)[..80];
    }

    public static string Str(string keyword, string value, string? comment = null) =>
        Card(keyword, "'" + value.Replace("'", "''").PadRight(8) + "'", comment);

    public static string Num(string keyword, double value, string? comment = null) =>
        Card(keyword, value.ToString("0.0###############", CultureInfo.InvariantCulture).PadLeft(20), comment);

    public static string Int(string keyword, long value, string? comment = null) =>
        Card(keyword, value.ToString(CultureInfo.InvariantCulture).PadLeft(20), comment);

    public static string Bool(string keyword, bool value, string? comment = null) =>
        Card(keyword, (value ? "T" : "F").PadLeft(20), comment);

    public static string Text(string keyword, string text) => (keyword.PadRight(8) + " " + text).PadRight(80)[..80];

    /// <summary>Add the primary HDU. <paramref name="cards"/> are 80-char strings placed after NAXISn.</summary>
    public FitsBuilder Primary(IEnumerable<string> cards, int bitpix = 16, int[]? naxis = null, byte[]? data = null)
    {
        var axes = naxis ?? new[] { 8, 6 };
        hdus.Add(new Hdu(null, bitpix, axes, 0, 1, cards.ToList(), data ?? Pattern(DataLen(bitpix, axes, 0, 1), 1)));
        return this;
    }

    public FitsBuilder Extension(string xtension, IEnumerable<string> cards, int bitpix = 8, int[]? naxis = null, int pcount = 0, int gcount = 1, byte[]? data = null)
    {
        var axes = naxis ?? new[] { 16, 4 };
        hdus.Add(new Hdu(xtension, bitpix, axes, pcount, gcount, cards.ToList(), data ?? Pattern(DataLen(bitpix, axes, pcount, gcount), 2 + hdus.Count)));
        return this;
    }

    private static int DataLen(int bitpix, int[] axes, int pcount, int gcount)
    {
        if (axes.Length == 0) return 0;
        var product = 1;
        foreach (var a in axes) product *= a;
        return Math.Abs(bitpix) / 8 * gcount * (pcount + product);
    }

    /// <summary>Deterministic pseudo-random bytes that are never all zero, so a lost data unit cannot pass as padding.</summary>
    public static byte[] Pattern(int length, int seed)
    {
        var b = new byte[length];
        var x = (uint)(seed * 2654435761u + 12345);
        for (var i = 0; i < b.Length; i++)
        {
            x = x * 1664525u + 1013904223u;
            b[i] = (byte)((x >> 24) | 1);
        }
        return b;
    }

    public byte[] Build()
    {
        using var ms = new MemoryStream();
        for (var i = 0; i < hdus.Count; i++)
        {
            var h = hdus[i];
            var cards = new List<string>();
            if (h.Xtension is null)
            {
                cards.Add(Bool("SIMPLE", true));
            }
            else
            {
                cards.Add(Str("XTENSION", h.Xtension));
            }
            cards.Add(Int("BITPIX", h.Bitpix));
            cards.Add(Int("NAXIS", h.Naxis.Length));
            for (var n = 0; n < h.Naxis.Length; n++) cards.Add(Int("NAXIS" + (n + 1), h.Naxis[n]));
            if (h.Xtension is not null)
            {
                cards.Add(Int("PCOUNT", h.Pcount));
                cards.Add(Int("GCOUNT", h.Gcount));
            }
            cards.AddRange(h.Cards);
            cards.Add("END".PadRight(80));
            var headerText = string.Concat(cards);
            var pad = (2880 - headerText.Length % 2880) % 2880;
            var headerBytes = Encoding.ASCII.GetBytes(headerText + new string(' ', pad));
            ms.Write(headerBytes);
            ms.Write(h.Data);
            var dataPad = (2880 - h.Data.Length % 2880) % 2880;
            ms.Write(new byte[dataPad]);
        }
        return ms.ToArray();
    }

    public string WriteTo(string directory, string name)
    {
        var path = Path.Combine(directory, name);
        File.WriteAllBytes(path, Build());
        return path;
    }

    /// <summary>A light frame with the cards N.I.N.A. 3.x writes, including the observer-location cards that must not leave the machine.</summary>
    public static FitsBuilder NinaLight(string filter = "Ha", double exposure = 300, bool withLocation = true, int width = 64, int height = 48)
    {
        var cards = new List<string>
        {
            Num("BZERO", 32768), Num("BSCALE", 1), Str("ROWORDER", "TOP-DOWN"),
            Str("IMAGETYP", "LIGHT"), Num("EXPOSURE", exposure, "[s] Exposure duration"), Num("EXPTIME", exposure),
            Str("DATE-LOC", "2026-10-12T22:15:03.512"), Str("DATE-OBS", "2026-10-13T02:15:03.512"),
            Int("XBINNING", 1), Int("YBINNING", 1), Num("GAIN", 100), Num("OFFSET", 50),
            Num("XPIXSZ", 3.76), Num("YPIXSZ", 3.76),
            Str("INSTRUME", "ZWO ASI2600MM Pro"), Num("SET-TEMP", -10), Num("CCD-TEMP", -9.9),
            Str("TELESCOP", "RedCat 51"), Num("FOCALLEN", 250), Num("FOCRATIO", 4.9),
            Num("RA", 85.2501), Num("DEC", -2.4583), Str("OBJECT", "Horsehead Nebula"),
            Str("OBJCTRA", "05 41 00"), Str("OBJCTDEC", "-02 27 30"), Num("PIXSCALE", 3.1),
            Str("FILTER", filter), Str("CALSTAT", ""),
            Str("SWCREATE", "N.I.N.A. 3.1.2.9001 (x64)"),
        };
        if (withLocation)
        {
            cards.AddRange(new[]
            {
                Num("SITELAT", 41.2345), Num("SITELONG", -73.5432), Num("SITEELEV", 123.4),
                Str("OBSERVER", "Jane Q. Imager"), Text("COMMENT", "Taken from the back garden"),
            });
        }
        return new FitsBuilder().Primary(cards, 16, new[] { width, height });
    }

    /// <summary>
    /// A bias, dark or dark-flat frame (or a master: pass <paramref name="imageTyp"/> "Master Dark"), with the cards N.I.N.A. writes and, by default, the
    /// observer location. <paramref name="seed"/> makes the pixel data (and so the SHA-256) differ from every other frame.
    /// </summary>
    public static FitsBuilder Calibration(string imageTyp = "Dark", double exposure = 120, int seed = 1, string? instrume = "ASI2600MM",
        double? gain = 100, double? offset = 50, double? setTemp = -10, double? ccdTemp = null, int binning = 1, int width = 64, int height = 48,
        string dateObs = "2026-10-12T02:15:03.512", bool withLocation = true)
    {
        var cards = new List<string>
        {
            Str("IMAGETYP", imageTyp), Num("EXPOSURE", exposure), Num("EXPTIME", exposure), Str("DATE-OBS", dateObs),
            Int("XBINNING", binning), Int("YBINNING", binning),
        };
        if (gain is { } g) cards.Add(Num("GAIN", g));
        if (offset is { } o) cards.Add(Num("OFFSET", o));
        if (instrume is not null) cards.Add(Str("INSTRUME", instrume));
        if (setTemp is { } t) cards.Add(Num("SET-TEMP", t));
        if (ccdTemp is { } c) cards.Add(Num("CCD-TEMP", c));
        if (withLocation)
            cards.AddRange(new[] { Num("SITELAT", 41.2345), Num("SITELONG", -73.5432), Num("SITEELEV", 123.4), Str("OBSERVER", "Jane Q. Imager") });
        return new FitsBuilder().Primary(cards, 16, new[] { width, height }, Pattern(width * height * 2, seed * 7919));
    }
}

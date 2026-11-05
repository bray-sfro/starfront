// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using System.Text;

namespace RapidStackerCollab.Core.Fits;

/// <summary>Not a FITS file, or a damaged one (no END card, truncated data unit, absurd header).</summary>
public sealed class FitsFormatException : Exception
{
    public FitsFormatException(string message) : base(message) { }
}

/// <summary>
/// One 80-byte header card. <see cref="Raw"/> is kept verbatim so a card that survives a rewrite is copied
/// byte for byte; only <see cref="Keyword"/>, <see cref="Value"/> and <see cref="Comment"/> are interpreted.
/// </summary>
public sealed class FitsCard
{
    public const int Length = 80;

    private FitsCard(byte[] raw, string keyword, string? value, bool isString, string? comment)
    {
        Raw = raw;
        Keyword = keyword;
        Value = value;
        IsString = isString;
        Comment = comment;
    }

    public byte[] Raw { get; }

    /// <summary>Upper-case keyword without padding. For <c>HIERARCH</c> cards this is the long name after <c>HIERARCH </c>.</summary>
    public string Keyword { get; }

    /// <summary>The value with string quoting removed, or null for a card without a value (COMMENT, HISTORY, END, blank, CONTINUE).</summary>
    public string? Value { get; }

    /// <summary>True when the value was a quoted FITS string.</summary>
    public bool IsString { get; }

    public string? Comment { get; }

    public bool IsEnd => Keyword == "END";

    /// <summary>
    /// A card built from parts, for headers that did not come from a FITS file (XISF <c>FITSKeyword</c> elements).
    /// <see cref="Raw"/> is a best-effort 80-column rendering and must not be written back to a file.
    /// </summary>
    internal static FitsCard Synthesize(string keyword, string? value, bool isString, string? comment = null)
    {
        var text = isString
            ? $"{keyword,-8}= '{value?.Replace("'", "''")}'"
            : $"{keyword,-8}= {value}";
        text = text.Length > Length ? text[..Length] : text.PadRight(Length);
        return new FitsCard(Encoding.Latin1.GetBytes(text), keyword.ToUpperInvariant(), value, isString, comment);
    }

    public static FitsCard Parse(ReadOnlySpan<byte> card)
    {
        if (card.Length != Length) throw new ArgumentException("A FITS card is 80 bytes.", nameof(card));
        var raw = card.ToArray();
        var text = Encoding.Latin1.GetString(raw);
        var keyword = text[..8].TrimEnd().ToUpperInvariant();
        string? value = null;
        string? comment = null;
        var isString = false;

        if (keyword == "HIERARCH")
        {
            var eq = text.IndexOf('=', 9);
            if (eq > 0)
            {
                keyword = text[9..eq].Trim().ToUpperInvariant();
                ParseValue(text, eq + 1, out value, out isString, out comment);
            }
        }
        else if (text.Length >= 10 && text[8] == '=' && text[9] == ' ')
        {
            ParseValue(text, 10, out value, out isString, out comment);
        }
        return new FitsCard(raw, keyword, value, isString, comment);
    }

    private static void ParseValue(string text, int start, out string? value, out bool isString, out string? comment)
    {
        isString = false;
        comment = null;
        var i = start;
        while (i < text.Length && text[i] == ' ') i++;
        if (i >= text.Length)
        {
            value = null;
            return;
        }
        if (text[i] == '\'')
        {
            var sb = new StringBuilder();
            i++;
            while (i < text.Length)
            {
                if (text[i] == '\'')
                {
                    if (i + 1 < text.Length && text[i + 1] == '\'')
                    {
                        sb.Append('\'');
                        i += 2;
                        continue;
                    }
                    i++;
                    break;
                }
                sb.Append(text[i]);
                i++;
            }
            isString = true;
            value = sb.ToString().TrimEnd();
            var slash = text.IndexOf('/', i);
            if (slash >= 0) comment = text[(slash + 1)..].Trim();
            return;
        }
        var end = text.IndexOf('/', i);
        var raw = end >= 0 ? text[i..end] : text[i..];
        if (end >= 0) comment = text[(end + 1)..].Trim();
        raw = raw.Trim();
        value = raw.Length == 0 ? null : raw;
    }

    /// <summary>The value as a finite number, accepting Fortran <c>D</c> exponents; also tolerates a numeric string.</summary>
    public bool TryGetDouble(out double d)
    {
        d = 0;
        if (Value is null) return false;
        var v = Value.Replace('D', 'E').Replace('d', 'e');
        return double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d) && double.IsFinite(d);
    }

    /// <summary>The value as an integer; a whole-valued float such as <c>1.0</c> counts.</summary>
    public bool TryGetInt(out int i)
    {
        i = 0;
        if (!TryGetDouble(out var d)) return false;
        if (Math.Abs(d - Math.Round(d)) > 1e-9 || Math.Abs(d) > int.MaxValue) return false;
        i = (int)Math.Round(d);
        return true;
    }

    public bool TryGetLogical(out bool b)
    {
        b = false;
        if (Value is "T") { b = true; return true; }
        return Value is "F";
    }

    public override string ToString() => Encoding.Latin1.GetString(Raw).TrimEnd();
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Fits;

/// <summary>The cards of one HDU header, in file order, plus the raw header bytes they came from.</summary>
public sealed class FitsHeader
{
    public const int BlockSize = 2880;
    public const int CardsPerBlock = BlockSize / FitsCard.Length;

    /// <summary>A header larger than this many blocks (1 MiB) is not a header; guards against reading junk forever.</summary>
    public const int MaxBlocks = 364;

    private readonly Dictionary<string, FitsCard> firstByKeyword = new(StringComparer.Ordinal);

    public FitsHeader(IReadOnlyList<FitsCard> cards, int blockCount, byte[]? raw = null)
    {
        Cards = cards;
        BlockCount = blockCount;
        Raw = raw ?? Array.Empty<byte>();
        foreach (var c in cards)
            if (c.Value is not null) firstByKeyword.TryAdd(c.Keyword, c);
    }

    /// <summary>Every card up to and including END.</summary>
    public IReadOnlyList<FitsCard> Cards { get; }

    /// <summary>2880-byte blocks the header occupied in the file.</summary>
    public int BlockCount { get; }

    /// <summary>The header exactly as it was in the file (<see cref="BlockCount"/> × 2880 bytes) when read from a stream.</summary>
    public byte[] Raw { get; }

    public bool IsPrimary => Cards.Count > 0 && Cards[0].Keyword == "SIMPLE";
    public bool IsExtension => Cards.Count > 0 && Cards[0].Keyword == "XTENSION";

    /// <summary>The first card carrying a value for <paramref name="keyword"/>, or null.</summary>
    public FitsCard? Get(string keyword) => firstByKeyword.GetValueOrDefault(keyword);

    public string? GetString(string keyword) => Get(keyword)?.Value;

    public double? GetDouble(string keyword) => Get(keyword) is { } c && c.TryGetDouble(out var d) ? d : null;

    public int? GetInt(string keyword) => Get(keyword) is { } c && c.TryGetInt(out var i) ? i : null;

    /// <summary>The value as a 64-bit integer (a whole-valued float counts), or null.</summary>
    public long? GetLong(string keyword) =>
        Get(keyword) is { } c && c.TryGetDouble(out var d) && Math.Abs(d - Math.Round(d)) <= 1e-9 && Math.Abs(d) < long.MaxValue / 2.0 ? (long)Math.Round(d) : null;

    /// <summary>
    /// Reads the next header (whole 2880-byte blocks up to the one containing END) from <paramref name="s"/>,
    /// leaving the stream at the first byte of the data unit. Returns null at a clean end of file.
    /// </summary>
    public static FitsHeader? Read(Stream s)
    {
        var cards = new List<FitsCard>();
        var block = new byte[BlockSize];
        var raw = new MemoryStream();
        var blocks = 0;
        while (true)
        {
            var got = ReadFully(s, block);
            if (got == 0 && blocks == 0) return null;
            if (got != BlockSize) throw new FitsFormatException("The file ends inside a header block.");
            blocks++;
            raw.Write(block, 0, BlockSize);
            for (var i = 0; i < CardsPerBlock; i++)
            {
                var card = FitsCard.Parse(block.AsSpan(i * FitsCard.Length, FitsCard.Length));
                if (blocks == 1 && i == 0 && card.Keyword is not ("SIMPLE" or "XTENSION"))
                    throw new FitsFormatException("The first card is neither SIMPLE nor XTENSION; this is not a FITS file.");
                cards.Add(card);
                if (card.IsEnd) return new FitsHeader(cards, blocks, raw.ToArray());
            }
            if (blocks >= MaxBlocks) throw new FitsFormatException("No END card within the first 1 MiB of a header.");
        }
    }

    /// <summary>
    /// Bytes in the data unit that follows this header, before padding to a multiple of 2880:
    /// <c>|BITPIX|/8 * GCOUNT * (PCOUNT + product of NAXISn)</c>, with the random-groups convention that
    /// <c>NAXIS1 = 0</c> is excluded from the product.
    /// </summary>
    public long DataLength()
    {
        var bitpix = GetLong("BITPIX") ?? throw new FitsFormatException("BITPIX is missing.");
        var naxis = GetLong("NAXIS") ?? throw new FitsFormatException("NAXIS is missing.");
        if (naxis < 0 || naxis > 999) throw new FitsFormatException("NAXIS is out of range.");
        if (naxis == 0) return 0;
        try
        {
            long product = 1;
            for (var n = 1; n <= naxis; n++)
            {
                var len = GetLong("NAXIS" + n) ?? throw new FitsFormatException($"NAXIS{n} is missing.");
                if (len < 0) throw new FitsFormatException($"NAXIS{n} is negative.");
                if (n == 1 && len == 0 && GetCard("GROUPS")?.TryGetLogical(out var g) == true && g) continue;
                checked { product *= len; }
            }
            var pcount = Math.Max(GetLong("PCOUNT") ?? 0, 0);
            var gcount = Math.Max(GetLong("GCOUNT") ?? 1, 0);
            checked { return Math.Abs(bitpix) / 8 * gcount * (pcount + product); }
        }
        catch (OverflowException)
        {
            throw new FitsFormatException("The data unit is absurdly large.");
        }
    }

    private FitsCard? GetCard(string keyword) => Get(keyword);

    /// <summary>The data unit's length rounded up to whole blocks.</summary>
    public long PaddedDataLength()
    {
        var n = DataLength();
        return (n + BlockSize - 1) / BlockSize * BlockSize;
    }

    internal static int ReadFully(Stream s, byte[] buffer)
    {
        var total = 0;
        while (total < buffer.Length)
        {
            var n = s.Read(buffer, total, buffer.Length - total);
            if (n == 0) break;
            total += n;
        }
        return total;
    }
}

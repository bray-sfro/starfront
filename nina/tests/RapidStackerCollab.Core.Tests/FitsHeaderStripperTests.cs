// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Security.Cryptography;
using System.Text;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

public class FitsHeaderStripperTests
{
    private string dir = null!;

    [SetUp]
    public void SetUp() => dir = Directory.CreateTempSubdirectory("rsc-strip-").FullName;

    [TearDown]
    public void TearDown() => Directory.Delete(dir, true);

    private static byte[] Strip(byte[] input, StripOptions? o = null, StripResult? result = null)
    {
        using var dst = new MemoryStream();
        FitsHeaderStripper.Strip(new MemoryStream(input), dst, o);
        return dst.ToArray();
    }

    private static List<FitsHeader> Headers(byte[] fits)
    {
        var ms = new MemoryStream(fits);
        var list = new List<FitsHeader>();
        while (FitsHeader.Read(ms) is { } h)
        {
            list.Add(h);
            ms.Position += h.PaddedDataLength();
            if (ms.Position >= ms.Length) break;
        }
        return list;
    }

    private static byte[] DataUnit(byte[] fits, int hduIndex)
    {
        var ms = new MemoryStream(fits);
        for (var i = 0; ; i++)
        {
            var h = FitsHeader.Read(ms)!;
            var len = (int)h.PaddedDataLength();
            var data = new byte[len];
            ms.ReadExactly(data);
            if (i == hduIndex) return data;
        }
    }

    [Test]
    public void The_four_named_cards_are_removed_and_every_other_card_is_kept_byte_for_byte()
    {
        var input = FitsBuilder.NinaLight(withLocation: true).Build();
        var output = Strip(input);

        var before = Headers(input).Single();
        var after = Headers(output).Single();
        foreach (var gone in new[] { "SITELAT", "SITELONG", "SITEELEV", "OBSERVER" })
        {
            Assert.That(before.Get(gone), Is.Not.Null, gone + " is in the input");
            Assert.That(after.Get(gone), Is.Null, gone + " is gone");
        }
        var keptBefore = before.Cards.Where(c => !FitsHeaderStripper.ShouldStrip(c.Keyword) && c.Keyword != "COMMENT").Select(c => c.ToString()).ToList();
        var keptAfter = after.Cards.Where(c => c.Keyword != "COMMENT").Select(c => c.ToString()).ToList();
        Assert.That(keptAfter, Is.EqualTo(keptBefore));
        Assert.That(Encoding.Latin1.GetString(output), Does.Not.Contain("41.2345").And.Not.Contain("Jane Q."));
    }

    [Test]
    public void The_data_unit_is_byte_exact_and_the_header_is_whole_blocks_ending_in_one_END_with_blank_padding()
    {
        var input = FitsBuilder.NinaLight(withLocation: true, width: 100, height: 80).Build();
        var output = Strip(input);

        Assert.That(output.Length % 2880, Is.Zero);
        Assert.That(DataUnit(output, 0), Is.EqualTo(DataUnit(input, 0)));

        var h = Headers(output).Single();
        var raw = h.Raw;
        Assert.That(raw.Length, Is.EqualTo(h.BlockCount * 2880));
        var endIndex = h.Cards.Count - 1;
        Assert.That(h.Cards[endIndex].IsEnd);
        Assert.That(h.Cards.Count(c => c.IsEnd), Is.EqualTo(1));
        Assert.That(raw.Skip(h.Cards.Count * 80).All(b => b == (byte)' '), "everything after END is blank padding");
    }

    [Test]
    public void The_header_shrinks_by_a_block_when_the_removals_make_it_fit()
    {
        // 36 cards fill a block exactly (END included); 37 need two. Removing one location card brings it back to one.
        var cards = new List<string>();
        var b = new FitsBuilder();
        // Primary adds SIMPLE, BITPIX, NAXIS, NAXIS1, NAXIS2 (5) + END (1): 30 filler cards => 36; +1 SITELAT => 37.
        for (var i = 0; i < 30; i++) cards.Add(FitsBuilder.Int("FILL" + i, i));
        cards.Add(FitsBuilder.Num("SITELAT", 41.0));
        var input = b.Primary(cards).Build();
        Assert.That(Headers(input).Single().BlockCount, Is.EqualTo(2));
        var output = Strip(input);
        Assert.That(Headers(output).Single().BlockCount, Is.EqualTo(1));
        Assert.That(DataUnit(output, 0), Is.EqualTo(DataUnit(input, 0)));
    }

    [Test]
    public void A_file_without_location_cards_comes_out_identical()
    {
        var input = FitsBuilder.NinaLight(withLocation: false).Build();
        Assert.That(Strip(input), Is.EqualTo(input));
    }

    [Test]
    public void Every_HDU_of_a_multi_HDU_file_is_cleaned_and_every_data_unit_survives()
    {
        var input = FitsBuilder.NinaLight(withLocation: true)
            .Extension("IMAGE", new[] { FitsBuilder.Num("SITELAT", 41.2), FitsBuilder.Str("EXTNAME", "SECOND"), FitsBuilder.Str("OBSERVER", "Jane") }, bitpix: 16, naxis: new[] { 30, 20 })
            .Extension("BINTABLE", new[] { FitsBuilder.Num("LAT-OBS", 41.2), FitsBuilder.Num("OBSGEO-X", 1234567.0), FitsBuilder.Str("TTYPE1", "COL") }, bitpix: 8, naxis: new[] { 10, 7 }, pcount: 300)
            .Build();
        var output = Strip(input);

        var hs = Headers(output);
        Assert.That(hs, Has.Count.EqualTo(3));
        foreach (var h in hs)
            foreach (var banned in new[] { "SITELAT", "SITELONG", "SITEELEV", "OBSERVER", "LAT-OBS", "OBSGEO-X" })
                Assert.That(h.Get(banned), Is.Null, banned);
        Assert.That(hs[1].GetString("EXTNAME"), Is.EqualTo("SECOND"));
        Assert.That(hs[2].GetString("TTYPE1"), Is.EqualTo("COL"));
        for (var i = 0; i < 3; i++)
            Assert.That(DataUnit(output, i), Is.EqualTo(DataUnit(input, i)), $"HDU {i} data unit");
        Assert.That(hs.Select(h => h.PaddedDataLength()), Is.EqualTo(Headers(input).Select(h => h.PaddedDataLength())));
    }

    [TestCase("LAT-OBS")]
    [TestCase("LAT_OBS")]
    [TestCase("LATOBS")]
    [TestCase("LONG-OBS")]
    [TestCase("ALT-OBS")]
    [TestCase("OBSGEO-Z")]
    [TestCase("OBSGEO-L")]
    [TestCase("GEOLAT")]
    [TestCase("LONGITUD")]
    [TestCase("ELEVATIO")]
    [TestCase("SITENAME")]
    [TestCase("OBSERVAT")]
    [TestCase("sitelat")]
    public void The_usual_variants_are_stripped(string keyword)
    {
        var input = new FitsBuilder().Primary(new[] { FitsBuilder.Str("FILTER", "Ha"), FitsBuilder.Num(keyword.ToUpperInvariant(), 12.5) }).Build();
        var h = Headers(Strip(input)).Single();
        Assert.That(h.Cards.Any(c => FitsHeaderStripper.Normalise(c.Keyword) == FitsHeaderStripper.Normalise(keyword)), Is.False);
        Assert.That(h.GetString("FILTER"), Is.EqualTo("Ha"));
    }

    [Test]
    public void Hierarch_cards_are_matched_by_their_long_keyword()
    {
        var hier = ("HIERARCH SITE LAT = 41.2345").PadRight(80);
        var keep = ("HIERARCH ESO TEL FOCU = 12").PadRight(80);
        var input = new FitsBuilder().Primary(new[] { hier, keep }).Build();
        var text = Encoding.Latin1.GetString(Strip(input));
        Assert.That(text, Does.Not.Contain("SITE LAT"));
        Assert.That(text, Does.Contain("ESO TEL FOCU"));
    }

    [Test]
    public void A_long_stripped_string_takes_its_CONTINUE_cards_with_it()
    {
        var cards = new[]
        {
            ("OBSERVER= 'Jane Q. Public of 12 Elm &'").PadRight(80),
            ("CONTINUE  'Street, Springfield&'").PadRight(80),
            ("CONTINUE  ' USA'").PadRight(80),
            FitsBuilder.Str("FILTER", "Ha"),
        };
        var h = Headers(Strip(new FitsBuilder().Primary(cards).Build())).Single();
        Assert.That(h.Cards.Any(c => c.Keyword == "CONTINUE"), Is.False);
        Assert.That(h.Raw.Length, Is.EqualTo(2880));
        Assert.That(Encoding.Latin1.GetString(h.Raw), Does.Not.Contain("Springfield"));
        Assert.That(h.GetString("FILTER"), Is.EqualTo("Ha"));
    }

    [Test]
    public void Horizon_coordinates_go_by_default_because_alt_az_pins_the_observer_down_and_can_be_kept()
    {
        var cards = new[] { FitsBuilder.Num("CENTALT", 55.1), FitsBuilder.Num("CENTAZ", 180.2), FitsBuilder.Num("AIRMASS", 1.2), FitsBuilder.Str("FILTER", "Ha") };
        var input = new FitsBuilder().Primary(cards).Build();
        var def = Headers(Strip(input)).Single();
        Assert.That(new[] { "CENTALT", "CENTAZ", "AIRMASS" }.Select(def.Get), Is.All.Null);
        var kept = Headers(Strip(input, new StripOptions { IncludeHorizonCoordinates = false })).Single();
        Assert.That(kept.GetDouble("CENTALT"), Is.EqualTo(55.1));
    }

    [Test]
    public void The_stripped_file_still_reads_to_the_same_InitHeader()
    {
        var input = FitsBuilder.NinaLight(withLocation: true).Build();
        var a = ApiJson.Serialize(FitsHeaderReader.Read(new MemoryStream(input)));
        var b = ApiJson.Serialize(FitsHeaderReader.Read(new MemoryStream(Strip(input))));
        Assert.That(b, Is.EqualTo(a));
    }

    [Test]
    public void Stripping_is_deterministic_so_a_resumed_upload_hashes_the_same()
    {
        var path = FitsBuilder.NinaLight(withLocation: true).WriteTo(dir, "a.fits");
        FitsHeaderStripper.StripToFile(path, Path.Combine(dir, "1.tmp"));
        FitsHeaderStripper.StripToFile(path, Path.Combine(dir, "2.tmp"));
        Assert.That(SHA256.HashData(File.ReadAllBytes(Path.Combine(dir, "1.tmp"))),
            Is.EqualTo(SHA256.HashData(File.ReadAllBytes(Path.Combine(dir, "2.tmp")))));
    }

    [Test]
    public void The_original_is_never_modified_even_when_it_is_read_only()
    {
        var path = FitsBuilder.NinaLight(withLocation: true).WriteTo(dir, "orig.fits");
        var before = File.ReadAllBytes(path);
        File.SetLastWriteTimeUtc(path, new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc));
        File.SetAttributes(path, FileAttributes.ReadOnly);
        try
        {
            var result = FitsHeaderStripper.StripToFile(path, Path.Combine(dir, "out.tmp"));
            Assert.That(result.RemovedKeywords, Is.SupersetOf(new[] { "SITELAT", "SITELONG", "SITEELEV", "OBSERVER" }));
            Assert.That(File.ReadAllBytes(path), Is.EqualTo(before));
            Assert.That(File.GetLastWriteTimeUtc(path), Is.EqualTo(new DateTime(2026, 1, 2, 3, 4, 5, DateTimeKind.Utc)));
            Assert.That(new FileInfo(Path.Combine(dir, "out.tmp")).Length, Is.LessThanOrEqualTo(before.Length));
        }
        finally
        {
            File.SetAttributes(path, FileAttributes.Normal);
        }
    }

    [Test]
    public void The_destination_may_not_be_the_source_nor_an_existing_file()
    {
        var path = FitsBuilder.NinaLight().WriteTo(dir, "a.fits");
        Assert.Throws<ArgumentException>(() => FitsHeaderStripper.StripToFile(path, path));
        Assert.Throws<ArgumentException>(() => FitsHeaderStripper.StripToFile(path, Path.Combine(dir, ".", "a.fits")));
        var existing = Path.Combine(dir, "exists.tmp");
        File.WriteAllText(existing, "mine");
        Assert.Throws<IOException>(() => FitsHeaderStripper.StripToFile(path, existing));
        Assert.That(File.ReadAllText(existing), Is.EqualTo("mine"), "an existing file is left alone");
    }

    [Test]
    public void A_truncated_data_unit_fails_and_leaves_no_output()
    {
        var bytes = FitsBuilder.NinaLight(withLocation: true, width: 200, height: 200).Build();
        var path = Path.Combine(dir, "cut.fits");
        File.WriteAllBytes(path, bytes[..(bytes.Length - 20000)]);
        var dest = Path.Combine(dir, "cut.tmp");
        Assert.Throws<FitsFormatException>(() => FitsHeaderStripper.StripToFile(path, dest));
        Assert.That(File.Exists(dest), Is.False);
    }

    [Test]
    public void A_missing_final_padding_and_trailing_bytes_are_preserved_not_invented()
    {
        // Image data 6144 bytes (64x48x2) pads to 8640; drop the padding.
        var full = FitsBuilder.NinaLight(withLocation: true).Build();
        var noPad = full[..(full.Length - (8640 - 6144))];
        var output = Strip(noPad);
        var h = Headers(output).Single();
        Assert.That(output.Length, Is.EqualTo(h.Raw.Length + 6144));

        var withTail = full.Concat(Encoding.ASCII.GetBytes("TRAILER-JUNK")).ToArray();
        var outTail = Strip(withTail);
        Assert.That(Encoding.ASCII.GetString(outTail[^12..]), Is.EqualTo("TRAILER-JUNK"));
    }

    [Test]
    public void Large_data_units_stream_through_unchanged()
    {
        var big = FitsBuilder.Pattern(4_000_000 * 2, 9);
        var input = new FitsBuilder().Primary(new[] { FitsBuilder.Num("SITELAT", 1.0), FitsBuilder.Str("FILTER", "L") }, 16, new[] { 2000, 2000 }, big).Build();
        var output = Strip(input);
        Assert.That(DataUnit(output, 0), Is.EqualTo(DataUnit(input, 0)));
    }

    [Test]
    public void Non_fits_input_is_rejected()
    {
        using var dst = new MemoryStream();
        Assert.Throws<FitsFormatException>(() => FitsHeaderStripper.Strip(new MemoryStream(new byte[2880 * 2]), dst));
    }
}

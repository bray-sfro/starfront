// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text.Json;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

public class FitsHeaderReaderTests
{
    private static InitHeader Read(FitsBuilder b) => FitsHeaderReader.Read(new MemoryStream(b.Build()));

    [Test]
    public void A_nina_light_frame_yields_the_expected_allow_listed_header()
    {
        var h = Read(FitsBuilder.NinaLight("Ha", 300));
        Assert.Multiple(() =>
        {
            Assert.That(h.Filter, Is.EqualTo("Ha"));
            Assert.That(h.ImageTyp, Is.EqualTo("LIGHT"));
            Assert.That(h.ExpTime, Is.EqualTo(300));
            Assert.That(h.Gain, Is.EqualTo(100));
            Assert.That(h.Offset, Is.EqualTo(50));
            Assert.That(h.XBinning, Is.EqualTo(1));
            Assert.That(h.YBinning, Is.EqualTo(1));
            Assert.That(h.Instrume, Is.EqualTo("ZWO ASI2600MM Pro"));
            Assert.That(h.Telescop, Is.EqualTo("RedCat 51"));
            Assert.That(h.FocalLen, Is.EqualTo(250));
            Assert.That(h.XPixSz, Is.EqualTo(3.76));
            Assert.That(h.DateObs, Is.EqualTo("2026-10-13T02:15:03.512"));
            Assert.That(h.Object, Is.EqualTo("Horsehead Nebula"));
            Assert.That(h.Ra, Is.EqualTo(85.2501));
            Assert.That(h.Dec, Is.EqualTo(-2.4583));
            Assert.That(h.ObjctRa, Is.EqualTo("05 41 00"));
            Assert.That(h.ObjctDec, Is.EqualTo("-02 27 30"));
            Assert.That(h.PixScale, Is.EqualTo(3.1));
            Assert.That(h.SetTemp, Is.EqualTo(-10));
            Assert.That(h.CalStat, Is.Null, "an empty string card is no value");
            Assert.That(h.BitPix, Is.EqualTo(16));
            Assert.That(h.Naxis1, Is.EqualTo(64));
            Assert.That(h.Naxis2, Is.EqualTo(48));
        });
    }

    [Test]
    public void Location_and_observer_cards_never_reach_the_serialised_header()
    {
        var json = ApiJson.Serialize(Read(FitsBuilder.NinaLight(withLocation: true)));
        using var doc = JsonDocument.Parse(json);
        var keys = doc.RootElement.EnumerateObject().Select(p => p.Name).ToList();
        Assert.That(keys, Is.SubsetOf(InitHeader.AllowedKeys));
        foreach (var banned in new[] { "SITELAT", "SITELONG", "SITEELEV", "OBSERVER", "COMMENT" })
            Assert.That(keys, Does.Not.Contain(banned));
        Assert.That(json, Does.Not.Contain("41.2345").And.Not.Contain("Jane").And.Not.Contain("back garden"));
    }

    [Test]
    public void Quoted_strings_exponents_and_sexagesimal_coordinates_parse()
    {
        var b = new FitsBuilder().Primary(new[]
        {
            FitsBuilder.Str("OBJECT", "Barnard's Loop"),
            FitsBuilder.Card("FOCALLEN", "2.5D2".PadLeft(20)),
            FitsBuilder.Str("RA", "05 35 17.3"),
            FitsBuilder.Str("DEC", "-05 23 28"),
            FitsBuilder.Str("XBINNING", "2"),
            FitsBuilder.Num("EXPOSURE", 120),
        });
        var h = Read(b);
        Assert.That(h.Object, Is.EqualTo("Barnard's Loop"));
        Assert.That(h.FocalLen, Is.EqualTo(250));
        Assert.That(h.Ra, Is.EqualTo("05 35 17.3"));
        Assert.That(h.Dec, Is.EqualTo("-05 23 28"));
        Assert.That(h.XBinning, Is.EqualTo(2), "a numeric string is accepted");
        Assert.That(h.ExpTime, Is.EqualTo(120), "EXPOSURE stands in for a missing EXPTIME");
    }

    [Test]
    public void Long_strings_are_clamped_to_the_contract_lengths_and_bad_numbers_are_dropped()
    {
        var b = new FitsBuilder().Primary(new[]
        {
            FitsBuilder.Str("IMAGETYP", new string('x', 60)),
            FitsBuilder.Str("BAYERPAT", "RGGBRGGBRGGB"),
            FitsBuilder.Int("XBINNING", 0),
            FitsBuilder.Str("GAIN", "high"),
        });
        var h = Read(b);
        Assert.That(h.ImageTyp, Has.Length.EqualTo(40));
        Assert.That(h.BayerPat, Has.Length.EqualTo(8));
        Assert.That(h.XBinning, Is.Null);
        Assert.That(h.Gain, Is.Null);
    }

    [Test]
    public void Only_the_primary_header_is_read_and_the_stream_is_left_at_the_data_unit()
    {
        var bytes = FitsBuilder.NinaLight().Extension("BINTABLE", new[] { FitsBuilder.Str("FILTER", "ExtensionOnly") }).Build();
        using var ms = new MemoryStream(bytes);
        var h = FitsHeaderReader.Read(ms);
        Assert.That(h.Filter, Is.EqualTo("Ha"));
        Assert.That(ms.Position % 2880, Is.Zero);
    }

    [Test]
    public void Non_fits_input_is_a_format_exception()
    {
        Assert.Throws<FitsFormatException>(() => FitsHeaderReader.Read(new MemoryStream(new byte[2880])));
        Assert.Throws<FitsFormatException>(() => FitsHeaderReader.Read(new MemoryStream(Array.Empty<byte>())));
        Assert.Throws<FitsFormatException>(() => FitsHeaderReader.Read(new MemoryStream(new byte[100])));
        var noEnd = FitsBuilder.NinaLight().Build();
        // Blank out the END card: a header without END must not be read forever.
        var s = System.Text.Encoding.ASCII.GetString(noEnd);
        var i = s.IndexOf("END     ", StringComparison.Ordinal);
        var damaged = System.Text.Encoding.ASCII.GetBytes(s.Remove(i, 8).Insert(i, "NOTEND  "));
        Assert.Throws<FitsFormatException>(() => FitsHeaderReader.Read(new MemoryStream(damaged[..2880])));
    }

    [TestCase("LIGHT", FrameKind.Light)]
    [TestCase("Light Frame", FrameKind.Light)]
    [TestCase("light", FrameKind.Light)]
    [TestCase("DARK", FrameKind.Dark)]
    [TestCase("Dark Frame", FrameKind.Dark)]
    [TestCase("FLAT", FrameKind.Flat)]
    [TestCase("Flat Field", FrameKind.Flat)]
    [TestCase("BIAS", FrameKind.Bias)]
    [TestCase("Bias Frame", FrameKind.Bias)]
    [TestCase("Dark Flat", FrameKind.DarkFlat)]
    [TestCase("DARKFLAT", FrameKind.DarkFlat)]
    [TestCase("Master Bias", FrameKind.MasterBias)]
    [TestCase("SNAPSHOT", FrameKind.Other)]
    [TestCase("", FrameKind.Other)]
    [TestCase(null, FrameKind.Other)]
    public void Frame_kinds_are_classified_from_IMAGETYP(string? imagetyp, FrameKind expected) =>
        Assert.That(FrameKinds.Classify(imagetyp), Is.EqualTo(expected));

    [Test]
    public void Flats_follow_their_own_option_other_calibration_the_old_one_and_snapshots_never()
    {
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Light, false, false), Is.True);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Dark, true, false), Is.False);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Dark, false, true), Is.True);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Bias, false, true), Is.True);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.DarkFlat, false, true), Is.True);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Flat, true, false), Is.True);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.MasterFlat, true, false), Is.True, "a master flat is a flat");
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Flat, false, true), Is.False, "flats ignore the darks option");
        Assert.That(FrameKinds.ShouldUpload(FrameKind.MasterDark, true, false), Is.False);
        Assert.That(FrameKinds.ShouldUpload(FrameKind.Other, true, true), Is.False);
    }
}

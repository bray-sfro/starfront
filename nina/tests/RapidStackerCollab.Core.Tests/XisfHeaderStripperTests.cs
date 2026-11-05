// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

[TestFixture]
public class XisfHeaderStripperTests
{
    private string dir = "";

    [SetUp]
    public void SetUp() => dir = Directory.CreateTempSubdirectory("rsc-xstrip-").FullName;

    [TearDown]
    public void TearDown()
    {
        try { Directory.Delete(dir, true); } catch (IOException) { }
    }

    private static byte[] Block(int length, int seed) => FitsBuilder.Pattern(length, seed);

    private const string Template = """
        <?xml version="1.0" encoding="UTF-8"?>
        <xisf version="1.0" xmlns="http://www.pixinsight.com/xisf">
          <Image geometry="64:48:1" sampleFormat="UInt16" colorSpace="Gray" location="attachment:{0}:6144">
            <FITSKeyword name="FILTER" value="'Ha'" comment="filter"/>
            <FITSKeyword name="IMAGETYP" value="'LIGHT'" comment=""/>
            <FITSKeyword name="SITELAT" value="41.2345" comment="Observatory latitude"/>
            <FITSKeyword name="SITELONG" value="-73.5432" comment=""/>
            <FITSKeyword name="OBSERVER" value="'Jane Q. Imager'" comment=""/>
            <FITSKeyword name="OBJCTALT" value="35.2" comment="altitude > horizon"/>
            <FITSKeyword name="OBJECT" value="'Horsehead Nebula'" comment="a > b"/>
            <FITSKeyword name="EXPTIME" value="300" comment=""/>
            <Property id="Observation:Location:Latitude" type="Float64" value="41.2345"/>
            <Property id="Observation:Location:Longitude" type="Float64" value="-73.5432"/>
            <Property id="Observation:Location:Name" type="String">Jane's back garden</Property>
            <Property id="Observation:Location:Elevation" type="Float64" location="attachment:{1}:8" />
            <Property id="Observation:Time:Start" type="TimePoint" value="2026-10-13T02:15:03Z"/>
          </Image>
          <Metadata>
            <Property id="XISF:CreatorApplication" type="String">N.I.N.A.</Property>
            <Property id="Observation:Observer:Name" type="String" location="attachment:{2}:16"/>
          </Metadata>
        </xisf>
        """;

    private static byte[][] Blocks() => new[] { Block(6144, 1), new byte[] { 0x40, 0x5E, 0xD8, 0x00, 0, 0, 0, 0 }, Encoding.ASCII.GetBytes("Jane Q. Imager!!") };

    private string Write(byte[] bytes, string name = "in.xisf")
    {
        var path = Path.Combine(dir, name);
        File.WriteAllBytes(path, bytes);
        return path;
    }

    [Test]
    public void The_location_keywords_and_properties_are_removed_and_everything_else_is_kept()
    {
        var input = XisfBuilder.Build(Template, Blocks().Take(3).ToArray());
        var src = Write(input);
        var dest = Path.Combine(dir, "out.xisf");

        var result = XisfHeaderStripper.StripToFile(src, dest);

        var output = File.ReadAllBytes(dest);
        var header = XisfBuilder.Header(output);
        Assert.That(header, Does.Not.Contain("SITELAT").And.Not.Contain("SITELONG").And.Not.Contain("OBSERVER").And.Not.Contain("OBJCTALT")
            .And.Not.Contain("Observation:Location").And.Not.Contain("Observation:Observer").And.Not.Contain("Jane").And.Not.Contain("41.2345"));
        Assert.That(header, Does.Contain("FILTER").And.Contain("IMAGETYP").And.Contain("a &gt; b").Or.Contain("a > b"));
        Assert.That(header, Does.Contain("Observation:Time:Start").And.Contain("XISF:CreatorApplication").And.Contain("N.I.N.A."));
        Assert.That(result.RemovedKeywords, Is.EquivalentTo(new[]
        {
            "SITELAT", "SITELONG", "OBSERVER", "OBJCTALT", "Observation:Location:Latitude", "Observation:Location:Longitude",
            "Observation:Location:Name", "Observation:Location:Elevation", "Observation:Observer:Name",
        }));
    }

    [Test]
    public void The_header_length_and_every_block_position_stay_valid()
    {
        var input = XisfBuilder.Build(Template, Blocks());
        var dest = Path.Combine(dir, "out.xisf");
        XisfHeaderStripper.StripToFile(Write(input), dest);
        var output = File.ReadAllBytes(dest);

        Assert.That(output.Length, Is.EqualTo(input.Length), "same size: the header was padded back to its declared length");
        Assert.That(output.AsSpan(0, 16).ToArray(), Is.EqualTo(input.AsSpan(0, 16).ToArray()), "signature, declared length and reserved bytes");

        // The image block is addressed by the unchanged location attribute and holds the same bytes.
        var header = XisfBuilder.Header(output);
        var loc = System.Text.RegularExpressions.Regex.Match(header, "location=\"attachment:(\\d+):6144\"");
        Assert.That(loc.Success, Is.True);
        var pos = int.Parse(loc.Groups[1].Value);
        Assert.That(output.AsSpan(pos, 6144).ToArray(), Is.EqualTo(Blocks()[0]));

        // The padding is spaces after the root element, so the header is still well-formed XML of exactly the declared length.
        Assert.DoesNotThrow(() => System.Xml.Linq.XDocument.Parse(header));
        Assert.That(header.TrimEnd(), Does.EndWith("</xisf>"));
        Assert.That(Encoding.UTF8.GetByteCount(header), Is.EqualTo((int)BitConverter.ToUInt32(output, 8)));
    }

    [Test]
    public void Attached_blocks_of_removed_properties_are_overwritten_with_zeros_and_nothing_else_changes()
    {
        var blocks = Blocks();
        var input = XisfBuilder.Build(Template, blocks);
        var dest = Path.Combine(dir, "out.xisf");
        var result = XisfHeaderStripper.StripToFile(Write(input), dest);
        var output = File.ReadAllBytes(dest);

        Assert.That(result.ScrubbedBlocks, Is.EqualTo(2));
        Assert.That(Encoding.Latin1.GetString(output), Does.Not.Contain("Jane Q. Imager!!"));
        var firstBlock = input.AsSpan().IndexOf(blocks[0]);
        Assert.That(output.AsSpan(firstBlock, 6144).ToArray(), Is.EqualTo(blocks[0]), "the image data is untouched");
        var elevation = input.AsSpan().IndexOf(blocks[1]);
        Assert.That(output.AsSpan(elevation, 8).ToArray(), Is.EqualTo(new byte[8]));
        var observer = input.AsSpan().IndexOf(blocks[2]);
        Assert.That(output.AsSpan(observer, blocks[2].Length).ToArray(), Is.EqualTo(new byte[blocks[2].Length]));
    }

    [Test]
    public void The_metadata_read_from_the_stripped_copy_is_that_of_the_original_without_the_location()
    {
        var src = Write(XisfBuilder.Build(Template, Blocks()));
        var dest = Path.Combine(dir, "out.xisf");
        XisfHeaderStripper.StripToFile(src, dest);

        var before = XisfHeaderReader.Read(src);
        var after = XisfHeaderReader.Read(dest);
        Assert.That(ApiJson.Serialize(after), Is.EqualTo(ApiJson.Serialize(before)));
        Assert.That(after.Filter, Is.EqualTo("Ha"));
        Assert.That(after.Object, Is.EqualTo("Horsehead Nebula"));
        Assert.That(after.Naxis1, Is.EqualTo(64));
    }

    [Test]
    public void The_source_is_never_modified_and_the_output_is_deterministic()
    {
        var src = Write(XisfBuilder.Build(Template, Blocks()));
        var before = File.ReadAllBytes(src);
        var stamp = File.GetLastWriteTimeUtc(src);
        XisfHeaderStripper.StripToFile(src, Path.Combine(dir, "a.xisf"));
        XisfHeaderStripper.StripToFile(src, Path.Combine(dir, "b.xisf"));

        Assert.That(File.ReadAllBytes(src), Is.EqualTo(before));
        Assert.That(File.GetLastWriteTimeUtc(src), Is.EqualTo(stamp));
        Assert.That(File.ReadAllBytes(Path.Combine(dir, "a.xisf")), Is.EqualTo(File.ReadAllBytes(Path.Combine(dir, "b.xisf"))));
    }

    [Test]
    public void A_file_with_nothing_to_remove_is_copied_byte_for_byte()
    {
        var plain = XisfBuilder.Build("""<?xml version="1.0"?><xisf version="1.0"><Image geometry="64:48:1" sampleFormat="UInt16" location="attachment:{0}:6144"><FITSKeyword name="FILTER" value="'Ha'" comment=""/></Image></xisf>""", Block(6144, 7));
        var dest = Path.Combine(dir, "out.xisf");
        var result = XisfHeaderStripper.StripToFile(Write(plain), dest);
        Assert.That(result.Removed, Is.EqualTo(0));
        Assert.That(File.ReadAllBytes(dest), Is.EqualTo(plain));
    }

    [Test]
    public void Crlf_line_endings_a_tab_indent_and_non_ascii_text_do_not_confuse_the_cut()
    {
        var template = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\r\n<xisf version=\"1.0\">\r\n\t<Image geometry=\"8:8:1\" sampleFormat=\"UInt8\" location=\"attachment:{0}:64\">\r\n"
            + "\t\t<FITSKeyword name=\"OBJECT\" value=\"'Méssier 42 – Orion'\" comment=\"café\"/>\r\n"
            + "\t\t<FITSKeyword name=\"SITELAT\" value=\"48.85\" comment=\"Paris été\"/>\r\n"
            + "\t\t<FITSKeyword name=\"FILTER\" value=\"'L'\" comment=\"\"/>\r\n\t</Image>\r\n</xisf>";
        var input = XisfBuilder.Build(template, Block(64, 3));
        var dest = Path.Combine(dir, "out.xisf");
        XisfHeaderStripper.StripToFile(Write(input), dest);
        var header = XisfBuilder.Header(File.ReadAllBytes(dest));

        Assert.That(header, Does.Not.Contain("SITELAT").And.Not.Contain("Paris"));
        Assert.That(header, Does.Contain("Méssier 42 – Orion").And.Contain("name=\"FILTER\""));
        Assert.That(File.ReadAllBytes(dest).Length, Is.EqualTo(input.Length));
    }

    [Test]
    public void Horizon_coordinates_go_unless_the_option_is_off()
    {
        var input = XisfBuilder.Build("""<?xml version="1.0"?><xisf version="1.0"><Image geometry="8:8:1" sampleFormat="UInt8" location="attachment:{0}:64"><FITSKeyword name="AIRMASS" value="1.2" comment=""/><FITSKeyword name="CENTALT" value="55" comment=""/></Image></xisf>""", Block(64, 5));
        var on = Path.Combine(dir, "on.xisf");
        var off = Path.Combine(dir, "off.xisf");
        XisfHeaderStripper.StripToFile(Write(input), on);
        XisfHeaderStripper.StripToFile(Path.Combine(dir, "in.xisf"), off, new StripOptions { IncludeHorizonCoordinates = false });
        Assert.That(XisfBuilder.Header(File.ReadAllBytes(on)), Does.Not.Contain("AIRMASS").And.Not.Contain("CENTALT"));
        Assert.That(XisfBuilder.Header(File.ReadAllBytes(off)), Does.Contain("AIRMASS").And.Contain("CENTALT"));
    }

    [Test]
    public void It_refuses_a_non_xisf_file_a_damaged_header_and_never_overwrites()
    {
        var notXisf = Write(FitsBuilder.NinaLight().Build(), "a.fits");
        var dest = Path.Combine(dir, "out.xisf");
        Assert.Throws<FitsFormatException>(() => XisfHeaderStripper.StripToFile(notXisf, dest));
        Assert.That(File.Exists(dest), Is.False, "a failed strip leaves no output behind");

        var good = Write(XisfBuilder.Build(Template, Blocks()), "good.xisf");
        var truncated = Write(File.ReadAllBytes(good).Take(40).ToArray(), "trunc.xisf");
        Assert.Throws<FitsFormatException>(() => XisfHeaderStripper.StripToFile(truncated, dest));

        Assert.Throws<ArgumentException>(() => XisfHeaderStripper.StripToFile(good, good));
        File.WriteAllText(dest, "exists");
        Assert.Throws<IOException>(() => XisfHeaderStripper.StripToFile(good, dest));
        Assert.That(File.ReadAllText(dest), Is.EqualTo("exists"), "an existing file is never replaced");
    }

    [TestCase("Observation:Location:Latitude", true)]
    [TestCase("observation:location:name", true)]
    [TestCase("Observation:Observer:Name", true)]
    [TestCase("Observation:Time:Start", false)]
    [TestCase("Observation:Object:Name", false)]
    [TestCase("Instrument:Camera:Name", false)]
    [TestCase(null, false)]
    public void Property_ids_are_matched_by_prefix(string? id, bool strip) =>
        Assert.That(XisfHeaderStripper.ShouldStripProperty(id), Is.EqualTo(strip));
}

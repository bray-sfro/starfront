// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using NUnit.Framework;
using RapidStackerCollab.Core.Fits;

namespace RapidStackerCollab.Core.Tests;

public class XisfHeaderReaderTests
{
    private static byte[] Xisf(string imageInner, string imageAttrs = "geometry=\"64:48:1\" sampleFormat=\"UInt16\" colorSpace=\"Gray\" location=\"attachment:4096:6144\"")
    {
        var xml = $"""<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" xmlns="http://www.pixinsight.com/xisf"><Image {imageAttrs}>{imageInner}</Image><Metadata><Property id="XISF:CreatorApplication" type="String">N.I.N.A.</Property></Metadata></xisf>""";
        var body = Encoding.UTF8.GetBytes(xml);
        using var ms = new MemoryStream();
        ms.Write(Encoding.ASCII.GetBytes("XISF0100"));
        ms.Write(BitConverter.GetBytes((uint)body.Length));
        ms.Write(new byte[4]);
        ms.Write(body);
        ms.Write(new byte[8192]);
        return ms.ToArray();
    }

    [Test]
    public void FITSKeyword_elements_become_the_same_InitHeader_as_a_fits_file_would()
    {
        var bytes = Xisf("""
            <FITSKeyword name="FILTER" value="'Ha'" comment=""/>
            <FITSKeyword name="IMAGETYP" value="'LIGHT'" comment=""/>
            <FITSKeyword name="EXPTIME" value="300" comment=""/>
            <FITSKeyword name="XBINNING" value="1" comment=""/>
            <FITSKeyword name="OBJECT" value="Barnard's Loop" comment=""/>
            <FITSKeyword name="RA" value="85.25" comment=""/>
            <FITSKeyword name="SITELAT" value="41.2345" comment=""/>
            <FITSKeyword name="OBSERVER" value="'Jane'" comment=""/>
            """);
        var h = XisfHeaderReader.Read(new MemoryStream(bytes));
        Assert.Multiple(() =>
        {
            Assert.That(h.Filter, Is.EqualTo("Ha"));
            Assert.That(h.ImageTyp, Is.EqualTo("LIGHT"));
            Assert.That(h.ExpTime, Is.EqualTo(300));
            Assert.That(h.XBinning, Is.EqualTo(1));
            Assert.That(h.Object, Is.EqualTo("Barnard's Loop"));
            Assert.That(h.Ra, Is.EqualTo(85.25));
            Assert.That(h.Naxis1, Is.EqualTo(64));
            Assert.That(h.Naxis2, Is.EqualTo(48));
            Assert.That(h.BitPix, Is.EqualTo(16));
        });
        Assert.That(Api.ApiJson.Serialize(h), Does.Not.Contain("41.2345").And.Not.Contain("Jane"));
    }

    [Test]
    public void IsXisf_recognises_the_signature_and_rejects_fits()
    {
        var dir = Directory.CreateTempSubdirectory("rsc-xisf-").FullName;
        try
        {
            var x = Path.Combine(dir, "a.xisf");
            File.WriteAllBytes(x, Xisf(""));
            var f = Path.Combine(dir, "a.fits");
            File.WriteAllBytes(f, Support.FitsBuilder.NinaLight().Build());
            Assert.That(XisfHeaderReader.IsXisf(x), Is.True);
            Assert.That(XisfHeaderReader.IsXisf(f), Is.False);
            Assert.That(XisfHeaderReader.IsXisf(Path.Combine(dir, "missing")), Is.False);
            Assert.That(XisfHeaderReader.Read(x).Naxis1, Is.EqualTo(64));
        }
        finally { Directory.Delete(dir, true); }
    }

    [Test]
    public void Damaged_input_is_a_format_exception_and_dtds_are_refused()
    {
        Assert.Throws<FitsFormatException>(() => XisfHeaderReader.Read(new MemoryStream(Encoding.ASCII.GetBytes("NOTXISF000000000"))));
        var bad = Xisf("");
        bad[8] = 0xFF; bad[9] = 0xFF; bad[10] = 0xFF; bad[11] = 0x7F; // header length beyond the cap
        Assert.Throws<FitsFormatException>(() => XisfHeaderReader.Read(new MemoryStream(bad)));

        var xml = """<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><xisf><Image geometry="1:1:1">&e;</Image></xisf>""";
        var body = Encoding.UTF8.GetBytes(xml);
        using var ms = new MemoryStream();
        ms.Write(Encoding.ASCII.GetBytes("XISF0100")); ms.Write(BitConverter.GetBytes((uint)body.Length)); ms.Write(new byte[4]); ms.Write(body);
        Assert.Throws<FitsFormatException>(() => XisfHeaderReader.Read(new MemoryStream(ms.ToArray())));
    }

    [Test]
    public void A_header_without_an_Image_is_rejected()
    {
        var body = Encoding.UTF8.GetBytes("""<xisf version="1.0"></xisf>""");
        using var ms = new MemoryStream();
        ms.Write(Encoding.ASCII.GetBytes("XISF0100")); ms.Write(BitConverter.GetBytes((uint)body.Length)); ms.Write(new byte[4]); ms.Write(body);
        Assert.Throws<FitsFormatException>(() => XisfHeaderReader.Read(new MemoryStream(ms.ToArray())));
    }
}

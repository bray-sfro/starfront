// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;

namespace RapidStackerCollab.Core.Tests.Support;

/// <summary>
/// Writes a small monolithic XISF: signature, header length, the XML, zero padding to a 64-byte boundary, then the data blocks laid
/// out one after the other. <c>{0}</c>, <c>{1}</c> ... in the XML are replaced by the blocks' absolute positions, written as six
/// digits so the header's length does not depend on where the blocks land.
/// </summary>
public static class XisfBuilder
{
    public static byte[] Build(string xmlTemplate, params byte[][] blocks)
    {
        string Fill(IReadOnlyList<long> positions)
        {
            var xml = xmlTemplate;
            for (var i = 0; i < positions.Count; i++) xml = xml.Replace("{" + i + "}", positions[i].ToString("D6"));
            return xml;
        }

        var headerBytes = Encoding.UTF8.GetBytes(Fill(blocks.Select(_ => 0L).ToList())).Length;
        var start = (16 + headerBytes + 63) / 64 * 64;
        var positions = new List<long>();
        var at = (long)start;
        foreach (var b in blocks)
        {
            positions.Add(at);
            at += b.Length;
        }
        var body = Encoding.UTF8.GetBytes(Fill(positions));
        using var ms = new MemoryStream();
        ms.Write(Encoding.ASCII.GetBytes("XISF0100"));
        ms.Write(BitConverter.GetBytes((uint)body.Length));
        ms.Write(new byte[4]);
        ms.Write(body);
        ms.Write(new byte[start - 16 - body.Length]);
        foreach (var b in blocks) ms.Write(b);
        return ms.ToArray();
    }

    /// <summary>The XML header of a built file.</summary>
    public static string Header(byte[] file)
    {
        var length = (int)BitConverter.ToUInt32(file, 8);
        return Encoding.UTF8.GetString(file, 16, length);
    }
}

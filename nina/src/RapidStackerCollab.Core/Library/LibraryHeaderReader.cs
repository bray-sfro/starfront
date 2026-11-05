// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;

namespace RapidStackerCollab.Core.Library;

/// <summary>
/// The allow-listed header of a library candidate. Same as the live reader, with one addition: a frame without <c>SET-TEMP</c> takes its
/// <c>CCD-TEMP</c> as the set-point (the contract's <c>set_temp_c</c> is "<c>SET-TEMP</c>, else <c>CCD-TEMP</c>"), so a cooled camera that
/// does not write <c>SET-TEMP</c> still groups, and is filed, by temperature. Reads FITS and XISF; opens the file read-only.
/// </summary>
public static class LibraryHeaderReader
{
    public static InitHeader Read(string path)
    {
        var xisf = XisfHeaderReader.IsXisf(path);
        using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.SequentialScan);
        var cards = xisf ? XisfHeaderReader.ReadCards(s) : FitsHeaderReader.ReadCards(s);
        var header = FitsHeaderReader.FromHeader(cards);
        if (header.SetTemp is null && cards.GetDouble("CCD-TEMP") is { } ccd && ccd is > -150 and < 100)
            header.SetTemp = ccd;
        return header;
    }
}

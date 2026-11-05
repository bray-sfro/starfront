// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Fits;

/// <summary>The kinds of frame the plugin tells apart from <c>IMAGETYP</c>. The server derives its own <c>frame_type</c> too.</summary>
public enum FrameKind
{
    /// <summary>Snapshots, test shots, previews and anything unrecognised: never uploaded.</summary>
    Other,
    Light,
    Dark,
    Flat,
    Bias,
    DarkFlat,
    MasterDark,
    MasterFlat,
    MasterBias,
    MasterDarkFlat,
}

public static class FrameKinds
{
    /// <summary>Classify an <c>IMAGETYP</c> value ("LIGHT", "Light Frame", "Dark", "FLAT", "Dark Flat", "Master Bias" ...).</summary>
    public static FrameKind Classify(string? imageTyp)
    {
        if (string.IsNullOrWhiteSpace(imageTyp)) return FrameKind.Other;
        var key = new string(imageTyp.Where(char.IsLetter).ToArray()).ToLowerInvariant();
        key = key.Replace("frame", "").Replace("field", "");
        return key switch
        {
            "light" or "lights" => FrameKind.Light,
            "dark" or "darks" => FrameKind.Dark,
            "flat" or "flats" => FrameKind.Flat,
            "bias" or "offset" => FrameKind.Bias,
            "darkflat" or "flatdark" => FrameKind.DarkFlat,
            "masterdark" => FrameKind.MasterDark,
            "masterflat" => FrameKind.MasterFlat,
            "masterbias" or "masteroffset" => FrameKind.MasterBias,
            "masterdarkflat" or "masterflatdark" => FrameKind.MasterDarkFlat,
            _ => FrameKind.Other,
        };
    }

    public static bool IsCalibration(FrameKind k) => k is not (FrameKind.Light or FrameKind.Other);

    /// <summary>A flat or a master flat.</summary>
    public static bool IsFlat(FrameKind k) => k is FrameKind.Flat or FrameKind.MasterFlat;

    /// <summary>
    /// Whether a frame of this kind is queued: lights always, flats (and master flats) when <paramref name="uploadFlats"/> is on,
    /// the other calibration kinds (darks, bias, dark flats and their masters) when <paramref name="includeCalibration"/> is on,
    /// never <see cref="FrameKind.Other"/>.
    /// </summary>
    public static bool ShouldUpload(FrameKind k, bool uploadFlats, bool includeCalibration) =>
        k == FrameKind.Light || (IsFlat(k) ? uploadFlats : includeCalibration && IsCalibration(k));
}

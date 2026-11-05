// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Projects;

/// <summary>Coordinate parsing and the project-grid footprint test.</summary>
public static class SkyMath
{
    /// <summary>
    /// Parse a right ascension. Sexagesimal (<c>05 41 00</c>, <c>05:41:00.5</c>, <c>05h41m00s</c>) is hours; a plain
    /// number is degrees unless <paramref name="plainNumberIsHours"/> (the convention of <c>OBJCTRA</c>).
    /// Returns degrees in [0, 360).
    /// </summary>
    public static bool TryParseRa(object? value, bool plainNumberIsHours, out double degrees)
    {
        degrees = 0;
        switch (value)
        {
            case double d when double.IsFinite(d):
                degrees = Wrap360(plainNumberIsHours ? d * 15 : d);
                return true;
            case string s:
                if (!TryParseSexagesimal(s, out var v, out var parts)) return false;
                degrees = Wrap360(parts > 1 || plainNumberIsHours ? v * 15 : v);
                return true;
            default:
                return false;
        }
    }

    /// <summary>Parse a declination (degrees, or sexagesimal degrees). Rejects values outside [-90, 90].</summary>
    public static bool TryParseDec(object? value, out double degrees)
    {
        degrees = 0;
        double v;
        switch (value)
        {
            case double d: v = d; break;
            case string s:
                if (!TryParseSexagesimal(s, out v, out _)) return false;
                break;
            default: return false;
        }
        if (!double.IsFinite(v) || v < -90 || v > 90) return false;
        degrees = v;
        return true;
    }

    // Accepts "h m s", "h:m:s", "10h20m30.5s", "-02 27 30", "+5.5", signs on the leading part. Returns the value in the
    // first unit (hours or degrees) and how many parts there were.
    private static bool TryParseSexagesimal(string text, out double value, out int parts)
    {
        value = 0;
        parts = 0;
        var s = text.Trim();
        if (s.Length == 0) return false;
        var negative = s.StartsWith('-');
        if (negative || s.StartsWith('+')) s = s[1..];
        var tokens = s.Split(new[] { ' ', ':', 'h', 'H', 'd', 'D', 'm', 'M', 's', 'S', '°', '\'', '"', '′', '″' },
            StringSplitOptions.RemoveEmptyEntries);
        if (tokens.Length is < 1 or > 3) return false;
        double total = 0, scale = 1;
        foreach (var t in tokens)
        {
            if (!double.TryParse(t, NumberStyles.Float, CultureInfo.InvariantCulture, out var n) || !double.IsFinite(n) || n < 0) return false;
            total += n / scale;
            scale *= 60;
        }
        parts = tokens.Length;
        value = negative ? -total : total;
        return true;
    }

    public static double Wrap360(double d)
    {
        var r = d % 360.0;
        return r < 0 ? r + 360.0 : r;
    }

    /// <summary>Angular distance between two sky positions, degrees (the haversine form, sound at small separations).</summary>
    public static double SeparationDeg(double ra1Deg, double dec1Deg, double ra2Deg, double dec2Deg)
    {
        const double rad = Math.PI / 180;
        var sinDDec = Math.Sin((dec2Deg - dec1Deg) * rad / 2);
        var sinDRa = Math.Sin((ra2Deg - ra1Deg) * rad / 2);
        var h = sinDDec * sinDDec + Math.Cos(dec1Deg * rad) * Math.Cos(dec2Deg * rad) * sinDRa * sinDRa;
        return 2 * Math.Asin(Math.Sqrt(Math.Clamp(h, 0, 1))) / rad;
    }

    /// <summary>
    /// Whether (<paramref name="raDeg"/>, <paramref name="decDeg"/>) falls inside <paramref name="grid"/>, shrunk by
    /// <paramref name="marginPx"/> on every side. The grid is a TAN projection centred on its (RA, Dec) with CRPIX at the
    /// frame centre; <c>rotation_deg</c> is the position angle of image-up, east of north, so with rotation 0 north is
    /// up and east is left. This is the convention of <c>worker/crates/engine/src/project.rs</c>.
    /// </summary>
    public static bool InsideGrid(Grid grid, double raDeg, double decDeg, double marginPx = 0)
    {
        if (grid.ScaleArcsecPx <= 0 || grid.WidthPx < 1 || grid.HeightPx < 1) return false;
        if (!TryGridOffset(grid, raDeg, decDeg, out var right, out var up)) return false;
        var halfW = grid.WidthPx / 2.0 - marginPx;
        var halfH = grid.HeightPx / 2.0 - marginPx;
        return Math.Abs(right) <= halfW && Math.Abs(up) <= halfH;
    }

    /// <summary>
    /// Offset of a sky position from the grid centre in grid pixels: <paramref name="rightPx"/> along image-right,
    /// <paramref name="upPx"/> along image-up. False for positions on the far side of the sphere.
    /// </summary>
    public static bool TryGridOffset(Grid grid, double raDeg, double decDeg, out double rightPx, out double upPx)
    {
        rightPx = upPx = 0;
        var a = raDeg * Math.PI / 180;
        var d = decDeg * Math.PI / 180;
        var a0 = grid.CenterRaDeg * Math.PI / 180;
        var d0 = grid.CenterDecDeg * Math.PI / 180;
        var da = a - a0;
        var cosc = Math.Sin(d0) * Math.Sin(d) + Math.Cos(d0) * Math.Cos(d) * Math.Cos(da);
        if (cosc <= 1e-9) return false; // the far hemisphere has no TAN image
        // Standard coordinates (tangent plane), radians: xi towards east, eta towards north.
        var xi = Math.Cos(d) * Math.Sin(da) / cosc;
        var eta = (Math.Cos(d0) * Math.Sin(d) - Math.Sin(d0) * Math.Cos(d) * Math.Cos(da)) / cosc;
        var theta = grid.RotationDeg * Math.PI / 180;
        var (s, c) = (Math.Sin(theta), Math.Cos(theta));
        // Image-up is (east, north) = (sin t, cos t); image-right is (-cos t, sin t).
        var arcsecPerRad = 180 / Math.PI * 3600;
        rightPx = (-c * xi + s * eta) * arcsecPerRad / grid.ScaleArcsecPx;
        upPx = (s * xi + c * eta) * arcsecPerRad / grid.ScaleArcsecPx;
        return true;
    }
}

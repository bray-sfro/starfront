// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;

namespace RapidStackerCollab.Core.Panels;

/// <summary>Right ascension and declination as people read them. Invariant culture.</summary>
public static class Sexagesimal
{
    /// <summary>Right ascension (given in degrees) as "05h 35m 17.3s", or "05:35:17.3" with <paramref name="colon"/>.</summary>
    public static string Ra(double raDeg, int decimals = 1, bool colon = false)
    {
        var hours = ((raDeg % 360) + 360) % 360 / 15.0;
        var (a, b, c) = Split(hours, decimals, 24);
        var cs = c.ToString("00." + new string('0', decimals), CultureInfo.InvariantCulture);
        if (decimals == 0) cs = c.ToString("00", CultureInfo.InvariantCulture);
        return colon
            ? string.Create(CultureInfo.InvariantCulture, $"{a:00}:{b:00}:{cs}")
            : string.Create(CultureInfo.InvariantCulture, $"{a:00}h {b:00}m {cs}s");
    }

    /// <summary>Declination as "+41° 16' 09&quot;" (degree sign U+00B0), or "+41:16:09" with <paramref name="colon"/>.</summary>
    public static string Dec(double decDeg, int decimals = 0, bool colon = false)
    {
        var sign = decDeg < 0 ? '-' : '+';
        var (a, b, c) = Split(Math.Abs(decDeg), decimals, 91);
        var cs = decimals == 0 ? c.ToString("00", CultureInfo.InvariantCulture) : c.ToString("00." + new string('0', decimals), CultureInfo.InvariantCulture);
        // A value that rounds to zero keeps no minus sign ("-00 00 00" would be a different, unreachable position).
        if (a == 0 && b == 0 && c == 0) sign = '+';
        return colon
            ? string.Create(CultureInfo.InvariantCulture, $"{sign}{a:00}:{b:00}:{cs}")
            : string.Create(CultureInfo.InvariantCulture, $"{sign}{a:00}° {b:00}' {cs}\"");
    }

    /// <summary>Whole units, whole sub-units and fractional seconds, rounded to <paramref name="decimals"/> without a 60 carry-over error.</summary>
    private static (int, int, double) Split(double value, int decimals, int wrapAt)
    {
        var scale = Math.Pow(10, decimals);
        // Work in the smallest unit as an integer so 59.96 s rounds up into the minute, not to "60.0".
        var total = (long)Math.Round(value * 3600 * scale, MidpointRounding.AwayFromZero);
        var unit = (long)(3600 * scale);
        var whole = total / unit;
        var rem = total % unit;
        var minutes = rem / (long)(60 * scale);
        var seconds = (rem % (long)(60 * scale)) / scale;
        if (whole >= wrapAt && wrapAt == 24) whole -= 24; // 23h 59m 59.96s rounds to 0h, not 24h
        return ((int)whole, (int)minutes, seconds);
    }
}

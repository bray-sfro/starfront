// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Frames;

/// <summary>
/// Which lights a flat belongs to, decided on the PC alone (calibration library plan A5). A flat goes with the lights of the same
/// rig, the same filter and the same <em>local night</em>. The night is a date in the PC's own time zone, never a location: the
/// local date of the wall-clock time twelve hours earlier, so an evening flat and the lights after it (and a morning flat taken
/// after them) all fall on one night, and a night turns over at local noon.
/// </summary>
public static class FlatRouting
{
    /// <summary>How long after local noon a night's evening flats keep waiting for lights before the fallbacks run.</summary>
    public static readonly TimeSpan HoldGrace = TimeSpan.FromHours(1);

    /// <summary>The night (a local date) of an instant: the date of its local wall-clock time minus 12 h.</summary>
    public static DateOnly LocalNight(DateTime dateObsUtc, TimeZoneInfo tz)
    {
        var utc = DateTime.SpecifyKind(dateObsUtc, DateTimeKind.Utc);
        var local = TimeZoneInfo.ConvertTimeFromUtc(utc, tz);
        return DateOnly.FromDateTime(local.AddHours(-12));
    }

    /// <summary><c>yyyy-MM-dd</c>, the form kept in the queue.</summary>
    public static string Format(DateOnly night) => night.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

    public static DateOnly? ParseNight(string? text) =>
        DateOnly.TryParseExact(text, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out var d) ? d : null;

    /// <summary><c>DATE-OBS</c> as UTC. A value without a zone is UTC, as N.I.N.A. writes it; null when it cannot be read.</summary>
    public static DateTime? ParseDateObs(string? dateObs)
    {
        if (string.IsNullOrWhiteSpace(dateObs)) return null;
        return DateTime.TryParse(dateObs.Trim(), CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var t)
            ? DateTime.SpecifyKind(t, DateTimeKind.Utc)
            : null;
    }

    /// <summary>The local night of a frame's header, or null without a readable <c>DATE-OBS</c>.</summary>
    public static string? NightOf(InitHeader header, TimeZoneInfo tz) =>
        ParseDateObs(header.DateObs) is { } t ? Format(LocalNight(t, tz)) : null;

    /// <summary>The filter as compared: trimmed and lower-cased; a frame with no filter has the empty key, which equals only another empty key.</summary>
    public static string FilterKey(string? filter) => string.IsNullOrWhiteSpace(filter) ? "" : filter.Trim().ToLowerInvariant();

    public static bool Matches(string? flatRig, string? flatFilter, string? flatNight, string? lightRig, string? lightFilter, string? lightNight) =>
        !string.IsNullOrEmpty(flatNight) && flatNight == lightNight
        && string.Equals(flatRig, lightRig, StringComparison.Ordinal)
        && FilterKey(flatFilter) == FilterKey(lightFilter);

    /// <summary>Local noon after the flat's night, plus <see cref="HoldGrace"/>, as an instant (UTC).</summary>
    public static DateTimeOffset HoldUntil(DateTime flatUtc, TimeZoneInfo tz)
    {
        var night = LocalNight(flatUtc, tz);
        var noon = night.AddDays(1).ToDateTime(new TimeOnly(12, 0), DateTimeKind.Unspecified);
        if (tz.IsInvalidTime(noon)) noon = noon.AddHours(1);
        var utc = TimeZoneInfo.ConvertTimeToUtc(noon, tz);
        return new DateTimeOffset(DateTime.SpecifyKind(utc, DateTimeKind.Utc)) + HoldGrace;
    }
}

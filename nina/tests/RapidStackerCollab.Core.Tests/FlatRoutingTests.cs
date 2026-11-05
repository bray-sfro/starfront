// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Frames;

namespace RapidStackerCollab.Core.Tests;

[TestFixture]
public class FlatRoutingTests
{
    private static readonly TimeZoneInfo Pacific = TimeZoneInfo.CreateCustomTimeZone("utc-8", TimeSpan.FromHours(-8), "UTC-8", "UTC-8");
    private static readonly TimeZoneInfo Sydney = TimeZoneInfo.CreateCustomTimeZone("utc+10", TimeSpan.FromHours(10), "UTC+10", "UTC+10");
    private static readonly TimeZoneInfo Utc = TimeZoneInfo.Utc;

    private static DateTime At(int y, int mo, int d, int h, int mi = 0) => new(y, mo, d, h, mi, 0, DateTimeKind.Utc);

    private static string Night(DateTime utc, TimeZoneInfo tz) => FlatRouting.Format(FlatRouting.LocalNight(utc, tz));

    [Test]
    public void At_UTC_minus_8_the_night_turns_over_at_local_noon_which_is_20_UTC()
    {
        // 11:59 local on 14 Oct = 19:59 UTC is still the night of 13 Oct; 12:00 local starts the night of 14 Oct.
        Assert.That(Night(At(2026, 10, 14, 19, 59), Pacific), Is.EqualTo("2026-10-13"));
        Assert.That(Night(At(2026, 10, 14, 20, 0), Pacific), Is.EqualTo("2026-10-14"));
        // An evening flat (18:00 local, 02:00 UTC next day) and the lights after midnight share a night.
        Assert.That(Night(At(2026, 10, 14, 2, 0), Pacific), Is.EqualTo("2026-10-13"));
        Assert.That(Night(At(2026, 10, 14, 10, 0), Pacific), Is.EqualTo("2026-10-13"), "02:00 local, a light");
        Assert.That(Night(At(2026, 10, 14, 14, 30), Pacific), Is.EqualTo("2026-10-13"), "a morning flat at 06:30 local");
    }

    [Test]
    public void At_UTC_plus_10_the_noon_boundary_is_02_UTC()
    {
        Assert.That(Night(At(2026, 10, 14, 1, 59), Sydney), Is.EqualTo("2026-10-13"));
        Assert.That(Night(At(2026, 10, 14, 2, 0), Sydney), Is.EqualTo("2026-10-14"));
        Assert.That(Night(At(2026, 10, 13, 10, 0), Sydney), Is.EqualTo("2026-10-13"), "20:00 local");
        Assert.That(Night(At(2026, 10, 13, 18, 0), Sydney), Is.EqualTo("2026-10-13"), "04:00 local the next morning");
    }

    [Test]
    public void At_UTC_the_noon_boundary_is_12_UTC()
    {
        Assert.That(Night(At(2026, 10, 14, 11, 59), Utc), Is.EqualTo("2026-10-13"));
        Assert.That(Night(At(2026, 10, 14, 12, 0), Utc), Is.EqualTo("2026-10-14"));
        Assert.That(Night(At(2026, 1, 1, 0, 30), Utc), Is.EqualTo("2025-12-31"), "across the year");
    }

    [Test]
    public void A_daylight_saving_change_moves_the_boundary_with_the_wall_clock_and_never_the_night()
    {
        TimeZoneInfo tz;
        try { tz = TimeZoneInfo.FindSystemTimeZoneById("America/Los_Angeles"); }
        catch (TimeZoneNotFoundException) { Assert.Ignore("No IANA time zones on this machine."); return; }

        // 2026-11-01: clocks go back at 02:00 PDT -> 01:00 PST. Noon local that day is 20:00 UTC (PST).
        Assert.That(Night(At(2026, 11, 1, 18, 59), tz), Is.EqualTo("2026-10-31"), "10:59 PST, still the night before");
        Assert.That(Night(At(2026, 11, 1, 19, 59), tz), Is.EqualTo("2026-10-31"), "11:59 PST");
        Assert.That(Night(At(2026, 11, 1, 20, 0), tz), Is.EqualTo("2026-11-01"), "12:00 PST");
        // The night of 31 Oct holds the repeated 01:00 hour: lights before and after the change are one night.
        Assert.That(Night(At(2026, 11, 1, 8, 30), tz), Is.EqualTo("2026-10-31"), "01:30 PDT");
        Assert.That(Night(At(2026, 11, 1, 9, 30), tz), Is.EqualTo("2026-10-31"), "01:30 PST");

        // 2026-03-08: clocks go forward at 02:00 PST. Local noon is 19:00 UTC (PDT).
        Assert.That(Night(At(2026, 3, 8, 18, 59), tz), Is.EqualTo("2026-03-07"));
        Assert.That(Night(At(2026, 3, 8, 19, 0), tz), Is.EqualTo("2026-03-08"));
    }

    [Test]
    public void The_hold_ends_an_hour_after_local_noon_after_the_flats_night()
    {
        // An evening flat at 18:00 local (UTC-8) on 13 Oct: the hold ends 14 Oct 13:00 local = 21:00 UTC.
        Assert.That(FlatRouting.HoldUntil(At(2026, 10, 14, 2, 0), Pacific), Is.EqualTo(new DateTimeOffset(At(2026, 10, 14, 21, 0))));
        // A morning flat on the same night has the same hold.
        Assert.That(FlatRouting.HoldUntil(At(2026, 10, 14, 14, 30), Pacific), Is.EqualTo(new DateTimeOffset(At(2026, 10, 14, 21, 0))));
        // UTC+10: night of 13 Oct, noon 14 Oct 12:00 local = 02:00 UTC, +1 h.
        Assert.That(FlatRouting.HoldUntil(At(2026, 10, 13, 9, 0), Sydney), Is.EqualTo(new DateTimeOffset(At(2026, 10, 14, 3, 0))));
        Assert.That(FlatRouting.HoldUntil(At(2026, 10, 13, 20, 0), Utc), Is.EqualTo(new DateTimeOffset(At(2026, 10, 14, 13, 0))));
        // Always after the flat itself.
        Assert.That(FlatRouting.HoldUntil(At(2026, 10, 14, 11, 59), Utc).UtcDateTime, Is.GreaterThan(At(2026, 10, 14, 11, 59)));
    }

    [Test]
    public void The_hold_uses_the_zones_offset_on_that_date()
    {
        TimeZoneInfo tz;
        try { tz = TimeZoneInfo.FindSystemTimeZoneById("America/Los_Angeles"); }
        catch (TimeZoneNotFoundException) { Assert.Ignore("No IANA time zones on this machine."); return; }
        // Night of 31 Oct 2026 (PDT -> PST on 1 Nov): the next noon is 12:00 PST on 1 Nov = 20:00 UTC, hold 21:00 UTC.
        Assert.That(FlatRouting.HoldUntil(At(2026, 11, 1, 3, 0), tz), Is.EqualTo(new DateTimeOffset(At(2026, 11, 1, 21, 0))));
    }

    [Test]
    public void DATE_OBS_without_a_zone_is_UTC_and_junk_is_no_night()
    {
        Assert.That(FlatRouting.ParseDateObs("2026-10-13T02:15:03.512"), Is.EqualTo(new DateTime(2026, 10, 13, 2, 15, 3, 512, DateTimeKind.Utc)));
        Assert.That(FlatRouting.ParseDateObs("2026-10-13T02:15:03Z")!.Value.Kind, Is.EqualTo(DateTimeKind.Utc));
        Assert.That(FlatRouting.ParseDateObs("not a date"), Is.Null);
        Assert.That(FlatRouting.ParseDateObs(null), Is.Null);
        Assert.That(FlatRouting.NightOf(new InitHeader { DateObs = "2026-10-13T02:15:03" }, Pacific), Is.EqualTo("2026-10-12"));
        Assert.That(FlatRouting.NightOf(new InitHeader(), Pacific), Is.Null);
    }

    [Test]
    public void A_flat_matches_a_light_by_rig_filter_and_night_only()
    {
        Assert.That(FlatRouting.Matches("rig_a", "Ha", "2026-10-13", "rig_a", " ha ", "2026-10-13"), Is.True, "trimmed, ignoring case");
        Assert.That(FlatRouting.Matches("rig_a", "Ha", "2026-10-13", "rig_b", "Ha", "2026-10-13"), Is.False);
        Assert.That(FlatRouting.Matches("rig_a", "Ha", "2026-10-13", "rig_a", "OIII", "2026-10-13"), Is.False);
        Assert.That(FlatRouting.Matches("rig_a", "Ha", "2026-10-13", "rig_a", "Ha", "2026-10-14"), Is.False);
        Assert.That(FlatRouting.Matches(null, null, "2026-10-13", null, "  ", "2026-10-13"), Is.True, "no filter equals no filter, no rig equals no rig");
        Assert.That(FlatRouting.Matches("rig_a", null, "2026-10-13", "rig_a", "Ha", "2026-10-13"), Is.False);
        Assert.That(FlatRouting.Matches("rig_a", "Ha", null, "rig_a", "Ha", null), Is.False, "no night never matches");
    }
}

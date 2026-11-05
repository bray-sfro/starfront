// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Upload;

namespace RapidStackerCollab.Core.Tests;

public class UploadPolicyTests
{
    private static readonly TimeSpan Utc4 = TimeSpan.FromHours(-4);

    private static DateTimeOffset At(int h, int m = 0, int day = 13) => new(2026, 10, day, h, m, 0, Utc4);

    private static UploadPolicy PolicyAt(DateTimeOffset now) => new(new FakeClock(now));

    [Test]
    public void Immediately_always_allows()
    {
        var p = PolicyAt(At(23));
        var busy = new ImagingState(ExposureInProgress: true, SequenceRunning: true);
        Assert.That(p.Decide(new UploadPolicySettings(UploadMode.Immediately), busy).Allow, Is.True);
    }

    [Test]
    public void The_pause_switch_beats_every_mode_and_even_upload_now()
    {
        var p = PolicyAt(At(10));
        foreach (var mode in Enum.GetValues<UploadMode>())
        {
            var d = p.Decide(new UploadPolicySettings(mode, Paused: true), new ImagingState(UploadNowRequested: true));
            Assert.That(d.Allow, Is.False, mode.ToString());
            Assert.That(d.Reason, Does.Contain("paused"));
        }
    }

    [Test]
    public void Upload_now_overrides_the_mode_but_not_the_pause()
    {
        var p = PolicyAt(At(23)); // outside 06-14
        var s = new UploadPolicySettings(UploadMode.TimeWindow);
        Assert.That(p.Decide(s, new ImagingState()).Allow, Is.False);
        Assert.That(p.Decide(s, new ImagingState(UploadNowRequested: true)).Allow, Is.True);
        var seq = new UploadPolicySettings(UploadMode.AfterSequenceEnd);
        Assert.That(p.Decide(seq, new ImagingState(SequenceRunning: true, UploadNowRequested: true)).Allow, Is.True);
    }

    [Test]
    public void When_idle_waits_for_the_exposure_and_then_for_the_grace_period()
    {
        var saved = At(22, 0);
        var s = new UploadPolicySettings(UploadMode.WhenIdle, IdleGrace: TimeSpan.FromSeconds(30));

        var exposing = PolicyAt(At(22, 0)).Decide(s, new ImagingState(ExposureInProgress: true));
        Assert.That(exposing.Allow, Is.False);
        Assert.That(exposing.RetryAt, Is.Null, "the state change is the trigger");

        var clock = new FakeClock(saved.AddSeconds(10));
        var p = new UploadPolicy(clock);
        var graceful = p.Decide(s, new ImagingState(LastImageSavedAt: saved));
        Assert.That(graceful.Allow, Is.False);
        Assert.That(graceful.RetryAt, Is.EqualTo(saved.AddSeconds(30)));

        clock.Advance(TimeSpan.FromSeconds(25));
        Assert.That(p.Decide(s, new ImagingState(LastImageSavedAt: saved)).Allow, Is.True);

        Assert.That(PolicyAt(At(22)).Decide(new UploadPolicySettings(UploadMode.WhenIdle), new ImagingState(LastImageSavedAt: At(22))).Allow, Is.True, "no grace configured");
    }

    [Test]
    public void After_sequence_end_needs_a_finished_sequence_and_no_images_saved_since()
    {
        var s = new UploadPolicySettings(UploadMode.AfterSequenceEnd);
        var p = PolicyAt(At(5));

        Assert.That(p.Decide(s, new ImagingState(SequenceRunning: true, LastSequenceEndedAt: At(1))).Allow, Is.False);
        Assert.That(p.Decide(s, new ImagingState(ExposureInProgress: true, LastSequenceEndedAt: At(1))).Allow, Is.False);
        Assert.That(p.Decide(s, new ImagingState()).Reason, Does.Contain("sequence to end"));
        Assert.That(p.Decide(s, new ImagingState(LastSequenceEndedAt: At(4), LastImageSavedAt: At(4, 5))).Allow, Is.False,
            "a manual image after the sequence ended waits for the next sequence");
        Assert.That(p.Decide(s, new ImagingState(LastSequenceEndedAt: At(4), LastImageSavedAt: At(3, 59))).Allow, Is.True);
        Assert.That(p.Decide(s, new ImagingState(LastSequenceEndedAt: At(4))).Allow, Is.True);
    }

    [TestCase(5, 59, false)]
    [TestCase(6, 0, true)]
    [TestCase(10, 30, true)]
    [TestCase(13, 59, true)]
    [TestCase(14, 0, false)]
    [TestCase(23, 0, false)]
    public void A_daytime_window_is_start_inclusive_and_end_exclusive(int h, int m, bool expected)
    {
        var s = new UploadPolicySettings(UploadMode.TimeWindow, new TimeOnly(6, 0), new TimeOnly(14, 0));
        Assert.That(PolicyAt(At(h, m)).Decide(s, new ImagingState()).Allow, Is.EqualTo(expected));
    }

    [TestCase(21, 59, false)]
    [TestCase(22, 0, true)]
    [TestCase(23, 59, true)]
    [TestCase(0, 0, true)]
    [TestCase(5, 59, true)]
    [TestCase(6, 0, false)]
    [TestCase(12, 0, false)]
    public void A_window_that_crosses_midnight_works(int h, int m, bool expected)
    {
        var s = new UploadPolicySettings(UploadMode.TimeWindow, new TimeOnly(22, 0), new TimeOnly(6, 0));
        Assert.That(PolicyAt(At(h, m)).Decide(s, new ImagingState()).Allow, Is.EqualTo(expected));
    }

    [Test]
    public void Outside_the_window_the_decision_says_when_it_opens()
    {
        var s = new UploadPolicySettings(UploadMode.TimeWindow, new TimeOnly(6, 0), new TimeOnly(14, 0));
        Assert.That(PolicyAt(At(3)).Decide(s, new ImagingState()).RetryAt, Is.EqualTo(At(6)), "later today");
        Assert.That(PolicyAt(At(15)).Decide(s, new ImagingState()).RetryAt, Is.EqualTo(At(6, 0, day: 14)), "tomorrow");
        var night = new UploadPolicySettings(UploadMode.TimeWindow, new TimeOnly(22, 0), new TimeOnly(6, 0));
        Assert.That(PolicyAt(At(12)).Decide(night, new ImagingState()).RetryAt, Is.EqualTo(At(22)));
    }

    [Test]
    public void Equal_start_and_end_means_all_day_and_the_default_window_is_six_to_fourteen()
    {
        var all = new UploadPolicySettings(UploadMode.TimeWindow, new TimeOnly(0, 0), new TimeOnly(0, 0));
        Assert.That(PolicyAt(At(3)).Decide(all, new ImagingState()).Allow, Is.True);
        var def = new UploadPolicySettings(UploadMode.TimeWindow);
        Assert.That((def.Start, def.End), Is.EqualTo((new TimeOnly(6, 0), new TimeOnly(14, 0))));
    }

    [Test]
    public void The_window_uses_the_wall_clock_of_the_clock_offset()
    {
        // 03:00 in UTC-4 is 07:00 UTC; the window is wall-clock local time, so it is still closed.
        var s = new UploadPolicySettings(UploadMode.TimeWindow, new TimeOnly(6, 0), new TimeOnly(14, 0));
        var utc = new DateTimeOffset(2026, 10, 13, 7, 0, 0, TimeSpan.Zero);
        Assert.That(PolicyAt(utc).Decide(s, new ImagingState()).Allow, Is.True, "07:00 on the clock's own wall time");
        Assert.That(PolicyAt(utc.ToOffset(Utc4)).Decide(s, new ImagingState()).Allow, Is.False, "the same instant read as 03:00 local");
    }
}

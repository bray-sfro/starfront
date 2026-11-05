// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Security.Cryptography;
using NUnit.Framework;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Upload;
using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Tests;

public class BackoffTests
{
    [Test]
    public void Delays_grow_geometrically_and_are_capped()
    {
        var b = new Backoff { Base = TimeSpan.FromSeconds(1), Factor = 2, Max = TimeSpan.FromSeconds(30), Jitter = 0 };
        Assert.That(Enumerable.Range(1, 8).Select(a => b.Delay(a).TotalSeconds), Is.EqualTo(new[] { 1.0, 2, 4, 8, 16, 30, 30, 30 }));
    }

    [Test]
    public void Jitter_spreads_the_delay_over_the_upper_part_of_the_nominal_value()
    {
        var b = new Backoff { Base = TimeSpan.FromSeconds(10), Jitter = 0.5 };
        Assert.That(b.Delay(1, () => 0.0).TotalSeconds, Is.EqualTo(5).Within(1e-9));
        Assert.That(b.Delay(1, () => 0.999999).TotalSeconds, Is.EqualTo(10).Within(1e-3));
        var seen = Enumerable.Range(0, 200).Select(_ => b.Delay(1).TotalSeconds).ToList();
        Assert.That(seen.Min(), Is.GreaterThanOrEqualTo(5));
        Assert.That(seen.Max(), Is.LessThanOrEqualTo(10));
        Assert.That(seen.Distinct().Count(), Is.GreaterThan(50), "actually random");
    }

    [Test]
    public void Absurd_attempt_numbers_do_not_overflow()
    {
        var b = new Backoff { Jitter = 0 };
        Assert.That(b.Delay(10_000), Is.EqualTo(b.Max));
        Assert.That(b.Delay(-5), Is.EqualTo(b.Base));
    }
}

public class BandwidthLimiterTests
{
    [Test]
    public async Task Sustained_traffic_is_paced_to_the_cap_after_the_burst()
    {
        var clock = new FakeClock();
        var start = clock.Now;
        var limiter = new BandwidthLimiter(clock, kilobytesPerSecond: 100);
        for (var i = 0; i < 20; i++) await limiter.AcquireAsync(50_000); // 1,000,000 bytes at 100,000 B/s
        var elapsed = clock.Now - start;
        Assert.That(elapsed.TotalSeconds, Is.EqualTo(10 - 0.25).Within(0.01));
    }

    [Test]
    public async Task No_cap_never_waits()
    {
        var clock = new FakeClock();
        var limiter = new BandwidthLimiter(clock, 0);
        for (var i = 0; i < 100; i++) await limiter.AcquireAsync(10_000_000);
        Assert.That(clock.Delays, Is.Empty);
    }

    [Test]
    public async Task A_small_request_inside_the_burst_is_not_delayed()
    {
        var clock = new FakeClock();
        var limiter = new BandwidthLimiter(clock, 100);
        await limiter.AcquireAsync(20_000); // 0.2 s worth
        Assert.That(clock.Delays, Is.Empty);
    }

    [Test]
    public async Task Parallel_callers_share_one_budget()
    {
        var clock = new FakeClock();
        var start = clock.Now;
        var limiter = new BandwidthLimiter(clock, 200);
        await Task.WhenAll(Enumerable.Range(0, 4).Select(async _ =>
        {
            for (var i = 0; i < 10; i++) await limiter.AcquireAsync(50_000);
        }));
        // 2,000,000 bytes at 200,000 B/s.
        Assert.That((clock.Now - start).TotalSeconds, Is.EqualTo(10 - 0.25).Within(0.05));
    }

    [Test]
    public async Task The_cap_can_change_while_running()
    {
        var clock = new FakeClock();
        var limiter = new BandwidthLimiter(clock, 100);
        Assert.That(limiter.LimitKilobytesPerSecond, Is.EqualTo(100));
        limiter.SetLimit(0);
        await limiter.AcquireAsync(100_000_000);
        Assert.That(clock.Delays, Is.Empty);
        limiter.SetLimit(1000);
        await limiter.AcquireAsync(1_000_000);
        await limiter.AcquireAsync(1_000_000);
        Assert.That(clock.Delays, Is.Not.Empty);
    }

    [Test]
    public void Cancellation_is_honoured()
    {
        var limiter = new BandwidthLimiter(new FakeClock(), 10);
        using var cts = new CancellationTokenSource();
        cts.Cancel();
        Assert.CatchAsync<OperationCanceledException>(() => limiter.AcquireAsync(1000, cts.Token));
    }
}

public class FileDigestTests
{
    [Test]
    public async Task Digest_matches_sha256_and_reports_progress()
    {
        var dir = Directory.CreateTempSubdirectory("rsc-hash-").FullName;
        try
        {
            var bytes = Support.FitsBuilder.Pattern(3_000_000, 3);
            var path = Path.Combine(dir, "f.bin");
            await File.WriteAllBytesAsync(path, bytes);
            long last = 0;
            var d = await FileDigest.ComputeAsync(path, new Progress<long>(v => last = Math.Max(last, v)));
            Assert.That(d.Sha256, Is.EqualTo(Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant()));
            Assert.That(d.Size, Is.EqualTo(3_000_000));
            await Task.Delay(50);
            Assert.That(last, Is.GreaterThan(0));
            Assert.That(d.Sha256, Does.Match("^[0-9a-f]{64}$"));
        }
        finally { TempDirs.Delete(dir); }
    }
}

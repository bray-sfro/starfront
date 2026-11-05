// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Upload;

/// <summary>
/// One shared upload speed cap for all parallel parts (plan §8.1: "bandwidth cap (KB/s)"). Callers
/// <see cref="AcquireAsync"/> before sending each chunk and are delayed so the long-run rate stays at the cap; a
/// quarter-second burst is allowed so small requests are not slowed. The cap can be changed or removed while uploads run.
/// 1 KB is 1000 bytes, as network speeds are quoted.
/// </summary>
public sealed class BandwidthLimiter
{
    private static readonly TimeSpan Burst = TimeSpan.FromMilliseconds(250);

    private readonly IClock clock;
    private readonly object gate = new();
    private double bytesPerSecond;
    private DateTimeOffset nextFree;

    public BandwidthLimiter(IClock clock, int kilobytesPerSecond = 0)
    {
        this.clock = clock;
        nextFree = clock.Now;
        SetLimit(kilobytesPerSecond);
    }

    /// <summary>The cap in KB/s, or 0 for unlimited.</summary>
    public int LimitKilobytesPerSecond
    {
        get { lock (gate) return (int)Math.Round(bytesPerSecond / 1000); }
    }

    /// <summary>Set the cap in KB/s; 0 or negative removes it.</summary>
    public void SetLimit(int kilobytesPerSecond)
    {
        lock (gate)
        {
            bytesPerSecond = kilobytesPerSecond > 0 ? kilobytesPerSecond * 1000.0 : 0;
            nextFree = clock.Now;
        }
    }

    public async Task AcquireAsync(int bytes, CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();
        TimeSpan wait;
        lock (gate)
        {
            if (bytesPerSecond <= 0 || bytes <= 0) return;
            var now = clock.Now;
            if (nextFree < now) nextFree = now;
            nextFree += TimeSpan.FromSeconds(bytes / bytesPerSecond);
            wait = nextFree - now - Burst;
        }
        if (wait > TimeSpan.Zero)
            await clock.DelayAsync(wait, ct).ConfigureAwait(false);
    }
}

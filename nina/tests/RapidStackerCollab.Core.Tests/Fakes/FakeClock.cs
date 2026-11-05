// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Tests.Fakes;

/// <summary>Virtual time: <see cref="DelayAsync"/> advances <see cref="Now"/> and returns at once, recording what was asked.</summary>
public sealed class FakeClock : IClock
{
    private readonly object gate = new();
    private DateTimeOffset now;

    public FakeClock(DateTimeOffset? start = null) =>
        now = start ?? new DateTimeOffset(2026, 10, 13, 22, 0, 0, TimeSpan.FromHours(-4));

    public DateTimeOffset Now
    {
        get { lock (gate) return now; }
        set { lock (gate) now = value; }
    }

    public List<TimeSpan> Delays { get; } = new();

    /// <summary>Called after each recorded delay; a test can cancel a token or flip server state from here.</summary>
    public Action<int>? OnDelay { get; set; }

    public void Advance(TimeSpan by)
    {
        lock (gate) now += by;
    }

    public Task DelayAsync(TimeSpan delay, CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();
        int n;
        lock (gate)
        {
            Delays.Add(delay);
            n = Delays.Count;
            if (delay > TimeSpan.Zero) now += delay;
        }
        OnDelay?.Invoke(n);
        ct.ThrowIfCancellationRequested();
        return Task.CompletedTask;
    }
}

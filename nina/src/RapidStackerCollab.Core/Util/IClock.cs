// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Util;

/// <summary>
/// Wall-clock time and waiting in one seam, so every timed behaviour (device polling, back-off, policy windows,
/// the bandwidth cap) runs instantly under a fake clock in tests.
/// </summary>
public interface IClock
{
    /// <summary>The current local time. The offset matters: time-window policies use the wall-clock time of day.</summary>
    DateTimeOffset Now { get; }

    Task DelayAsync(TimeSpan delay, CancellationToken ct = default);
}

public sealed class SystemClock : IClock
{
    public static readonly SystemClock Instance = new();

    public DateTimeOffset Now => DateTimeOffset.Now;

    public Task DelayAsync(TimeSpan delay, CancellationToken ct = default) =>
        delay <= TimeSpan.Zero ? Task.CompletedTask : Task.Delay(delay, ct);
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Util;

/// <summary>
/// Exponential back-off with jitter: attempt 1 waits about <see cref="Base"/>, each later attempt <see cref="Factor"/> times
/// longer, capped at <see cref="Max"/>, and the result is spread over the upper <see cref="Jitter"/> fraction of that
/// delay so many clients (or four parallel parts) do not retry in lock step.
/// </summary>
public sealed class Backoff
{
    public TimeSpan Base { get; init; } = TimeSpan.FromSeconds(1);
    public double Factor { get; init; } = 2;
    public TimeSpan Max { get; init; } = TimeSpan.FromSeconds(60);

    /// <summary>0 = no jitter, 0.5 = between 50 % and 100 % of the nominal delay, 1 = anywhere between 0 and the nominal delay.</summary>
    public double Jitter { get; init; } = 0.5;

    /// <summary>The delay before retry number <paramref name="attempt"/> (1-based). <paramref name="random"/> returns [0,1).</summary>
    public TimeSpan Delay(int attempt, Func<double>? random = null)
    {
        if (attempt < 1) attempt = 1;
        var nominal = Math.Min(Max.TotalSeconds, Base.TotalSeconds * Math.Pow(Factor, attempt - 1));
        if (double.IsNaN(nominal) || nominal < 0) nominal = Max.TotalSeconds;
        var r = (random ?? Random.Shared.NextDouble)();
        var jitter = Math.Clamp(Jitter, 0, 1);
        var seconds = nominal * (1 - jitter + jitter * r);
        return TimeSpan.FromSeconds(seconds);
    }
}

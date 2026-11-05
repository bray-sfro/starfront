// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Upload;

/// <summary>When uploads may run (plan §8.1).</summary>
public enum UploadMode
{
    /// <summary>As soon as a frame is queued.</summary>
    Immediately,
    /// <summary>Whenever no exposure is in progress (and, if set, a grace period has passed since the last image was saved).</summary>
    WhenIdle,
    /// <summary>Only after a sequence has finished and nothing is running.</summary>
    AfterSequenceEnd,
    /// <summary>Only inside a daily window of local wall-clock time, for example 06:00 to 14:00.</summary>
    TimeWindow,
}

/// <param name="Mode">The user's choice.</param>
/// <param name="WindowStart">Local time the window opens (inclusive). Used by <see cref="UploadMode.TimeWindow"/>.</param>
/// <param name="WindowEnd">Local time the window closes (exclusive). Earlier than the start means the window crosses midnight; equal means all day.</param>
/// <param name="IdleGrace">For <see cref="UploadMode.WhenIdle"/>: how long after the last saved image to wait, so a download is not competing with the next exposure's setup.</param>
/// <param name="Paused">The user's pause switch. Beats every mode, including "upload now".</param>
public sealed record UploadPolicySettings(
    UploadMode Mode = UploadMode.WhenIdle,
    TimeOnly? WindowStart = null,
    TimeOnly? WindowEnd = null,
    TimeSpan IdleGrace = default,
    bool Paused = false)
{
    public TimeOnly Start => WindowStart ?? new TimeOnly(6, 0);
    public TimeOnly End => WindowEnd ?? new TimeOnly(14, 0);
}

/// <summary>What N.I.N.A. is doing, as the shell reports it. The shell fills this from the camera/imaging state and the sequencer.</summary>
/// <param name="ExposureInProgress">The camera is exposing or downloading.</param>
/// <param name="SequenceRunning">A sequence is executing.</param>
/// <param name="LastSequenceEndedAt">When the last sequence finished, if one has since N.I.N.A. started.</param>
/// <param name="LastImageSavedAt">When the last image was saved.</param>
/// <param name="UploadNowRequested">The user ran "Collab: Upload Pending Now" (or pressed the button): overrides the mode, not the pause.</param>
public sealed record ImagingState(
    bool ExposureInProgress = false,
    bool SequenceRunning = false,
    DateTimeOffset? LastSequenceEndedAt = null,
    DateTimeOffset? LastImageSavedAt = null,
    bool UploadNowRequested = false);

/// <param name="Allow">Whether a new upload may start now.</param>
/// <param name="Reason">Short text for the dock panel ("Waiting for the sequence to end").</param>
/// <param name="RetryAt">When it is worth asking again when that is knowable (window opens, grace ends); otherwise null (ask again when the state changes).</param>
public sealed record PolicyDecision(bool Allow, string Reason, DateTimeOffset? RetryAt = null);

/// <summary>
/// The decision of whether the runner may start another upload. Pure: the same settings, state and clock give the same
/// answer, and nothing is read from N.I.N.A. here. An upload already in flight is never interrupted by a policy change;
/// the answer applies to starting the next file.
/// </summary>
public sealed class UploadPolicy
{
    private readonly IClock clock;

    public UploadPolicy(IClock clock) => this.clock = clock;

    public PolicyDecision Decide(UploadPolicySettings settings, ImagingState state)
    {
        if (settings.Paused) return new PolicyDecision(false, "Uploads are paused");
        if (state.UploadNowRequested) return new PolicyDecision(true, "Upload requested");

        var now = clock.Now;
        switch (settings.Mode)
        {
            case UploadMode.Immediately:
                return new PolicyDecision(true, "Uploading immediately");

            case UploadMode.WhenIdle:
            {
                if (state.ExposureInProgress) return new PolicyDecision(false, "Waiting for the exposure to finish");
                if (settings.IdleGrace > TimeSpan.Zero && state.LastImageSavedAt is { } saved)
                {
                    var readyAt = saved + settings.IdleGrace;
                    if (now < readyAt) return new PolicyDecision(false, "Waiting for the camera to be idle", readyAt);
                }
                return new PolicyDecision(true, "Camera idle");
            }

            case UploadMode.AfterSequenceEnd:
            {
                if (state.SequenceRunning) return new PolicyDecision(false, "Waiting for the sequence to end");
                if (state.ExposureInProgress) return new PolicyDecision(false, "Waiting for the exposure to finish");
                if (state.LastSequenceEndedAt is not { } ended) return new PolicyDecision(false, "Waiting for a sequence to end");
                if (state.LastImageSavedAt is { } last && last > ended)
                    return new PolicyDecision(false, "Images were saved after the last sequence ended; waiting for the next one");
                return new PolicyDecision(true, "Sequence ended");
            }

            case UploadMode.TimeWindow:
            {
                var t = TimeOnly.FromTimeSpan(now.TimeOfDay);
                var (start, end) = (settings.Start, settings.End);
                var inside = start == end || (start < end ? t >= start && t < end : t >= start || t < end);
                if (inside) return new PolicyDecision(true, $"Inside the upload window ({start:HH\\:mm}-{end:HH\\:mm})");
                var opensToday = new DateTimeOffset(now.Date.Add(start.ToTimeSpan()), now.Offset);
                var opens = opensToday > now ? opensToday : opensToday.AddDays(1);
                return new PolicyDecision(false, $"Outside the upload window ({start:HH\\:mm}-{end:HH\\:mm})", opens);
            }

            default:
                return new PolicyDecision(false, "Unknown upload mode");
        }
    }
}

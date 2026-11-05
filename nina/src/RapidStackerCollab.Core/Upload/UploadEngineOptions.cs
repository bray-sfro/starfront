// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Upload;

public sealed class UploadEngineOptions
{
    /// <summary>Plan §8.1: 4 parts in flight.</summary>
    public int ParallelParts { get; init; } = 4;

    /// <summary>Tries per part in one pass before giving up on the pass (the item is retried later and resumes).</summary>
    public int MaxPartAttempts { get; init; } = 5;

    /// <summary>Tries for each of <c>init</c> / <c>complete</c> on a transient failure (no response, 5xx, garbled body).</summary>
    public int MaxApiAttempts { get; init; } = 4;

    /// <summary>
    /// How many times one <see cref="UploadEngine.UploadAsync"/> call re-runs <c>init</c> to resume: after an expired
    /// presigned URL (S3 403), <c>upload_incomplete</c> or <c>size_mismatch</c>.
    /// </summary>
    public int MaxResumes { get; init; } = 3;

    public Backoff PartBackoff { get; init; } = new() { Base = TimeSpan.FromSeconds(2), Max = TimeSpan.FromSeconds(60) };
    public Backoff ApiBackoff { get; init; } = new() { Base = TimeSpan.FromSeconds(2), Max = TimeSpan.FromSeconds(60) };

    /// <summary>One part PUT may take this long. A 16 MiB part at 100 KB/s is under three minutes.</summary>
    public TimeSpan PartTimeout { get; init; } = TimeSpan.FromMinutes(10);

    /// <summary><c>Retry-After</c> to assume for <c>rate_limited</c> / <c>ingest_paused</c> when the server gave none.</summary>
    public TimeSpan DefaultRateLimitRetry { get; init; } = TimeSpan.FromSeconds(60);
    public TimeSpan DefaultIngestPausedRetry { get; init; } = TimeSpan.FromMinutes(5);
}

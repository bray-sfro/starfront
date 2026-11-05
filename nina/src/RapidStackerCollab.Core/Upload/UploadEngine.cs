// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Collections.Concurrent;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Upload;

/// <summary>
/// Uploads one file to one project following "Upload flow" in docs/api/README.md: hash, <c>init</c>, PUT the parts the
/// server asks for straight to S3 (server-chosen part size, 4 in parallel, each retried with jitter), <c>complete</c>.
/// <list type="bullet">
/// <item><b>Resume.</b> Calling <c>init</c> again with the same hash returns the same frame and only the outstanding
/// parts, so a crash, a cancel, an expired presigned URL or <c>upload_incomplete</c> all recover the same way.</item>
/// <item><b>Outcomes, not exceptions.</b> Everything the server can say is mapped to an <see cref="UploadOutcome"/>
/// (<c>duplicate</c> is done; <c>quota_exceeded</c> blocks the project; <c>rate_limited</c> and <c>ingest_paused</c>
/// carry their <c>Retry-After</c>; other 5xx are retried with jitter then handed back). Only cancellation throws.</item>
/// </list>
/// The engine never touches the queue and never deletes anything.
/// </summary>
public sealed class UploadEngine
{
    private readonly IApiClient api;
    private readonly HttpClient s3;
    private readonly IClock clock;
    private readonly BandwidthLimiter limiter;
    private readonly UploadEngineOptions options;

    /// <param name="s3">Used for the presigned PUTs only. It must not add credentials: the URLs are the credential.</param>
    public UploadEngine(IApiClient api, HttpClient s3, IClock clock, BandwidthLimiter? limiter = null, UploadEngineOptions? options = null)
    {
        this.api = api;
        this.s3 = s3;
        this.clock = clock;
        this.limiter = limiter ?? new BandwidthLimiter(clock, 0);
        this.options = options ?? new UploadEngineOptions();
    }

    public BandwidthLimiter Limiter => limiter;

    public async Task<UploadResult> UploadAsync(UploadRequest request, IProgress<UploadProgress>? progress = null, CancellationToken ct = default)
    {
        // ---- hash -------------------------------------------------------------------------------------------------
        FileDigest digest;
        try
        {
            var size = new FileInfo(request.FilePath).Length;
            if (request.KnownSha256 is { } known && known.Length == 64)
            {
                digest = new FileDigest(known, size);
            }
            else
            {
                progress?.Report(new UploadProgress(UploadStage.Hashing, null, null, 0, size, 0, 0));
                digest = await FileDigest.ComputeAsync(request.FilePath, ct: ct).ConfigureAwait(false);
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            return Fail(null, "file_unreadable", "The file could not be read: " + ex.Message);
        }
        if (digest.Size < 1)
            return Fail(digest.Sha256, "file_empty", "The file is empty.");
        var libraryRig = LibraryRef.RigOf(request.ProjectRef);
        if (LibraryRef.IsLibrary(request.ProjectRef) && (libraryRig is null || !request.Stripped))
            return Fail(digest.Sha256, ApiCodes.StripRequired, "A calibration library frame is only uploaded with the observer location removed.");

        var sent = 0;
        var resumedAny = false;
        for (var pass = 0; ; pass++)
        {
            // ---- init (idempotent: also the resume call) -----------------------------------------------------------
            progress?.Report(new UploadProgress(UploadStage.Initialising, digest.Sha256, null, 0, digest.Size, 0, 0));
            var (init, initError) = libraryRig is not null
                ? await CallAsync(() => InitLibraryAsync(libraryRig, request, digest, ct), ct).ConfigureAwait(false)
                : await CallAsync(() => api.InitFrameAsync(request.ProjectRef, new FrameInitRequest
                {
                    Sha256 = digest.Sha256,
                    Size = digest.Size,
                    Filename = request.Filename,
                    Header = request.Header,
                    RigHint = request.RigId is null ? null : new RigHint { RigId = request.RigId },
                    Stripped = request.Stripped,
                }, ct), ct).ConfigureAwait(false);
            if (init is null) return Classify(initError!, digest.Sha256, null, sent);

            var up = init.Upload;
            resumedAny |= init.Resumed;
            if (up.Type != "multipart" || up.PartSize <= 0 || up.PartCount < 1)
                return Fail(digest.Sha256, "unsupported_upload", $"The server offered an upload type this plugin cannot use ({up.Type}).", init.FrameId);
            if (LengthOf(request.FilePath) != digest.Size)
                return Fail(digest.Sha256, "file_changed", "The file changed while it was being uploaded.", init.FrameId);

            var completed = up.CompletedParts.ToDictionary(p => p.PartNumber, p => p.Etag);
            var partsDone = completed.Count;
            long bytesSent = completed.Keys.Sum(n => PartLength(n, up.PartSize, up.PartCount, digest.Size));
            progress?.Report(new UploadProgress(UploadStage.Uploading, digest.Sha256, init.FrameId, bytesSent, digest.Size, partsDone, up.PartCount));

            // ---- parts -----------------------------------------------------------------------------------------------
            var outcome = await UploadPartsAsync(request, init, completed, digest, bytesSent, partsDone, progress, ct).ConfigureAwait(false);
            sent += outcome.PartsSent;
            if (outcome.Failure is { } failure)
            {
                if (outcome.UrlsExpired && pass < options.MaxResumes) continue; // re-init for fresh URLs for what is left
                return failure with { PartsSent = sent, Resumed = resumedAny };
            }

            // ---- complete -----------------------------------------------------------------------------------------------
            progress?.Report(new UploadProgress(UploadStage.Completing, digest.Sha256, init.FrameId, digest.Size, digest.Size, up.PartCount, up.PartCount));
            var etags = completed.OrderBy(kv => kv.Key).Select(kv => new CompletedPart { PartNumber = kv.Key, Etag = kv.Value }).ToList();
            var (done, completeError) = libraryRig is not null
                ? await CallAsync(() => CompleteLibraryAsync(libraryRig, init.FrameId, etags, ct), ct).ConfigureAwait(false)
                : await CallAsync(() => api.CompleteFrameAsync(request.ProjectRef, init.FrameId, etags, ct), ct).ConfigureAwait(false);
            if (done is not null)
            {
                progress?.Report(new UploadProgress(UploadStage.Done, digest.Sha256, init.FrameId, digest.Size, digest.Size, up.PartCount, up.PartCount));
                return new UploadResult(UploadOutcome.Uploaded, digest.Sha256, init.FrameId, done.Status, Resumed: resumedAny, PartsSent: sent);
            }

            switch (completeError!.Code)
            {
                case ApiCodes.UploadIncomplete or ApiCodes.SizeMismatch or ApiCodes.BadEtags when pass < options.MaxResumes:
                    continue; // start again from init: it reports what the server really holds
                case ApiCodes.FrameState:
                    // Someone (an earlier attempt of ours that lost its response) already completed it.
                    if (completeError.FrameStatus is { } st && st != FrameStatuses.Uploading)
                        return new UploadResult(UploadOutcome.Uploaded, digest.Sha256, init.FrameId, st, Resumed: true, PartsSent: sent);
                    break;
            }
            return Classify(completeError, digest.Sha256, init.FrameId, sent);
        }
    }

    // ---- parts ------------------------------------------------------------------------------------------------------

    private sealed record PartsOutcome(UploadResult? Failure, bool UrlsExpired, int PartsSent);

    private async Task<PartsOutcome> UploadPartsAsync(UploadRequest request, FrameInitResponse init, Dictionary<int, string> completed,
        FileDigest digest, long bytesAlready, int partsAlready, IProgress<UploadProgress>? progress, CancellationToken ct)
    {
        var up = init.Upload;
        var todo = up.PartUrls.OrderBy(p => p.PartNumber).ToList();
        foreach (var p in todo)
        {
            var offset = (long)(p.PartNumber - 1) * up.PartSize;
            if (p.PartNumber < 1 || p.PartNumber > up.PartCount || offset + p.Size > digest.Size || p.Size < 1)
                return new PartsOutcome(Fail(digest.Sha256, "bad_part_layout", $"The server's part {p.PartNumber} does not fit the file.", init.FrameId), false, 0);
        }

        long bytes = bytesAlready;
        var partsDone = partsAlready;
        var sentParts = 0;
        var etags = new ConcurrentDictionary<int, string>();
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        PartFailure? firstFailure = null;
        var failureGate = new object();

        void Report() => progress?.Report(new UploadProgress(UploadStage.Uploading, digest.Sha256, init.FrameId,
            Math.Min(Interlocked.Read(ref bytes), digest.Size), digest.Size, Volatile.Read(ref partsDone), up.PartCount));

        try
        {
            await Parallel.ForEachAsync(todo, new ParallelOptions { MaxDegreeOfParallelism = Math.Max(1, options.ParallelParts), CancellationToken = cts.Token },
                async (part, token) =>
                {
                    var offset = (long)(part.PartNumber - 1) * up.PartSize;
                    var result = await PutPartAsync(request.FilePath, part, offset, n => { Interlocked.Add(ref bytes, n); Report(); }, token).ConfigureAwait(false);
                    if (result.Failure is { } f)
                    {
                        lock (failureGate) firstFailure ??= f;
                        cts.Cancel();
                        return;
                    }
                    etags[part.PartNumber] = result.Etag!;
                    Interlocked.Increment(ref sentParts);
                    Interlocked.Increment(ref partsDone);
                    Report();
                }).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested && firstFailure is not null)
        {
            // One part failed for good and cancelled its siblings.
        }

        foreach (var kv in etags) completed[kv.Key] = kv.Value; // parts that finished count even if a sibling failed: they resume

        if (firstFailure is { } fail)
        {
            var result = fail.UrlExpired
                ? new UploadResult(UploadOutcome.RetryLater, digest.Sha256, init.FrameId, Code: "presigned_url_expired", Message: "A presigned upload URL expired; resuming.")
                : new UploadResult(UploadOutcome.RetryLater, digest.Sha256, init.FrameId, Code: fail.Code, Message: fail.Message);
            return new PartsOutcome(result, fail.UrlExpired, sentParts);
        }
        ct.ThrowIfCancellationRequested();
        if (completed.Count != up.PartCount)
            return new PartsOutcome(Fail(digest.Sha256, "bad_part_layout", "The server's part list does not cover the file.", init.FrameId), false, sentParts);
        return new PartsOutcome(null, false, sentParts);
    }

    private sealed record PartFailure(string Code, string Message, bool UrlExpired);

    private sealed record PutResult(string? Etag, PartFailure? Failure);

    // Sends one part, retrying with jitter. Returns the ETag, or why it gave up.
    private async Task<PutResult> PutPartAsync(string path, UploadPart part, long offset, Action<long> onBytes, CancellationToken ct)
    {
        string lastProblem = "";
        for (var attempt = 1; attempt <= options.MaxPartAttempts; attempt++)
        {
            long attemptBytes = 0;
            try
            {
                using var request = new HttpRequestMessage(HttpMethod.Put, part.Url)
                {
                    Content = new FilePartContent(path, offset, part.Size, limiter, n => { attemptBytes += n; onBytes(n); }),
                };
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeout.CancelAfter(options.PartTimeout);
                using var response = await s3.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token).ConfigureAwait(false);
                if (response.IsSuccessStatusCode)
                {
                    if (response.Headers.TryGetValues("ETag", out var values) && values.FirstOrDefault() is { Length: > 0 } etag)
                        return new PutResult(etag, null);
                    lastProblem = "the storage service returned no ETag";
                }
                else if ((int)response.StatusCode == 403)
                {
                    // An expired or already-used presigned URL: retrying the same URL cannot help.
                    onBytes(-attemptBytes);
                    return new PutResult(null, new PartFailure("presigned_url_expired", $"Part {part.PartNumber}: HTTP 403 from storage.", true));
                }
                else
                {
                    lastProblem = $"HTTP {(int)response.StatusCode}";
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex) when (ex is HttpRequestException or IOException or OperationCanceledException)
            {
                lastProblem = ex.Message;
            }
            onBytes(-attemptBytes); // this attempt's bytes are not delivered; they will be sent again
            if (attempt < options.MaxPartAttempts)
                await clock.DelayAsync(options.PartBackoff.Delay(attempt), ct).ConfigureAwait(false);
        }
        return new PutResult(null, new PartFailure("part_failed", $"Part {part.PartNumber} failed {options.MaxPartAttempts} times: {lastProblem}.", false));
    }

    // ---- API calls --------------------------------------------------------------------------------------------------

    // The library routes answer their own shapes; the flow after init is the same, so they are folded into the project types.
    private async Task<FrameInitResponse> InitLibraryAsync(string rigId, UploadRequest request, FileDigest digest, CancellationToken ct)
    {
        var r = await api.InitLibraryFrameAsync(rigId, new LibraryFrameInitRequest
        {
            Sha256 = digest.Sha256,
            Size = digest.Size,
            Filename = request.Filename,
            Header = request.Header,
            Stripped = true,
        }, ct).ConfigureAwait(false);
        return new FrameInitResponse { FrameId = r.FrameId, Status = r.Status, Resumed = r.Resumed, FrameType = r.FrameType, RigId = r.RigId, Upload = r.Upload };
    }

    private async Task<FrameCompleteResponse> CompleteLibraryAsync(string rigId, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct)
    {
        var r = await api.CompleteLibraryFrameAsync(rigId, frameId, etags, ct).ConfigureAwait(false);
        return new FrameCompleteResponse { FrameId = r.FrameId, Status = r.Status, AlreadyComplete = r.AlreadyComplete };
    }

    // Runs an API call; retries transient failures (no response, garbled body, 5xx) with jitter, up to MaxApiAttempts.
    // rate_limited and ingest_paused are NOT retried here: they carry a Retry-After the queue must honour.
    private async Task<(T? Value, ApiException? Error)> CallAsync<T>(Func<Task<T>> call, CancellationToken ct) where T : class
    {
        ApiException? last = null;
        for (var attempt = 1; attempt <= options.MaxApiAttempts; attempt++)
        {
            try
            {
                return (await call().ConfigureAwait(false), null);
            }
            catch (ApiException ex)
            {
                last = ex;
                var retryHere = ex.Code is ApiCodes.NetworkError or ApiCodes.BadResponse or ApiCodes.InternalError
                    || (ex.HttpStatus >= 500 && ex.Code is not (ApiCodes.IngestPaused or ApiCodes.RateLimited));
                if (!retryHere || attempt == options.MaxApiAttempts) break;
                await clock.DelayAsync(options.ApiBackoff.Delay(attempt), ct).ConfigureAwait(false);
            }
        }
        return (null, last);
    }

    private UploadResult Classify(ApiException ex, string? sha, string? frameId, int partsSent)
    {
        UploadResult R(UploadOutcome o, TimeSpan? retry = null) =>
            new(o, sha, frameId, Code: ex.Code, Message: ex.Message, RetryAfter: retry, PartsSent: partsSent, Quota: ex.Quota);

        switch (ex.Code)
        {
            case ApiCodes.Duplicate:
                return new UploadResult(UploadOutcome.Duplicate, sha, ex.ExistingFrame?.FrameId ?? frameId, ex.ExistingFrame?.Status,
                    ex.Code, ex.Message, PartsSent: partsSent);
            case ApiCodes.QuotaExceeded:
                return R(UploadOutcome.ProjectBlocked);
            // A removed member is answered not_member by the frame routes; membership_removed belongs to join only.
            case ApiCodes.ProjectNotOpen or ApiCodes.ProjectDeleted or ApiCodes.MembershipPending or ApiCodes.NotMember or ApiCodes.NotFound:
                return R(UploadOutcome.ProjectBlocked);
            case ApiCodes.StripRequired:
                // The project requires stripped frames and this request did not declare it. The shell strips whenever its
                // cached project policy says so, so this is a stale cache: a verdict for this file, not a reason to stop the project.
                return R(UploadOutcome.Failed);
            case ApiCodes.RateLimited:
                return R(UploadOutcome.RetryLater, ex.RetryAfter ?? options.DefaultRateLimitRetry);
            case ApiCodes.IngestPaused:
                return R(UploadOutcome.RetryLater, ex.RetryAfter ?? options.DefaultIngestPausedRetry);
            case ApiCodes.UploadIncomplete or ApiCodes.SizeMismatch or ApiCodes.BadEtags or ApiCodes.FrameState:
                // The upload is in a state init can repair; this pass ran out of resumes, a later one starts clean.
                return R(UploadOutcome.RetryLater, ex.RetryAfter);
            case ApiCodes.NotAuthenticated or ApiCodes.TokenInvalid or ApiCodes.SessionExpired or ApiCodes.TokenScope:
                return R(UploadOutcome.AuthRequired);
        }
        if (ex.IsTransient || ex.HttpStatus >= 500) return R(UploadOutcome.RetryLater, ex.RetryAfter);
        return R(UploadOutcome.Failed);
    }

    private static UploadResult Fail(string? sha, string code, string message, string? frameId = null) =>
        new(UploadOutcome.Failed, sha, frameId, Code: code, Message: message);

    private static long LengthOf(string path) => new FileInfo(path).Length;

    private static long PartLength(int partNumber, long partSize, int partCount, long total) =>
        partNumber < partCount ? partSize : total - partSize * (partCount - 1);
}

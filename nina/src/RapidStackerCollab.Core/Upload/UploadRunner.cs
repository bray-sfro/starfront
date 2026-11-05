// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Upload;

public sealed class UploadRunnerOptions
{
    /// <summary>Where the stripped temporary copies are written. The runner creates and deletes only files of its own naming here.</summary>
    public required string TempDirectory { get; init; }

    /// <summary>Remove observer-location cards from a temp copy before upload (plan §8.1: default on). FITS and XISF are both stripped.</summary>
    public bool StripLocation { get; init; } = true;

    /// <summary>
    /// Decides per item, at the moment it is prepared, whether to strip. When set it replaces <see cref="StripLocation"/>, so the
    /// shell can follow the options page live (and force stripping for a project whose owner requires it).
    /// </summary>
    public Func<QueueItem, bool>? StripPredicate { get; init; }

    public StripOptions Strip { get; init; } = StripOptions.Default;

    /// <summary>Attempts after which a file that keeps failing for per-file reasons becomes <see cref="QueueStates.Failed"/>. Rate limits and server outages never count towards it.</summary>
    public int MaxAttempts { get; init; } = 8;

    public Backoff RetryBackoff { get; init; } = new() { Base = TimeSpan.FromSeconds(30), Max = TimeSpan.FromMinutes(30) };

    /// <summary>How long <see cref="UploadRunner.RunAsync"/> sleeps when there is nothing to do and nothing to wait for.</summary>
    public TimeSpan IdlePoll { get; init; } = TimeSpan.FromSeconds(10);

    /// <summary>A project that hit its quota is looked at again after this long (a withdrawal may have freed space).</summary>
    public TimeSpan QuotaRecheck { get; init; } = TimeSpan.FromHours(6);

    /// <summary>A project that is not open, or where membership awaits approval, is looked at again after this long.</summary>
    public TimeSpan ProjectStateRecheck { get; init; } = TimeSpan.FromMinutes(15);
}

/// <summary>
/// The loop behind the dock panel: ask the <see cref="UploadPolicy"/>, claim the next due item from the
/// <see cref="UploadQueue"/>, make a stripped temporary copy, hand it to the <see cref="UploadEngine"/>, record the
/// outcome, delete its own temp file. It never reads, writes, moves or deletes a user's file: stripping reads the original
/// and writes a new file in <see cref="UploadRunnerOptions.TempDirectory"/>.
/// <para>Outcome handling: uploaded and duplicate are done; a quota or project-state refusal pauses that project while the
/// others continue; a rate limit, the ingest pause or an unreachable server holds the whole run until the time the
/// server named; a bad token stops everything until <see cref="ClearAuthRequired"/>; a file the server will never take is
/// failed once and not retried.</para>
/// </summary>
public sealed class UploadRunner
{
    private static readonly HashSet<string> ServerWideCodes = new()
    {
        ApiCodes.RateLimited, ApiCodes.IngestPaused, ApiCodes.NetworkError, ApiCodes.BadResponse, ApiCodes.InternalError,
    };

    private readonly UploadQueue queue;
    private readonly UploadEngine engine;
    private readonly UploadPolicy policy;
    private readonly Func<UploadPolicySettings> settings;
    private readonly Func<ImagingState> state;
    private readonly IClock clock;
    private readonly UploadRunnerOptions options;
    private TaskCompletionSource wake = NewWake();
    private DateTimeOffset holdUntil = DateTimeOffset.MinValue;
    private volatile bool authRequired;

    public UploadRunner(UploadQueue queue, UploadEngine engine, UploadPolicy policy, Func<UploadPolicySettings> settings,
        Func<ImagingState> state, IClock clock, UploadRunnerOptions options)
    {
        this.queue = queue;
        this.engine = engine;
        this.policy = policy;
        this.settings = settings;
        this.state = state;
        this.clock = clock;
        this.options = options;
        Directory.CreateDirectory(options.TempDirectory);
    }

    /// <summary>The token was rejected: nothing is uploaded until the user signs in again and <see cref="ClearAuthRequired"/> is called.</summary>
    public bool AuthRequired => authRequired;

    /// <summary>The last answer of the policy, for the dock panel's status line.</summary>
    public PolicyDecision? LastDecision { get; private set; }

    /// <summary>Set while the server asked everyone to wait (rate limit, ingest pause, outage).</summary>
    public DateTimeOffset? HeldUntil => holdUntil > clock.Now ? holdUntil : null;

    public Action<QueueItem, UploadProgress>? OnProgress { get; set; }
    public Action<QueueItem, UploadResult>? OnItemFinished { get; set; }
    public Action? OnAuthRequired { get; set; }

    public void ClearAuthRequired()
    {
        authRequired = false;
        Wake();
    }

    /// <summary>Ask a sleeping <see cref="RunAsync"/> to look at the queue now (a frame was queued, a setting changed).</summary>
    public void Wake() => wake.TrySetResult();

    /// <summary>Start-up recovery plus the endless loop. Returns when cancelled.</summary>
    public async Task RunAsync(CancellationToken ct)
    {
        queue.RecoverInterrupted();
        SweepOrphanTempFiles();
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await RunOnceAsync(ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            var delay = options.IdlePoll;
            var now = clock.Now;
            if (queue.NextDueAt() is { } due && due > now && due - now < delay) delay = due - now;
            if (LastDecision?.RetryAt is { } at && at > now && at - now < delay) delay = at - now;
            if (holdUntil > now && holdUntil - now < delay) delay = holdUntil - now;
            var signal = wake.Task;
            using var delayCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            var sleep = clock.DelayAsync(delay <= TimeSpan.Zero ? TimeSpan.FromMilliseconds(50) : delay, delayCts.Token);
            try
            {
                await Task.WhenAny(sleep, signal).ConfigureAwait(false);
            }
            finally
            {
                delayCts.Cancel();
                try { await sleep.ConfigureAwait(false); } catch (OperationCanceledException) { }
            }
            if (signal.IsCompleted) wake = NewWake();
        }
    }

    /// <summary>Upload everything that is eligible right now, then return how many items were attempted.</summary>
    public async Task<int> RunOnceAsync(CancellationToken ct = default)
    {
        var processed = 0;
        while (!ct.IsCancellationRequested)
        {
            if (authRequired) break;
            if (holdUntil > clock.Now)
            {
                LastDecision = new PolicyDecision(false, "Waiting: the server asked to retry later", holdUntil);
                break;
            }
            var decision = policy.Decide(settings(), state());
            LastDecision = decision;
            if (!decision.Allow) break;

            var item = queue.ClaimNext();
            if (item is null) break;
            processed++;
            await ProcessAsync(item, ct).ConfigureAwait(false);
        }
        ct.ThrowIfCancellationRequested();
        return processed;
    }

    // ---- one item ------------------------------------------------------------------------------------------------

    private async Task ProcessAsync(QueueItem item, CancellationToken ct)
    {
        string? tempPath = null;
        try
        {
            var prepared = Prepare(item, out tempPath);
            if (prepared.Failure is { } failure)
            {
                queue.MarkFailed(item.Id, failure.Code, failure.Message);
                Finished(item, new UploadResult(UploadOutcome.Failed, null, Code: failure.Code, Message: failure.Message));
                return;
            }

            var request = new UploadRequest(item.ProjectRef, prepared.UploadPath!, item.Filename, item.Header, prepared.Stripped, item.RigId);
            string? savedSha = null, savedFrame = null;
            var progress = new SyncProgress(p =>
            {
                // Persist what a resume needs once each, not on every 64 KiB chunk.
                if ((p.Sha256 is not null && p.Sha256 != savedSha) || (p.FrameId is not null && p.FrameId != savedFrame))
                {
                    queue.RecordProgress(item.Id, p.Sha256, p.TotalBytes, p.FrameId);
                    savedSha = p.Sha256 ?? savedSha;
                    savedFrame = p.FrameId ?? savedFrame;
                }
                OnProgress?.Invoke(item, p);
            });

            var result = await engine.UploadAsync(request, progress, ct).ConfigureAwait(false);
            Apply(item, result);
            Finished(item, result);
        }
        catch (OperationCanceledException)
        {
            queue.Release(item.Id); // not a failure; the next run resumes it
            throw;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or FitsFormatException)
        {
            var result = new UploadResult(UploadOutcome.RetryLater, null, Code: "io_error", Message: ex.Message);
            Apply(item, result);
            Finished(item, result);
        }
        finally
        {
            if (tempPath is not null) TryDelete(tempPath);
        }
    }

    private sealed record Prepared(string? UploadPath, bool Stripped, (string Code, string Message)? Failure);

    private Prepared Prepare(QueueItem item, out string? tempPath)
    {
        tempPath = null;
        if (!File.Exists(item.Path))
            return new Prepared(null, false, ("file_missing", "The file is no longer there: " + item.Path));

        // A library frame is always uploaded stripped, whatever the toggle says: it serves projects the user has not joined yet (README open point 104).
        if (!LibraryRef.IsLibrary(item.ProjectRef) && !(options.StripPredicate?.Invoke(item) ?? options.StripLocation))
            return new Prepared(item.Path, false, null);

        var isXisf = XisfHeaderReader.IsXisf(item.Path);
        if (!isXisf && !LooksLikeFits(item.Path))
            return new Prepared(null, false, ("unsupported_file_type", "Only FITS and XISF files can be uploaded."));

        var path = Path.Combine(options.TempDirectory, TempName(item.Id));
        TryDelete(path); // a leftover of a crashed run, by our own name
        tempPath = path;
        if (isXisf)
        {
            try { XisfHeaderStripper.StripToFile(item.Path, path, options.Strip); }
            catch (FitsFormatException ex)
            {
                // The cut would have damaged the file; refuse rather than send it with the location in it, or broken.
                return new Prepared(null, false, ("xisf_strip_failed", "The observer location could not be removed from this XISF file, so it was not uploaded: " + ex.Message));
            }
        }
        else
        {
            FitsHeaderStripper.StripToFile(item.Path, path, options.Strip);
        }
        return new Prepared(path, true, null);
    }

    private void Apply(QueueItem item, UploadResult r)
    {
        var now = clock.Now;
        switch (r.Outcome)
        {
            case UploadOutcome.Uploaded:
                queue.MarkUploaded(item.Id, r.FrameId, r.Status);
                break;

            case UploadOutcome.Duplicate:
                queue.MarkDuplicate(item.Id, r.FrameId, r.Status);
                break;

            case UploadOutcome.RetryLater:
            {
                var serverWide = r.Code is not null && ServerWideCodes.Contains(r.Code);
                var attempts = item.Attempts + 1;
                if (!serverWide && attempts >= options.MaxAttempts)
                {
                    queue.MarkFailed(item.Id, r.Code, $"Gave up after {attempts} attempts: {r.Message}");
                    break;
                }
                var wait = r.RetryAfter ?? options.RetryBackoff.Delay(Math.Max(1, serverWide ? 1 : attempts));
                queue.MarkRetry(item.Id, now + wait, r.Code, r.Message);
                if (serverWide) holdUntil = now + wait;
                break;
            }

            case UploadOutcome.ProjectBlocked:
            {
                queue.Release(item.Id);
                DateTimeOffset? resume = r.Code switch
                {
                    ApiCodes.QuotaExceeded => now + options.QuotaRecheck,
                    ApiCodes.ProjectNotOpen or ApiCodes.ProjectDeleted or ApiCodes.MembershipPending => now + options.ProjectStateRecheck, // a deleted project can be restored within its grace period
                    _ => null, // not a member / removed / gone: the user has to act
                };
                queue.PauseProject(item.ProjectRef, r.Message, r.Code, resume);
                break;
            }

            case UploadOutcome.AuthRequired:
                queue.Release(item.Id);
                authRequired = true;
                OnAuthRequired?.Invoke();
                break;

            case UploadOutcome.Failed:
                queue.MarkFailed(item.Id, r.Code, r.Message);
                break;
        }
    }

    private void Finished(QueueItem item, UploadResult result) => OnItemFinished?.Invoke(item, result);

    // ---- temp files ----------------------------------------------------------------------------------------------

    private const string TempPrefix = "rsc-upload-";
    private const string TempSuffix = ".fits.tmp";

    private static string TempName(long id) => $"{TempPrefix}{id}{TempSuffix}";

    /// <summary>Remove stripped copies left by a run that crashed. Only files matching the runner's own naming are touched.</summary>
    private void SweepOrphanTempFiles()
    {
        try
        {
            foreach (var f in Directory.EnumerateFiles(options.TempDirectory, TempPrefix + "*" + TempSuffix))
                TryDelete(f);
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    private static void TryDelete(string path)
    {
        try { File.Delete(path); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    private static bool LooksLikeFits(string path)
    {
        try
        {
            using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var b = new byte[6];
            return s.Read(b, 0, 6) == 6 && Encoding.ASCII.GetString(b) == "SIMPLE";
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static TaskCompletionSource NewWake() => new(TaskCreationOptions.RunContinuationsAsynchronously);

    /// <summary>An <see cref="IProgress{T}"/> that runs its callback on the reporting thread, so persistence happens in order.</summary>
    private sealed class SyncProgress : IProgress<UploadProgress>
    {
        private readonly Action<UploadProgress> action;

        public SyncProgress(Action<UploadProgress> action) => this.action = action;

        public void Report(UploadProgress value) => action(value);
    }
}

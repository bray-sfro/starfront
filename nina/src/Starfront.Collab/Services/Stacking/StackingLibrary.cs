using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Text;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Upload;
using RapidStackerCollab.Core.Util;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>What the library sync reads from the settings; blank means not set.</summary>
    public interface ILibrarySettings {
        string RigId { get; }
        string LibraryFolder { get; }
        int LibrarySyncHours { get; }
        bool LibraryPreferRaw { get; }
    }

    public enum LibrarySyncState {
        /// <summary>No rig, no folder or not signed in: nothing to do yet.</summary>
        NotConfigured,
        Idle,
        Syncing,
        /// <summary>The last pass finished (files may still be uploading).</summary>
        Done,
        /// <summary>The last pass could not run; it tries again later.</summary>
        Problem,
    }

    /// <summary>A library frame the stacking service could not use, or a local file that could not be read, with a plain reason.</summary>
    public sealed record LibraryProblemView(string File, string Text);

    /// <summary>What the last pass found and did.</summary>
    public sealed record LibrarySyncStatus(
        LibrarySyncState State,
        string Message,
        DateTimeOffset? LastSyncAt,
        int Found,
        int Planned,
        int Queued,
        int OnServer,
        int Skipped,
        int Unsettled,
        int Ignored,
        int ServerOnly,
        LibraryUsage Usage,
        int Masters,
        IReadOnlyList<LibraryProblemView> Problems,
        IReadOnlyList<string> Notes) {
        public static readonly LibrarySyncStatus Empty = new(LibrarySyncState.NotConfigured, "Choose your calibration folder to start.", null, 0, 0, 0, 0, 0, 0, 0, 0,
            null, 0, Array.Empty<LibraryProblemView>(), Array.Empty<string>());
    }

    /// <summary>
    /// Keeps the rig's calibration library on Rapid Stacker Collab in step
    /// with a folder of bias, dark and dark-flat frames, so every stacking
    /// project calibrates this telescope's lights with them without the darks
    /// being sent to each project. A pass scans the folder (files still being
    /// written wait for the next one), chooses per group the newest master or
    /// else the raw frames, strips and hashes each planned file once, asks
    /// the service what it already holds (paged by 100, the most it gives),
    /// and queues only what it lacks, within the library's quota, as
    /// <c>lib:{rig}</c> rows of the ordinary upload queue, always stripped.
    /// It runs when the rig, folder or "prefer raw" changes, every
    /// <see cref="ILibrarySettings.LibrarySyncHours"/>, and on demand, and it
    /// never withdraws anything: frames on the service that are not in the
    /// folder are only counted.
    ///
    /// Ported from Rapid Stacker Collab's plugin
    /// (<c>RapidStackerCollab.Shell/Library/LibrarySyncService.cs</c>) without
    /// its MVVM base, so it runs under the tests; the options page wraps it.
    /// </summary>
    public sealed class StackingLibrary {
        private const int PageSize = 100; // the contract's maximum for the library listing's limit (1-100)
        private const int MaxPages = 100;
        private const int MaxListed = 20;
        private static readonly TimeSpan RetryAfterProblem = TimeSpan.FromMinutes(15);

        /// <summary>How often the loop looks at whether a sync is due (real time). A settings change or <see cref="Wake"/> does not wait for it.</summary>
        public TimeSpan CheckEvery { get; set; } = TimeSpan.FromMinutes(1);

        private readonly Func<IApiClient> api;
        private readonly UploadQueue queue;
        private readonly ILibrarySettings settings;
        private readonly IClock clock;
        private readonly Func<bool> isSignedIn;
        private readonly Action<string> info;
        private readonly Action<string, Exception> error;
        private readonly LibraryScanner scanner;
        private readonly LibraryHasher hasher;
        private readonly SemaphoreSlim pass = new(1, 1);
        private TaskCompletionSource wake = NewWake();
        private LibrarySyncStatus status = LibrarySyncStatus.Empty;
        private DateTimeOffset nextDue = DateTimeOffset.MinValue;
        private string lastKey;

        public StackingLibrary(Func<IApiClient> api, UploadQueue queue, ILibrarySettings settings, IClock clock, Func<bool> isSignedIn, string tempDirectory,
                               Action<string> info = null, Action<string, Exception> error = null) {
            this.api = api;
            this.queue = queue;
            this.settings = settings;
            this.clock = clock;
            this.isSignedIn = isSignedIn;
            this.info = info ?? (_ => { });
            this.error = error ?? ((_, _) => { });
            scanner = new LibraryScanner(clock, tempDirectory);
            hasher = new LibraryHasher(queue, tempDirectory);
        }

        /// <summary>Raised after files were put in the queue, so the runner wakes.</summary>
        public event Action Queued;

        /// <summary>Raised on any thread when <see cref="Status"/> changed.</summary>
        public event Action Changed;

        public LibrarySyncStatus Status {
            get => status;
            private set {
                status = value;
                try { Changed?.Invoke(); } catch (Exception ex) { error("Starfront stacking: a library handler failed", ex); }
            }
        }

        private string Rig => string.IsNullOrWhiteSpace(settings.RigId) ? null : settings.RigId.Trim();
        private string Folder => string.IsNullOrWhiteSpace(settings.LibraryFolder) ? null : settings.LibraryFolder.Trim();

        public bool CanSync => Rig != null && Folder != null;

        /// <summary>One line for the options page.</summary>
        public string StatusText => status.State switch {
            LibrarySyncState.NotConfigured => Rig == null
                ? "The calibration library needs this telescope's rig, which is made when it first joins a stacking project."
                : Folder == null ? "Choose your calibration folder to start." : "Sign in to Rapid Stacker Collab to sync your calibration library.",
            LibrarySyncState.Syncing => "Syncing your calibration library...",
            _ => status.Message,
        };

        /// <summary>The counts of the last pass, one sentence, or empty before the first.</summary>
        public string DetailText {
            get {
                if (status.LastSyncAt == null) return "";
                var sb = new StringBuilder();
                sb.Append(CultureInfo.InvariantCulture, $"{status.Planned} file(s) planned: {status.OnServer} already on the server, {status.Queued} queued now");
                if (status.Skipped > 0) sb.Append(CultureInfo.InvariantCulture, $", {status.Skipped} skipped");
                if (status.Unsettled > 0) sb.Append(CultureInfo.InvariantCulture, $", {status.Unsettled} still being written");
                sb.Append('.');
                if (status.ServerOnly > 0) sb.Append(CultureInfo.InvariantCulture, $" {status.ServerOnly} on the server are not in your folder (nothing is removed automatically; withdraw them on the Rapid Stacker Collab website).");
                if (status.Usage is LibraryUsage u) {
                    sb.Append(CultureInfo.InvariantCulture, $" Library: {u.FramesUsed} of {u.MaxFrames} frames, {FormatBytes(u.BytesUsed)} of {FormatBytes(u.MaxBytes)}.");
                }
                if (status.Masters > 0) sb.Append(CultureInfo.InvariantCulture, $" {status.Masters} master(s) built.");
                return sb.ToString();
            }
        }

        /// <summary>The dock's line: uploads under way, problems, the last sync. Empty while the library has never been used.</summary>
        public string DockText() {
            if (Rig is not string rig) return "";
            var counts = queue.LibraryCounts(rig);
            var active = counts[QueueStates.Pending] + counts[QueueStates.Uploading];
            var parts = new List<string>();
            if (active > 0) parts.Add($"{active} file(s) uploading or waiting");
            if (status.Problems.Count > 0) parts.Add($"{status.Problems.Count} problem(s), see the plugin options");
            if (status.State == LibrarySyncState.Problem) parts.Add(status.Message);
            else if (status.LastSyncAt is DateTimeOffset at && parts.Count == 0) parts.Add("up to date, last synced " + Ago(clock.Now - at));
            return parts.Count == 0 ? "" : "Calibration library: " + string.Join("; ", parts) + ".";
        }

        public bool HasProblems => status.Problems.Count > 0;

        /// <summary>One line per problem frame or file, capped; empty when there are none.</summary>
        public string ProblemsText => string.Join(Environment.NewLine, status.Problems.Take(MaxListed).Select(p => $"{p.File}: {p.Text}")
            .Concat(status.Problems.Count > MaxListed ? new[] { $"... and {status.Problems.Count - MaxListed} more." } : Array.Empty<string>()));

        /// <summary>Ask the loop to look at the library now (the button, a setting, a sign-in).</summary>
        public void Wake() => wake.TrySetResult();

        /// <summary>The settings changed: a new rig, folder or "prefer raw" syncs at once.</summary>
        public void SettingsChanged() {
            if (Key() != lastKey) Wake();
            try { Changed?.Invoke(); } catch (Exception ex) { error("Starfront stacking: a library handler failed", ex); }
        }

        // -- the loop ----------------------------------------------------------

        /// <summary>Returns when cancelled; never throws. Syncs when due, when the rig or folder changed, or when woken.</summary>
        public async Task RunAsync(CancellationToken ct) {
            while (!ct.IsCancellationRequested) {
                try {
                    if (Due()) await SyncAsync(ct).ConfigureAwait(false);
                    var signal = wake.Task;
                    using var delayCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    // Real time on purpose: the injected clock only says what "due" means. A fake clock that returns at once would make
                    // this a busy loop that also drags that clock forward.
                    var sleep = Task.Delay(CheckEvery, delayCts.Token);
                    try {
                        await Task.WhenAny(sleep, signal).ConfigureAwait(false);
                    } finally {
                        delayCts.Cancel();
                        try { await sleep.ConfigureAwait(false); } catch (OperationCanceledException) { }
                    }
                    if (signal.IsCompleted) wake = NewWake();
                } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
                    return;
                } catch (Exception ex) {
                    error("Starfront stacking: the calibration library sync failed", ex);
                    nextDue = clock.Now + RetryAfterProblem;
                    try { await Task.Delay(TimeSpan.FromMinutes(1), ct).ConfigureAwait(false); } catch (OperationCanceledException) { return; }
                }
            }
        }

        private bool Due() {
            if (!CanSync || !isSignedIn()) return false;
            return Key() != lastKey || clock.Now >= nextDue;
        }

        private string Key() => (Rig ?? "") + "|" + (Folder ?? "") + "|" + settings.LibraryPreferRaw;

        // -- one pass ----------------------------------------------------------

        /// <summary>One pass; a call while another is running waits for that one and returns its result. Never throws except on cancellation.</summary>
        public async Task<LibrarySyncStatus> SyncAsync(CancellationToken ct = default) {
            if (!await pass.WaitAsync(0, ct).ConfigureAwait(false)) {
                await pass.WaitAsync(ct).ConfigureAwait(false);
                pass.Release();
                return Status;
            }
            try {
                var rigId = Rig;
                var folder = Folder;
                lastKey = Key();
                nextDue = clock.Now + TimeSpan.FromHours(Math.Max(1, settings.LibrarySyncHours));
                if (rigId == null || folder == null || !isSignedIn()) {
                    Status = LibrarySyncStatus.Empty with { State = LibrarySyncState.NotConfigured };
                    return Status;
                }
                if (!Directory.Exists(folder)) {
                    Status = Status with { State = LibrarySyncState.Problem, Message = $"The folder '{folder}' does not exist." };
                    nextDue = clock.Now + RetryAfterProblem;
                    return Status;
                }
                Status = Status with { State = LibrarySyncState.Syncing };
                try {
                    Status = await RunPassAsync(rigId, folder, ct).ConfigureAwait(false);
                    info("Starfront stacking: " + Status.Message);
                } catch (ApiException ex) {
                    nextDue = clock.Now + RetryAfterProblem;
                    Status = Status with { State = LibrarySyncState.Problem, Message = ProblemText(ex) };
                    info($"Starfront stacking: the calibration library sync could not run ({ex.Code})");
                } catch (HttpRequestException ex) {
                    nextDue = clock.Now + RetryAfterProblem;
                    Status = Status with { State = LibrarySyncState.Problem, Message = "Rapid Stacker Collab could not be reached; the library sync will try again." };
                    info($"Starfront stacking: the calibration library sync could not reach the server ({ex.Message})");
                }
                return Status;
            } finally {
                pass.Release();
            }
        }

        private async Task<LibrarySyncStatus> RunPassAsync(string rigId, string folder, CancellationToken ct) {
            var client = api();
            var notes = new List<string>();
            var problems = new List<LibraryProblemView>();

            // The rig the files must belong to.
            var rigs = await client.ListRigsAsync(ct).ConfigureAwait(false);
            var rig = rigs.FirstOrDefault(r => r.RigId == rigId)
                ?? throw new ApiException(ApiCodes.NotFound, "This telescope's rig is not on your account any more.", 0);
            var profile = new LibraryRigProfile(rig.Camera, rig.Binning, rig.WidthPx, rig.HeightPx);

            // Scan and read headers.
            var scan = await Task.Run(() => scanner.Scan(folder, ct), ct).ConfigureAwait(false);
            var candidates = new List<LibraryCandidate>();
            var unreadable = 0;
            await Task.Run(() => {
                foreach (var f in scan.Files) {
                    ct.ThrowIfCancellationRequested();
                    try {
                        candidates.Add(new LibraryCandidate(f.Path, f.Size, f.ModifiedMs, LibraryHeaderReader.Read(f.Path)));
                    } catch (Exception ex) when (ex is FitsFormatException or IOException or UnauthorizedAccessException) {
                        unreadable++;
                        notes.Add($"{Path.GetFileName(f.Path)}: the header could not be read ({ex.Message})");
                    }
                }
            }, ct).ConfigureAwait(false);
            if (scan.Truncated) notes.Add($"The folder has more than {LibraryScanner.MaxFiles:N0} files; only the first {LibraryScanner.MaxFiles:N0} were looked at.");

            var plan = LibraryPlanner.Plan(candidates, profile, new LibraryPlannerOptions(settings.LibraryPreferRaw));
            notes.AddRange(plan.Skipped.Select(s => s.Reason));
            var skipped = plan.Skipped.Count + unreadable;

            // Strip and hash what is planned (once per file).
            var hashed = new List<(LibraryPlanItem Item, string Sha)>();
            foreach (var item in plan.Upload) {
                ct.ThrowIfCancellationRequested();
                try {
                    hashed.Add((item, await hasher.StrippedSha256Async(item.File, ct).ConfigureAwait(false)));
                } catch (Exception ex) when (ex is FitsFormatException or IOException or UnauthorizedAccessException) {
                    notes.Add($"{Path.GetFileName(item.File.Path)}: could not be prepared for upload ({ex.Message})");
                    problems.Add(new LibraryProblemView(Path.GetFileName(item.File.Path), "could not be prepared for upload: " + ex.Message));
                    skipped++;
                }
            }
            queue.PruneLibraryFiles(candidates.Select(c => c.Path).ToHashSet(StringComparer.Ordinal));

            // What the server holds.
            var inventory = await ReadInventoryAsync(client, rigId, ct).ConfigureAwait(false);
            var ready = inventory.Frames.Where(f => f.Status == LibraryFrameStatuses.Ready).ToList();
            var readyShas = ready.Select(f => f.Sha256).ToHashSet(StringComparer.OrdinalIgnoreCase);
            foreach (var f in inventory.Frames.Where(f => f.Usable == false && !string.IsNullOrEmpty(f.Problem))) {
                problems.Add(new LibraryProblemView(f.Filename, LibraryProblems.Describe(f.Problem)));
            }

            // Queue what is missing, within the library quota.
            var libRef = LibraryRef.For(rigId);
            var usage = inventory.Usage;
            var counts = queue.LibraryCounts(rigId);
            long framesLeft = usage.MaxFrames - usage.FramesUsed - counts[QueueStates.Pending] - counts[QueueStates.Uploading];
            var bytesLeft = usage.MaxBytes - usage.BytesUsed;
            if (queue.ActivePauses().FirstOrDefault(p => p.ProjectRef == libRef) is { Code: ApiCodes.QuotaExceeded } && framesLeft > 0 && bytesLeft > 0) {
                queue.ResumeProject(libRef); // the person made room on the website
            }
            var onServer = 0;
            var queued = 0;
            var overQuota = 0;
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var (item, sha) in hashed) {
                if (readyShas.Contains(sha)) { onServer++; continue; }
                if (!seen.Add(sha)) { skipped++; notes.Add($"{Path.GetFileName(item.File.Path)}: the same content as another file of the plan."); continue; }

                var existing = queue.Find(libRef, item.File.Path);
                if (existing is { State: QueueStates.Pending or QueueStates.Uploading }) continue; // on its way
                if (existing is { State: QueueStates.Cancelled or QueueStates.Failed }) {
                    // Not retried behind the person's back: a removed file stays removed, a failed one is retried by hand.
                    skipped++;
                    notes.Add($"{Path.GetFileName(item.File.Path)}: " + (existing.State == QueueStates.Cancelled ? "it was removed from the upload queue." : "it failed to upload earlier."));
                    continue;
                }
                if (framesLeft < 1 || bytesLeft < item.File.Size) { overQuota++; continue; }

                var added = existing == null
                    ? queue.Enqueue(new NewQueueItem(libRef, item.File.Path, Path.GetFileName(item.File.Path), item.File.Kind, item.File.Header, rigId, "", "")).Added
                    : queue.RequeueFinished(existing.Id); // uploaded earlier, but the service no longer lists it (withdrawn)
                if (added) {
                    queued++;
                    framesLeft--;
                    bytesLeft -= item.File.Size;
                }
            }
            if (overQuota > 0) {
                notes.Add($"{overQuota} file(s) were not queued because your calibration library is full ({usage.FramesUsed} of {usage.MaxFrames} frames, {FormatBytes(usage.BytesUsed)} of {FormatBytes(usage.MaxBytes)}). Withdraw frames you no longer need on the Rapid Stacker Collab website.");
                skipped += overQuota;
            }
            if (queued > 0) {
                try { Queued?.Invoke(); } catch (Exception ex) { error("Starfront stacking: a library handler failed", ex); }
            }

            var plannedShas = hashed.Select(h => h.Sha).ToHashSet(StringComparer.OrdinalIgnoreCase);
            var serverOnly = ready.Count(f => !plannedShas.Contains(f.Sha256));
            var message = queued > 0
                ? $"Synced: {queued} file(s) queued for your calibration library."
                : candidates.Count == 0 && scan.Unsettled == 0 ? "No bias, dark or dark-flat frames found in the folder."
                : "Your calibration library is up to date.";
            return new LibrarySyncStatus(LibrarySyncState.Done, message, clock.Now, candidates.Count(FamilyKnown), hashed.Count, queued, onServer, skipped, scan.Unsettled,
                plan.Ignored, serverOnly, usage, inventory.Masters.Count, problems, notes);
        }

        private static bool FamilyKnown(LibraryCandidate c) =>
            c.Kind is FrameKind.Bias or FrameKind.Dark or FrameKind.DarkFlat or FrameKind.MasterBias or FrameKind.MasterDark or FrameKind.MasterDarkFlat;

        private sealed record Inventory(IReadOnlyList<LibraryFrame> Frames, LibraryUsage Usage, IReadOnlyList<LibraryMaster> Masters);

        // Pages through next_cursor; the first page also carries the usage and the masters.
        private static async Task<Inventory> ReadInventoryAsync(IApiClient client, string rigId, CancellationToken ct) {
            var frames = new List<LibraryFrame>();
            LibraryUsage usage = null;
            IReadOnlyList<LibraryMaster> masters = Array.Empty<LibraryMaster>();
            string cursor = null;
            for (var page = 0; page < MaxPages; page++) {
                var r = await client.GetLibraryAsync(rigId, PageSize, cursor, ct).ConfigureAwait(false);
                if (page == 0) { usage = r.Usage; masters = r.Masters; }
                frames.AddRange(r.Frames);
                if (string.IsNullOrEmpty(r.NextCursor)) break;
                cursor = r.NextCursor;
            }
            return new Inventory(frames, usage ?? new LibraryUsage(), masters);
        }

        private static string ProblemText(ApiException ex) => ex.Code switch {
            ApiCodes.NotAuthenticated or ApiCodes.TokenInvalid or ApiCodes.SessionExpired or ApiCodes.TokenScope => "Sign in to Rapid Stacker Collab again to sync your calibration library.",
            ApiCodes.NetworkError => "Rapid Stacker Collab could not be reached; the library sync will try again.",
            ApiCodes.NotFound => "This telescope's rig was not found on Rapid Stacker Collab; it is made again at the next join.",
            _ => $"The library sync could not run ({ex.Code}): {ex.Message}",
        };

        private static string Ago(TimeSpan span) =>
            span.TotalMinutes < 1 ? "just now"
            : span.TotalHours < 1 ? $"{(int)span.TotalMinutes} min ago"
            : span.TotalDays < 1 ? $"{(int)span.TotalHours} h ago"
            : $"{(int)span.TotalDays} d ago";

        private static string FormatBytes(long bytes) => bytes switch {
            >= 1L << 30 => string.Create(CultureInfo.InvariantCulture, $"{bytes / (double)(1L << 30):0.#} GiB"),
            >= 1L << 20 => string.Create(CultureInfo.InvariantCulture, $"{bytes / (double)(1L << 20):0.#} MiB"),
            _ => string.Create(CultureInfo.InvariantCulture, $"{bytes / 1024.0:0.#} KiB"),
        };

        private static TaskCompletionSource NewWake() => new(TaskCreationOptions.RunContinuationsAsynchronously);
    }
}

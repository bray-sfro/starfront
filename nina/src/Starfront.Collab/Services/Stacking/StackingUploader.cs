using System.IO;
using System.Threading.Channels;
using NINA.Core.Utility;
using NINA.WPF.Base.Interfaces.Mediator;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Frames;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Upload;
using RapidStackerCollab.Core.Util;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Live stacking: everything that sends a saved light to Rapid Stacker
    /// Collab, behind the one setting that turns it on. With the setting off
    /// nothing here is started, opened, hooked or connected, and the plugin is
    /// the plugin it was before. With it on: the links learn their join codes
    /// from each check-in and join; every light the Starfront container
    /// saves is looked up by its target name, exactly as the ledger does, and
    /// queued for its collaboration's stacking project (unless it was pointed
    /// at another target); flats follow the night's lights to their projects
    /// (<see cref="StackingFlats"/>); darks, bias and dark flats go to the
    /// telescope's calibration library, which a folder is also kept in step
    /// with (<see cref="StackingLibrary"/>); a runner sends the queue when the
    /// policy allows, through a copy with the observer's location removed;
    /// and the verdicts come back into the queue for the dock to show.
    /// Turning the setting off stops all of it without a restart. Nothing in
    /// here can stop a night: every entry N.I.N.A. calls catches everything
    /// and logs.
    /// </summary>
    public sealed class StackingUploader : IDisposable {
        private readonly object gate = new();
        private readonly object clientsGate = new();
        private readonly object notedGate = new();
        private readonly HostServices host;
        private readonly StarfrontSettings settings;
        private readonly FrameReporter reporter;
        private readonly StarfrontSession session;
        private readonly HashSet<string> unlinkedNoted = new(StringComparer.Ordinal);
        private StackingClients clients;
        private bool disposed;

        // The running parts; null while the setting is off.
        private CancellationTokenSource cts;
        private UploadQueue queue;
        private BandwidthLimiter limiter;
        private UploadRunner runner;
        private StackingIntake intake;
        private StackingFlats flats;
        private StackingLibrary library;
        private VerdictPoller verdicts;
        private CameraStateConsumer cameraConsumer;
        private Channel<(SavedFrame Frame, StackingLink Link)> frames;
        private readonly List<Task> tasks = new();

        public StackingUploader(HostServices host, StarfrontSettings settings, FrameReporter reporter, StarfrontSession session) {
            this.host = host;
            this.settings = settings;
            this.reporter = reporter;
            this.session = session;
            Links = new StackingLinks(Path.Combine(settings.DataFolder, "stacking", "links.json"));
            Imaging = new ImagingStateTracker();
            SignIn = new StackingSignIn(settings, () => Api);
            Joiner = new StackingJoiner(host, settings, Links, () => Api, link => EnsureClients().For(link), () => EnsureClients().Cloud);
            Joiner.AuthRequired += (_, _) => OnAuthRequired();
            Joiner.MembershipConfirmed += OnMembershipConfirmed;
            SignIn.Changed += OnSignInChanged;
            Links.Changed += (_, _) => RaiseChanged();
            settings.PropertyChanged += OnSettingChanged;
        }

        public StackingSignIn SignIn { get; }
        public StackingLinks Links { get; }

        /// <summary>The calibration library's sync; null while the setting is off.</summary>
        public StackingLibrary Library => library;
        public StackingJoiner Joiner { get; }
        public ImagingStateTracker Imaging { get; }

        /// <summary>Whether the setting is on and the parts are running.</summary>
        public bool Running { get; private set; }

        public string DataFolder => Path.Combine(settings.DataFolder, "stacking");
        public string TempDirectory => Path.Combine(DataFolder, "tmp");

        /// <summary>The file being sent and how far it is, for the dock; null between files.</summary>
        public (QueueItem Item, UploadProgress Progress)? Current { get; private set; }

        /// <summary>The last file that finished and how, for the dock.</summary>
        public string LastResult { get; private set; } = "";

        /// <summary>Why the runner is waiting, when it is.</summary>
        public PolicyDecision Decision => runner?.LastDecision;

        /// <summary>A hub link holds a grant: its project can be talked to with no Rapid Stacker Collab sign-in.</summary>
        public bool HasHubLink => Links.All().Any(l => l.IsHub && l.Grant.Length > 0);

        /// <summary>Whether rig- and library-scoped calls have somewhere to go: the cloud when signed in, the hub when it is the default backend and a link holds a grant.</summary>
        private bool LibraryReachable => settings.StackingSignedIn || (settings.StackingDefaultBackend == "hub" && HasHubLink);

        public bool AuthRequired => runner?.AuthRequired ?? SignIn.AuthRequired;

        /// <summary>Raised on any thread when something the dock or the options page shows has changed.</summary>
        public event EventHandler Changed;

        /// <summary>How many rows the queue holds in each state; empty while off.</summary>
        public IReadOnlyDictionary<string, int> Counts() {
            var q = queue;
            if (q == null) return new Dictionary<string, int>();
            try { return q.CountsByState(); } catch (Exception) { return new Dictionary<string, int>(); }
        }

        /// <summary>The dock's calibration line: flats waiting for their night's lights, and the library; empty when there is nothing to say.</summary>
        public string CalibrationText() {
            try {
                var parts = new List<string>();
                if (flats?.Waiting is int waiting && waiting > 0) parts.Add(waiting == 1 ? "1 flat waiting for its night's lights." : $"{waiting} flats waiting for their night's lights.");
                if (library?.DockText() is string text && text.Length > 0) parts.Add(text);
                return string.Join(" ", parts);
            } catch (Exception) {
                return "";
            }
        }

        /// <summary>The API client, made on first use. Only called while the setting is on.</summary>
        internal IApiClient Api {
            get {
                lock (clientsGate) {
                    if (disposed) throw new ObjectDisposedException(nameof(StackingUploader));
                    clients ??= new StackingClients(settings, Links);
                    return clients.Router;
                }
            }
        }

        private StackingClients EnsureClients() {
            lock (clientsGate) return clients ??= new StackingClients(settings, Links);
        }

        private void DropClients() {
            StackingClients old;
            lock (clientsGate) { old = clients; clients = null; }
            old?.Dispose();
        }

        // -- lifetime ------------------------------------------------------------

        /// <summary>Bring the parts in line with the setting. Idempotent; safe from any thread.</summary>
        public void Apply() {
            if (settings.StackingEnabled) Start(); else Stop();
        }

        private void Start() {
            lock (gate) {
                if (disposed || Running) return;
                try {
                    NativeSqlite.Preload();
                    Directory.CreateDirectory(TempDirectory);
                    Links.Load();
                    cts = new CancellationTokenSource();
                    queue = new UploadQueue(Path.Combine(DataFolder, "queue.db"), SystemClock.Instance);
                    var recovered = queue.RecoverInterrupted();
                    if (recovered > 0) Logger.Info($"Starfront stacking: {recovered} upload(s) interrupted last time will resume");
                    var connections = EnsureClients();
                    limiter = new BandwidthLimiter(SystemClock.Instance, settings.StackingBandwidthKBps);
                    var engine = new UploadEngine(connections.Router, connections.S3Http, SystemClock.Instance, limiter);
                    runner = new UploadRunner(queue, engine, new UploadPolicy(SystemClock.Instance), settings.StackingPolicy, CurrentImagingState,
                                              SystemClock.Instance, new UploadRunnerOptions {
                                                  TempDirectory = TempDirectory,
                                                  StripPredicate = item => settings.StackingStripLocation || Links.RequireStrip(item.ProjectRef),
                                              });
                    runner.OnProgress = (item, progress) => { Current = (item, progress); RaiseChanged(); };
                    runner.OnItemFinished = OnItemFinished;
                    runner.OnAuthRequired = OnAuthRequired;
                    intake = new StackingIntake(queue, settings, TimeZoneInfo.Local, LocalRigKey);
                    intake.Enqueued += _ => runner.Wake();
                    flats = new StackingFlats(queue, new FlatProjects(this), LocalRigKey, SystemClock.Instance, TimeZoneInfo.Local,
                                              message => Logger.Info(message), (message, ex) => Logger.Error(message, ex));
                    intake.Enqueued += flats.OnLightEnqueued;
                    flats.Enqueued += _ => runner.Wake();
                    library = new StackingLibrary(() => Api, queue, new LibrarySettings(settings, LibraryRigId), SystemClock.Instance, () => LibraryReachable, TempDirectory,
                                                  message => Logger.Info(message), (message, ex) => Logger.Error(message, ex));
                    library.Queued += runner.Wake;
                    library.Changed += RaiseChanged;
                    verdicts = new VerdictPoller(() => Api, queue);
                    Imaging.Changed += runner.Wake;
                    frames = Channel.CreateUnbounded<(SavedFrame, StackingLink)>(new UnboundedChannelOptions { SingleReader = true });
                    cameraConsumer = new CameraStateConsumer(Imaging);
                    host.Camera.RegisterConsumer(cameraConsumer);
                    host.ImageSave.ImageSaved += OnImageSaved;
                    session.Updated += OnSessionUpdated;
                    var token = cts.Token;
                    tasks.Clear();
                    tasks.Add(Task.Run(() => DrainAsync(frames.Reader, token)));
                    tasks.Add(Task.Run(() => RunnerLoopAsync(runner, token)));
                    tasks.Add(Task.Run(() => MaintenanceLoopAsync(token)));
                    tasks.Add(Task.Run(() => VerdictLoopAsync(token)));
                    tasks.Add(Task.Run(() => FlatsLoopAsync(token)));
                    tasks.Add(Task.Run(() => library.RunAsync(token)));
                    Running = true;
                } catch (Exception ex) {
                    Logger.Error("Starfront stacking could not start; it is off until N.I.N.A. is restarted or the setting is turned off and on", ex);
                    TearDownParts();
                    return;
                }
            }
            Logger.Info("Starfront stacking: on" + (settings.StackingSignedIn || HasHubLink ? "" : "; not signed in to Rapid Stacker Collab yet (hub projects need no sign-in)"));
            LogPauses();
            _ = ReconcileAsync();
            RaiseChanged();
        }

        /// <summary>Stop everything, waiting a bounded time for the loops, and drop the connections. Safe to call when nothing runs.</summary>
        public void Stop() {
            lock (gate) {
                if (!Running) {
                    DropClients();
                    return;
                }
                Running = false;
                TearDownParts();
            }
            SignIn.Cancel();
            Logger.Info("Starfront stacking: off");
            RaiseChanged();
        }

        /// <summary>Under the lock. Unhook, cancel, wait, dispose, in that order, so nothing lands in a queue being closed.</summary>
        private void TearDownParts() {
            try { host.ImageSave.ImageSaved -= OnImageSaved; } catch (Exception) { }
            try { session.Updated -= OnSessionUpdated; } catch (Exception) { }
            if (cameraConsumer != null) { try { host.Camera.RemoveConsumer(cameraConsumer); } catch (Exception) { } }
            if (runner != null) Imaging.Changed -= runner.Wake;
            if (library != null) library.Changed -= RaiseChanged;
            try { frames?.Writer.TryComplete(); } catch (Exception) { }
            try { cts?.Cancel(); } catch (Exception) { }
            foreach (var task in tasks) {
                try { task.Wait(TimeSpan.FromSeconds(5)); } catch (Exception) { }
            }
            tasks.Clear();
            try { queue?.Dispose(); } catch (Exception) { }
            try { cts?.Dispose(); } catch (Exception) { }
            cts = null; queue = null; limiter = null; runner = null; intake = null; flats = null; library = null; verdicts = null; cameraConsumer = null; frames = null;
            Current = null;
            DropClients();
        }

        public void Dispose() {
            settings.PropertyChanged -= OnSettingChanged;
            Stop();
            lock (clientsGate) disposed = true;
        }

        // -- what N.I.N.A. and the session call ------------------------------------

        /// <summary>
        /// Every saved image, on a thread-pool thread after the file is at its
        /// final path. A light is matched to its collaboration by the target
        /// name N.I.N.A. stamped, exactly as the ledger does it, and posted to
        /// the intake; a light of anything else is posted too while flats are
        /// sent, only so its night is remembered (that night's flats are its
        /// target's, not a collaboration's). A calibration frame is posted
        /// with the link of the last light sent, calibration having no target
        /// of its own; the intake sends it to the flat router, the library or
        /// that project. Cheap, and never throws.
        /// </summary>
        private void OnImageSaved(object sender, ImageSavedEventArgs args) {
            try {
                Imaging.ImageSaved();
                var path = args.PathToImage?.LocalPath;
                if (string.IsNullOrEmpty(path)) return;
                var type = args.MetaData?.Image?.ImageType ?? "";
                StackingLink link;
                if (type == "LIGHT") {
                    var context = reporter.Lookup(args.MetaData?.Target?.Name);
                    link = context == null ? null : Links.Lookup(context.ProjectId);
                    if (context != null && (link == null || !link.Feeds) && NoteOnce(context.ProjectId)) {
                        Logger.Info($"Starfront stacking: {context.ProjectName} is not linked to a stacking project"
                                    + (link == null ? "" : $" ({link.Describe()})") + "; its lights stay here");
                    }
                    if (link == null || !link.Feeds) {
                        if (!settings.StackingUploadFlats) return;   // nothing would read its night
                        link = null;
                    }
                } else {
                    link = Links.Lookup(reporter.LastProject);
                }
                var frame = new SavedFrame(path, args.Filter, args.Duration, args.IsBayered, args.FileType.ToString());
                frames?.Writer.TryWrite((frame, link));
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: a saved frame could not be handed to the uploader", ex);
            }
        }

        /// <summary>Say a thing once per session, not once per frame.</summary>
        private bool NoteOnce(string key) {
            lock (notedGate) return unlinkedNoted.Add(key);
        }

        private void OnSessionUpdated(object sender, EventArgs e) {
            _ = ReconcileAsync();
            runner?.Wake();
        }

        private async Task ReconcileAsync() {
            var token = cts?.Token ?? CancellationToken.None;
            try {
                await Joiner.ReconcileAsync(session.Projects, token).ConfigureAwait(false);
                // The check-in brought fresh grants: hub projects paused for a refused grant go again.
                var q = queue;
                if (q != null && HubAuth.ResumeRefreshed(q, Links.ByCollabProject) > 0) runner?.Wake();
                var lanClients = clients;
                if (lanClients != null) await lanClients.Lan.CheckAsync(Links.All(), token).ConfigureAwait(false);
            } catch (OperationCanceledException) {
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: the links could not be brought up to date", ex);
            }
            runner?.Wake();
        }

        private void OnSignInChanged(object sender, EventArgs e) {
            if (settings.StackingSignedIn) {
                runner?.ClearAuthRequired();
                _ = ReconcileAsync();
            }
            library?.Wake();
            runner?.Wake();
            RaiseChanged();
        }

        /// <summary>The rig id of the backend that carries rig- and library-scoped calls (decision D001): a hub link's when the default is the hub, else the cloud's. Blank when it has none.</summary>
        private string LibraryRigId() => RigIds.ForDefaultBackend(settings.StackingDefaultBackend, settings.StackingRigId, Links.All());

        /// <summary>The key lights and flats are grouped under, so a hub-only telescope has one too.</summary>
        private string LocalRigKey() => RigIds.LocalKey(settings.StackingDefaultBackend, settings.StackingRigId, Links.All());

        /// <summary>
        /// A backend read the membership as active: a "not a member" pause recorded before that read is answered, and
        /// goes. Without this a single not_member held a project's uploads for good, across restarts and reinstalls.
        /// </summary>
        private void OnMembershipConfirmed(StackingLink link, DateTimeOffset asked) {
            var q = queue;
            if (q == null || link == null) return;
            try {
                var lifted = MembershipPauses.ResumeConfirmed(q, link.CollabProjectId, asked);
                if (lifted.Count == 0) return;
                foreach (var pause in lifted)
                    Logger.Info($"Starfront stacking: {link.CollabName} reads this telescope as an active member, so its uploads go again (they were {MembershipPauses.Describe(pause)})");
                runner?.Wake();
                RaiseChanged();
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: a membership pause could not be lifted", ex);
            }
        }

        /// <summary>Every project whose uploads are held, said once at start: a held project still takes frames into the queue, so the queued lines alone look healthy.</summary>
        private void LogPauses() {
            try {
                foreach (var pause in queue?.ActivePauses() ?? Array.Empty<ProjectPause>())
                    Logger.Warning($"Starfront stacking: uploads to {NameOf(pause.ProjectRef)} are {MembershipPauses.Describe(pause)}");
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: the paused projects could not be read", ex);
            }
        }

        /// <summary>A frame was queued for a held project: say so once per pause, not once per frame.</summary>
        private void WarnIfPaused(StackingLink link) {
            var q = queue;
            if (q == null || link == null || link.CollabProjectId.Length == 0) return;
            var pause = q.ActivePauses().FirstOrDefault(p => p.ProjectRef == link.CollabProjectId);
            if (pause == null || !NoteOnce($"pause:{pause.ProjectRef}:{pause.PausedAt.ToUnixTimeMilliseconds()}")) return;
            Logger.Warning($"Starfront stacking: frames for {NameOf(pause.ProjectRef)} are queued but not sent: its uploads are {MembershipPauses.Describe(pause)}");
        }

        private string NameOf(string projectRef) {
            var link = Links.ByCollabProject(projectRef);
            return link == null || link.CollabName.Length == 0 ? projectRef : $"{link.CollabName} ({projectRef})";
        }

        /// <summary>Only the cloud sign-in lands here: a hub's refusal never reaches the runner as one (the router makes it a project pause).</summary>
        private void OnAuthRequired() {
            SignIn.MarkAuthRequired();
            RaiseChanged();
        }

        private void OnItemFinished(QueueItem item, UploadResult result) {
            Current = null;
            LastResult = result.Outcome switch {
                UploadOutcome.Uploaded => $"{item.Filename} sent" + (result.Resumed ? " (resumed)" : ""),
                UploadOutcome.Duplicate => $"{item.Filename} was already there",
                UploadOutcome.RetryLater => $"{item.Filename} will be tried again: {result.Message}",
                UploadOutcome.ProjectBlocked => $"{item.Filename} is waiting: {result.Message}",
                UploadOutcome.AuthRequired => "sign in to Rapid Stacker Collab again",
                _ => $"{item.Filename} failed: {result.Message}",
            };
            switch (result.Outcome) {
                case UploadOutcome.Uploaded: Logger.Info($"Starfront stacking: sent {item.Filename}"); break;
                case UploadOutcome.Duplicate: Logger.Info($"Starfront stacking: {item.Filename} was already on the server"); break;
                case UploadOutcome.Failed: Logger.Warning($"Starfront stacking: {item.Filename} will not be sent - {result.Message}"); break;
                default: Logger.Info($"Starfront stacking: {item.Filename} {result.Outcome} - {result.Message}"); break;
            }
            RaiseChanged();
        }

        // -- the buttons -----------------------------------------------------------

        /// <summary>The projects the queue is holding, for the dock; empty while off. A held project still takes frames into the queue, so without this list the dock showed them only as "waiting".</summary>
        public IReadOnlyList<HeldProject> HeldProjects() {
            var q = queue;
            if (q == null) return Array.Empty<HeldProject>();
            try {
                return q.ActivePauses().Select(p => new HeldProject(p.ProjectRef, MembershipPauses.DockText(p, ShortName(p.ProjectRef)))).ToList();
            } catch (Exception) {
                return Array.Empty<HeldProject>();
            }
        }

        /// <summary>
        /// The dock's Resume button: lift whatever pause holds one project, and send its lights again now. If the
        /// cause is still there, the next upload says so and the project is held again, so this cannot do harm.
        /// </summary>
        public void ResumeProject(string projectRef) {
            var q = queue;
            if (q == null || string.IsNullOrEmpty(projectRef)) return;
            try {
                var pause = q.ActivePauses().FirstOrDefault(p => p.ProjectRef == projectRef);
                if (pause == null) return;
                q.ResumeProject(projectRef);
                Logger.Info($"Starfront stacking: uploads to {NameOf(projectRef)} resumed from the dock (they were {MembershipPauses.Describe(pause)})");
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: the project could not be resumed", ex);
                return;
            }
            runner?.Wake();
            RaiseChanged();
        }

        private string ShortName(string projectRef) {
            if (LibraryRef.IsLibrary(projectRef)) return "Calibration library";
            var link = Links.ByCollabProject(projectRef);
            return link == null ? "" : link.CollabName.Length > 0 ? link.CollabName : link.StarfrontName;
        }

        public void UploadNow() {
            Imaging.RequestUploadNow();
            runner?.Wake();
        }

        public void TogglePause() {
            settings.StackingPaused = !settings.StackingPaused;
            runner?.Wake();
        }

        // -- the loops -------------------------------------------------------------

        /// <summary>Upload now lasts only while something is due, so it cannot linger and override the mode for later frames.</summary>
        private ImagingState CurrentImagingState() {
            var state = Imaging.Snapshot();
            var q = queue;
            if (state.UploadNowRequested && q != null && !(q.NextDueAt() is DateTimeOffset due && due <= DateTimeOffset.Now)) {
                Imaging.ClearUploadNow();
                state = Imaging.Snapshot();
            }
            return state;
        }

        private async Task DrainAsync(ChannelReader<(SavedFrame Frame, StackingLink Link)> reader, CancellationToken ct) {
            try {
                while (await reader.WaitToReadAsync(ct).ConfigureAwait(false)) {
                    while (reader.TryRead(out var next)) {
                        try {
                            var current = intake;
                            if (current == null) return;
                            var name = Path.GetFileName(next.Frame.Path);
                            var decision = current.Evaluate(next.Frame.Path, next.Frame.Filter, next.Link, Options());
                            switch (decision.Route) {
                                case IntakeRoute.Project:
                                    if (current.Enqueue(next.Frame.Path, decision, next.Link)?.Added == true) {
                                        Logger.Info($"Starfront stacking: {name} {decision.Reason}");
                                        WarnIfPaused(next.Link);
                                    }
                                    break;
                                case IntakeRoute.Library:
                                    if (current.EnqueueLibrary(next.Frame.Path, decision)?.Added == true) Logger.Info($"Starfront stacking: {name} {decision.Reason}");
                                    break;
                                case IntakeRoute.Flat:
                                    flats?.OnFlat(next.Frame.Path, decision.Header, immediate: false);
                                    break;
                                default:
                                    current.RecordOtherTarget(next.Frame.Path, decision);
                                    if (decision.Quiet) Logger.Debug($"Starfront stacking: not sending {name}: {decision.Reason}");
                                    else Logger.Info($"Starfront stacking: not sending {name}: {decision.Reason}");
                                    break;
                            }
                        } catch (Exception ex) {
                            Logger.Error("Starfront stacking: a saved frame could not be queued", ex);
                        }
                    }
                }
            } catch (OperationCanceledException) {
            }
        }

        /// <summary>The switches the intake follows; the library needs the rig.</summary>
        private IntakeOptions Options() =>
            new(settings.StackingUploadFlats, settings.StackingLibraryLive && LibraryRigId().Length > 0, settings.StackingIncludeCalibration);

        /// <summary>
        /// Once a minute: flats whose wait for lights is over take the
        /// fallbacks. The cadence is real time; the clock only says which
        /// flats are due.
        /// </summary>
        private async Task FlatsLoopAsync(CancellationToken ct) {
            while (!ct.IsCancellationRequested) {
                try {
                    if (flats?.Tick(DateTimeOffset.Now) > 0) {
                        runner?.Wake();
                        RaiseChanged();
                    }
                    await Task.Delay(TimeSpan.FromMinutes(1), ct).ConfigureAwait(false);
                } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
                    return;
                } catch (Exception ex) {
                    Logger.Error("Starfront stacking: the held flats could not be looked at", ex);
                    try { await Task.Delay(TimeSpan.FromMinutes(1), ct).ConfigureAwait(false); } catch (OperationCanceledException) { return; }
                }
            }
        }

        private static async Task RunnerLoopAsync(UploadRunner runner, CancellationToken ct) {
            while (!ct.IsCancellationRequested) {
                try {
                    await runner.RunAsync(ct).ConfigureAwait(false);
                    return;
                } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
                    return;
                } catch (Exception ex) {
                    // The runner is not meant to throw; if it does, say so and start it again rather than stop uploading silently.
                    Logger.Error("Starfront stacking: the upload loop failed and will restart", ex);
                    try { await Task.Delay(TimeSpan.FromSeconds(30), ct).ConfigureAwait(false); } catch (OperationCanceledException) { return; }
                }
            }
        }

        private async Task MaintenanceLoopAsync(CancellationToken ct) {
            while (!ct.IsCancellationRequested) {
                try {
                    TempCleaner.Sweep(TempDirectory, TimeSpan.FromHours(settings.StackingTempRetentionHours), DateTimeOffset.Now);
                    queue?.Purge(DateTimeOffset.Now - TimeSpan.FromDays(settings.StackingHistoryDays));
                    await Joiner.RefreshMembershipsAsync(ct).ConfigureAwait(false);
                    await Task.Delay(TimeSpan.FromMinutes(30), ct).ConfigureAwait(false);
                } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
                    return;
                } catch (Exception ex) {
                    Logger.Error("Starfront stacking: maintenance failed", ex);
                    try { await Task.Delay(TimeSpan.FromMinutes(5), ct).ConfigureAwait(false); } catch (OperationCanceledException) { return; }
                }
            }
        }

        private async Task VerdictLoopAsync(CancellationToken ct) {
            while (!ct.IsCancellationRequested) {
                try {
                    var waiting = queue?.AwaitingVerdict(1).Count > 0;
                    if (waiting && (settings.StackingSignedIn || HasHubLink)) {
                        var learnt = await verdicts.PollAsync(ct).ConfigureAwait(false);
                        if (learnt > 0) RaiseChanged();
                    }
                    await Task.Delay(waiting ? TimeSpan.FromSeconds(30) : TimeSpan.FromMinutes(5), ct).ConfigureAwait(false);
                } catch (OperationCanceledException) when (ct.IsCancellationRequested) {
                    return;
                } catch (Exception ex) {
                    Logger.Debug($"Starfront stacking: the verdicts could not be read - {ex.Message}");
                    try { await Task.Delay(TimeSpan.FromMinutes(2), ct).ConfigureAwait(false); } catch (OperationCanceledException) { return; }
                }
            }
        }

        // -- settings --------------------------------------------------------------

        private void OnSettingChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) {
            try {
                // A blank name is "everything changed": the file was loaded or
                // the profile switched. Apply is idempotent, so that is cheap.
                switch (string.IsNullOrEmpty(e.PropertyName) ? null : e.PropertyName) {
                    case nameof(StarfrontSettings.StackingEnabled):
                    case null:
                        // Off the UI thread: stopping waits for the loops.
                        Task.Run(Apply);
                        break;
                    case nameof(StarfrontSettings.StackingApiBase):
                        // New connections for a new server: the running parts hold the old ones, so they go round.
                        Task.Run(() => { Stop(); Apply(); });
                        break;
                    case nameof(StarfrontSettings.StackingRigId):
                    case nameof(StarfrontSettings.StackingLibraryFolder):
                    case nameof(StarfrontSettings.StackingLibraryPreferRaw):
                    case nameof(StarfrontSettings.StackingLibrarySyncHours):
                        library?.SettingsChanged();
                        RaiseChanged();
                        break;
                    case nameof(StarfrontSettings.StackingBandwidthKBps):
                        limiter?.SetLimit(settings.StackingBandwidthKBps);
                        runner?.Wake();
                        break;
                    case nameof(StarfrontSettings.StackingPaused):
                    case nameof(StarfrontSettings.StackingUploadMode):
                    case nameof(StarfrontSettings.StackingWindowStart):
                    case nameof(StarfrontSettings.StackingWindowEnd):
                    case nameof(StarfrontSettings.StackingIdleGraceSeconds):
                    case nameof(StarfrontSettings.StackingStripLocation):
                        runner?.Wake();
                        RaiseChanged();
                        break;
                }
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: a setting change could not be applied", ex);
            }
        }

        // -- what the flat router and the library read ------------------------------

        /// <summary>The links and the ledger, as the flat router asks about them.</summary>
        private sealed class FlatProjects : IFlatProjects {
            private readonly StackingUploader owner;

            public FlatProjects(StackingUploader owner) => this.owner = owner;

            public string Eligible(string collabProjectId, string rawFilter) {
                var link = owner.Links.ByCollabProject(collabProjectId);
                return link != null && link.Feeds && link.TakesCalibration && !link.Skips(rawFilter) ? link.CollabName : null;
            }

            public string ProjectOfTarget(string objectName) {
                if (string.IsNullOrWhiteSpace(objectName)) return null;
                var context = owner.reporter.Lookup(objectName.Trim());
                return context == null ? null : owner.Links.Lookup(context.ProjectId)?.CollabProjectId;
            }

            public string LastProject => owner.Links.Lookup(owner.reporter.LastProject)?.CollabProjectId;
        }

        /// <summary>The settings, as the library sync reads them.</summary>
        private sealed class LibrarySettings : ILibrarySettings {
            private readonly StarfrontSettings settings;

            private readonly Func<string> rigId;

            public LibrarySettings(StarfrontSettings settings, Func<string> rigId) { this.settings = settings; this.rigId = rigId; }

            public string RigId => rigId();
            public string LibraryFolder => settings.StackingLibraryFolder;
            public int LibrarySyncHours => settings.StackingLibrarySyncHours;
            public bool LibraryPreferRaw => settings.StackingLibraryPreferRaw;
        }

        private void RaiseChanged() {
            try { Changed?.Invoke(this, EventArgs.Empty); } catch (Exception ex) { Logger.Error("Starfront stacking: a change handler failed", ex); }
        }
    }
}

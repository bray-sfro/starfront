using System.ComponentModel;
using System.ComponentModel.Composition;
using System.IO;
using System.Runtime.Serialization;
using NINA.Astrometry;
using NINA.Astrometry.Interfaces;
using NINA.Core.Enum;
using NINA.Core.Locale;
using NINA.Core.Model;
using NINA.Core.Model.Equipment;
using NINA.Core.Utility;
using NINA.Core.Utility.Notification;
using NINA.Core.Utility.WindowService;
using NINA.Equipment.Interfaces;
using NINA.Equipment.Interfaces.Mediator;
using NINA.PlateSolving.Interfaces;
using NINA.Profile.Interfaces;
using NINA.Sequencer.Conditions;
using NINA.Sequencer.Container;
using NINA.Sequencer.SequenceItem;
using NINA.Sequencer.SequenceItem.FilterWheel;
using NINA.Sequencer.SequenceItem.Guider;
using NINA.Sequencer.SequenceItem.Imaging;
using NINA.Sequencer.SequenceItem.Platesolving;
using NINA.Sequencer.Trigger;
using NINA.Sequencer.Trigger.Guider;
using NINA.Sequencer.Trigger.Platesolving;
using NINA.WPF.Base.Interfaces.Mediator;
using NINA.WPF.Base.Interfaces.ViewModel;
using Newtonsoft.Json;
using Starfront.Collab.Server;
using Starfront.Collab.Services;
using Starfront.Collab.Sky;

namespace Starfront.Collab.Sequencer {

    /// <summary>A collaboration to choose in the container's editor.</summary>
    public sealed record ProjectChoice(string Id, string Name);

    /// <summary>
    /// The one instruction a N.I.N.A. user needs: drop it where a target
    /// would go, inside the autofocus, meridian-flip and dither triggers they
    /// already use. When it runs it asks the server what this telescope was
    /// dealt tonight - once; a night's deal holds for the night - and builds
    /// a panel container per dealt panel: is it up, centre (and rotate, with
    /// a rotator), switch filter, take the frames. The host's own
    /// instructions do the work, so everything around them behaves as it
    /// would around a target, and every light is credited to its panel.
    /// </summary>
    [ExportMetadata("Name", "Starfront collaboration")]
    [ExportMetadata("Description", "Shoots what the Starfront server dealt this telescope tonight - the field, or its panels of a mosaic, and the filter - at your own exposures.")]
    [ExportMetadata("Icon", "Starfront_CollabSVG")]
    [ExportMetadata("Category", "Starfront")]
    [Export(typeof(ISequenceItem))]
    [Export(typeof(ISequenceContainer))]
    [JsonObject(MemberSerialization.OptIn)]
    public class StarfrontCollaborationContainer : SequenceContainer {
        private readonly IProfileService profileService;
        private readonly ICameraMediator cameraMediator;
        private readonly ITelescopeMediator telescopeMediator;
        private readonly IImagingMediator imagingMediator;
        private readonly IImageSaveMediator imageSaveMediator;
        private readonly IImageHistoryVM imageHistoryVM;
        private readonly IFilterWheelMediator filterWheelMediator;
        private readonly IGuiderMediator guiderMediator;
        private readonly IRotatorMediator rotatorMediator;
        private readonly IDomeMediator domeMediator;
        private readonly IDomeFollower domeFollower;
        private readonly IPlateSolverFactory plateSolverFactory;
        private readonly IWindowServiceFactory windowServiceFactory;
        private readonly INighttimeCalculator nighttimeCalculator;
        private readonly IApplicationStatusMediator applicationStatusMediator;
        private readonly ISafetyMonitorMediator safetyMonitorMediator;

        private string projectId = "";
        private bool waitForTargetToRise = true;
        private string summary = "";
        private IList<string> issues = new List<string>();

        [ImportingConstructor]
        public StarfrontCollaborationContainer(IProfileService profileService,
                                               ICameraMediator cameraMediator,
                                               ITelescopeMediator telescopeMediator,
                                               IImagingMediator imagingMediator,
                                               IImageSaveMediator imageSaveMediator,
                                               IImageHistoryVM imageHistoryVM,
                                               IFilterWheelMediator filterWheelMediator,
                                               IGuiderMediator guiderMediator,
                                               IRotatorMediator rotatorMediator,
                                               IDomeMediator domeMediator,
                                               IDomeFollower domeFollower,
                                               IPlateSolverFactory plateSolverFactory,
                                               IWindowServiceFactory windowServiceFactory,
                                               INighttimeCalculator nighttimeCalculator,
                                               IApplicationStatusMediator applicationStatusMediator,
                                               ISafetyMonitorMediator safetyMonitorMediator) : base(new OnceStrategy()) {
            this.applicationStatusMediator = applicationStatusMediator;
            this.safetyMonitorMediator = safetyMonitorMediator;
            this.profileService = profileService;
            this.cameraMediator = cameraMediator;
            this.telescopeMediator = telescopeMediator;
            this.imagingMediator = imagingMediator;
            this.imageSaveMediator = imageSaveMediator;
            this.imageHistoryVM = imageHistoryVM;
            this.filterWheelMediator = filterWheelMediator;
            this.guiderMediator = guiderMediator;
            this.rotatorMediator = rotatorMediator;
            this.domeMediator = domeMediator;
            this.domeFollower = domeFollower;
            this.plateSolverFactory = plateSolverFactory;
            this.windowServiceFactory = windowServiceFactory;
            this.nighttimeCalculator = nighttimeCalculator;
            Runtime.Ensure(new HostServices(profileService, cameraMediator, telescopeMediator, filterWheelMediator,
                                            rotatorMediator, guiderMediator, imageSaveMediator, nighttimeCalculator));
        }

        private bool listening;

        /// <summary>Follow the session only while this container sits in a sequence; the palette's prototype and detached clones do not.</summary>
        private void Listen(bool on) {
            if (!Runtime.Ready || on == listening) return;
            if (on) Runtime.Session.Updated += OnSessionUpdated; else Runtime.Session.Updated -= OnSessionUpdated;
            listening = on;
        }

        private void OnSessionUpdated(object sender, EventArgs e) {
            Ui.Dispatch.OnUi(() => {
                RaisePropertyChanged(nameof(ProjectChoices));
                RaisePropertyChanged(nameof(TonightText));
            });
        }

        // -- what is saved -------------------------------------------------------

        /// <summary>One collaboration to shoot, or blank for every one this telescope has joined.</summary>
        [JsonProperty]
        public string ProjectId {
            get => projectId;
            set { projectId = value ?? ""; RaisePropertyChanged(); RaisePropertyChanged(nameof(TonightText)); }
        }

        /// <summary>When a dealt field is still below the floor, sit until it rises
        /// rather than ending the container and leaving a gap in the sequence.
        /// On by default; a mosaic still shoots the fields that are already up first.</summary>
        [JsonProperty(DefaultValueHandling = DefaultValueHandling.Populate)]
        [DefaultValue(true)]
        public bool WaitForTargetToRise {
            get => waitForTargetToRise;
            set { waitForTargetToRise = value; RaisePropertyChanged(); }
        }

        [OnDeserialized]
        public void OnDeserializedStarfront(StreamingContext context) {
            // The panels are tonight's, built when the container runs. Ones
            // written into a saved sequence are last night's and are dropped.
            OnUiThread(() => {
                foreach (var item in Items.ToList()) {
                    item.AttachNewParent(null);
                    Items.Remove(item);
                }
            });
        }

        /// <summary>
        /// Run something that changes the item list on the thread that owns
        /// the window. The sequencer runs a container on a worker thread, and
        /// the item list is bound to the sequence editor: a change from any
        /// other thread is refused by WPF outright ("This type of
        /// CollectionView does not support changes to its SourceCollection
        /// from a thread different from the Dispatcher thread"), which is how
        /// the first beta night ended with the container failing before its
        /// first frame and N.I.N.A. carrying on with the user's own target.
        /// </summary>
        private static void OnUiThread(Action action) {
            var dispatcher = System.Windows.Application.Current?.Dispatcher;
            if (dispatcher == null || dispatcher.CheckAccess()) {
                action();
            } else {
                dispatcher.Invoke(action);
            }
        }

        // -- what is shown -------------------------------------------------------

        public string Summary {
            get => summary;
            private set { summary = value ?? ""; RaisePropertyChanged(); }
        }

        public new IList<string> Issues {
            get => issues;
            private set { issues = value; RaisePropertyChanged(); }
        }

        public IList<ProjectChoice> ProjectChoices {
            get {
                var choices = new List<ProjectChoice> { new("", "Every collaboration I have joined") };
                if (Runtime.Ready) {
                    foreach (var group in Runtime.Session.Tasks.Where(t => t.Live).GroupBy(t => t.Project)) {
                        choices.Add(new ProjectChoice(group.Key, group.First().ProjectName));
                    }
                }
                if (ProjectId.Length > 0 && choices.All(c => c.Id != ProjectId)) choices.Add(new ProjectChoice(ProjectId, $"{ProjectId} (not on this telescope's list yet)"));
                return choices;
            }
        }

        /// <summary>What the last check-in dealt, before the run reads it for real.</summary>
        public string TonightText {
            get {
                if (!Runtime.Ready) return "Starfront is not set up";
                if (!Runtime.Settings.Enrolled) return "Sign in with Discord under Options > Plugins > Starfront";
                if (Status == SequenceEntityStatus.RUNNING && Summary.Length > 0) return Summary;
                var deals = Runtime.Session.DealTonight(ProjectId);
                if (deals.Count == 0) {
                    return Runtime.Session.Tasks.Count == 0
                        ? "No collaboration joined yet; join one on the Starfront panel of the Imaging tab"
                        : "Nothing dealt for tonight yet; the deal is read when the sequence runs";
                }
                return string.Join(" · ", deals.Select(d => $"{d.ProjectName}: {StarfrontSession.Describe(d)}"));
            }
        }

        // -- running -----------------------------------------------------------------

        public override async Task Execute(IProgress<ApplicationStatus> progress, CancellationToken token) {
            ClearRuntimeChildren();
            if (!Runtime.Ready || !Runtime.Settings.Enrolled) {
                throw new SequenceEntityFailedException("Starfront: sign in with Discord under Options > Plugins > Starfront first");
            }
            var session = Runtime.Session;
            var settings = Runtime.Settings;
            var reporter = Runtime.Reporter;
            progress?.Report(new ApplicationStatus { Status = "Starfront: asking what to shoot tonight" });
            try {
                await session.PollAsync(token);
            } catch (ServerException ex) {
                if (session.DealTonight(ProjectId).Count == 0) {
                    throw new SequenceEntityFailedException($"Starfront: {ex.Message}");
                }
                Logger.Warning($"Starfront: {ex.Message}; shooting the deal from the last check-in");
            }
            var deals = session.DealTonight(ProjectId);
            var plans = PlanPanels(deals, settings);
            if (plans.Count == 0) {
                Summary = deals.Count == 0 ? "Nothing dealt to this telescope tonight" : "The deal has no frames for this telescope tonight";
                Logger.Info($"Starfront: {Summary}");
                Notification.ShowInformation($"Starfront: {Summary}");
                return;
            }
            Order(plans);
            // A camera that cannot turn shoots at whatever angle it sits, and the
            // server lays its panels along that angle - as last reported. Before
            // the first panel, make sure the angle is the real one, and if the
            // panels were laid for another, have them laid again now rather than
            // shoot a night on a grid the camera cannot cover.
            if (await ReconcileCameraAngleAsync(plans, settings, progress, token)) {
                deals = session.DealTonight(ProjectId);
                plans = PlanPanels(deals, settings);
                if (plans.Count == 0) {
                    Summary = "Nothing dealt to this telescope tonight after the panels were laid again";
                    Logger.Info($"Starfront: {Summary}");
                    return;
                }
                Order(plans);
            }
            // Built here, added on the window's thread: see OnUiThread.
            var panels = plans.Select(plan => BuildPanel(plan, settings, reporter)).ToList();
            OnUiThread(() => { foreach (var panel in panels) Add(panel); });
            WatchPanels();
            PublishPlan();
            var totalHours = plans.Sum(p => p.Seconds) / 3600.0;
            Summary = $"{string.Join(" · ", deals.Select(d => $"{d.ProjectName}: {StarfrontSession.Describe(d)}"))} · about {totalHours:0.0} h";
            RaisePropertyChanged(nameof(TonightText));
            Logger.Info($"Starfront: tonight - {Summary}");
            foreach (var plan in plans) Logger.Info($"Starfront:   {plan.TargetName}: {plan.FramesText}"
                                                    + (plan.HoursUntilUp is double wait && wait > 0 ? $" (up in {wait:0.0} h)" : ""));

            session.Run.Running = true;
            session.Run.Summary = Summary;
            try {
                await base.Execute(progress, token);
                // Panels that were not up when their turn came get another
                // turn once the rest are done, while any has risen.
                for (var pass = 0; pass < 2; pass++) {
                    token.ThrowIfCancellationRequested();
                    var again = Items.OfType<StarfrontPanel>().Where(p => p.SkippedForAltitude && IsUpNow(p.Plan)).ToList();
                    if (again.Count == 0) break;
                    Logger.Info($"Starfront: coming back to {again.Count} field(s) that have risen");
                    foreach (var panel in again) panel.ResetAll();
                    await base.Execute(progress, token);
                }
                // After the fields that are already up have been tried, sit for
                // any that will still rise tonight, rather than ending and
                // leaving a hole in the sequence.
                await WaitForRiseAsync(progress, token);
            } finally {
                session.Run.Running = false;
                session.Run.TargetName = null;
                session.Run.ProjectId = null;
                PublishPlan();
                var done = Items.OfType<StarfrontPanel>().Count(p => p.Status == SequenceEntityStatus.FINISHED && !p.SkippedForAltitude);
                var skipped = Items.OfType<StarfrontPanel>().Count(p => p.SkippedForAltitude);
                var unit = plans.All(p => p.SingleField) ? (plans.Count == 1 ? "field" : "fields") : "panels";
                Summary = plans.Count == 1 && done == 1 ? $"the {unit} was shot"
                        : $"{done} of {plans.Count} {unit} shot" + (skipped > 0 ? $", {skipped} never rose above the floor" : "");
                Logger.Info($"Starfront: {Summary}");
                // On a token of its own: a run that was stopped arrives here
                // with the sequence's token already cancelled, and the frames
                // shot since the last check-in would wait for the next one.
                // Half a minute is plenty for one small request.
                using var grace = new CancellationTokenSource(TimeSpan.FromSeconds(30));
                try { await session.ReportPendingAsync(grace.Token); } catch (Exception ex) { StarfrontClient.Log("reporting the night", ex); }
            }
        }

        /// <summary>
        /// For a camera without a rotator: learn its angle from one plate solve
        /// at the first panel if it is not known or the panels were laid for a
        /// different one, tell the server, and re-read the deal. True when the
        /// deal was re-read and the plans must be built again.
        /// </summary>
        private async Task<bool> ReconcileCameraAngleAsync(List<PanelPlan> plans, Settings.StarfrontSettings settings,
                                                            IProgress<ApplicationStatus> progress, CancellationToken token) {
            if (RigProfile.HasRotator(Runtime.Host)) return false;
            // A single field is shot at whatever angle the camera sits; only a
            // mosaic's grid depends on it. Solving here cost a slew and a
            // minute, then warned twice about panels that were never laid.
            if (plans.All(p => p.SingleField)) return false;
            var laidFor = plans[0].Cell.Rotation;
            var known = settings.CameraAngle;
            if (!double.IsNaN(known) && RigProfile.HalfTurnDifference(known, laidFor) <= 2.0) return false;

            var first = plans.FirstOrDefault(p => p.HoursUntilUp is 0.0) ?? plans[0];
            Logger.Info($"Starfront: the panels were laid for a camera at {laidFor:0.0}° and the camera's angle is "
                        + (double.IsNaN(known) ? "not measured yet" : $"{known:0.0}°") + "; solving once at the first panel to be sure");
            progress?.Report(new ApplicationStatus { Status = "Starfront: measuring the camera's angle" });
            if (telescopeMediator.GetInfo().AtPark) throw new SequenceEntityFailedException("Starfront: the mount is parked");

            double measured;
            var stoppedGuiding = false;
            try {
                stoppedGuiding = await guiderMediator.StopGuiding(token);
                await telescopeMediator.SlewToCoordinatesAsync(first.Coordinates, token);
                var plateSolveSettings = profileService.ActiveProfile.PlateSolveSettings;
                var solver = plateSolverFactory.GetCaptureSolver(plateSolverFactory.GetPlateSolver(plateSolveSettings),
                                                                 plateSolverFactory.GetBlindSolver(plateSolveSettings),
                                                                 imagingMediator, filterWheelMediator);
                var parameter = new NINA.PlateSolving.CaptureSolverParameter {
                    Attempts = plateSolveSettings.NumberOfAttempts,
                    Binning = plateSolveSettings.Binning,
                    Coordinates = telescopeMediator.GetCurrentPosition(),
                    DownSampleFactor = plateSolveSettings.DownSampleFactor,
                    FocalLength = profileService.ActiveProfile.TelescopeSettings.FocalLength,
                    MaxObjects = plateSolveSettings.MaxObjects,
                    PixelSize = profileService.ActiveProfile.CameraSettings.PixelSize,
                    ReattemptDelay = TimeSpan.FromMinutes(plateSolveSettings.ReattemptDelay),
                    Regions = plateSolveSettings.Regions,
                    SearchRadius = plateSolveSettings.SearchRadius,
                    BlindFailoverEnabled = plateSolveSettings.BlindFailoverEnabled,
                };
                var sequence = new NINA.Equipment.Model.CaptureSequence(plateSolveSettings.ExposureTime, "SNAPSHOT", plateSolveSettings.Filter,
                                                                         new BinningMode(plateSolveSettings.Binning, plateSolveSettings.Binning), 1) {
                    Gain = plateSolveSettings.Gain,
                };
                var result = await solver.Solve(sequence, parameter, null, progress, token);
                if (result == null || !result.Success) {
                    Logger.Warning("Starfront: the angle solve failed; shooting the panels as laid and learning the angle from the first centring");
                    return false;
                }
                measured = result.PositionAngle;
            } finally {
                if (stoppedGuiding) {
                    try { await guiderMediator.StartGuiding(false, progress, token); } catch (Exception ex) { Logger.Warning($"Starfront: guiding did not resume - {ex.Message}"); }
                }
            }

            settings.CameraAngle = Math.Round(measured, 2);
            if (RigProfile.HalfTurnDifference(measured, laidFor) <= 2.0) {
                Logger.Info($"Starfront: camera sits at {measured:0.0}°, as the panels were laid; carrying on");
                return false;
            }
            Logger.Info($"Starfront: camera sits at {measured:0.0}°, not {laidFor:0.0}°; asking the server to lay the panels again before starting");
            progress?.Report(new ApplicationStatus { Status = "Starfront: the panels are being laid again for the camera's real angle" });
            try {
                await Runtime.Session.PollAsync(token);
            } catch (ServerException ex) {
                Logger.Warning($"Starfront: {ex.Message}; shooting the panels as laid");
                return false;
            }
            var again = Runtime.Session.DealTonight(ProjectId);
            var relaid = again.Any(d => d.Cells.Count > 0 && RigProfile.HalfTurnDifference(d.Cells[0].Rotation, measured) <= 2.0);
            if (!relaid) Logger.Warning("Starfront: the server kept the panels as they were; shooting them as laid");
            return relaid;
        }

        private bool IsUpNow(PanelPlan plan) {
            var (altitude, azimuth) = SkyMath.AltAz(plan.Coordinates, profileService);
            return altitude >= Math.Max(plan.MinAltitude, SkyMath.HorizonAltitude(profileService, azimuth));
        }

        private bool IsMoonSafe(PanelPlan plan) {
            if (plan.MinMoonSeparation is double wanted && wanted > 0) {
                var (separation, moonUp) = SkyMath.MoonSeparation(plan.Coordinates, profileService);
                if (moonUp && separation < wanted) return false;
            }
            return true;
        }

        private DateTime DawnTonight() {
            try {
                var (_, dawn) = SkyMath.DarkHours(nighttimeCalculator.Calculate());
                return dawn;
            } catch (Exception ex) {
                Logger.Debug($"Starfront: dawn could not be worked out - {ex.Message}");
                var now = DateTime.Now;
                return now.Hour >= 12 ? now.Date.AddDays(1).AddHours(6) : now.Date.AddHours(6);
            }
        }

        /// <summary>Where a panel stands, and when (if at all) it will next
        /// clear the floor, sampled from now until dawn.</summary>
        private RiseWaitField RiseField(StarfrontPanel panel, DateTime now, DateTime dawn) {
            var plan = panel.Plan;
            var (altitude, azimuth) = SkyMath.AltAz(plan.Coordinates, profileService, now);
            var horizon = SkyMath.HorizonAltitude(profileService, azimuth);
            var floor = Math.Max(plan.MinAltitude, horizon);
            var up = altitude >= floor;
            DateTime? riseAt = up ? now : null;
            var willClear = up;
            var end = dawn > now ? dawn : now.AddHours(12);
            if (!up) {
                for (var t = now; t <= end; t = t.AddMinutes(10)) {
                    var (a, z) = SkyMath.AltAz(plan.Coordinates, profileService, t);
                    var f = Math.Max(plan.MinAltitude, SkyMath.HorizonAltitude(profileService, z));
                    if (a >= f) {
                        willClear = true;
                        riseAt = t;
                        break;
                    }
                }
            }
            return new RiseWaitField(plan.TargetName, plan.MinAltitude, horizon, panel.SkippedForAltitude, willClear, up, IsMoonSafe(plan), riseAt);
        }

        /// <summary>Wait until a still-skipped field is up and Moon-safe, then
        /// shoot it, repeating until dawn or nothing left will rise.</summary>
        private async Task WaitForRiseAsync(IProgress<ApplicationStatus> progress, CancellationToken token) {
            if (!WaitForTargetToRise) return;
            while (true) {
                token.ThrowIfCancellationRequested();
                var now = DateTime.Now;
                var dawn = DawnTonight();
                var fields = Items.OfType<StarfrontPanel>().Select(p => RiseField(p, now, dawn)).ToList();
                var status = RiseWait.Decide(fields, now, dawn, WaitForTargetToRise);
                if (status.Action == RiseWaitAction.Stop) {
                    if (status.Message.Length > 0) Logger.Info($"Starfront: stopped waiting - {status.Message}");
                    return;
                }
                if (status.Action == RiseWaitAction.ShootReady) {
                    var ready = Items.OfType<StarfrontPanel>().Where(p => p.SkippedForAltitude && IsUpNow(p.Plan) && IsMoonSafe(p.Plan)).ToList();
                    if (ready.Count == 0) return;
                    Logger.Info($"Starfront: {ready.Count} field(s) have risen; shooting them");
                    foreach (var panel in ready) panel.ResetAll();
                    await base.Execute(progress, token);
                    continue;
                }
                Summary = status.Message;
                if (Runtime.Ready) Runtime.Session.Run.Summary = status.Message;
                Logger.Info($"Starfront: {status.Message}");
                progress?.Report(new ApplicationStatus { Status = $"Starfront: {status.Message}" });
                await Task.Delay(RiseWait.PollInterval, token);
            }
        }

        private void ClearRuntimeChildren() {
            var stale = Items.ToList();
            foreach (var item in stale) {
                if (item is StarfrontPanel panel) {
                    Runtime.Reporter?.Forget(panel.Plan.TargetName);
                    Unwatch(panel);
                }
            }
            OnUiThread(() => {
                foreach (var item in stale) {
                    item.AttachNewParent(null);
                    Items.Remove(item);
                }
            });
            Summary = "";
            if (Runtime.Ready) Runtime.Session.Run.Plan = Array.Empty<RunItem>();
        }

        // -- what the Imaging tab shows while it runs --------------------------------

        private void WatchPanels() {
            foreach (var panel in Items.OfType<StarfrontPanel>()) {
                panel.PropertyChanged += OnRunChanged;
                foreach (var child in panel.GetItemsSnapshot()) {
                    if (child is System.ComponentModel.INotifyPropertyChanged watched) watched.PropertyChanged += OnRunChanged;
                    if (child is TakeManyExposures many) many.GetTakeExposure().PropertyChanged += OnRunChanged;
                }
            }
        }

        private void Unwatch(StarfrontPanel panel) {
            panel.PropertyChanged -= OnRunChanged;
            foreach (var child in panel.GetItemsSnapshot()) {
                if (child is System.ComponentModel.INotifyPropertyChanged watched) watched.PropertyChanged -= OnRunChanged;
                if (child is TakeManyExposures many) many.GetTakeExposure().PropertyChanged -= OnRunChanged;
            }
        }

        private void OnRunChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) {
            if (e.PropertyName == "Status" || e.PropertyName == "ExposureCount" || e.PropertyName == nameof(StarfrontPanel.SkipReason)) PublishPlan();
        }

        /// <summary>Every field of tonight's run with where it has got to, for the Starfront panel on the Imaging tab.</summary>
        private void PublishPlan() {
            if (!Runtime.Ready) return;
            var rows = new List<RunItem>();
            foreach (var panel in Items.OfType<StarfrontPanel>()) {
                var plan = panel.Plan;
                var total = plan.Filters.Sum(f => f.Frames);
                var shot = panel.GetItemsSnapshot().OfType<TakeManyExposures>().Sum(m => m.GetTakeExposure().ExposureCount);
                string state, detail;
                if (panel.SkippedForAltitude) {
                    state = "skipped";
                    detail = panel.SkipReason.Replace("skipped for now: ", "");
                } else if (panel.Status == SequenceEntityStatus.RUNNING) {
                    state = "shooting";
                    var active = panel.GetItemsSnapshot().FirstOrDefault(i => i.Status == SequenceEntityStatus.RUNNING);
                    detail = active switch {
                        AltitudeGate => "checking it is up",
                        Center => "slewing and centring",
                        SwitchFilter => "changing filter",
                        TakeManyExposures => $"frame {Math.Min(total, shot + 1)} of {total}",
                        _ => $"{shot} of {total} frames",
                    };
                } else if (panel.Status == SequenceEntityStatus.FINISHED) {
                    state = "done";
                    detail = $"{shot} of {total} frames";
                } else if (panel.Status == SequenceEntityStatus.SKIPPED || panel.Status == SequenceEntityStatus.FAILED) {
                    state = panel.Status == SequenceEntityStatus.FAILED ? "failed" : "skipped";
                    detail = shot > 0 ? $"{shot} of {total} frames" : "";
                } else {
                    state = "waiting";
                    detail = plan.HoursUntilUp is double wait && wait > 0 ? $"up in {wait:0.0} h" : "";
                }
                rows.Add(new RunItem(panel.Name, plan.FramesText, state, detail));
            }
            Runtime.Session.Run.Plan = rows;
        }

        /// <summary>
        /// Tonight's deal as panels: which cells, which filters, how many frames
        /// at which exposure - less whatever this telescope has already shot on
        /// them tonight, so a sequence started again after a restart or a cloud
        /// carries on where it left off rather than shooting the deal from the top.
        /// </summary>
        private List<PanelPlan> PlanPanels(List<DealTask> deals, Settings.StarfrontSettings settings) {
            var plans = new List<PanelPlan>();
            var wheel = settings.ProfileFilterNames();
            var tonight = Runtime.Ledger.Rows(Runtime.Session.Tonight);
            foreach (var deal in deals) {
                var wants = Runtime.Session.RequirementsFor(deal.Project);
                var frames = (deal.Visit?.Frames ?? new Dictionary<string, int>()).Where(kv => kv.Value > 0).ToList();
                if (frames.Count == 0) continue;
                var filters = new List<FilterFrames>();
                foreach (var (name, count) in frames) {
                    var canonical = Filters.Canonical(name);
                    var exposure = deal.ExposureFor(name);
                    if (exposure <= 0) exposure = settings.ExposureFor(name) ?? 0.0;
                    if (exposure <= 0) {
                        Logger.Warning($"Starfront: no exposure known for {settings.FilterName(name)} on {deal.ProjectName}; set one in the exposure table");
                        continue;
                    }
                    var wheelName = wheel.FirstOrDefault(w => Filters.Same(w, name));
                    if (wheel.Count > 0 && wheelName == null && !settings.Colour) {
                        Logger.Warning($"Starfront: this profile's filter wheel has no filter matching '{name}'; {deal.ProjectName} dealt it anyway and it is skipped");
                        continue;
                    }
                    filters.Add(new FilterFrames(canonical, wheelName, count, exposure));
                }
                if (filters.Count == 0) continue;
                foreach (var index in deal.Share.Distinct()) {
                    if (index < 0 || index >= deal.Cells.Count) continue;
                    var cell = deal.Cells[index];
                    var panelNumber = (index + 1).ToString();
                    var left = new List<FilterFrames>();
                    foreach (var block in filters) {
                        var shot = tonight.Where(r => r.Task == deal.Id && r.Panel == panelNumber && Filters.Same(r.Filter, block.Filter)).Sum(r => r.Frames);
                        if (shot >= block.Frames) {
                            Logger.Info($"Starfront: {deal.ProjectName} panel {panelNumber} already has its {block.Frames} × {block.WheelName ?? block.Filter} tonight; not shot again");
                            continue;
                        }
                        left.Add(shot > 0 ? block with { Frames = block.Frames - shot } : block);
                    }
                    if (left.Count == 0) continue;
                    var coordinates = new Coordinates(Angle.ByDegree(cell.Ra), Angle.ByDegree(cell.Dec), Epoch.J2000);
                    var targetName = deal.Kind == "single" ? Ascii(deal.ProjectName) : $"{Ascii(deal.ProjectName)} panel {index + 1}";
                    plans.Add(new PanelPlan {
                        Deal = deal, CellIndex = index, Cell = cell, Coordinates = coordinates, Filters = left,
                        MinAltitude = wants?.MinAltitude ?? 0.0, MinMoonSeparation = wants?.MinMoonSeparation,
                        TargetName = targetName,
                    });
                }
            }
            return plans;
        }

        private static string Ascii(string text) {
            var clean = new string((text ?? "").Where(c => c < 128 && !Path.GetInvalidFileNameChars().Contains(c)).ToArray()).Trim();
            return clean.Length > 0 ? clean : "Starfront";
        }

        /// <summary>
        /// Panels that are up go first, the one that sets soonest at the
        /// front; panels still to rise follow in the order they rise. A panel
        /// that never clears the floor in the next twelve hours is dropped
        /// here rather than slewed to and skipped.
        /// </summary>
        private void Order(List<PanelPlan> plans) {
            var now = DateTime.Now;
            foreach (var plan in plans) {
                double? up = null, down = null;
                for (var minutes = 0; minutes <= 12 * 60; minutes += 10) {
                    var at = now.AddMinutes(minutes);
                    var (altitude, azimuth) = SkyMath.AltAz(plan.Coordinates, profileService, at);
                    var floor = Math.Max(plan.MinAltitude, SkyMath.HorizonAltitude(profileService, azimuth));
                    var above = altitude >= floor;
                    if (above && up == null) up = minutes / 60.0;
                    if (!above && up != null && down == null) { down = minutes / 60.0; break; }
                }
                plan.HoursUntilUp = up;
                plan.HoursAbove = up.HasValue ? (down ?? 12.0) : null;
            }
            var never = plans.Where(p => p.HoursUntilUp == null).ToList();
            foreach (var plan in never) {
                Logger.Warning($"Starfront: {plan.TargetName} does not rise above its floor in the next twelve hours and is left out tonight");
                plans.Remove(plan);
            }
            var ordered = plans.OrderBy(p => p.HoursUntilUp > 0 ? 1 : 0)
                               .ThenBy(p => p.HoursUntilUp > 0 ? p.HoursUntilUp : p.HoursAbove)
                               .ThenBy(p => p.PanelNumber).ToList();
            plans.Clear();
            plans.AddRange(ordered);
        }

        private StarfrontPanel BuildPanel(PanelPlan plan, Settings.StarfrontSettings settings, FrameReporter reporter) {
            var panel = new StarfrontPanel(profileService, nighttimeCalculator, plan);
            var scale = RigProfile.Scale(Runtime.Host, settings);
            var focal = profileService.ActiveProfile.TelescopeSettings.FocalLength;
            // The frame's footprint is the camera's own field at the angle it is
            // shot, which is what the depth map is built from; the cell's size is
            // a bounding box and would be credited twice over at an angle.
            var field = RigProfile.Field(Runtime.Host, settings);
            var angle = RigProfile.HasRotator(Runtime.Host) || double.IsNaN(settings.CameraAngle) ? plan.Cell.Rotation : settings.CameraAngle;
            reporter.Register(new PanelContext {
                TargetName = plan.TargetName, ProjectId = plan.Deal.Project, ProjectName = plan.Deal.ProjectName,
                TaskId = plan.Deal.Id, PanelNumber = plan.PanelNumber, Filter = plan.Filters.First().Filter,
                Footprint = new Region {
                    Ra = plan.Cell.Ra, Dec = plan.Cell.Dec,
                    Width = field?.width ?? plan.Cell.Width, Height = field?.height ?? plan.Cell.Height,
                    Rotation = angle,
                },
                Scale = scale, FocalLength = focal > 0 ? focal : null, Colour = settings.Colour,
                Bandpass = Filters.Bandpass(plan.Filters.First().WheelName ?? ""),
                MaxHfr = Runtime.Session.RequirementsFor(plan.Deal.Project)?.MaxHfr,
            });

            panel.Add(new AltitudeGate(panel, profileService, Runtime.Session.Run));

            Center center;
            var useRotator = settings.MatchRotation && RigProfile.HasRotator(Runtime.Host);
            if (useRotator) {
                var rotate = new CenterAndRotate(profileService, telescopeMediator, imagingMediator, rotatorMediator, filterWheelMediator,
                                                 guiderMediator, domeMediator, domeFollower, plateSolverFactory, windowServiceFactory) {
                    Name = Icons.Label("Lbl_SequenceItem_Platesolving_CenterAndRotate_Name", "Slew, center and rotate"),
                    Icon = Icons.Find("PlatesolveAndRotateSVG"),
                };
                center = rotate;
            } else {
                center = new Center(profileService, telescopeMediator, imagingMediator, filterWheelMediator, guiderMediator,
                                    domeMediator, domeFollower, plateSolverFactory, windowServiceFactory) {
                    Name = Icons.Label("Lbl_SequenceItem_Platesolving_Center_Name", "Slew and center"),
                    Icon = Icons.Find("PlatesolveSVG"),
                };
            }
            center.Category = "Starfront";
            center.Attempts = 2;
            panel.Add(center);
            panel.Add(new NoteCameraAngle(center, panel, Runtime.Host, settings, reporter));

            // Guiding, the way a target container starts it after its centre.
            // N.I.N.A.'s Center resumes guiding only if it stopped it, so a
            // sequence that starts the guider inside each of its own targets
            // rather than in its Start area arrived here with the guider idle,
            // and one tester's 23 Blue subs were shot unguided at 5-7" with
            // every dither a no-op. Only with a guider connected: Start Guiding
            // on a rig without one fails the panel.
            if (guiderMediator.GetInfo().Connected) {
                var guide = HostItems.Make<StartGuiding>(guiderMediator);
                guide.ForceCalibration = false;
                guide.Name = Icons.Label("Lbl_SequenceItem_Guider_StartGuiding_Name", "Start guiding");
                guide.Icon = Icons.Find("GuiderSVG");
                guide.Category = "Starfront";
                panel.Add(guide);
            }

            foreach (var block in plan.Filters) {
                if (block.WheelName != null) {
                    var wheelFilter = profileService.ActiveProfile.FilterWheelSettings.FilterWheelFilters
                        .FirstOrDefault(f => string.Equals(f.Name, block.WheelName, StringComparison.OrdinalIgnoreCase));
                    if (wheelFilter != null) {
                        panel.Add(new SwitchFilter(profileService, filterWheelMediator) {
                            Filter = wheelFilter,
                            Name = Icons.Label("Lbl_SequenceItem_FilterWheel_SwitchFilter_Name", "Switch filter"),
                            Icon = Icons.Find("FW_NoFill_SVG"),
                            Category = "Starfront",
                        });
                    }
                }
                var many = new TakeManyExposures(profileService, cameraMediator, imagingMediator, imageSaveMediator, imageHistoryVM) {
                    Name = Icons.Label("Lbl_SequenceItem_Imaging_TakeManyExposures_Name", "Take many exposures"),
                    Icon = Icons.Find("CameraSVG"),
                    Category = "Starfront",
                };
                var take = many.GetTakeExposure();
                take.ExposureTime = block.Exposure;
                take.Gain = settings.Gain;
                take.Offset = settings.Offset;
                take.Binning = new BinningMode(settings.Binning, settings.Binning);
                take.ImageType = "LIGHT";
                many.GetLoopCondition().Iterations = block.Frames;
                panel.Add(many);
            }

            // The two triggers are built through HostItems: N.I.N.A. 3.3 gave
            // both a safety monitor parameter, and a constructor bound at
            // compile time against 3.2 is not found there.
            if (settings.DitherEvery > 0) {
                var dither = HostItems.Make<DitherAfterExposures>(guiderMediator, imageHistoryVM, profileService, safetyMonitorMediator);
                dither.AfterExposures = settings.DitherEvery;
                dither.Name = Icons.Label("Lbl_SequenceTrigger_Guider_DitherAfterExposures_Name", "Dither after exposures");
                dither.Icon = Icons.Find("DitherSVG");
                dither.Category = "Starfront";
                panel.Add(dither);
            }
            // The field's own drift check, on the field's own coordinates. A
            // "Center after drift" trigger a user puts on a container above
            // this one takes its coordinates from that container's target:
            // one tester's, on an M33 container around this, measured 889
            // arcminutes of "drift" and slewed to M33 in the middle of M31.
            if (settings.RecentreArcMinutes > 0) {
                var drift = HostItems.Make<CenterAfterDriftTrigger>(profileService, telescopeMediator, filterWheelMediator, guiderMediator,
                                                                    imagingMediator, cameraMediator, domeMediator, domeFollower,
                                                                    imageSaveMediator, applicationStatusMediator, safetyMonitorMediator);
                drift.DistanceArcMinutes = settings.RecentreArcMinutes;
                drift.AfterExposures = 1;
                drift.Name = Icons.Label("Lbl_SequenceTrigger_CenterAfterDriftTrigger_Name", "Center after drift");
                drift.Icon = Icons.Find("PlatesolveSVG");
                drift.Category = "Starfront";
                panel.Add(drift);
            }
            return panel;
        }

        // -- the usual container duties ----------------------------------------------

        public override object Clone() {
            var copy = new StarfrontCollaborationContainer(profileService, cameraMediator, telescopeMediator, imagingMediator, imageSaveMediator,
                                                           imageHistoryVM, filterWheelMediator, guiderMediator, rotatorMediator, domeMediator,
                                                           domeFollower, plateSolverFactory, windowServiceFactory, nighttimeCalculator,
                                                           applicationStatusMediator, safetyMonitorMediator) {
                ProjectId = ProjectId,
                WaitForTargetToRise = WaitForTargetToRise,
            };
            copy.CopyMetaData(this);
            foreach (var condition in Conditions) { var child = (ISequenceCondition)condition.Clone(); copy.Conditions.Add(child); child.AttachNewParent(copy); }
            foreach (var trigger in Triggers) { var child = (ISequenceTrigger)trigger.Clone(); copy.Triggers.Add(child); child.AttachNewParent(copy); }
            return copy;
        }

        public override bool Validate() {
            var list = new List<string>();
            if (!Runtime.Ready) list.Add("Starfront is not set up yet");
            else if (!Runtime.Settings.Enrolled) list.Add("Sign in with Discord under Options > Plugins > Starfront");
            else if (Runtime.Settings.Exposures.Count == 0) list.Add("Set the exposure for each filter under Options > Plugins > Starfront");
            if (!cameraMediator.GetInfo().Connected) list.Add(Loc.Instance["LblCameraNotConnected"]);
            if (!telescopeMediator.GetInfo().Connected) list.Add(Loc.Instance["LblTelescopeNotConnected"]);
            var ok = base.Validate();
            Issues = list;
            return ok && list.Count == 0;
        }

        public override void AfterParentChanged() {
            base.AfterParentChanged();
            Listen(Parent != null);
            Validate();
            RaisePropertyChanged(nameof(TonightText));
        }

        public override void Teardown() {
            base.Teardown();
            Listen(false);
        }

        /// <summary>
        /// One exposure, not the night. This is what N.I.N.A.'s triggers ask
        /// before they let an item run: "how long until I next get a look?"
        /// Take Many Exposures answers with one sub for the same reason. The
        /// container used to add up every panel's frames, and a meridian-flip
        /// trigger on the container above it, told the next instruction would
        /// take 5.8 hours, decided it had no time left and ran the whole flip
        /// routine - autofocus, recentre, settle - at the start of a night the
        /// mount was already on the right side for. The triggers above get
        /// their look again before every item inside the panels.
        /// </summary>
        public override TimeSpan GetEstimatedDuration() {
            var first = Items.OfType<StarfrontPanel>()
                .SelectMany(p => p.Plan.Filters)
                .Select(f => f.Exposure)
                .FirstOrDefault(e => e > 0);
            return TimeSpan.FromSeconds(first);
        }

        public override string ToString() => $"Category: {Category}, Container: StarfrontCollaborationContainer, Project: {(ProjectId.Length > 0 ? ProjectId : "all")}, Panels: {Items.Count}";
    }
}

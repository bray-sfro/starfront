using System.Collections.ObjectModel;
using System.ComponentModel.Composition;
using System.Windows;
using System.Windows.Media;
using CommunityToolkit.Mvvm.Input;
using RelayCommand = CommunityToolkit.Mvvm.Input.RelayCommand;
using NINA.Astrometry;
using NINA.Astrometry.Interfaces;
using NINA.Core.Enum;
using NINA.Core.Utility;
using NINA.Core.Utility.Notification;
using NINA.Equipment.Interfaces.Mediator;
using NINA.Equipment.Interfaces.ViewModel;
using NINA.Profile.Interfaces;
using NINA.WPF.Base.Interfaces.Mediator;
using NINA.WPF.Base.Interfaces.ViewModel;
using NINA.WPF.Base.ViewModel;
using Starfront.Collab.Server;
using Starfront.Collab.Services;
using Starfront.Collab.Services.Stacking;
using Starfront.Collab.Sky;

namespace Starfront.Collab.Ui {

    /// <summary>One channel's depth on a collaboration, for the bar on its card.</summary>
    public sealed record FilterProgressVM(string Name, double Percent, string Text, bool Met);

    /// <summary>One telescope online, for the table at the foot of the panel.</summary>
    public sealed record TelescopeRowVM(string Name, string Owner, string Doing, string Target, string Project);

    /// <summary>One telescope on a collaboration, for the list on its card.</summary>
    public sealed record ParticipantVM(string Name, bool Online, string Doing);

    /// <summary>One open collaboration, as the panel lists it.</summary>
    public sealed class ProjectRowVM : BaseINPC {
        public const int ThumbWidth = 320;
        public const int ThumbHeight = 200;
        private ImageSource framing;
        private string framingNote = "fetching the sky…";

        private readonly StarfrontDockVM owner;
        private bool expanded;
        private double? altitudeNow;
        private bool altitudeKnown;

        public ProjectRowVM(ProjectListing listing, DealTask task, StarfrontDockVM owner, bool expanded = false) {
            this.owner = owner;
            this.expanded = expanded;
            Listing = listing;
            Task = task;
            Mine = Runtime.Ready && listing.OwnerId.Length > 0 && listing.OwnerId == Runtime.Session.UserId;
            JoinCommand = new AsyncRelayCommand(() => owner.JoinAsync(listing.Id), () => CanJoin);
            LeaveCommand = new AsyncRelayCommand(() => owner.LeaveAsync(task?.Id), () => task != null);
            ShowInFramingCommand = new AsyncRelayCommand(() => owner.ShowInFramingAsync(listing), () => listing.Region != null);
            OpenFilesCommand = new RelayCommand(() => {
                try {
                    System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(listing.FilesUrl) { UseShellExecute = true });
                } catch (Exception ex) {
                    Logger.Error(ex);
                }
            }, () => !string.IsNullOrEmpty(listing.FilesUrl));
            EditCommand = new RelayCommand(() => owner.Start.BeginEdit(listing), () => Mine);
            CloseCommand = new AsyncRelayCommand(() => owner.CloseAsync(listing), () => Mine);
            RedealCommand = new AsyncRelayCommand(() => owner.RedealAsync(listing), () => Mine);
            ToggleCommand = new RelayCommand(() => Expanded = !Expanded);
        }

        /// <summary>Whether the card is open. The list keeps one line per collaboration until it is.</summary>
        public bool Expanded {
            get => expanded;
            set {
                if (expanded == value) return;
                expanded = value;
                RaisePropertyChanged();
                RaisePropertyChanged(nameof(Chevron));
                owner.Remember(Id, value);
                if (value) owner.LoadVisuals(this);
            }
        }

        public string Chevron => expanded ? "▾" : "▸";
        public RelayCommand ToggleCommand { get; }
        /// <summary>Whether the sky picture and the altitude chart have been fetched for this row; only open cards fetch them.</summary>
        public bool VisualsLoaded { get; set; }

        /// <summary>The one line a browser reads before opening a card.</summary>
        public string Summary {
            get {
                var region = Listing.Region;
                var size = region == null ? "" : $"{region.Width:0.0}° × {region.Height:0.0}°";
                var kind = Listing.Kind == "single" ? "single field" : "mosaic";
                var collected = (Listing.Collected ?? new Dictionary<string, double>()).Values.Sum();
                var progress = Listing.Progress ?? new Dictionary<string, ProgressFigures>();
                var done = progress.Count > 0 ? $"{progress.Values.Average(p => p.AtGoal) * 100:0}% done" : "";
                var hours = collected > 0 ? $"{collected:0.0} h collected" : "nothing collected yet";
                var settings = Runtime.Ready ? Runtime.Settings : null;
                var wants = string.Join(" ", (Listing.Goals ?? new Dictionary<string, double>()).Keys
                    .Select(k => settings != null ? settings.FilterName(k) : k));
                var people = Listing.Participants > 0
                    ? $"{Listing.Participants} telescope{(Listing.Participants == 1 ? "" : "s")}{(Listing.ParticipantsOnline > 0 ? $", {Listing.ParticipantsOnline} online" : "")}"
                    : "nobody on it yet";
                var up = AltitudeNow is double altitude ? $"{altitude:0}° up now" : "";
                return string.Join(" · ", new[] { kind, size, wants, done, hours, people, up }.Where(s => s.Length > 0));
            }
        }

        /// <summary>The target's altitude right now, from this site, for the sort; null without a region.</summary>
        public double? AltitudeNow {
            get {
                if (altitudeKnown) return altitudeNow;
                altitudeKnown = true;
                var region = Listing.Region;
                if (region == null || !Runtime.Ready) return altitudeNow = null;
                try {
                    var coordinates = new Coordinates(Angle.ByDegree(region.Ra), Angle.ByDegree(region.Dec), Epoch.J2000);
                    altitudeNow = SkyMath.AltAz(coordinates, Runtime.Host.Profile, DateTime.Now).altitude;
                } catch (Exception) {
                    altitudeNow = null;
                }
                return altitudeNow;
            }
        }

        /// <summary>How far along the field is on average over its filters, 0 to 1; a project with no goals counts as empty.</summary>
        public double Average {
            get {
                var progress = Listing.Progress ?? new Dictionary<string, ProgressFigures>();
                return progress.Count > 0 ? progress.Values.Average(p => p.Average) : 0.0;
            }
        }

        /// <summary>Whether a word somebody typed is anywhere on this card.</summary>
        public bool Matches(string needle) {
            if (string.IsNullOrWhiteSpace(needle)) return true;
            var words = needle.Split((char[])null, StringSplitOptions.RemoveEmptyEntries);
            var text = string.Join(" ", new[] {
                Listing.Name, Listing.Coordinator, Listing.Notes, Listing.Kind,
                string.Join(" ", (Listing.Goals ?? new Dictionary<string, double>()).Keys.Select(Sky.Filters.Label)),
                string.Join(" ", (Listing.Requirements?.Filters ?? new Dictionary<string, double?>()).Keys.Select(Sky.Filters.Label)),
                string.Join(" ", Listing.ParticipantNames ?? new List<string>()),
                Joined ? "joined" : "", Mine ? "mine yours" : "",
            }.Where(s => !string.IsNullOrEmpty(s)));
            return words.All(w => text.IndexOf(w, StringComparison.OrdinalIgnoreCase) >= 0);
        }

        public ProjectListing Listing { get; }
        public DealTask Task { get; }
        public string Id => Listing.Id;
        public string Name => Listing.Name;
        public string Coordinator => string.IsNullOrEmpty(Listing.Coordinator) ? "" : $"by {Listing.Coordinator}";
        public bool Joined => Listing.Joined || Task != null;
        public bool CanJoin => !Joined && Listing.Compatibility?.Ok == true;
        public bool Compatible => Listing.Compatibility?.Ok == true;
        /// <summary>Started by the person signed in here, so it can be edited, closed and dealt afresh from this panel.</summary>
        public bool Mine { get; }
        public AsyncRelayCommand JoinCommand { get; }
        public AsyncRelayCommand LeaveCommand { get; }
        public AsyncRelayCommand ShowInFramingCommand { get; }
        public RelayCommand OpenFilesCommand { get; }
        /// <summary>Whether the lights are stacked as they land, and how this telescope stands with that stack; blank when they are not.</summary>
        public string StackingText {
            get {
                var enabled = Runtime.Ready && Runtime.Settings.StackingEnabled;
                return StackingLine.Describe(Listing, enabled, enabled ? Runtime.Stacking.Links.Lookup(Listing.Id) : null, Joined);
            }
        }

        public void RefreshStacking() => RaisePropertyChanged(nameof(StackingText));

        /// <summary>Where the lights go, when the collaboration collects them; blank when it does not.</summary>
        public string FilesText => Listing.CollectFiles && !string.IsNullOrEmpty(Listing.FilesUrl)
            ? "Collects the lights, flats, darks and bias: send yours from the files page."
            : "";
        public RelayCommand EditCommand { get; }
        public AsyncRelayCommand CloseCommand { get; }
        public AsyncRelayCommand RedealCommand { get; }

        /// <summary>The sky with the region outlined and, once joined, this telescope's panels on it.</summary>
        public ImageSource Framing {
            get => framing;
            set { framing = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(HasFraming)); }
        }

        public bool HasFraming => framing != null;

        private ImageSource transit;
        private string transitText = "";

        /// <summary>The target's altitude through tonight, as a chart: how well placed it is at a glance.</summary>
        public ImageSource Transit {
            get => transit;
            set { transit = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(HasTransit)); }
        }

        public bool HasTransit => transit != null;

        public string TransitText {
            get => transitText;
            set { transitText = value ?? ""; RaisePropertyChanged(); }
        }

        public string FramingNote {
            get => framingNote;
            set { framingNote = value ?? ""; RaisePropertyChanged(); }
        }

        public string FramingCaption {
            get {
                if (Listing.Region == null) return "";
                var cells = Task?.Cells?.Count ?? 0;
                if (cells == 0) return "yellow: the collaboration's region";
                var mine = Task?.Share?.Count ?? 0;
                return mine > 0 ? $"yellow: the region · white: your {cells} panels · green: tonight's {mine}" : $"yellow: the region · white: your {cells} panels";
            }
        }

        public string Detail {
            get {
                var region = Listing.Region;
                var size = region == null ? "" : $"{region.Width:0.0}° × {region.Height:0.0}°";
                var kind = Listing.Kind == "single" ? "single field" : "mosaic";
                var settings = Runtime.Ready ? Runtime.Settings : null;
                var goals = string.Join(" · ", (Listing.Goals ?? new Dictionary<string, double>())
                    .Select(kv => $"{(settings != null ? settings.FilterName(kv.Key) : kv.Key)} {kv.Value:0.#} h"));
                return string.Join(" · ", new[] { kind, size, goals }.Where(s => s.Length > 0));
            }
        }

        public string ProgressText {
            get {
                var progress = Listing.Progress ?? new Dictionary<string, ProgressFigures>();
                var collected = (Listing.Collected ?? new Dictionary<string, double>()).Values.Sum();
                if (progress.Count == 0) return collected > 0 ? $"{collected:0.0} h collected in all" : "nothing collected yet";
                var done = progress.Values.Average(p => p.AtGoal);
                return $"{done * 100:0}% of the field at full depth · {collected:0.0} h collected in all";
            }
        }

        /// <summary>One line per channel the project wants: how deep it is against its goal, as a bar.</summary>
        public List<FilterProgressVM> FilterProgress {
            get {
                var goals = Listing.Goals ?? new Dictionary<string, double>();
                var collected = Listing.Collected ?? new Dictionary<string, double>();
                var progress = Listing.Progress ?? new Dictionary<string, ProgressFigures>();
                var names = goals.Keys.Concat(collected.Keys).Distinct()
                    .OrderBy(n => { var i = Array.IndexOf(Sky.Filters.Channels, n); return i < 0 ? 99 : i; }).ThenBy(n => n).ToList();
                var rows = new List<FilterProgressVM>();
                foreach (var name in names) {
                    goals.TryGetValue(name, out var goal);
                    collected.TryGetValue(name, out var hours);
                    progress.TryGetValue(name, out var figures);
                    // Done is the field's depth against the goal, averaged over
                    // the region: ten hours on one panel of fifteen is a
                    // fifteenth done, and one hundred means every part has its goal.
                    var fraction = figures != null ? figures.Average : (goal > 0 ? Math.Min(1.0, hours / goal) : (hours > 0 ? 1.0 : 0.0));
                    var text = goal > 0 ? $"{hours:0.0} of {goal:0.#} h · {fraction * 100:0}% done" : $"{hours:0.0} h, no goal";
                    rows.Add(new FilterProgressVM(Sky.Filters.Label(name), Math.Max(0, Math.Min(100, fraction * 100)), text, fraction >= 0.999));
                }
                return rows;
            }
        }

        /// <summary>Whether this telescope can take part, in one line.</summary>
        public bool FitOk => Listing.Compatibility?.Ok == true;
        public string FitText {
            get {
                var fit = Listing.Compatibility;
                if (fit == null) return "";
                if (fit.Ok) {
                    var filters = fit.Checks?.FirstOrDefault(c => c.Check == "filters")?.Detail ?? "";
                    return "Your telescope can take part" + (filters.Length > 0 ? $": {filters}" : "");
                }
                var why = string.Join("; ", (fit.Checks ?? new List<CompatibilityCheck>()).Where(c => c.Ok == false).Select(c => c.Detail));
                return "Your telescope cannot take part" + (why.Length > 0 ? $": {why}" : (fit.Summary.Length > 0 ? $": {fit.Summary}" : ""));
            }
        }

        public string PeopleText => Listing.Participants == 0
            ? "Nobody is on it yet"
            : $"{Listing.Participants} telescope{(Listing.Participants == 1 ? "" : "s")} on it, {Listing.ParticipantsOnline} online now";

        /// <summary>Whether a presence state means the camera is open: the plugin says "imaging", Starfront says "exposing" or "imaging".</summary>
        public static bool Shooting(string state) => state == "imaging" || state == "exposing";

        /// <summary>Every telescope on it, by name, the ones online marked and what they are doing.</summary>
        public List<ParticipantVM> Participants {
            get {
                var presence = Runtime.Ready ? (Runtime.Session.Presence?.Telescopes ?? new List<TelescopePresence>()) : new List<TelescopePresence>();
                var rows = new List<ParticipantVM>();
                foreach (var name in (Listing.ParticipantNames ?? new List<string>()).Distinct().OrderBy(n => n, StringComparer.CurrentCultureIgnoreCase)) {
                    var seen = presence.FirstOrDefault(t => t.Online && string.Equals(t.Name, name, StringComparison.OrdinalIgnoreCase));
                    var doing = seen == null ? "" : Shooting(seen.State) && seen.Project == Listing.Id ? "shooting it now"
                        : Shooting(seen.State) ? "shooting elsewhere" : seen.State;
                    rows.Add(new ParticipantVM(name, seen != null, doing));
                }
                return rows.OrderByDescending(r => r.Online).ThenBy(r => r.Name, StringComparer.CurrentCultureIgnoreCase).ToList();
            }
        }

        public string CompatibilityText => Listing.Compatibility?.Summary ?? "";

        public string TonightText => Task == null ? "" : $"Tonight: {StarfrontSession.Describe(Task)}";
    }

    /// <summary>
    /// The Starfront panel on the Imaging tab: who is about, which
    /// collaborations are open to this telescope, and what it was dealt
    /// tonight. The host composes this in its own container, so the shared
    /// state comes through <see cref="Runtime"/> like everywhere else.
    /// </summary>
    [Export(typeof(IDockableVM))]
    public class StarfrontDockVM : DockableVM, IDisposable {
        private readonly IFramingAssistantVM framingAssistant;
        private readonly IApplicationMediator applicationMediator;
        private readonly SkyThumbnails thumbnails;
        private bool initialized;
        private string message = "";
        // The whole list, and what the person asked to see of it.
        private List<ProjectRowVM> all = new();
        private readonly HashSet<string> opened = new(StringComparer.Ordinal);
        private string search = "";
        private string show = "all";
        private string sortBy = "activity";

        /// <summary>A way to sort the list, or a slice of it to show.</summary>
        public sealed record SortChoice(string Code, string Label);

        public IReadOnlyList<SortChoice> SortChoices { get; } = new List<SortChoice> {
            new("activity", "Most active"),
            new("thinnest", "Least done"),
            new("altitude", "Highest now"),
            new("newest", "Newest"),
            new("name", "By name"),
        };

        public IReadOnlyList<SortChoice> ShowChoices { get; } = new List<SortChoice> {
            new("all", "All collaborations"),
            new("fit", "Ones I can join"),
            new("joined", "Ones I am on"),
        };

        [ImportingConstructor]
        public StarfrontDockVM(IProfileService profileService,
                               ICameraMediator cameraMediator,
                               ITelescopeMediator telescopeMediator,
                               IFilterWheelMediator filterWheelMediator,
                               IRotatorMediator rotatorMediator,
                               IGuiderMediator guiderMediator,
                               IImageSaveMediator imageSaveMediator,
                               INighttimeCalculator nighttimeCalculator,
                               IFramingAssistantVM framingAssistant,
                               IApplicationMediator applicationMediator) : base(profileService) {
            Runtime.Ensure(new HostServices(profileService, cameraMediator, telescopeMediator, filterWheelMediator,
                                            rotatorMediator, guiderMediator, imageSaveMediator, nighttimeCalculator));
            this.framingAssistant = framingAssistant;
            this.applicationMediator = applicationMediator;
            thumbnails = new SkyThumbnails(Runtime.Settings.DataFolder);
            Title = "Starfront";
            try {
                var dictionary = new ResourceDictionary { Source = new Uri("Starfront.Collab;component/Sequencer/SequencerTemplates.xaml", UriKind.RelativeOrAbsolute) };
                if (dictionary["Starfront_CollabSVG"] is GeometryGroup geometry) {
                    geometry.Freeze();
                    ImageGeometry = geometry;
                }
            } catch (Exception ex) {
                Logger.Debug($"Starfront: panel icon - {ex.Message}");
            }
            RefreshCommand = new AsyncRelayCommand(RefreshAsync);
            SignInCommand = new AsyncRelayCommand(SignInAsync);
            ClearSearchCommand = new RelayCommand(() => Search = "");
            Start = new StartCollabVM(framingAssistant, RefreshAsync);
            Projects = new ObservableCollection<ProjectRowVM>();
            Tonight = new ObservableCollection<string>();
            PlanLines = new ObservableCollection<string>();
            Telescopes = new ObservableCollection<TelescopeRowVM>();
            Verdicts = new ObservableCollection<string>();
            Stacking = new StackingStatusVM(Runtime.Stacking, Runtime.Settings);
            Initialize();
        }

        /// <summary>The Live stacking lines.</summary>
        public StackingStatusVM Stacking { get; }

        private void OnOwnPropertyChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) {
            if (e.PropertyName == nameof(IsVisible)) Stacking.PanelVisible(IsVisible);
        }

        private void OnStackingChanged(object sender, EventArgs e) => Dispatch.OnUi(() => {
            foreach (var row in all) row.RefreshStacking();
        });

        private void Initialize() {
            if (initialized) return;
            initialized = true;
            Runtime.Session.Updated += OnUpdated;
            Runtime.Session.Run.PropertyChanged += OnRunChanged;
            Runtime.Settings.PropertyChanged += OnSettingsChanged;
            Runtime.Stacking.Changed += OnStackingChanged;
            Runtime.Stacking.Links.Changed += OnStackingChanged;
            PropertyChanged += OnOwnPropertyChanged;
            Stacking.PanelVisible(IsVisible);
            Plugin.ShuttingDown += Dispose;
            Rebuild();
        }

        private void OnRunChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) => Dispatch.OnUi(RebuildPlan);

        /// <summary>Tonight's fields with where each has got to, live while the container runs and kept afterwards.</summary>
        private void RebuildPlan() {
            if (!initialized) return;
            PlanLines.Clear();
            foreach (var item in Runtime.Session.Run.Plan) {
                var mark = item.State switch { "done" => "✓", "shooting" => "▶", "skipped" => "↷", "failed" => "✗", _ => "·" };
                var tail = string.IsNullOrEmpty(item.Detail) ? item.State : $"{item.State}, {item.Detail}";
                PlanLines.Add($"{mark} {item.Name} — {item.Frames} — {tail}");
            }
            RaisePropertyChanged(nameof(RunText));
            RaisePropertyChanged(nameof(ShowPlan));
        }

        public ObservableCollection<string> PlanLines { get; }
        public bool ShowPlan => PlanLines.Count > 0;

        public override string ContentId => typeof(StarfrontDockVM).FullName;

        public AsyncRelayCommand RefreshCommand { get; }
        public AsyncRelayCommand SignInCommand { get; }
        public RelayCommand ClearSearchCommand { get; }

        /// <summary>A word or two; every card with all of them somewhere stays in the list.</summary>
        public string Search { get => search; set { search = value ?? ""; RaisePropertyChanged(); Refilter(); } }
        /// <summary>Which slice of the list: all, the ones this telescope could join, the ones it is on.</summary>
        public string Show { get => show; set { show = value ?? "all"; RaisePropertyChanged(); Refilter(); } }
        public string SortBy { get => sortBy; set { sortBy = value ?? "activity"; RaisePropertyChanged(); Refilter(); } }
        public string ShownText => all.Count == 0 ? "" : (Projects.Count == all.Count ? $"{all.Count} open" : $"{Projects.Count} of {all.Count} open");
        public string EmptyText {
            get {
                if (all.Count == 0) return "None are open right now.";
                if (Projects.Count == 0) return "Nothing matches. Clear the search, or show all collaborations.";
                return "";
            }
        }

        /// <summary>The form that starts a collaboration, and edits one of yours.</summary>
        public StartCollabVM Start { get; }
        /// <summary>Anybody signed in may start one: being in the Discord server is the whole gate.</summary>
        public bool CanStart => Runtime.Ready && Runtime.Session.SignedIn;
        public ObservableCollection<ProjectRowVM> Projects { get; }
        public ObservableCollection<string> Tonight { get; }
        /// <summary>Every telescope online right now, as a table: who, doing what, on which collaboration.</summary>
        public ObservableCollection<TelescopeRowVM> Telescopes { get; }
        public string TelescopesText => Telescopes.Count == 0 ? "" : $"Telescopes online ({Telescopes.Count})";
        private bool telescopesOpen;
        /// <summary>Whether the table of telescopes online is unfolded; folded until asked, and kept as left across check-ins.</summary>
        public bool TelescopesOpen { get => telescopesOpen; set { telescopesOpen = value; RaisePropertyChanged(); } }
        public ObservableCollection<string> Verdicts { get; }

        public bool Enrolled => Runtime.Ready && Runtime.Settings.Enrolled;
        public string StatusText => Runtime.Ready ? Runtime.Session.StatusText : "Starfront is not set up";
        public string OnlineText {
            get {
                if (!Runtime.Ready) return "";
                var presence = Runtime.Session.Presence;
                if (presence == null || presence.Telescopes.Count == 0) return "";
                return $"{presence.Online} telescope{(presence.Online == 1 ? "" : "s")} online, {presence.People} {(presence.People == 1 ? "person" : "people")}";
            }
        }
        public string RunText {
            get {
                if (!Runtime.Ready) return "";
                var run = Runtime.Session.Run;
                if (run.Running) return string.IsNullOrEmpty(run.TargetName) ? "The collaboration container is running" : $"Shooting now: {run.TargetName}";
                return run.Plan.Count > 0 ? "Last run:" : "";
            }
        }

        public string Message {
            get => message;
            private set { message = value ?? ""; RaisePropertyChanged(); }
        }

        private void OnUpdated(object sender, EventArgs e) => Dispatch.OnUi(Rebuild);
        private void OnSettingsChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) => Dispatch.OnUi(() => { RaisePropertyChanged(nameof(Enrolled)); RaisePropertyChanged(nameof(CanStart)); });

        private void Rebuild() {
            if (!initialized) return;
            try {
                var session = Runtime.Session;
                var tasks = session.Tasks;
                // Rows for every open collaboration, none of them fetching a
                // picture: the pictures come when a card is opened, and a card
                // that was open stays open across a check-in.
                all = session.Projects.Select(listing => new ProjectRowVM(
                    listing, tasks.FirstOrDefault(t => t.Live && t.Project == listing.Id), this, opened.Contains(listing.Id))).ToList();
                Refilter();
                Tonight.Clear();
                foreach (var deal in session.DealTonight()) Tonight.Add($"{deal.ProjectName}: {StarfrontSession.Describe(deal)}");
                if (Tonight.Count == 0) Tonight.Add(tasks.Count == 0 ? "No collaboration joined yet." : "Nothing dealt for tonight yet; the deal is read when the sequence runs.");
                Telescopes.Clear();
                var names = session.Projects.ToDictionary(p => p.Id, p => p.Name);
                var online = (session.Presence?.Telescopes ?? new List<TelescopePresence>()).Where(t => t.Online)
                    .OrderByDescending(t => ProjectRowVM.Shooting(t.State)).ThenBy(t => t.OwnerName).ThenBy(t => t.Name);
                foreach (var scope in online) {
                    names.TryGetValue(scope.Project ?? "", out var onProject);
                    Telescopes.Add(new TelescopeRowVM(
                        scope.Name, scope.OwnerName ?? "",
                        ProjectRowVM.Shooting(scope.State) ? "shooting"
                            : scope.State switch { "slewing" => "slewing", "parked" => "parked", "cooling" => "cooling",
                                                   "centring" => "centring", "focusing" => "focusing", "paused" => "paused", _ => "idle" },
                        scope.Target ?? "", onProject ?? ""));
                }
                RaisePropertyChanged(nameof(TelescopesText));
                Verdicts.Clear();
                foreach (var verdict in session.LastVerdicts.Take(6)) Verdicts.Add(verdict);
            } catch (Exception ex) {
                Logger.Error("Starfront: the panel could not be refreshed", ex);
            }
            RaisePropertyChanged(nameof(Enrolled));
            RaisePropertyChanged(nameof(CanStart));
            RaisePropertyChanged(nameof(StatusText));
            RaisePropertyChanged(nameof(OnlineText));
            RebuildPlan();
        }

        /// <summary>The list as asked for: the search, the two switches, the sort.</summary>
        private void Refilter() {
            if (!initialized) return;
            IEnumerable<ProjectRowVM> rows = all.Where(r => r.Matches(search));
            if (show == "fit") rows = rows.Where(r => r.FitOk || r.Joined);
            if (show == "joined") rows = rows.Where(r => r.Joined);
            // The ones this telescope is on come first whatever the sort: they
            // are the ones its night is made of.
            rows = sortBy switch {
                "thinnest" => rows.OrderByDescending(r => r.Joined).ThenBy(r => r.Average).ThenBy(r => r.Name),
                "altitude" => rows.OrderByDescending(r => r.Joined).ThenByDescending(r => r.AltitudeNow ?? -90.0).ThenBy(r => r.Name),
                "newest" => rows.OrderByDescending(r => r.Joined).ThenByDescending(r => r.Listing.Created).ThenBy(r => r.Name),
                "name" => rows.OrderByDescending(r => r.Joined).ThenBy(r => r.Name, StringComparer.CurrentCultureIgnoreCase),
                _ => rows.OrderByDescending(r => r.Joined).ThenByDescending(r => r.Listing.ParticipantsOnline)
                         .ThenByDescending(r => r.Listing.Participants).ThenBy(r => r.Name),
            };
            Projects.Clear();
            foreach (var row in rows) {
                Projects.Add(row);
                if (row.Expanded) LoadVisuals(row);
            }
            RaisePropertyChanged(nameof(ShownText));
            RaisePropertyChanged(nameof(EmptyText));
        }

        /// <summary>Keep a card's open state across check-ins and re-sorts.</summary>
        public void Remember(string projectId, bool open) {
            if (open) opened.Add(projectId); else opened.Remove(projectId);
        }

        /// <summary>Fetch a card's sky picture and altitude chart, once, when it is opened.</summary>
        public void LoadVisuals(ProjectRowVM row) {
            if (row.VisualsLoaded) return;
            row.VisualsLoaded = true;
            LoadTransit(row);
            _ = LoadFramingAsync(row);
        }

        /// <summary>Close one of your collaborations: off everybody's list, the ledger kept.</summary>
        public async Task CloseAsync(ProjectListing listing) {
            if (listing == null) return;
            if (MessageBox.Show($"Close \"{listing.Name}\"? It comes off everybody's list; what was collected stays on record.",
                                "Starfront", MessageBoxButton.OKCancel, MessageBoxImage.Question) != MessageBoxResult.OK) return;
            try {
                Message = "Closing...";
                await Runtime.Session.CloseProjectAsync(listing.Id, CancellationToken.None);
                Message = $"\"{listing.Name}\" is closed.";
            } catch (Exception ex) {
                Message = $"Could not close it: {ex.Message}";
            }
        }

        /// <summary>Deal every telescope on one of your collaborations afresh at its next check-in.</summary>
        public async Task RedealAsync(ProjectListing listing) {
            if (listing == null) return;
            try {
                Message = "Asking the server to deal it again...";
                var forgotten = await Runtime.Session.RedealProjectAsync(listing.Id, CancellationToken.None);
                Message = forgotten == 0
                    ? $"Nothing was held for tonight on \"{listing.Name}\"; every telescope is dealt afresh at its next check-in anyway."
                    : $"{forgotten} telescope{(forgotten == 1 ? "" : "s")} on \"{listing.Name}\" will be dealt afresh at their next check-in.";
            } catch (Exception ex) {
                Message = $"Could not re-deal: {ex.Message}";
            }
        }

        private async Task RefreshAsync() {
            try {
                Message = "";
                await Runtime.Session.PollAsync(CancellationToken.None);
            } catch (Exception ex) {
                Message = ex.Message;
            }
        }

        /// <summary>The altitude chart for a row, drawn here and now: a few dozen transforms, on the UI thread it is called on.</summary>
        private void LoadTransit(ProjectRowVM row) {
            var region = row.Listing.Region;
            if (region == null || !Runtime.Ready) return;
            try {
                var floor = row.Listing.Requirements?.MinAltitude ?? 30.0;
                var (image, caption) = SkyThumbnails.Transit(region, Runtime.Host.Profile, Runtime.Host.Nighttime, floor,
                                                             ProjectRowVM.ThumbWidth, 90);
                row.Transit = image;
                row.TransitText = caption;
            } catch (Exception ex) {
                Logger.Debug($"Starfront: transit chart - {ex.Message}");
            }
        }

        /// <summary>The sky picture for a row: the cutout from the cache or CDS, then the region and panels drawn on it here.</summary>
        private async Task LoadFramingAsync(ProjectRowVM row) {
            var region = row.Listing.Region;
            if (region == null || region.Width <= 0 || region.Height <= 0) {
                row.FramingNote = "";
                return;
            }
            try {
                var cutout = await thumbnails.CutoutAsync(region, ProjectRowVM.ThumbWidth, ProjectRowVM.ThumbHeight);
                Dispatch.OnUi(() => {
                    try {
                        row.Framing = SkyThumbnails.Compose(cutout, region, row.Task?.Cells, row.Task?.Share, ProjectRowVM.ThumbWidth, ProjectRowVM.ThumbHeight);
                        row.FramingNote = cutout == null ? "no sky picture (offline?); the outline is drawn to scale" : "";
                    } catch (Exception ex) {
                        row.FramingNote = "";
                        Logger.Debug($"Starfront: framing picture - {ex.Message}");
                    }
                });
            } catch (Exception ex) {
                row.FramingNote = "";
                Logger.Debug($"Starfront: framing picture - {ex.Message}");
            }
        }

        /// <summary>Open N.I.N.A.'s Framing Assistant on the collaboration's region, at its angle, with this camera's footprint.</summary>
        public async Task ShowInFramingAsync(ProjectListing listing) {
            var region = listing?.Region;
            if (region == null) return;
            try {
                var profile = Runtime.Host.Profile.ActiveProfile;
                var coordinates = new Coordinates(Angle.ByDegree(region.Ra), Angle.ByDegree(region.Dec), Epoch.J2000);
                var dso = new DeepSkyObject(listing.Name, coordinates, profile.AstrometrySettings.Horizon) {
                    RotationPositionAngle = region.Rotation,
                };
                // Wide enough to see the whole region, within what the survey will serve.
                framingAssistant.FieldOfView = Math.Max(0.2, Math.Min(50.0, Math.Max(region.Width, region.Height) * 1.4));
                applicationMediator.ChangeTab(ApplicationTab.FRAMINGASSISTANT);
                await framingAssistant.SetCoordinates(dso);
                Message = $"Framing Assistant shows {listing.Name}: the region is {region.Width:0.0}° × {region.Height:0.0}°; the rectangle is your camera.";
            } catch (Exception ex) {
                Message = $"Could not open the Framing Assistant: {ex.Message}";
                Logger.Error("Starfront: framing assistant", ex);
            }
        }

        private async Task SignInAsync() {
            try {
                Message = "";
                await Runtime.Session.SignInAsync(CancellationToken.None);
            } catch (Exception ex) {
                Message = $"Sign-in failed: {ex.Message}";
            }
        }

        public async Task JoinAsync(string projectId) {
            try {
                Message = "Joining...";
                var said = await Runtime.Session.JoinAsync(projectId, CancellationToken.None);
                Message = said;
                Notification.ShowSuccess($"Starfront: {said}");
            } catch (Exception ex) {
                Message = $"Could not join: {ex.Message}";
                Notification.ShowWarning($"Starfront: {ex.Message}");
            }
        }

        public async Task LeaveAsync(string taskId) {
            if (string.IsNullOrEmpty(taskId)) return;
            try {
                Message = "Leaving...";
                await Runtime.Session.LeaveAsync(taskId, CancellationToken.None);
                Message = "Left the collaboration. Frames already shot stay credited.";
            } catch (Exception ex) {
                Message = $"Could not leave: {ex.Message}";
            }
        }

        public void Dispose() {
            if (!initialized) return;
            initialized = false;
            if (Runtime.Ready) {
                Runtime.Session.Updated -= OnUpdated;
                Runtime.Session.Run.PropertyChanged -= OnRunChanged;
                Runtime.Settings.PropertyChanged -= OnSettingsChanged;
                Runtime.Stacking.Changed -= OnStackingChanged;
                Runtime.Stacking.Links.Changed -= OnStackingChanged;
            }
            PropertyChanged -= OnOwnPropertyChanged;
            Stacking.Dispose();
            Plugin.ShuttingDown -= Dispose;
        }
    }
}

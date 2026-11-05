using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NINA.Core.Utility;
using NINA.Profile;
using NINA.Profile.Interfaces;
using RapidStackerCollab.Core.Upload;

namespace Starfront.Collab.Settings {

    /// <summary>
    /// Everything the plugin remembers.
    ///
    /// Two homes, because two kinds of thing. What belongs to the *person and
    /// this PC* - the server, the Discord sign-in, the telescope's token - is
    /// global, in the plugin's own file under N.I.N.A.'s application data,
    /// with the credentials protected for this Windows account (see
    /// <see cref="Protected"/>). What belongs to the *equipment* - the exposure
    /// each filter is shot at, the camera's angle, gain, binning - is per
    /// N.I.N.A. profile, through the host's own plugin-settings store, so a
    /// second profile for a second camera keeps its own table.
    ///
    /// Live stacking adds a third credential, the Rapid Stacker Collab sign-in,
    /// kept to the same rules as the other two: global, protected for this
    /// Windows account, and never written anywhere else.
    /// </summary>
    public sealed class StarfrontSettings : BaseINPC {
        public const string DefaultServer = "https://collab.starfront.space";

        /// <summary>The Rapid Stacker Collab API the lights are stacked by, with its trailing slash.</summary>
        public const string DefaultStackingApi = "https://collab.astro.ekeservices.com/api/";

        /// <summary>Addresses the server used to live at; one typed in is pointed at the current one.</summary>
        private static readonly string[] LegacyServers = { "https://starfront-bray.duckdns.org" };

        private readonly IProfileService profiles;
        private readonly PluginOptionsAccessor options;
        private readonly object gate = new();
        private readonly string globalPath;
        private GlobalSettings global = new();

        public StarfrontSettings(IProfileService profiles) {
            this.profiles = profiles;
            var id = typeof(StarfrontSettings).Assembly.GetCustomAttributes(typeof(GuidAttribute), false)
                .OfType<GuidAttribute>().First().Value;
            PluginId = Guid.Parse(id);
            options = new PluginOptionsAccessor(profiles, PluginId);
            DataFolder = Path.Combine(CoreUtil.APPLICATIONTEMPPATH, "PluginSettings", PluginId.ToString("D").ToUpperInvariant());
            globalPath = Path.Combine(DataFolder, "settings.json");
            profiles.ProfileChanged += (_, _) => RaiseAllPropertiesChanged();
        }

        public Guid PluginId { get; }
        public string DataFolder { get; }

        // -- the file --------------------------------------------------------

        private sealed class GlobalSettings {
            public int Version { get; set; } = 1;
            public string ServerUrl { get; set; } = "";
            public string UserToken { get; set; } = "";
            public string UserName { get; set; } = "";
            public string UserId { get; set; } = "";
            public string AgentToken { get; set; } = "";
            public string AgentId { get; set; } = "";
            public string AgentName { get; set; } = "";
            public bool SharePosition { get; set; } = true;
            public int PollMinutes { get; set; } = 10;   // no longer read; kept so older files load

            // Live stacking: the lights sent to a Rapid Stacker Collab project
            // as they land. Off unless asked, and nothing below is read while
            // it is off.
            public bool StackingEnabled { get; set; }
            public string StackingApiBase { get; set; } = "";
            public string StackingToken { get; set; } = "";          // protected, like the two above
            public string StackingTokenExpiresUtc { get; set; } = "";
            public string StackingUserName { get; set; } = "";
            public string StackingUserEmail { get; set; } = "";
            public string StackingDisplayName { get; set; } = "";
            public string StackingUploadMode { get; set; } = "WhenIdle";
            public string StackingWindowStart { get; set; } = "06:00";
            public string StackingWindowEnd { get; set; } = "14:00";
            public int StackingIdleGraceSeconds { get; set; } = 30;
            public int StackingBandwidthKBps { get; set; }
            public bool StackingStripLocation { get; set; } = true;
            public bool StackingIncludeCalibration { get; set; }
            public bool StackingUploadFlats { get; set; } = true;
            public bool StackingLibraryLive { get; set; } = true;
            public bool StackingLibraryPreferRaw { get; set; }
            public int StackingLibrarySyncHours { get; set; } = 24;
            public int StackingTempRetentionHours { get; set; } = 24;
            public int StackingHistoryDays { get; set; } = 30;
            public bool StackingHubTrustLan { get; set; } = true;
            public string StackingDefaultBackend { get; set; } = "cloud";
            public bool StackingPaused { get; set; }
        }

        public void Load() {
            lock (gate) {
                try {
                    if (File.Exists(globalPath)) {
                        var text = File.ReadAllText(globalPath);
                        global = JsonConvert.DeserializeObject<GlobalSettings>(text) ?? new GlobalSettings();
                    }
                } catch (Exception ex) {
                    // A corrupt file is kept aside rather than overwritten, and
                    // the plugin starts signed out.
                    Logger.Error("Starfront settings could not be read; starting afresh", ex);
                    try { File.Copy(globalPath, globalPath + ".broken", true); } catch (Exception) { }
                    global = new GlobalSettings();
                }
            }
            RaiseAllPropertiesChanged();
        }

        private void Save() {
            lock (gate) {
                Directory.CreateDirectory(DataFolder);
                var temporary = globalPath + ".tmp";
                File.WriteAllText(temporary, JsonConvert.SerializeObject(global, Formatting.Indented));
                File.Move(temporary, globalPath, true);
            }
        }

        // -- the person and this PC -------------------------------------------

        /// <summary>The collaboration server, without a trailing slash. Blank means the built-in one.</summary>
        public string ServerUrl {
            get {
                var url = (global.ServerUrl ?? "").Trim().TrimEnd('/');
                if (LegacyServers.Contains(url)) url = "";
                return url.Length > 0 ? url : DefaultServer;
            }
            set {
                var url = (value ?? "").Trim().TrimEnd('/');
                if (url == DefaultServer || LegacyServers.Contains(url)) url = "";
                global.ServerUrl = url;
                Save();
                RaisePropertyChanged();
            }
        }

        public string UserToken {
            get => Protected.Unwrap(global.UserToken);
            set { global.UserToken = Protected.Wrap(value); Save(); RaisePropertyChanged(); RaisePropertyChanged(nameof(SignedIn)); }
        }

        public string UserName {
            get => global.UserName ?? "";
            set { global.UserName = value ?? ""; Save(); RaisePropertyChanged(); }
        }

        public string UserId {
            get => global.UserId ?? "";
            set { global.UserId = value ?? ""; Save(); RaisePropertyChanged(); }
        }

        /// <summary>Whether somebody has signed in on this PC. The telescope's own token is what the night runs on.</summary>
        public bool SignedIn => UserToken.Length > 0 || Enrolled;

        public string AgentToken {
            get => Protected.Unwrap(global.AgentToken);
            set { global.AgentToken = Protected.Wrap(value); Save(); RaisePropertyChanged(); RaisePropertyChanged(nameof(Enrolled)); RaisePropertyChanged(nameof(SignedIn)); }
        }

        public string AgentId {
            get => global.AgentId ?? "";
            set { global.AgentId = value ?? ""; Save(); RaisePropertyChanged(); }
        }

        public string AgentName {
            get => global.AgentName ?? "";
            set { global.AgentName = value ?? ""; Save(); RaisePropertyChanged(); }
        }

        /// <summary>Whether this PC holds a telescope token - the credential the night runs on.</summary>
        public bool Enrolled => AgentToken.Length > 0;

        public bool SharePosition {
            get => global.SharePosition;
            set { global.SharePosition = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>How often the plugin talks to the server, in minutes. Fixed; it was a setting once and nobody needed to change it.</summary>
        public const int PollMinutes = 10;

        /// <summary>Forget the sign-in and the telescope token on this PC.</summary>
        public void ForgetCredentials() {
            global.UserToken = "";
            global.UserName = "";
            global.UserId = "";
            global.AgentToken = "";
            global.AgentId = "";
            global.AgentName = "";
            Save();
            RaiseAllPropertiesChanged();
        }

        // -- live stacking: the person and this PC ----------------------------

        /// <summary>Send the lights of a linked collaboration to Rapid Stacker Collab as they land. Off by default.</summary>
        public bool StackingEnabled {
            get => global.StackingEnabled;
            set { global.StackingEnabled = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>The Rapid Stacker Collab API, ending in a slash. Blank means the built-in one.</summary>
        public string StackingApiBase {
            get {
                var url = (global.StackingApiBase ?? "").Trim();
                if (url.Length == 0) return DefaultStackingApi;
                return url.EndsWith('/') ? url : url + "/";
            }
            set {
                var url = (value ?? "").Trim();
                if (url.Length > 0 && !url.EndsWith('/')) url += "/";
                if (url == DefaultStackingApi) url = "";
                global.StackingApiBase = url;
                Save();
                RaisePropertyChanged();
            }
        }

        /// <summary>Use a hub's address on this PC's own network when it answers, instead of going through the server.</summary>
        public bool StackingHubTrustLan {
            get => global.StackingHubTrustLan;
            set { global.StackingHubTrustLan = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>The backend for rig- and library-scoped calls, which belong to no project: "cloud" or "hub".</summary>
        public string StackingDefaultBackend {
            get => string.Equals(global.StackingDefaultBackend, "hub", StringComparison.OrdinalIgnoreCase) ? "hub" : "cloud";
            set { global.StackingDefaultBackend = string.Equals(value?.Trim(), "hub", StringComparison.OrdinalIgnoreCase) ? "hub" : "cloud"; Save(); RaisePropertyChanged(); }
        }

        /// <summary>The Rapid Stacker Collab token (<c>rscu_…</c>), protected like the other two credentials.</summary>
        public string StackingToken {
            get => Protected.Unwrap(global.StackingToken);
            set { global.StackingToken = Protected.Wrap(value); Save(); RaisePropertyChanged(); RaisePropertyChanged(nameof(StackingSignedIn)); }
        }

        /// <summary>When the token lapses, as the server said at sign-in; null when unknown. There is no refresh: a lapsed token means signing in again.</summary>
        public DateTime? StackingTokenExpiresUtc {
            get => DateTime.TryParse(global.StackingTokenExpiresUtc, CultureInfo.InvariantCulture,
                                     DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var when) ? when : null;
            set { global.StackingTokenExpiresUtc = value?.ToUniversalTime().ToString("o", CultureInfo.InvariantCulture) ?? ""; Save(); RaisePropertyChanged(); }
        }

        public string StackingUserName {
            get => global.StackingUserName ?? "";
            set { global.StackingUserName = value ?? ""; Save(); RaisePropertyChanged(); }
        }

        public string StackingUserEmail {
            get => global.StackingUserEmail ?? "";
            set { global.StackingUserEmail = value ?? ""; Save(); RaisePropertyChanged(); }
        }

        /// <summary>The name this person contributes under on Rapid Stacker Collab; blank falls back to the Discord name, then the telescope's.</summary>
        public string StackingDisplayName {
            get => global.StackingDisplayName ?? "";
            set { global.StackingDisplayName = (value ?? "").Trim(); Save(); RaisePropertyChanged(); }
        }

        /// <summary>Whether somebody is signed in to Rapid Stacker Collab on this PC.</summary>
        public bool StackingSignedIn => StackingToken.Length > 0;

        /// <summary>Forget the Rapid Stacker Collab sign-in on this PC. The Starfront sign-in is untouched.</summary>
        public void ForgetStackingCredentials() {
            global.StackingToken = "";
            global.StackingTokenExpiresUtc = "";
            global.StackingUserName = "";
            global.StackingUserEmail = "";
            Save();
            RaiseAllPropertiesChanged();
        }

        /// <summary>
        /// When uploads may run: Immediately, WhenIdle or TimeWindow, as
        /// <see cref="UploadMode"/> spells them. AfterSequenceEnd is upstream's
        /// fourth mode and is read as WhenIdle here: nothing in this plugin
        /// reports the sequencer's state, and a mode that waited for it would
        /// wait for ever.
        /// </summary>
        public string StackingUploadMode {
            get => StackingMode.ToString();
            set { global.StackingUploadMode = ParseMode(value).ToString(); Save(); RaisePropertyChanged(); RaisePropertyChanged(nameof(StackingMode)); }
        }

        public UploadMode StackingMode => ParseMode(global.StackingUploadMode);

        private static UploadMode ParseMode(string text) {
            if (!Enum.TryParse<UploadMode>((text ?? "").Trim(), true, out var mode) || !Enum.IsDefined(mode)) return UploadMode.WhenIdle;
            return mode == UploadMode.AfterSequenceEnd ? UploadMode.WhenIdle : mode;
        }

        /// <summary>The daily upload window's start, local time, HH:mm. Used by the TimeWindow mode.</summary>
        public string StackingWindowStart {
            get => WindowStart.ToString("HH:mm", CultureInfo.InvariantCulture);
            set { global.StackingWindowStart = ParseTime(value, WindowStart).ToString("HH:mm", CultureInfo.InvariantCulture); Save(); RaisePropertyChanged(); }
        }

        public string StackingWindowEnd {
            get => WindowEnd.ToString("HH:mm", CultureInfo.InvariantCulture);
            set { global.StackingWindowEnd = ParseTime(value, WindowEnd).ToString("HH:mm", CultureInfo.InvariantCulture); Save(); RaisePropertyChanged(); }
        }

        private TimeOnly WindowStart => TryParseTime(global.StackingWindowStart, out var when) ? when : new TimeOnly(6, 0);
        private TimeOnly WindowEnd => TryParseTime(global.StackingWindowEnd, out var when) ? when : new TimeOnly(14, 0);

        /// <summary>A typed time: blank keeps what was there, anything unreadable is refused so the options page can say so.</summary>
        private static TimeOnly ParseTime(string text, TimeOnly current) {
            if (string.IsNullOrWhiteSpace(text)) return current;
            if (TryParseTime(text, out var when)) return when;
            throw new ArgumentException("A time is written as HH:mm, like 06:00");
        }

        /// <summary>A stored time: the file is read without exceptions, whatever is in it.</summary>
        private static bool TryParseTime(string text, out TimeOnly when) {
            var clean = (text ?? "").Trim();
            return TimeOnly.TryParseExact(clean, "HH:mm", CultureInfo.InvariantCulture, DateTimeStyles.None, out when)
                || TimeOnly.TryParseExact(clean, "H:mm", CultureInfo.InvariantCulture, DateTimeStyles.None, out when)
                || TimeOnly.TryParse(clean, CultureInfo.CurrentCulture, out when);
        }

        /// <summary>For WhenIdle: seconds to wait after the last saved image before an upload starts, 0 to 600.</summary>
        public int StackingIdleGraceSeconds {
            get => Math.Max(0, Math.Min(600, global.StackingIdleGraceSeconds));
            set { global.StackingIdleGraceSeconds = Math.Max(0, Math.Min(600, value)); Save(); RaisePropertyChanged(); }
        }

        /// <summary>The upload cap in KB/s; 0 is unlimited.</summary>
        public int StackingBandwidthKBps {
            get => Math.Max(0, global.StackingBandwidthKBps);
            set { global.StackingBandwidthKBps = Math.Max(0, value); Save(); RaisePropertyChanged(); }
        }

        /// <summary>Remove the observer's location from a temporary copy before it is uploaded. Default on; a project may insist on it.</summary>
        public bool StackingStripLocation {
            get => global.StackingStripLocation;
            set { global.StackingStripLocation = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>
        /// Also send darks, bias and dark flats to the project of the last
        /// light sent, when they do not go to the rig's calibration library
        /// (<see cref="StackingLibraryLive"/>). Default off. Flats follow
        /// <see cref="StackingUploadFlats"/>.
        /// </summary>
        public bool StackingIncludeCalibration {
            get => global.StackingIncludeCalibration;
            set { global.StackingIncludeCalibration = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>
        /// Send flats, each to the stacking projects that got this telescope's
        /// lights of the same filter that night (an evening flat waits for
        /// them). Default on: a light without its flat is stacked uncorrected.
        /// </summary>
        public bool StackingUploadFlats {
            get => global.StackingUploadFlats;
            set { global.StackingUploadFlats = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>
        /// The darks, bias and dark flats N.I.N.A. saves go to this
        /// telescope's calibration library on Rapid Stacker Collab, which
        /// serves every project, instead of to one project. Default on; needs
        /// the rig, which the first join makes.
        /// </summary>
        public bool StackingLibraryLive {
            get => global.StackingLibraryLive;
            set { global.StackingLibraryLive = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>Send a folder's raw calibration frames rather than its masters, for masters made by tools that already subtract the bias. Default off.</summary>
        public bool StackingLibraryPreferRaw {
            get => global.StackingLibraryPreferRaw;
            set { global.StackingLibraryPreferRaw = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>Hours between two syncs of the calibration folder, 1 to 720.</summary>
        public int StackingLibrarySyncHours {
            get => Math.Max(1, Math.Min(720, global.StackingLibrarySyncHours));
            set { global.StackingLibrarySyncHours = Math.Max(1, Math.Min(720, value)); Save(); RaisePropertyChanged(); }
        }

        /// <summary>Leftover temporary copies older than this many hours are deleted, 1 to 720.</summary>
        public int StackingTempRetentionHours {
            get => Math.Max(1, Math.Min(720, global.StackingTempRetentionHours));
            set { global.StackingTempRetentionHours = Math.Max(1, Math.Min(720, value)); Save(); RaisePropertyChanged(); }
        }

        /// <summary>Finished uploads stay in the queue's history this many days, 1 to 3650.</summary>
        public int StackingHistoryDays {
            get => Math.Max(1, Math.Min(3650, global.StackingHistoryDays));
            set { global.StackingHistoryDays = Math.Max(1, Math.Min(3650, value)); Save(); RaisePropertyChanged(); }
        }

        /// <summary>The pause switch: nothing is uploaded while it is on, whatever the mode says.</summary>
        public bool StackingPaused {
            get => global.StackingPaused;
            set { global.StackingPaused = value; Save(); RaisePropertyChanged(); }
        }

        /// <summary>The upload policy as the engine reads it.</summary>
        public UploadPolicySettings StackingPolicy() =>
            new(StackingMode, WindowStart, WindowEnd, TimeSpan.FromSeconds(StackingIdleGraceSeconds), StackingPaused);

        // -- the equipment: per N.I.N.A. profile -------------------------------

        /// <summary>The Rapid Stacker Collab rig (<c>rig_…</c>) this profile's equipment was confirmed as; blank until the first join.</summary>
        public string StackingRigId {
            get => options.GetValueString(nameof(StackingRigId), "");
            set { options.SetValueString(nameof(StackingRigId), (value ?? "").Trim()); RaisePropertyChanged(); }
        }

        /// <summary>
        /// The folder of bias, dark and dark-flat frames (and their masters)
        /// kept in step with this telescope's calibration library; blank for
        /// none. Per profile, like the rig it belongs to.
        /// </summary>
        public string StackingLibraryFolder {
            get => options.GetValueString(nameof(StackingLibraryFolder), "");
            set { options.SetValueString(nameof(StackingLibraryFolder), (value ?? "").Trim()); RaisePropertyChanged(); }
        }

        /// <summary>What this telescope is called on the chart: the profile's name unless typed over.</summary>
        public string TelescopeName {
            get {
                var typed = options.GetValueString(nameof(TelescopeName), "");
                return typed.Length > 0 ? typed : (profiles.ActiveProfile?.Name ?? "Telescope");
            }
            set { options.SetValueString(nameof(TelescopeName), (value ?? "").Trim()); RaisePropertyChanged(); }
        }

        /// <summary>Seconds per filter, by the filter's name as the wheel spells it.</summary>
        public Dictionary<string, double> Exposures {
            get {
                var text = options.GetValueString(nameof(Exposures), "{}");
                try {
                    var table = JsonConvert.DeserializeObject<Dictionary<string, double>>(text) ?? new Dictionary<string, double>();
                    return table.Where(kv => kv.Value > 0).ToDictionary(kv => kv.Key, kv => kv.Value);
                } catch (Exception) {
                    return new Dictionary<string, double>();
                }
            }
            set {
                var clean = (value ?? new Dictionary<string, double>()).Where(kv => kv.Value > 0 && !string.IsNullOrWhiteSpace(kv.Key))
                    .ToDictionary(kv => kv.Key.Trim(), kv => Math.Round(kv.Value, 3));
                options.SetValueString(nameof(Exposures), JsonConvert.SerializeObject(clean));
                RaisePropertyChanged();
            }
        }

        /// <summary>
        /// What each filter delivers, by the wheel's name for it: a delivery
        /// code from <see cref="Sky.Filters.Deliveries"/>. Only what somebody
        /// chose by hand is stored; a filter not here is read from its name.
        /// </summary>
        public Dictionary<string, string> FilterMap {
            get {
                var text = options.GetValueString(nameof(FilterMap), "{}");
                try {
                    return JsonConvert.DeserializeObject<Dictionary<string, string>>(text) ?? new Dictionary<string, string>();
                } catch (Exception) {
                    return new Dictionary<string, string>();
                }
            }
            set {
                var clean = (value ?? new Dictionary<string, string>()).Where(kv => !string.IsNullOrWhiteSpace(kv.Key) && !string.IsNullOrWhiteSpace(kv.Value))
                    .ToDictionary(kv => kv.Key.Trim(), kv => kv.Value.Trim().ToUpperInvariant());
                options.SetValueString(nameof(FilterMap), JsonConvert.SerializeObject(clean));
                RaisePropertyChanged();
            }
        }

        public void SetDelivery(string filter, string code) {
            var table = FilterMap;
            var clean = (code ?? "").Trim().ToUpperInvariant();
            if (clean.Length > 0) table[filter.Trim()] = clean; else table.Remove(filter.Trim());
            FilterMap = table;
        }

        /// <summary>The delivery code for a filter: chosen by hand, or read from its name, or "" when neither says.</summary>
        public string DeliveryFor(string filter) {
            var name = (filter ?? "").Trim();
            if (name.Length == 0) return "";
            var table = FilterMap;
            if (table.TryGetValue(name, out var chosen) && chosen.Length > 0) return chosen;
            foreach (var kv in table) {
                if (string.Equals(kv.Key, name, StringComparison.OrdinalIgnoreCase)) return kv.Value;
            }
            return Sky.Filters.GuessDelivery(name, Colour);
        }

        /// <summary>The band a filter delivers in a sub: ["H"] for a mono Ha, ["OSC-Ha+OIII"] for an L-eXtreme on a colour camera, ["OSC-RGB"] for its clear filter.</summary>
        public List<string> ChannelsFor(string filter) => Sky.Filters.ChannelsOf(DeliveryFor(filter), Colour);

        /// <summary>The sky's Bortle class at this telescope, 1 (darkest) to 9; 0 until rated. Sent with the profile.</summary>
        public int Bortle {
            get => options.GetValueInt32(nameof(Bortle), 0);
            set { options.SetValueInt32(nameof(Bortle), Math.Max(0, Math.Min(9, value))); RaisePropertyChanged(); }
        }

        public void SetExposure(string filter, double? seconds) {
            var table = Exposures;
            if (seconds is > 0) table[filter.Trim()] = seconds.Value; else table.Remove(filter.Trim());
            Exposures = table;
        }

        /// <summary>The exposure for a filter, matched by the one spelling of its name; null when unset.</summary>
        public double? ExposureFor(string filter) {
            var wanted = Sky.Filters.Canonical(filter).ToLowerInvariant();
            foreach (var kv in Exposures) {
                if (Sky.Filters.Canonical(kv.Key).ToLowerInvariant() == wanted) return kv.Value;
            }
            return null;
        }

        /// <summary>Dither after this many frames of a panel; 0 leaves dithering to the triggers around the container.</summary>
        public int DitherEvery {
            get => options.GetValueInt32(nameof(DitherEvery), 0);
            set { options.SetValueInt32(nameof(DitherEvery), Math.Max(0, value)); RaisePropertyChanged(); }
        }

        /// <summary>Recentre a field once it has drifted this far, in arcminutes; 0 never recentres.</summary>
        public double RecentreArcMinutes {
            get => options.GetValueDouble(nameof(RecentreArcMinutes), 5.0);
            set { options.SetValueDouble(nameof(RecentreArcMinutes), Math.Max(0.0, Math.Min(180.0, value))); RaisePropertyChanged(); }
        }

        /// <summary>Turn the camera to the project's angle when a rotator is connected.</summary>
        public bool MatchRotation {
            get => options.GetValueBoolean(nameof(MatchRotation), true);
            set { options.SetValueBoolean(nameof(MatchRotation), value); RaisePropertyChanged(); }
        }

        /// <summary>The camera's position angle on the sky for a rig without a rotator, from its last plate solve. NaN until measured.</summary>
        public double CameraAngle {
            get => options.GetValueDouble(nameof(CameraAngle), double.NaN);
            set { options.SetValueDouble(nameof(CameraAngle), value); RaisePropertyChanged(); RaisePropertyChanged(nameof(CameraAngleText)); }
        }

        public string CameraAngleText => double.IsNaN(CameraAngle) ? "not measured yet" : CameraAngle.ToString("0.0", CultureInfo.InvariantCulture) + "°";

        /// <summary>Sensor size in pixels, remembered from the last connected camera so the afternoon knows it too.</summary>
        public int SensorWidth {
            get => options.GetValueInt32(nameof(SensorWidth), 0);
            set { options.SetValueInt32(nameof(SensorWidth), value); RaisePropertyChanged(); }
        }

        public int SensorHeight {
            get => options.GetValueInt32(nameof(SensorHeight), 0);
            set { options.SetValueInt32(nameof(SensorHeight), value); RaisePropertyChanged(); }
        }

        /// <summary>Pixel size in microns as the camera reported it; falls back to the profile's.</summary>
        public double PixelSize {
            get => options.GetValueDouble(nameof(PixelSize), 0.0);
            set { options.SetValueDouble(nameof(PixelSize), value); RaisePropertyChanged(); }
        }

        /// <summary>A one-shot colour camera, as last seen connected.</summary>
        public bool Colour {
            get => options.GetValueBoolean(nameof(Colour), false);
            set { options.SetValueBoolean(nameof(Colour), value); RaisePropertyChanged(); }
        }

        /// <summary>Gain for collaboration frames; -1 is the camera's default.</summary>
        public int Gain {
            get => options.GetValueInt32(nameof(Gain), -1);
            set { options.SetValueInt32(nameof(Gain), Math.Max(-1, value)); RaisePropertyChanged(); }
        }

        public int Offset {
            get => options.GetValueInt32(nameof(Offset), -1);
            set { options.SetValueInt32(nameof(Offset), Math.Max(-1, value)); RaisePropertyChanged(); }
        }

        public short Binning {
            get => options.GetValueInt16(nameof(Binning), 1);
            set { options.SetValueInt16(nameof(Binning), (short)Math.Max(1, Math.Min(4, (int)value))); RaisePropertyChanged(); }
        }

        /// <summary>
        /// A filter as this telescope's wheel spells it. The server speaks of
        /// filters in its own standard names and a project in whatever its
        /// coordinator typed; what the person sees here is their own wheel's
        /// name for the same filter, or the name as given when the wheel has
        /// no such filter.
        /// </summary>
        public string FilterName(string name) {
            if (string.IsNullOrWhiteSpace(name)) return "";
            foreach (var own in ProfileFilterNames()) {
                if (Sky.Filters.Same(own, name)) return own;
            }
            foreach (var own in Exposures.Keys) {
                if (Sky.Filters.Same(own, name)) return own;
            }
            return name.Trim();
        }

        /// <summary>The filters the profile knows, as the wheel spells them.</summary>
        public List<string> ProfileFilterNames() {
            var names = profiles.ActiveProfile?.FilterWheelSettings?.FilterWheelFilters?
                .Select(f => (f.Name ?? "").Trim()).Where(n => n.Length > 0).Distinct().ToList() ?? new List<string>();
            return names;
        }
    }
}

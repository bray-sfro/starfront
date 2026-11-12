using System.Diagnostics;
using Newtonsoft.Json.Linq;
using NINA.Core.Utility;
using NINA.Core.Utility.Notification;
using Starfront.Collab.Ledger;
using Starfront.Collab.Server;
using Starfront.Collab.Settings;
using Starfront.Collab.Sky;

namespace Starfront.Collab.Services {

    /// <summary>
    /// The telescope's conversation with the server: a check-in every few
    /// minutes (hello, what should I shoot, who else is about, here is what I
    /// shot), and the few things a person does - sign in, enrol the telescope,
    /// join or leave a collaboration.
    ///
    /// Nothing here can stop a night. The server is a source of a plan, not
    /// a dependency: a check-in that fails is logged and tried again later,
    /// and the deal already in hand stays in hand.
    /// </summary>
    public sealed class StarfrontSession : BaseINPC {
        private readonly HostServices host;
        private readonly StarfrontSettings settings;
        private readonly StarfrontClient client;
        private readonly FrameLedger ledger;
        private readonly FrameReporter reporter;
        private readonly SemaphoreSlim gate = new(1, 1);
        private Timer timer;
        private DateTime lastAttemptUtc = DateTime.MinValue;
        private string moonNight = "";
        private MoonTonight moonCached;

        private bool busy;
        private string lastError;
        private string signInStatus = "";
        private DateTime? lastPollUtc;
        private double? clockSkewSeconds;
        private List<DealTask> tasks = new();
        private List<ProjectListing> projects = new();
        private PresenceAnswer presence = new();
        private Dictionary<string, Requirements> requirementsByProject = new();
        private List<string> lastVerdicts = new();

        public StarfrontSession(HostServices host, StarfrontSettings settings, StarfrontClient client, FrameLedger ledger, FrameReporter reporter) {
            this.host = host;
            this.settings = settings;
            this.client = client;
            this.ledger = ledger;
            this.reporter = reporter;
            Run = new RunState();
        }

        /// <summary>Raised after every check-in, sign-in, join or leave, on a worker thread.</summary>
        public event EventHandler Updated;

        public RunState Run { get; }

        public bool Busy { get => busy; private set { busy = value; RaisePropertyChanged(); } }
        public string LastError { get => lastError; private set { lastError = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(StatusText)); } }
        public string SignInStatus { get => signInStatus; private set { signInStatus = value ?? ""; RaisePropertyChanged(); } }
        public DateTime? LastPollUtc { get => lastPollUtc; private set { lastPollUtc = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(Online)); RaisePropertyChanged(nameof(StatusText)); } }
        public double? ClockSkewSeconds { get => clockSkewSeconds; private set { clockSkewSeconds = value; RaisePropertyChanged(); } }
        public bool Online => LastPollUtc.HasValue && DateTime.UtcNow - LastPollUtc.Value < TimeSpan.FromMinutes(15);

        public IReadOnlyList<DealTask> Tasks { get { lock (tasks) return tasks.ToList(); } }
        public IReadOnlyList<ProjectListing> Projects { get { lock (projects) return projects.ToList(); } }
        public PresenceAnswer Presence { get => presence; private set { presence = value ?? new PresenceAnswer(); RaisePropertyChanged(); } }
        public IReadOnlyDictionary<string, Requirements> RequirementsByProject { get { lock (requirementsByProject) return new Dictionary<string, Requirements>(requirementsByProject); } }
        public IReadOnlyList<string> LastVerdicts { get { lock (lastVerdicts) return lastVerdicts.ToList(); } }

        public string StatusText {
            get {
                if (!settings.Enrolled) return settings.UserToken.Length > 0 ? "Signed in; telescope not enrolled yet" : "Not signed in";
                if (LastError != null) return LastError;
                if (LastPollUtc is DateTime at) return $"Checked in {Ago(at)}";
                return "Not checked in yet";
            }
        }

        private static string Ago(DateTime utc) {
            var span = DateTime.UtcNow - utc;
            if (span < TimeSpan.FromMinutes(1)) return "just now";
            if (span < TimeSpan.FromHours(1)) return $"{(int)span.TotalMinutes} min ago";
            return $"{span.TotalHours:0.0} h ago";
        }

        // -- lifecycle -----------------------------------------------------------

        public void Start() {
            timer ??= new Timer(_ => Tick(), null, TimeSpan.FromSeconds(15), TimeSpan.FromSeconds(60));
        }

        public void Stop() {
            timer?.Dispose();
            timer = null;
        }

        private void Tick() {
            if (!settings.Enrolled) return;
            if (DateTime.UtcNow - lastAttemptUtc < TimeSpan.FromMinutes(StarfrontSettings.PollMinutes)) return;
            _ = PollSafeAsync();
        }

        private async Task PollSafeAsync() {
            try {
                await PollAsync(CancellationToken.None).ConfigureAwait(false);
            } catch (Exception ex) {
                StarfrontClient.Log("check-in", ex);
            }
        }

        public string Tonight => SkyMath.NightName(DateTime.Now);

        public MoonTonight MoonTonight() {
            var night = Tonight;
            if (moonNight != night || moonCached == null) {
                moonCached = SkyMath.Moon(host.Profile, host.Nighttime);
                moonNight = night;
            }
            return moonCached;
        }

        // -- the check-in --------------------------------------------------------

        /// <summary>
        /// Hello, what should I shoot, who is about, here is what I shot.
        /// Returns the live tasks; throws when the server could not be asked,
        /// leaving whatever was known before in place.
        /// </summary>
        public async Task<IReadOnlyList<DealTask>> PollAsync(CancellationToken token) {
            await gate.WaitAsync(token).ConfigureAwait(false);
            lastAttemptUtc = DateTime.UtcNow;
            Busy = true;
            try {
                if (!settings.Enrolled) {
                    LastError = null;
                    return Tasks;
                }
                var agentToken = settings.AgentToken;
                var profile = RigProfile.Build(host, settings, ledger);
                var presenceSaid = settings.SharePosition ? RigProfile.Presence(host, settings, Run, reporter) : null;
                var hello = await client.HelloAsync(agentToken, profile, presenceSaid, token).ConfigureAwait(false);
                if (hello.ServerTime > 0) ClockSkewSeconds = Math.Round(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - hello.ServerTime, 1);
                await RememberScopesAsync(agentToken, hello, token).ConfigureAwait(false);

                var moon = MoonTonight();
                var answer = await client.TaskAsync(agentToken, Tonight, moon?.Illumination, moon?.UpFraction, token).ConfigureAwait(false);
                var live = answer.All.Where(t => t != null && t.Live).ToList();
                lock (tasks) tasks = live;
                lock (requirementsByProject) requirementsByProject = answer.RequirementsByProject ?? new Dictionary<string, Requirements>();

                try {
                    var listed = await client.ProjectsAsync(agentToken, token).ConfigureAwait(false);
                    lock (projects) projects = listed;
                } catch (ServerException ex) {
                    Logger.Warning($"Starfront: could not list projects - {ex.Message}");
                }
                try {
                    Presence = await client.PresenceAsync(agentToken, token).ConfigureAwait(false);
                } catch (ServerException ex) {
                    Logger.Warning($"Starfront: could not read presence - {ex.Message}");
                }
                try {
                    await ReportPendingAsync(token).ConfigureAwait(false);
                } catch (ServerException ex) {
                    Logger.Warning($"Starfront: frames not reported this time - {ex.Message}");
                }

                LastPollUtc = DateTime.UtcNow;
                LastError = null;
                return live;
            } catch (ServerException ex) {
                LastError = ex.Status == 401
                    ? "This telescope's token is no longer accepted by the server; sign in again."
                    : ex.Message;
                throw;
            } finally {
                Busy = false;
                gate.Release();
                RaisePropertyChanged(nameof(Tasks));
                RaisePropertyChanged(nameof(Projects));
                RaisePropertyChanged(nameof(StatusText));
                Updated?.Invoke(this, EventArgs.Empty);
            }
        }

        /// <summary>
        /// Scopes from GET /scopes when the server has them, else the list hello
        /// just returned. A failure leaves whatever was already remembered.
        /// </summary>
        private async Task RememberScopesAsync(string agentToken, HelloAnswer hello, CancellationToken token) {
            try {
                var listed = await client.ScopesAsync(agentToken, token).ConfigureAwait(false);
                if (listed.Count > 0) {
                    ScopeDirectory.Replace(listed);
                    return;
                }
            } catch (ServerException ex) {
                Logger.Debug($"Starfront: could not list scopes - {ex.Message}");
            }
            if (hello?.Scopes != null && hello.Scopes.Count > 0) ScopeDirectory.Replace(hello.Scopes);
        }

        /// <summary>Send every panel shot and not yet recorded by the server, and remember which it took.</summary>
        public async Task<int> ReportPendingAsync(CancellationToken token) {
            var rows = ledger.Unreported();
            if (rows.Count == 0) return 0;
            var sent = 0;
            foreach (var batch in rows.Chunk(100)) {
                var contributions = batch.Select(ToContribution).ToList();
                var answer = await client.ReportAsync(settings.AgentToken, contributions, token).ConfigureAwait(false);
                var verdicts = new List<string>();
                foreach (var (row, recorded) in batch.Zip(answer.Recorded ?? new List<RecordedRow>())) {
                    if (recorded?.Id == null) continue;
                    ledger.MarkReported(row.Key);
                    sent++;
                    var where = Tasks.FirstOrDefault(t => t.Id == row.Task)?.Kind == "single" ? "the field" : $"panel {row.Panel}";
                    var line = $"{row.Night} {where} {settings.FilterName(row.Filter)}: {(recorded.Accepted ? "accepted" : "rejected")}"
                               + (string.IsNullOrEmpty(recorded.Summary) ? "" : $" - {recorded.Summary}");
                    verdicts.Add(line);
                    if (recorded.Accepted) Logger.Info("Starfront: " + line);
                    else Logger.Warning("Starfront: " + line);
                }
                lock (lastVerdicts) lastVerdicts = verdicts.Concat(lastVerdicts).Take(20).ToList();
            }
            RaisePropertyChanged(nameof(LastVerdicts));
            return sent;
        }

        private JObject ToContribution(LedgerRow row) {
            var body = new JObject {
                ["project"] = row.Project,
                ["task"] = row.Task,
                ["night"] = row.Night,
                ["panel"] = row.Panel,
                ["filterName"] = row.Filter,
                ["frames"] = row.Frames,
                ["seconds"] = Math.Round(row.Seconds, 1),
                ["exposure"] = row.Frames > 0 ? Math.Round(row.Seconds / row.Frames, 2) : 0.0,
                ["footprint"] = row.Footprint == null ? JValue.CreateNull() : JObject.FromObject(row.Footprint),
                ["scale"] = row.Scale.HasValue ? new JValue(row.Scale.Value) : JValue.CreateNull(),
                ["focalLength"] = row.FocalLength.HasValue ? new JValue(row.FocalLength.Value) : JValue.CreateNull(),
                ["colour"] = row.Colour,
                ["hfr"] = row.MeanHfr.HasValue ? new JValue(Math.Round(row.MeanHfr.Value, 3)) : JValue.CreateNull(),
                ["guideRms"] = row.MeanRms.HasValue ? new JValue(Math.Round(row.MeanRms.Value, 3)) : JValue.CreateNull(),
                ["bandpass"] = row.Bandpass.HasValue ? new JValue(row.Bandpass.Value) : JValue.CreateNull(),
                // The channels the frames carry: a night through a dual-band
                // filter on a colour camera is credited to both its lines.
                ["channels"] = new JArray(settings.ChannelsFor(row.Filter)),
                ["bortle"] = settings.Bortle > 0 ? new JValue(settings.Bortle) : JValue.CreateNull(),
            };
            return body;
        }

        // -- tonight's deal ------------------------------------------------------

        /// <summary>The shares with panels dealt for tonight, optionally for one project.</summary>
        public List<DealTask> DealTonight(string projectId = null) {
            var tonight = Tonight;
            return Tasks.Where(t => t.Live && t.Share.Count > 0 && t.Visit != null && t.Cells.Count > 0
                                    && (string.IsNullOrEmpty(t.AssignedNight) || t.AssignedNight == tonight)
                                    && (string.IsNullOrEmpty(projectId) || t.Project == projectId))
                        .ToList();
        }

        public Requirements RequirementsFor(string projectId) {
            lock (requirementsByProject) {
                return requirementsByProject.TryGetValue(projectId ?? "", out var wants) ? wants : null;
            }
        }

        // -- signing in ------------------------------------------------------------

        /// <summary>
        /// The device-code flow: ask the server for a code, open Discord in the
        /// browser, and wait for the person to come back signed in. Then enrol
        /// this telescope under their account and check in for the first time.
        /// </summary>
        public async Task SignInAsync(CancellationToken token) {
            await gate.WaitAsync(token).ConfigureAwait(false);
            Busy = true;
            try {
                SignInStatus = "Asking the server for a sign-in code...";
                var begun = await client.BeginLoginAsync(token).ConfigureAwait(false);
                SignInStatus = "Sign in with Discord in the browser window that just opened. Waiting...";
                try {
                    Process.Start(new ProcessStartInfo(begun.Url) { UseShellExecute = true });
                } catch (Exception ex) {
                    SignInStatus = $"Open this address in a browser to sign in: {begun.Url}";
                    Logger.Warning($"Starfront: could not open the browser - {ex.Message}");
                }
                var deadline = DateTime.UtcNow.AddSeconds(Math.Max(60, begun.ExpiresIn));
                LoginPolled polled = null;
                while (DateTime.UtcNow < deadline) {
                    await Task.Delay(TimeSpan.FromSeconds(2), token).ConfigureAwait(false);
                    polled = await client.PollLoginAsync(begun.Code, token).ConfigureAwait(false);
                    if (polled.State == "done") break;
                    if (polled.State == "expired" || polled.State == "claimed") {
                        throw new InvalidOperationException("the sign-in code expired before anybody signed in; try again");
                    }
                }
                if (polled == null || polled.State != "done" || string.IsNullOrEmpty(polled.Token)) {
                    throw new InvalidOperationException("nobody signed in within ten minutes; try again");
                }
                settings.UserToken = polled.Token;
                settings.UserName = polled.User?.Name ?? "";
                settings.UserId = polled.User?.Id ?? "";
                SignInStatus = $"Signed in as {settings.UserName}. Enrolling this telescope...";
                Logger.Info($"Starfront: signed in as {settings.UserName}");
            } catch (Exception ex) {
                SignInStatus = $"Sign-in failed: {ex.Message}";
                throw;
            } finally {
                Busy = false;
                gate.Release();
            }
            await EnsureTelescopeAsync(token).ConfigureAwait(false);
            try {
                await PollAsync(token).ConfigureAwait(false);
            } catch (Exception ex) {
                StarfrontClient.Log("first check-in", ex);
            }
            SignInStatus = $"Signed in as {settings.UserName}; this telescope is enrolled as {settings.AgentName}.";
            Notification.ShowSuccess($"Starfront: signed in as {settings.UserName}");
        }

        /// <summary>Enrol this PC's telescope under the signed-in person, once.</summary>
        public async Task EnsureTelescopeAsync(CancellationToken token) {
            if (settings.Enrolled) return;
            if (settings.UserToken.Length == 0) throw new InvalidOperationException("sign in with Discord first");
            await gate.WaitAsync(token).ConfigureAwait(false);
            Busy = true;
            try {
                var answer = await client.EnrolTelescopeAsync(settings.UserToken, settings.TelescopeName, settings.UserName, token).ConfigureAwait(false);
                if (string.IsNullOrEmpty(answer.Token)) throw new InvalidOperationException("the server did not hand back a telescope token");
                settings.AgentToken = answer.Token;
                settings.AgentId = answer.Agent?.Id ?? "";
                settings.AgentName = answer.Agent?.Name ?? settings.TelescopeName;
                Logger.Info($"Starfront: telescope enrolled as {settings.AgentName} ({settings.AgentId})");
            } catch (ServerException ex) when (ex.Status == 401) {
                settings.UserToken = "";
                throw new InvalidOperationException("the sign-in has expired; sign in with Discord again");
            } finally {
                Busy = false;
                gate.Release();
                Updated?.Invoke(this, EventArgs.Empty);
            }
        }

        public async Task SignOutAsync(CancellationToken token) {
            await gate.WaitAsync(token).ConfigureAwait(false);
            Busy = true;
            try {
                var userToken = settings.UserToken;
                if (userToken.Length > 0) {
                    try { await client.LogoutAsync(userToken, token).ConfigureAwait(false); } catch (Exception ex) { Logger.Debug($"Starfront: logout - {ex.Message}"); }
                }
                settings.ForgetCredentials();
                lock (tasks) tasks = new List<DealTask>();
                lock (projects) projects = new List<ProjectListing>();
                Presence = new PresenceAnswer();
                LastPollUtc = null;
                LastError = null;
                SignInStatus = "Signed out. The frames already shot stay in the ledger and are reported after the next sign-in.";
            } finally {
                Busy = false;
                gate.Release();
                RaisePropertyChanged(nameof(Tasks));
                RaisePropertyChanged(nameof(Projects));
                Updated?.Invoke(this, EventArgs.Empty);
            }
        }

        // -- starting and running a collaboration ----------------------------------

        /// <summary>The signed-in person's Discord id, which is what a project's owner is recorded as.</summary>
        public string UserId => settings.UserId ?? "";

        public bool SignedIn => settings.UserToken.Length > 0;

        private string UserTokenOrThrow() {
            if (settings.UserToken.Length == 0) throw new InvalidOperationException("sign in with Discord first");
            return settings.UserToken;
        }

        /// <summary>A person's call, as the three telescope calls are made: a 401 means the sign-in has lapsed.</summary>
        private async Task<T> AsPersonAsync<T>(Func<string, Task<T>> call) {
            try {
                return await call(UserTokenOrThrow()).ConfigureAwait(false);
            } catch (ServerException ex) when (ex.Status == 401) {
                throw new InvalidOperationException("the sign-in has expired; sign in with Discord again");
            }
        }

        public Task<LookupAnswer> LookupAsync(string name, CancellationToken token) =>
            AsPersonAsync(userToken => client.LookupAsync(userToken, name, token));

        /// <summary>Start a collaboration. The listing is re-read so the panel shows it at once.</summary>
        public async Task<string> CreateProjectAsync(JObject body, CancellationToken token) {
            var answer = await AsPersonAsync(userToken => client.CreateProjectAsync(userToken, body, token)).ConfigureAwait(false);
            var id = answer["project"]?["id"]?.ToString() ?? "";
            Logger.Info($"Starfront: started collaboration '{body["name"]}' ({id})");
            await RefreshQuietlyAsync(token).ConfigureAwait(false);
            return id;
        }

        public async Task UpdateProjectAsync(string projectId, JObject body, CancellationToken token) {
            await AsPersonAsync(userToken => client.UpdateProjectAsync(userToken, projectId, body, token)).ConfigureAwait(false);
            Logger.Info($"Starfront: updated collaboration {projectId}");
            await RefreshQuietlyAsync(token).ConfigureAwait(false);
        }

        public async Task CloseProjectAsync(string projectId, CancellationToken token) {
            await AsPersonAsync(userToken => client.UpdateProjectAsync(userToken, projectId, new JObject { ["status"] = "closed" }, token)).ConfigureAwait(false);
            Logger.Info($"Starfront: closed collaboration {projectId}");
            await RefreshQuietlyAsync(token).ConfigureAwait(false);
        }

        public async Task<int> RedealProjectAsync(string projectId, CancellationToken token) {
            var answer = await AsPersonAsync(userToken => client.RedealProjectAsync(userToken, projectId, token)).ConfigureAwait(false);
            var forgotten = answer["forgotten"]?.ToObject<int>() ?? 0;
            Logger.Info($"Starfront: collaboration {projectId} will be dealt afresh ({forgotten} list(s) forgotten)");
            await RefreshQuietlyAsync(token).ConfigureAwait(false);
            return forgotten;
        }

        private async Task RefreshQuietlyAsync(CancellationToken token) {
            try {
                await PollAsync(token).ConfigureAwait(false);
            } catch (Exception ex) {
                StarfrontClient.Log("re-reading the collaborations", ex);
            }
        }

        // -- joining and leaving ---------------------------------------------------

        /// <summary>
        /// Take a share of a project. Checked here first - every filter the
        /// project wants needs an exposure in the table, within the project's
        /// bounds - so the person is told what to change rather than refused
        /// by the server with the same words a minute later.
        /// </summary>
        public async Task<string> JoinAsync(string projectId, CancellationToken token) {
            if (!settings.Enrolled) throw new InvalidOperationException("sign in with Discord first");
            if (RigProfile.WhatIsMissing(host, settings) is string missing) {
                // Said here as what to do, rather than by the server as "this
                // telescope has not said what it can see".
                throw new InvalidOperationException(missing + ". Then join again.");
            }
            var listing = Projects.FirstOrDefault(p => p.Id == projectId);
            if (listing != null) {
                if (!listing.Compatibility.Ok) throw new InvalidOperationException(listing.Compatibility.Summary);
                var wants = listing.Requirements ?? new Requirements();
                var names = wants.Filters?.Keys.ToList() ?? new List<string>();
                if (names.Count == 0) names = listing.Goals?.Keys.ToList() ?? new List<string>();
                // Only the channels this telescope's own filters deliver are its
                // business: a mono rig is not asked for an OSC-RGB exposure
                // because the project also takes colour data, and a colour rig
                // is not asked for SII. The server deals the rest to others.
                var mine = RigProfile.FilterNames(settings)
                    .Select(f => (name: f, bands: settings.ChannelsFor(f)))
                    .Where(t => t.bands.Count > 0)
                    .ToList();
                foreach (var filterName in names) {
                    var canon = Filters.Canonical(filterName);
                    var givers = mine.Where(t => t.bands.Any(b => Covers(b, canon))).Select(t => t.name).ToList();
                    if (givers.Count == 0) continue;
                    double? seconds = null;
                    var label = settings.FilterName(givers[0]);
                    foreach (var giver in givers) {
                        seconds = settings.ExposureFor(giver);
                        if (seconds != null) { label = settings.FilterName(giver); break; }
                    }
                    if (seconds == null) {
                        throw new InvalidOperationException($"set an exposure for {label} in the exposure table first (Options > Plugins > Starfront)");
                    }
                    if (wants.MinExposure is double low && seconds < low) {
                        throw new InvalidOperationException($"this telescope shoots {label} at {seconds:0.#}s and the project wants {low:0.#}s or longer; change it in the exposure table");
                    }
                    if (wants.MaxExposure is double high && seconds > high) {
                        throw new InvalidOperationException($"this telescope shoots {label} at {seconds:0.#}s and the project wants {high:0.#}s or shorter; change it in the exposure table");
                    }
                }
            }
            var body = new JObject {
                ["hours"] = 0.0,   // the server sizes the share to the project's goals
                ["exposure"] = 0.0,
                ["exposures"] = JObject.FromObject(settings.Exposures),
                ["night"] = Tonight,
            };
            var moon = MoonTonight();
            if (moon != null) {
                body["moon"] = moon.Illumination;
                body["moonUp"] = moon.UpFraction;
            }
            JoinAnswer answer;
            await gate.WaitAsync(token).ConfigureAwait(false);
            Busy = true;
            try {
                // The server judges the join by the profile it holds, which was
                // sent at the last check-in - up to ten minutes ago, before the
                // camera was connected. Send it afresh first, so a camera
                // connected a moment ago counts.
                var profile = RigProfile.Build(host, settings, ledger);
                var presenceSaid = settings.SharePosition ? RigProfile.Presence(host, settings, Run, reporter) : null;
                await client.HelloAsync(settings.AgentToken, profile, presenceSaid, token).ConfigureAwait(false);
                answer = await client.JoinAsync(settings.AgentToken, projectId, body, token).ConfigureAwait(false);
                if (answer.Task != null) {
                    lock (tasks) {
                        tasks = tasks.Where(t => t.Id != answer.Task.Id).Append(answer.Task).ToList();
                    }
                }
            } finally {
                Busy = false;
                gate.Release();
            }
            try { await PollAsync(token).ConfigureAwait(false); } catch (Exception ex) { StarfrontClient.Log("check-in after joining", ex); }
            var name = answer.Task?.ProjectName ?? listing?.Name ?? "the collaboration";
            var message = answer.AlreadyJoined ? $"Already on {name}." : $"Joined {name}.";
            if (answer.Task?.Share?.Count > 0) message += $" Tonight: {Describe(answer.Task)}.";
            Logger.Info("Starfront: " + message);
            return message;
        }

        /// <summary>Whether a band this rig delivers gives a wanted channel: "H" gives H, mono "Ha+OIII" gives H and O, "OSC-RGB" gives only OSC-RGB.</summary>
        private static bool Covers(string band, string wantedCanonical) {
            if (string.Equals(band, wantedCanonical, StringComparison.OrdinalIgnoreCase)) return true;
            if (Filters.IsOsc(band)) return false;
            return band.Split('+').Any(part => string.Equals(Filters.Canonical(part), wantedCanonical, StringComparison.OrdinalIgnoreCase));
        }

        public async Task LeaveAsync(string taskId, CancellationToken token) {
            if (!settings.Enrolled) throw new InvalidOperationException("sign in with Discord first");
            await gate.WaitAsync(token).ConfigureAwait(false);
            Busy = true;
            try {
                await client.SetTaskStateAsync(settings.AgentToken, taskId, "declined", token).ConfigureAwait(false);
                lock (tasks) tasks = tasks.Where(t => t.Id != taskId).ToList();
            } finally {
                Busy = false;
                gate.Release();
            }
            try { await PollAsync(token).ConfigureAwait(false); } catch (Exception ex) { StarfrontClient.Log("check-in after leaving", ex); }
        }

        public Task<JObject> DepthAsync(string projectId, CancellationToken token) =>
            client.DepthAsync(settings.AgentToken, projectId, token);

        /// <summary>"4 panels, 12 × OIII each" or "the field, 12 × OIII" for a dealt task, filters as this wheel spells them.</summary>
        public static string Describe(DealTask task) {
            if (task?.Visit == null || task.Share.Count == 0) return "nothing dealt tonight";
            var settings = Runtime.Ready ? Runtime.Settings : null;
            var parts = task.Visit.Frames.Where(kv => kv.Value > 0)
                .Select(kv => $"{kv.Value} × {(settings != null ? settings.FilterName(kv.Key) : kv.Key)}");
            var frames = string.Join(", ", parts);
            if (frames.Length == 0) frames = "no frames";
            if (task.Kind == "single") return $"the field, {frames}";
            return $"{task.Share.Count} panel{(task.Share.Count == 1 ? "" : "s")}, {frames} each";
        }
    }
}

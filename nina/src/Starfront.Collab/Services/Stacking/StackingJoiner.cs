using System.Net.Http;
using NINA.Core.Enum;
using NINA.Core.Utility;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Filters;
using Starfront.Collab.Server;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Joins the stacking project behind each linked collaboration, with
    /// nobody at the keyboard: the code arrives with the check-in, the rig is
    /// described from what the camera and the profile say, the filters are
    /// mapped from the person's own delivery table and the stacking service's
    /// name table, and the join is sent. What cannot be done headlessly (the
    /// terms changing, a camera that has never been connected) is left for the
    /// options page to say, and tried again at a later check-in.
    /// </summary>
    public sealed class StackingJoiner {
        private static readonly TimeSpan AfterFailure = TimeSpan.FromHours(6);
        private static readonly TimeSpan AfterTransient = TimeSpan.FromMinutes(30);
        private static readonly TimeSpan AfterNoFilters = TimeSpan.FromHours(1);

        private readonly HostServices host;
        private readonly StarfrontSettings settings;
        private readonly StackingLinks links;
        private readonly Func<IApiClient> api;
        private readonly SemaphoreSlim gate = new(1, 1);
        private FilterNormalizer normalizer;
        private readonly HubJoiner hub;
        private readonly Func<IApiClient> cloud;
        /// <summary>The state last logged for each collaboration that could not be joined yet, so the log says it once, not at every check-in.</summary>
        private readonly Dictionary<string, string> notReadyLogged = new();

        /// <param name="cloud">The cloud client itself: the cloud rig is made there whatever the default backend is.</param>
        /// <param name="forLink">The client a hub link talks through (its own base and grant); the router when omitted.</param>
        public StackingJoiner(HostServices host, StarfrontSettings settings, StackingLinks links, Func<IApiClient> api, Func<StackingLink, IApiClient> forLink = null, Func<IApiClient> cloud = null) {
            this.cloud = cloud ?? api;
            this.host = host;
            this.settings = settings;
            this.links = links;
            this.api = api;
            hub = new HubJoiner(new HubContext(this), links, forLink ?? (_ => api()), line => Logger.Info(line));
        }

        /// <summary>This PC's facts for a hub join; the same rig and filter rules as the cloud join.</summary>
        private sealed class HubContext : IHubJoinContext {
            private readonly StackingJoiner owner;
            public HubContext(StackingJoiner owner) => this.owner = owner;
            public string WhatIsMissingForRig() => RigProfile.WhatIsMissing(owner.host, owner.settings);
            public RigUpsertRequest DescribeRig() => owner.DescribeRig();
            public IReadOnlyList<string> WheelFilterNames => RigProfile.FilterNames(owner.settings);
            public bool Colour => owner.settings.Colour;
            public IReadOnlyDictionary<string, string> FilterMap => owner.settings.FilterMap;
            public string DisplayName {
                get {
                    var name = owner.settings.UserName;
                    if (name.Length == 0) name = owner.settings.TelescopeName;
                    return HubJoiner.Clamp(name);
                }
            }
            public string TelescopeName => owner.settings.TelescopeName;
            public FilterNormalizer Normalizer => owner.normalizer ??= FilterNormalizer.LoadBundled();
        }

        /// <summary>The service rejected the sign-in; nothing more is tried until somebody signs in again.</summary>
        public event EventHandler AuthRequired;

        /// <summary>Bring the links in line with what the check-in listed. One at a time; a second call waits.</summary>
        public async Task ReconcileAsync(IReadOnlyList<ProjectListing> listings, CancellationToken ct) {
            if (!settings.StackingEnabled) return;
            await gate.WaitAsync(ct).ConfigureAwait(false);
            try {
                var cloudRefused = false;
                foreach (var listing in listings ?? Array.Empty<ProjectListing>()) {
                    ct.ThrowIfCancellationRequested();
                    var route = JoinRoutes.Of(listing, settings.StackingSignedIn);
                    if (route == JoinRoute.Skip) continue;
                    if (route == JoinRoute.Hub) {
                        // A hub project needs the grant the check-in carried and no Rapid Stacker Collab sign-in.
                        try {
                            await hub.SyncAsync(listing, listing.EffectiveStacking(), ct).ConfigureAwait(false);
                        } catch (OperationCanceledException) {
                            throw;
                        } catch (Exception ex) {
                            Logger.Error($"Starfront stacking: joining the hub project of {listing.Name} failed", ex);
                        }
                        continue;
                    }
                    // Cloud links need the person signed in to Rapid Stacker Collab.
                    if (cloudRefused || route == JoinRoute.CloudNeedsSignIn) continue;
                    if (string.IsNullOrWhiteSpace(listing.StackingJoinCode)) {
                        // A project the server is still making, or failed to make,
                        // has no code yet. Say so once: this was a silent skip.
                        var binding = listing.EffectiveStacking();
                        if (binding.Managed && binding.State is "pending" or "failed") {
                            if (!notReadyLogged.TryGetValue(listing.Id, out var said) || said != binding.State) {
                                notReadyLogged[listing.Id] = binding.State;
                                var why = string.IsNullOrWhiteSpace(binding.Problem) ? "" : $": {binding.Problem}";
                                Logger.Info($"Starfront stacking: {listing.Name} is not ready on Rapid Stacker Collab yet ({binding.State}{why})");
                            }
                        }
                        continue;
                    }
                    notReadyLogged.Remove(listing.Id);
                    if (!JoinCodes.TryNormalize(listing.StackingJoinCode, out var code) || code.Length == 0) {
                        Logger.Warning($"Starfront stacking: {listing.Name} carries a join code that cannot be one ({listing.StackingJoinCode}); ignored");
                        continue;
                    }
                    var link = links.Lookup(listing.Id);
                    if (link != null && link.JoinCode == code && link.Active) continue;
                    if (link != null && link.JoinCode == code && link.NextAttemptUtc is DateTime next && next > DateTime.UtcNow) continue;
                    if (link != null && link.JoinCode == code && link.Status is LinkStatus.TermsOutdated or LinkStatus.Pending) continue;
                    if (await JoinOneAsync(listing, code, ct).ConfigureAwait(false) == Outcome.AuthRequired) cloudRefused = true;
                }
            } finally {
                gate.Release();
            }
        }

        /// <summary>Somebody read the new terms and pressed Accept: join again, accepting the current version.</summary>
        public async Task AcceptTermsAsync(string starfrontProjectId, CancellationToken ct) {
            var link = links.Lookup(starfrontProjectId);
            if (link == null) return;
            await gate.WaitAsync(ct).ConfigureAwait(false);
            try {
                if (link.IsHub) { await hub.JoinAsync(link, ct).ConfigureAwait(false); return; }
                await JoinOneAsync(new ProjectListing { Id = link.StarfrontProjectId, Name = link.StarfrontName, StackingJoinCode = link.JoinCode }, link.JoinCode, ct, acceptTerms: true).ConfigureAwait(false);
            } finally {
                gate.Release();
            }
        }

        /// <summary>Try a failed link again now rather than when its wait is over.</summary>
        public async Task RetryAsync(string starfrontProjectId, CancellationToken ct) {
            var link = links.Lookup(starfrontProjectId);
            if (link == null || link.Active) return;
            await gate.WaitAsync(ct).ConfigureAwait(false);
            try {
                if (link.IsHub) { await hub.JoinAsync(link, ct).ConfigureAwait(false); return; }
                await JoinOneAsync(new ProjectListing { Id = link.StarfrontProjectId, Name = link.StarfrontName, StackingJoinCode = link.JoinCode }, link.JoinCode, ct, acceptTerms: link.Status == LinkStatus.TermsOutdated).ConfigureAwait(false);
            } finally {
                gate.Release();
            }
        }

        /// <summary>
        /// A pending membership may have been approved, and an active one may
        /// have been removed: ask. The project's frame is read again too: its
        /// first light sets its centre, rotation and scale, and its owner can
        /// move it, and the other-target rule must judge against the frame as
        /// it is now.
        /// </summary>
        public async Task RefreshMembershipsAsync(CancellationToken ct) {
            if (!settings.StackingEnabled) return;
            foreach (var link in links.All()) {
                ct.ThrowIfCancellationRequested();
                if (link.CollabProjectId.Length == 0 || link.Status is not (LinkStatus.Pending or LinkStatus.Active)) continue;
                if (!link.IsHub && !settings.StackingSignedIn) continue;
                try {
                    var framed = await RefreshProjectAsync(link, ct).ConfigureAwait(false);
                    var membership = await api().GetMembershipAsync(link.CollabProjectId, ct).ConfigureAwait(false);
                    var status = membership.Status == MemberStatuses.Active ? LinkStatus.Active
                               : membership.Status == MemberStatuses.Pending ? LinkStatus.Pending : LinkStatus.Failed;
                    if (status == link.Status) {
                        if (framed) links.Upsert(link);
                        continue;
                    }
                    link.Status = status;
                    link.Message = status == LinkStatus.Failed ? $"removed from {link.CollabName}" : "";
                    link.NextAttemptUtc = status == LinkStatus.Failed ? DateTime.UtcNow + AfterFailure : null;
                    link.Mapping = membership.FilterMapping.ToDictionary(m => m.Raw, m => m.Canonical, StringComparer.Ordinal);
                    links.Upsert(link);
                    Logger.Info($"Starfront stacking: {link.StarfrontName} is now {link.Describe()}");
                } catch (ApiException ex) when (ex.IsAuthenticationFailure) {
                    if (link.IsHub) {
                        // Only this hub link's grant goes; the next check-in brings a new one. The cloud sign-in is untouched.
                        new GrantTokenStore(link.StarfrontProjectId, links.Lookup, links.Upsert).Clear();
                        continue;
                    }
                    Raise(AuthRequired);
                    return;
                } catch (Exception ex) when (ex is ApiException or HttpRequestException) {
                    Logger.Debug($"Starfront stacking: the membership of {link.CollabName} could not be read - {ex.Message}");
                }
            }
        }

        /// <summary>Read the project's frame, target and calibration policy into the link; true when any of them changed. A failure keeps what the link had.</summary>
        private async Task<bool> RefreshProjectAsync(StackingLink link, CancellationToken ct) {
            ProjectDetail project;
            try {
                project = await api().GetProjectAsync(link.CollabProjectId, ct).ConfigureAwait(false);
            } catch (ApiException ex) when (!ex.IsAuthenticationFailure) {
                Logger.Debug($"Starfront stacking: the project {link.CollabName} could not be read - {ex.Message}");
                return false;
            }
            var pending = project.RotationFromFirstLight == true;
            var calibration = project.Policy?.Calibration ?? link.Calibration;
            var changed = !SameGrid(link.Grid, project.Grid) || link.GridPending != pending || link.Calibration != calibration || link.TargetName != project.TargetName;
            if (project.Grid != null) link.Grid = project.Grid;
            link.GridPending = pending;
            link.Calibration = calibration;
            link.TargetName = project.TargetName ?? "";
            return changed;
        }

        private static bool SameGrid(Grid a, Grid b) =>
            a == null ? b == null
            : b != null && a.CenterRaDeg == b.CenterRaDeg && a.CenterDecDeg == b.CenterDecDeg && a.RotationDeg == b.RotationDeg
              && a.ScaleArcsecPx == b.ScaleArcsecPx && a.WidthPx == b.WidthPx && a.HeightPx == b.HeightPx;

        private enum Outcome { Done, Later, AuthRequired }

        private async Task<Outcome> JoinOneAsync(ProjectListing listing, string code, CancellationToken ct, bool acceptTerms = false) {
            var link = links.Lookup(listing.Id) ?? new StackingLink { StarfrontProjectId = listing.Id };
            link.StarfrontName = string.IsNullOrEmpty(listing.Name) ? link.StarfrontName : listing.Name;
            link.JoinCode = code;
            if (link.IsHub) {
                // The collaboration moved from a hub back to the cloud.
                link.Backend = "cloud"; link.ApiBase = ""; link.LanApiBase = ""; link.Grant = ""; link.GrantExpiresUtc = null; link.HubId = ""; link.HubName = "";
            }
            var client = api();
            try {
                var resolved = await client.ResolveJoinCodeAsync(code, ct).ConfigureAwait(false);
                link.CollabProjectId = resolved.Project.ProjectId;
                link.CollabName = resolved.Project.Name;
                link.RequireStrip = resolved.Policy.RequireCoordinateStrip;
                link.DeclaredFilters = resolved.Project.Filters.ToList();
                link.TargetName = resolved.Project.TargetName ?? "";
                link.Grid = resolved.Grid ?? resolved.Project.Grid ?? link.Grid;
                link.Calibration = resolved.Policy.Calibration;
                link.TermsUrl = resolved.TermsUrl;

                if (resolved.AlreadyMember) {
                    var membership = await client.GetMembershipAsync(resolved.Project.ProjectId, ct).ConfigureAwait(false);
                    return Joined(link, membership, "already a member");
                }

                // The rig, from what the camera and the profile say.
                var rigId = settings.StackingRigId;
                if (rigId.Length == 0) {
                    var missing = RigProfile.WhatIsMissing(host, settings);
                    if (missing != null) return Hold(link, LinkStatus.NeedsRig, missing, AfterTransient);
                    var saved = await cloud().UpsertRigAsync(DescribeRig()).ConfigureAwait(false);
                    if (!saved.Confirmed) saved = await cloud().PatchRigAsync(saved.RigId, new RigPatchRequest { Confirmed = true }, ct).ConfigureAwait(false);
                    rigId = saved.RigId;
                    settings.StackingRigId = rigId;
                    Logger.Info($"Starfront stacking: this telescope is rig {rigId} on Rapid Stacker Collab");
                }
                link.RigId = rigId;

                // The filters, from the person's own table and the names.
                normalizer ??= FilterNormalizer.LoadBundled();
                var mapping = StackingMapping.Build(RigProfile.FilterNames(settings), resolved.Project.Filters, settings.Colour, settings.FilterMap, normalizer);
                if (mapping.AllSkipped) return Hold(link, LinkStatus.NoFilters, mapping.Summary, AfterNoFilters);

                var name = DisplayName();
                link.DisplayName = name;
                Membership joined;
                try {
                    joined = await client.JoinProjectAsync(resolved.Project.ProjectId, Request(code, name, resolved.TermsVersion, mapping.Entries), ct).ConfigureAwait(false);
                } catch (ApiException ex) when (ex.Code == ApiCodes.DisplayNameTaken) {
                    // Somebody else in the project has this name; the telescope's tells them apart.
                    name = Clamp($"{name} ({settings.TelescopeName})");
                    link.DisplayName = name;
                    joined = await client.JoinProjectAsync(resolved.Project.ProjectId, Request(code, name, resolved.TermsVersion, mapping.Entries), ct).ConfigureAwait(false);
                } catch (ApiException ex) when (ex.Code == ApiCodes.AlreadyMember) {
                    joined = await client.GetMembershipAsync(resolved.Project.ProjectId, ct).ConfigureAwait(false);
                }
                link.TermsVersion = resolved.TermsVersion;
                Logger.Info($"Starfront stacking: filters for {link.CollabName}: {mapping.Summary}");
                return Joined(link, joined, "joined");
            } catch (OperationCanceledException) {
                throw;
            } catch (ApiException ex) when (ex.Code == ApiCodes.TermsOutdated) {
                // The terms changed under the join: the plugin accepts a
                // version it has seen, never one it has not, so a person reads
                // the new ones and presses Accept on the options page. (A join
                // from that button sends the version the resolve just gave.)
                return Hold(link, LinkStatus.TermsOutdated, acceptTerms ? "the terms changed again while joining; try once more" : "the terms of contribution changed", null);
            } catch (ApiException ex) when (ex.IsAuthenticationFailure) {
                Hold(link, LinkStatus.Failed, "not signed in to Rapid Stacker Collab", AfterTransient);
                Raise(AuthRequired);
                return Outcome.AuthRequired;
            } catch (ApiException ex) when (ex.Code is ApiCodes.InvalidCode or ApiCodes.ProjectNotOpen or ApiCodes.ProjectDeleted or ApiCodes.NotFound) {
                return Hold(link, LinkStatus.Failed, Describe(ex), AfterFailure);
            } catch (ApiException ex) when (ex.IsTransient) {
                return Hold(link, LinkStatus.Failed, "Rapid Stacker Collab could not be reached: " + ex.Message, AfterTransient);
            } catch (Exception ex) when (ex is ApiException or HttpRequestException) {
                return Hold(link, LinkStatus.Failed, ex.Message, AfterFailure);
            } catch (Exception ex) {
                Logger.Error($"Starfront stacking: joining the stacking project of {link.StarfrontName} failed", ex);
                return Hold(link, LinkStatus.Failed, ex.Message, AfterFailure);
            }
        }

        private Outcome Joined(StackingLink link, Membership membership, string how) {
            link.Status = membership.Status == MemberStatuses.Active ? LinkStatus.Active
                        : membership.Status == MemberStatuses.Pending ? LinkStatus.Pending : LinkStatus.Failed;
            link.Message = link.Status == LinkStatus.Failed ? $"the membership of {link.CollabName} is {membership.Status}" : "";
            link.NextAttemptUtc = link.Status == LinkStatus.Failed ? DateTime.UtcNow + AfterFailure : null;
            if (membership.FilterMapping.Count > 0) link.Mapping = membership.FilterMapping.ToDictionary(m => m.Raw, m => m.Canonical, StringComparer.Ordinal);
            if (!string.IsNullOrEmpty(membership.DisplayName)) link.DisplayName = membership.DisplayName;
            if (!string.IsNullOrEmpty(membership.AcceptedTermsVersion)) link.TermsVersion = membership.AcceptedTermsVersion;
            links.Upsert(link);
            Logger.Info($"Starfront stacking: {link.StarfrontName} -> {link.CollabName} ({how}): {link.Describe()}");
            return Outcome.Done;
        }

        private Outcome Hold(StackingLink link, string status, string message, TimeSpan? retryAfter) {
            link.Status = status;
            link.Message = message ?? "";
            link.NextAttemptUtc = retryAfter is TimeSpan wait ? DateTime.UtcNow + wait : null;
            links.Upsert(link);
            Logger.Warning($"Starfront stacking: {link.StarfrontName}: {link.Describe()}");
            return Outcome.Later;
        }

        private RigUpsertRequest DescribeRig() {
            var profile = host.Profile.ActiveProfile;
            var camera = host.Camera.GetInfo();
            var pixel = settings.PixelSize > 0 ? settings.PixelSize : profile.CameraSettings.PixelSize;
            return new RigUpsertRequest {
                Camera = (camera?.Connected == true ? camera.Name : null) ?? profile.CameraSettings.Id ?? "camera",
                Telescope = string.IsNullOrWhiteSpace(profile.TelescopeSettings.Name) ? "telescope" : profile.TelescopeSettings.Name,
                FocalLengthMm = profile.TelescopeSettings.FocalLength,
                PixelSizeUm = pixel,
                Binning = Math.Max(1, (int)settings.Binning),
                BayerPattern = settings.Colour ? BayerPattern(camera?.SensorType) : null,
                WidthPx = settings.SensorWidth > 0 ? settings.SensorWidth : null,
                HeightPx = settings.SensorHeight > 0 ? settings.SensorHeight : null,
                Name = settings.TelescopeName,
            };
        }

        /// <summary>The sensor's Bayer order as the service spells it. The frames carry their own BAYERPAT, which is what the stack is built from; this only describes the rig.</summary>
        private static string BayerPattern(SensorType? sensor) => sensor switch {
            SensorType.BGGR => "BGGR",
            SensorType.GRBG => "GRBG",
            SensorType.GBRG => "GBRG",
            _ => "RGGB",
        };

        private string DisplayName() {
            var name = settings.StackingDisplayName;
            if (name.Length == 0) name = settings.UserName;
            if (name.Length == 0) name = settings.TelescopeName;
            return Clamp(name);
        }

        /// <summary>Two to forty characters, as the service asks.</summary>
        private static string Clamp(string name) {
            name = (name ?? "").Trim();
            if (name.Length < 2) name = "Starfront telescope";
            return name.Length > 40 ? name[..40].TrimEnd() : name;
        }

        private static JoinRequest Request(string code, string name, string termsVersion, IReadOnlyList<ContributorMappingEntry> mapping) => new() {
            Code = code,
            DisplayName = name,
            AcceptedTermsVersion = termsVersion,
            AllowOwnerRawDownload = false,
            FilterMapping = mapping,
        };

        private static string Describe(ApiException ex) => ex.Code switch {
            ApiCodes.InvalidCode => "the join code is not one Rapid Stacker Collab knows; check it on the collaboration",
            ApiCodes.ProjectNotOpen => "the stacking project is not taking new members",
            ApiCodes.ProjectDeleted => "the stacking project was deleted",
            ApiCodes.NotFound => "the stacking project was not found",
            _ => ex.Message,
        };

        private void Raise(EventHandler handler) {
            try { handler?.Invoke(this, EventArgs.Empty); } catch (Exception ex) { Logger.Error("Starfront stacking: a handler failed", ex); }
        }
    }
}

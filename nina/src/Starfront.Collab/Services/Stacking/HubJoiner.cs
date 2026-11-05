using System.Net.Http;
using System.Text;
using Newtonsoft.Json.Linq;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Filters;
using Starfront.Collab.Server;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>What the hub join needs to know about this PC, apart from the links: settings and the profile, behind a seam so the join can be tested without N.I.N.A.</summary>
    public interface IHubJoinContext {
        /// <summary>What stops the rig being described (camera never connected, no focal length), or null when it can be.</summary>
        string WhatIsMissingForRig();
        RigUpsertRequest DescribeRig();
        IReadOnlyList<string> WheelFilterNames { get; }
        bool Colour { get; }
        IReadOnlyDictionary<string, string> FilterMap { get; }
        /// <summary>The name this person contributes under on a hub: the Starfront user name, else the telescope's. Already clamped.</summary>
        string DisplayName { get; }
        string TelescopeName { get; }
        FilterNormalizer Normalizer { get; }
    }

    /// <summary>The slice of <see cref="StackingLinks"/> the hub join uses.</summary>
    public interface IHubLinkStore {
        StackingLink Lookup(string starfrontProjectId);
        void Upsert(StackingLink link);
        IReadOnlyList<StackingLink> All();
    }

    /// <summary>Which joiner path a listing takes at a check-in, and whether the Rapid Stacker Collab sign-in stands in its way.</summary>
    public enum JoinRoute { Skip, Hub, Cloud, CloudNeedsSignIn }

    public static class JoinRoutes {
        public static JoinRoute Of(ProjectListing listing, bool signedInToCloud) {
            var binding = listing.EffectiveStacking();
            if (string.Equals(binding.Backend, StackingBinding.Hub, StringComparison.OrdinalIgnoreCase)) {
                // The grant the check-in carried is all a hub project needs.
                return string.IsNullOrWhiteSpace(binding.Grant) || string.IsNullOrWhiteSpace(binding.CollabProjectId) ? JoinRoute.Skip : JoinRoute.Hub;
            }
            return signedInToCloud ? JoinRoute.Cloud : JoinRoute.CloudNeedsSignIn;
        }
    }

    /// <summary>The expiry inside a hub grant (<c>sfg_</c> + base64url JSON + "." + signature). The signature is the hub's to check, never the plugin's.</summary>
    public static class HubGrants {
        public static DateTime? ExpiresUtc(string grant) {
            try {
                if (string.IsNullOrEmpty(grant) || !grant.StartsWith("sfg_", StringComparison.Ordinal)) return null;
                var body = grant[4..];
                var dot = body.IndexOf('.');
                if (dot > 0) body = body[..dot];
                body = body.Replace('-', '+').Replace('_', '/');
                body = body.PadRight(body.Length + (4 - body.Length % 4) % 4, '=');
                var claims = JObject.Parse(Encoding.UTF8.GetString(Convert.FromBase64String(body)));
                var exp = claims["exp"];
                if (exp == null || (exp.Type != JTokenType.Integer && exp.Type != JTokenType.Float)) return null;
                return DateTimeOffset.FromUnixTimeSeconds((long)exp).UtcDateTime;
            } catch (Exception) {
                return null;
            }
        }
    }

    /// <summary>
    /// The hub half of the joiner: a collaboration stacked on a Starfront hub is joined with the grant the
    /// check-in carried and nothing else, no Rapid Stacker Collab sign-in and no join code. Every call goes to
    /// the link's own client; the cloud token is never read and the cloud's auth-required flag never raised.
    /// </summary>
    public sealed class HubJoiner {
        private static readonly TimeSpan AfterFailure = TimeSpan.FromHours(6);
        private static readonly TimeSpan AfterTransient = TimeSpan.FromMinutes(30);
        private static readonly TimeSpan AfterNoFilters = TimeSpan.FromHours(1);

        private readonly IHubJoinContext context;
        private readonly IHubLinkStore links;
        private readonly Func<StackingLink, IApiClient> clientFor;
        private readonly Action<string> log;

        /// <summary>The hub read this telescope's membership as active; the time is when that read was sent (see <see cref="MembershipPauses"/>).</summary>
        public Action<StackingLink, DateTimeOffset> MembershipConfirmed { get; set; }

        public HubJoiner(IHubJoinContext context, IHubLinkStore links, Func<StackingLink, IApiClient> clientFor, Action<string> log = null) {
            this.context = context;
            this.links = links;
            this.clientFor = clientFor;
            this.log = log ?? (_ => { });
        }

        /// <summary>The binding of a link already made, for a retry or an accepted-terms join that has no listing to hand.</summary>
        public static StackingBinding BindingOf(StackingLink link) => new() {
            Backend = StackingBinding.Hub, HubId = link.HubId, HubName = link.HubName, HubLanUrl = link.LanApiBase, ApiBase = link.ApiBase,
            CollabProjectId = link.CollabProjectId, Grant = link.Grant,
        };

        /// <summary>Take the check-in's binding into the link (a fresh grant every time) and join when the link is not joined yet.</summary>
        public async Task SyncAsync(ProjectListing listing, StackingBinding binding, CancellationToken ct) {
            var link = links.Lookup(listing.Id);
            var fresh = link == null || !link.IsHub || link.CollabProjectId != binding.CollabProjectId || link.HubId != binding.HubId;
            if (fresh) {
                // A rebound collaboration is a new link, but the telescope is still the same rig on the same hub.
                var rig = link != null && link.IsHub && link.HubId == binding.HubId ? link.RigId : "";
                link = new StackingLink { StarfrontProjectId = listing.Id, Backend = StackingBinding.Hub, Message = "joining", RigId = rig };
            }
            var expires = HubGrants.ExpiresUtc(binding.Grant);
            var lan = binding.HubLanUrl ?? "";
            var name = string.IsNullOrEmpty(listing.Name) ? link.StarfrontName : listing.Name;
            var changed = fresh || link.Grant != binding.Grant || link.GrantExpiresUtc != expires || link.ApiBase != binding.ApiBase
                          || link.LanApiBase != lan || link.StarfrontName != name || link.HubName != (binding.HubName ?? "");
            link.StarfrontName = name;
            link.Backend = StackingBinding.Hub;
            link.HubId = binding.HubId ?? "";
            link.HubName = binding.HubName ?? "";
            link.ApiBase = binding.ApiBase ?? "";
            link.LanApiBase = lan;
            link.CollabProjectId = binding.CollabProjectId ?? "";
            link.Grant = binding.Grant;
            link.GrantExpiresUtc = expires;
            if (changed) links.Upsert(link);
            if (!fresh) {
                if (link.Active) return;
                if (link.NextAttemptUtc is DateTime next && next > DateTime.UtcNow) return;
                if (link.Status is LinkStatus.TermsOutdated or LinkStatus.Pending) return;
            }
            await JoinAsync(link, ct).ConfigureAwait(false);
        }

        /// <summary>Join (or find the membership of) the hub project behind a link that already holds its binding.</summary>
        public async Task JoinAsync(StackingLink link, CancellationToken ct) {
            var client = clientFor(link);
            var asked = DateTimeOffset.Now;
            try {
                var project = await client.GetProjectAsync(link.CollabProjectId, ct).ConfigureAwait(false);
                link.CollabName = project.Name;
                link.RequireStrip = project.Policy.RequireCoordinateStrip;
                link.DeclaredFilters = project.Filters.ToList();
                link.TargetName = project.TargetName ?? "";
                link.Grid = project.Grid ?? link.Grid;
                link.GridPending = project.RotationFromFirstLight == true;
                link.Calibration = project.Policy.Calibration;
                link.TermsVersion = project.TermsVersion;

                if (project.MyMembership is { } mine && mine.Status is MemberStatuses.Active or MemberStatuses.Pending) {
                    Joined(link, mine, "already a member", asked);
                    return;
                }

                // The hub's rig id lives on the hub side (this link, or a sibling on the same hub); the cloud's setting is never read or written here.
                var rigId = RigIds.OfHub(link, links.All());
                if (string.IsNullOrEmpty(rigId)) {
                    var missing = context.WhatIsMissingForRig();
                    if (missing != null) { Hold(link, LinkStatus.NeedsRig, missing, AfterTransient); return; }
                    var saved = await client.UpsertRigAsync(context.DescribeRig(), ct).ConfigureAwait(false);
                    if (!saved.Confirmed) saved = await client.PatchRigAsync(saved.RigId, new RigPatchRequest { Confirmed = true }, ct).ConfigureAwait(false);
                    rigId = saved.RigId;
                    log($"Starfront stacking: this telescope is rig {rigId} on {link.HubId}");
                }
                link.RigId = rigId;
                links.Upsert(link);

                var mapping = StackingMapping.Build(context.WheelFilterNames, project.Filters, context.Colour, context.FilterMap, context.Normalizer);
                if (mapping.AllSkipped) { Hold(link, LinkStatus.NoFilters, mapping.Summary, AfterNoFilters); return; }

                var name = context.DisplayName;
                link.DisplayName = name;
                Membership joined;
                try {
                    joined = await client.JoinProjectAsync(link.CollabProjectId, Request(name, project.TermsVersion, mapping.Entries), ct).ConfigureAwait(false);
                } catch (ApiException ex) when (ex.Code == ApiCodes.DisplayNameTaken) {
                    name = Clamp($"{name} ({context.TelescopeName})");
                    link.DisplayName = name;
                    joined = await client.JoinProjectAsync(link.CollabProjectId, Request(name, project.TermsVersion, mapping.Entries), ct).ConfigureAwait(false);
                } catch (ApiException ex) when (ex.Code == ApiCodes.AlreadyMember) {
                    joined = await client.GetMembershipAsync(link.CollabProjectId, ct).ConfigureAwait(false);
                }
                log($"Starfront stacking: filters for {link.CollabName}: {mapping.Summary}");
                Joined(link, joined, "joined", asked);
            } catch (OperationCanceledException) {
                throw;
            } catch (ApiException ex) when (ex.Code == ApiCodes.TermsOutdated) {
                Hold(link, LinkStatus.TermsOutdated, "the terms of contribution changed", null);
            } catch (ApiException ex) when (ex.IsAuthenticationFailure) {
                // The hub refused the grant: drop it (the next check-in brings a fresh one). The cloud sign-in is none of its business.
                links.Upsert(link);
                new GrantTokenStore(link.StarfrontProjectId, links.Lookup, links.Upsert).Clear();
                log($"Starfront stacking: {link.StarfrontName}: the hub refused this telescope's grant");
            } catch (ApiException ex) when (ex.Code is ApiCodes.ProjectNotOpen or ApiCodes.ProjectDeleted or ApiCodes.NotFound) {
                Hold(link, LinkStatus.Failed, ex.Message, AfterFailure);
            } catch (ApiException ex) when (ex.IsTransient) {
                Hold(link, LinkStatus.Failed, "the hub could not be reached: " + ex.Message, AfterTransient);
            } catch (Exception ex) when (ex is ApiException or HttpRequestException) {
                Hold(link, LinkStatus.Failed, ex.Message, AfterFailure);
            }
        }

        private void Joined(StackingLink link, Membership membership, string how, DateTimeOffset asked) {
            link.Status = membership.Status == MemberStatuses.Active ? LinkStatus.Active
                        : membership.Status == MemberStatuses.Pending ? LinkStatus.Pending : LinkStatus.Failed;
            link.Message = link.Status == LinkStatus.Failed ? $"the membership of {link.CollabName} is {membership.Status}" : "";
            link.NextAttemptUtc = link.Status == LinkStatus.Failed ? DateTime.UtcNow + AfterFailure : null;
            if (membership.FilterMapping.Count > 0) link.Mapping = membership.FilterMapping.ToDictionary(m => m.Raw, m => m.Canonical, StringComparer.Ordinal);
            if (!string.IsNullOrEmpty(membership.DisplayName)) link.DisplayName = membership.DisplayName;
            if (!string.IsNullOrEmpty(membership.AcceptedTermsVersion)) link.TermsVersion = membership.AcceptedTermsVersion;
            links.Upsert(link);
            log($"Starfront stacking: {link.StarfrontName} -> {link.CollabName} ({how}, hub): {link.Describe()}");
            if (link.Status == LinkStatus.Active) MembershipConfirmed?.Invoke(link, asked);
        }

        private void Hold(StackingLink link, string status, string message, TimeSpan? retryAfter) {
            link.Status = status;
            link.Message = message ?? "";
            link.NextAttemptUtc = retryAfter is TimeSpan wait ? DateTime.UtcNow + wait : null;
            links.Upsert(link);
            log($"Starfront stacking: {link.StarfrontName}: {link.Describe()}");
        }

        /// <summary>Two to forty characters, as the service asks.</summary>
        public static string Clamp(string name) {
            name = (name ?? "").Trim();
            if (name.Length < 2) name = "Starfront telescope";
            return name.Length > 40 ? name[..40].TrimEnd() : name;
        }

        private static JoinRequest Request(string name, string termsVersion, IReadOnlyList<ContributorMappingEntry> mapping) => new() {
            Code = "",
            DisplayName = name,
            AcceptedTermsVersion = termsVersion,
            AllowOwnerRawDownload = false,
            FilterMapping = mapping,
        };
    }
}

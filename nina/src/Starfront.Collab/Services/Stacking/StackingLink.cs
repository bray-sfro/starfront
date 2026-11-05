using Newtonsoft.Json;
using RapidStackerCollab.Core.Api;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>What a link to a stacking project is in, as the options page and the log say it.</summary>
    public static class LinkStatus {
        /// <summary>Joined; frames go.</summary>
        public const string Active = "active";
        /// <summary>Joined, waiting for the project's owner to approve; frames queue and wait.</summary>
        public const string Pending = "pending";
        /// <summary>The project's terms changed; somebody has to read and accept them before frames go.</summary>
        public const string TermsOutdated = "terms_outdated";
        /// <summary>The camera has not told the plugin what it is yet.</summary>
        public const string NeedsRig = "needs_rig";
        /// <summary>None of this wheel's filters is one the project wants.</summary>
        public const string NoFilters = "no_filters";
        /// <summary>The join did not go through; the message says why and when it is tried again.</summary>
        public const string Failed = "failed";
        /// <summary>A hub refused the grant: lights are still queued (held by the project pause) and go once the next check-in brings a new grant.</summary>
        public const string GrantRefresh = "grant_refresh";
    }

    /// <summary>One Starfront collaboration's link to the Rapid Stacker Collab project that stacks its lights.</summary>
    public sealed class StackingLink {
        public string StarfrontProjectId { get; set; } = "";
        public string StarfrontName { get; set; } = "";
        public string JoinCode { get; set; } = "";
        public string CollabProjectId { get; set; } = "";
        public string CollabName { get; set; } = "";
        /// <summary>"cloud" or "hub". Links written before hubs existed have no such field and read as cloud.</summary>
        public string Backend { get; set; } = "cloud";
        /// <summary>The API the project is on, ending in a slash; blank for a cloud link means the settings' base.</summary>
        public string ApiBase { get; set; } = "";
        /// <summary>The hub's address on its own network (hub links); null or blank when there is none.</summary>
        public string LanApiBase { get; set; } = "";
        /// <summary>The hub grant this telescope uploads with (hub links); blank for cloud links.</summary>
        public string Grant { get; set; } = "";
        public DateTime? GrantExpiresUtc { get; set; }
        public string HubId { get; set; } = "";
        /// <summary>The hub's name as the Starfront server calls it (hub links), for the options page.</summary>
        public string HubName { get; set; } = "";
        public string Status { get; set; } = LinkStatus.Failed;
        public string Message { get; set; } = "";
        public string TermsVersion { get; set; } = "";
        public string TermsUrl { get; set; } = "";
        public string RigId { get; set; } = "";
        public string DisplayName { get; set; } = "";
        /// <summary>The project insists on frames with the observer's location removed.</summary>
        public bool RequireStrip { get; set; }
        public List<string> DeclaredFilters { get; set; } = new();
        /// <summary>The stacking project's target, as its owner typed it; the other-target rule compares <c>OBJECT</c> with it.</summary>
        public string TargetName { get; set; } = "";
        /// <summary>The stacking project's frame on the sky; null until it is known. A light pointed well away from it is not sent.</summary>
        public Grid Grid { get; set; }
        /// <summary>The frame waits for its first light, which sets its centre and scale: the rule then allows three degrees.</summary>
        public bool GridPending { get; set; }
        /// <summary>The project's calibration policy; "none" takes no flats.</summary>
        public string Calibration { get; set; } = "preferred";
        /// <summary>The wheel's names and the project's filter each delivers, or SKIP; what the membership holds.</summary>
        public Dictionary<string, string> Mapping { get; set; } = new();
        public DateTime UpdatedUtc { get; set; }
        /// <summary>Not before this is a join tried again; null means at the next check-in.</summary>
        public DateTime? NextAttemptUtc { get; set; }

        [JsonIgnore] public bool IsHub => string.Equals(Backend, "hub", StringComparison.OrdinalIgnoreCase);
        [JsonIgnore] public bool Active => Status == LinkStatus.Active && CollabProjectId.Length > 0;
        [JsonIgnore] public bool Feeds => CollabProjectId.Length > 0 && Status is LinkStatus.Active or LinkStatus.Pending or LinkStatus.GrantRefresh;

        /// <summary>Whether the project takes calibration frames at all.</summary>
        [JsonIgnore] public bool TakesCalibration => !string.Equals(Calibration, "none", StringComparison.OrdinalIgnoreCase);

        /// <summary>Why a light is another target's than this project's, or null when it is the project's or cannot be told (no frame yet).</summary>
        public string OtherTargetReason(InitHeader header) =>
            StackingTargets.OtherTargetReason(header, StackingTargets.Project(CollabProjectId, CollabName, TargetName, Grid, GridPending));

        /// <summary>Whether a light through this wheel filter is one the project never wants.</summary>
        public bool Skips(string rawFilter) {
            if (string.IsNullOrWhiteSpace(rawFilter)) return false;
            foreach (var kv in Mapping) {
                if (string.Equals(kv.Key.Trim(), rawFilter.Trim(), StringComparison.OrdinalIgnoreCase)) return kv.Value == "SKIP";
            }
            return false;
        }

        public string Describe() => Status switch {
            LinkStatus.Active => $"Joined {CollabName}",
            LinkStatus.Pending => $"Joined {CollabName}; waiting for its owner to approve",
            LinkStatus.GrantRefresh => $"Waiting for a new grant from Starfront for {CollabName}; frames are kept and sent when it arrives",
            LinkStatus.TermsOutdated => $"The terms of {CollabName} changed; accept them to carry on",
            LinkStatus.NeedsRig => "Not joined yet: " + Message,
            LinkStatus.NoFilters => $"Not joined: none of this wheel's filters is one {CollabName} wants",
            _ => "Not joined: " + Message,
        };
    }

}

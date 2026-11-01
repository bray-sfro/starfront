using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Starfront.Collab.Server {

    /// <summary>A rectangle of sky as the server speaks it: degrees throughout, RA in degrees.</summary>
    public class Region {
        [JsonProperty("ra")] public double Ra { get; set; }
        [JsonProperty("dec")] public double Dec { get; set; }
        [JsonProperty("width")] public double Width { get; set; }
        [JsonProperty("height")] public double Height { get; set; }
        [JsonProperty("rotation")] public double Rotation { get; set; }
    }

    /// <summary>One cell of a telescope's own tiling of a project: cell i is panel i + 1.</summary>
    public class Cell : Region {
        [JsonProperty("row")] public int Row { get; set; }
        [JsonProperty("column")] public int Column { get; set; }
    }

    public class FilterTask {
        [JsonProperty("filter")] public string Filter { get; set; } = "";
        [JsonProperty("exposure")] public double Exposure { get; set; }
        [JsonProperty("hours")] public double Hours { get; set; }
    }

    /// <summary>Tonight's slice of a task: how many frames of which filter on each dealt panel.</summary>
    public class Visit {
        [JsonProperty("seconds")] public double Seconds { get; set; }
        [JsonProperty("frames")] public Dictionary<string, int> Frames { get; set; } = new();
        [JsonProperty("filter")] public string Filter { get; set; }
        [JsonProperty("moon")] public double? Moon { get; set; }
    }

    /// <summary>A telescope's share of a project, as dealt.</summary>
    public class DealTask {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("project")] public string Project { get; set; } = "";
        [JsonProperty("projectName")] public string ProjectName { get; set; } = "";
        [JsonProperty("agent")] public string Agent { get; set; } = "";
        [JsonProperty("region")] public Region Region { get; set; }
        [JsonProperty("filters")] public List<FilterTask> Filters { get; set; } = new();
        [JsonProperty("state")] public string State { get; set; } = "";
        [JsonProperty("version")] public int Version { get; set; }
        [JsonProperty("issued")] public double Issued { get; set; }
        [JsonProperty("note")] public string Note { get; set; } = "";
        [JsonProperty("cells")] public List<Cell> Cells { get; set; } = new();
        [JsonProperty("share")] public List<int> Share { get; set; } = new();
        [JsonProperty("kind")] public string Kind { get; set; } = "mosaic";
        [JsonProperty("visit")] public Visit Visit { get; set; }
        [JsonProperty("assignedNight")] public string AssignedNight { get; set; } = "";
        [JsonProperty("dealtHours")] public double? DealtHours { get; set; }

        public bool Live => State == "offered" || State == "accepted";

        /// <summary>The exposure this task shoots a filter at, by the one spelling of the name.</summary>
        public double ExposureFor(string filter) {
            var wanted = Sky.Filters.Canonical(filter);
            foreach (var entry in Filters ?? new List<FilterTask>()) {
                if (Sky.Filters.Same(entry.Filter, wanted)) return entry.Exposure;
            }
            return 0.0;
        }
    }

    public class Requirements {
        [JsonProperty("minFocalLength")] public double? MinFocalLength { get; set; }
        [JsonProperty("maxFocalLength")] public double? MaxFocalLength { get; set; }
        [JsonProperty("minScale")] public double? MinScale { get; set; }
        [JsonProperty("maxScale")] public double? MaxScale { get; set; }
        [JsonProperty("acceptColour")] public bool AcceptColour { get; set; } = true;
        [JsonProperty("acceptOscRgb")] public bool AcceptOscRgb { get; set; } = true;
        [JsonProperty("acceptOscNarrowband")] public bool AcceptOscNarrowband { get; set; } = true;
        [JsonProperty("maxBortle")] public int? MaxBortle { get; set; }
        [JsonProperty("colourMaxMoon")] public double? ColourMaxMoon { get; set; }
        [JsonProperty("maxHfr")] public double? MaxHfr { get; set; }
        [JsonProperty("maxGuideRms")] public double? MaxGuideRms { get; set; }
        [JsonProperty("minExposure")] public double? MinExposure { get; set; }
        [JsonProperty("maxExposure")] public double? MaxExposure { get; set; }
        [JsonProperty("filters")] public Dictionary<string, double?> Filters { get; set; } = new();
        [JsonProperty("maxMoonIllumination")] public double? MaxMoonIllumination { get; set; }
        [JsonProperty("minMoonSeparation")] public double? MinMoonSeparation { get; set; }
        [JsonProperty("minAltitude")] public double? MinAltitude { get; set; }
        [JsonProperty("requireCalibrated")] public bool RequireCalibrated { get; set; }
        [JsonProperty("minFramesPerVisit")] public int MinFramesPerVisit { get; set; } = 10;
    }

    public class TaskAnswer {
        [JsonProperty("task")] public DealTask Task { get; set; }
        [JsonProperty("tasks")] public List<DealTask> Tasks { get; set; }
        [JsonProperty("requirementsByProject")] public Dictionary<string, Requirements> RequirementsByProject { get; set; } = new();
        [JsonProperty("requirements")] public Requirements Requirements { get; set; }
        [JsonProperty("version")] public int Version { get; set; }
        [JsonProperty("protocol")] public int Protocol { get; set; }

        public List<DealTask> All => Tasks ?? (Task != null ? new List<DealTask> { Task } : new List<DealTask>());
    }

    public class CompatibilityCheck {
        [JsonProperty("check")] public string Check { get; set; } = "";
        [JsonProperty("ok")] public bool? Ok { get; set; }
        [JsonProperty("detail")] public string Detail { get; set; } = "";
    }

    public class Compatibility {
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("certain")] public bool Certain { get; set; }
        [JsonProperty("summary")] public string Summary { get; set; } = "";
        [JsonProperty("checks")] public List<CompatibilityCheck> Checks { get; set; } = new();
    }

    public class ProgressFigures {
        [JsonProperty("goalHours")] public double GoalHours { get; set; }
        [JsonProperty("atGoal")] public double AtGoal { get; set; }
        [JsonProperty("average")] public double Average { get; set; }
        [JsonProperty("thinnest")] public double Thinnest { get; set; }
    }

    /// <summary>Where a collaboration's lights are stacked, as the server lists it: Rapid Stacker Collab in the cloud, or a hub on the owner's network.</summary>
    public class StackingBinding {
        public const string Cloud = "cloud";
        public const string Hub = "hub";

        /// <summary>"cloud", "hub", or blank when the collaboration is not stacked.</summary>
        [JsonProperty("backend")] public string Backend { get; set; } = "";
        [JsonProperty("hubId")] public string HubId { get; set; } = "";
        [JsonProperty("hubName")] public string HubName { get; set; } = "";
        /// <summary>The hub's address on its own network, for the LAN short-cut; blank when it has none.</summary>
        [JsonProperty("hubLanUrl")] public string HubLanUrl { get; set; } = "";
        [JsonProperty("apiBase")] public string ApiBase { get; set; } = "";
        [JsonProperty("collabProjectId")] public string CollabProjectId { get; set; } = "";
        /// <summary>The token that lets this telescope upload to the hub (hub projects only).</summary>
        [JsonProperty("grant")] public string Grant { get; set; } = "";
        /// <summary>A cloud project the Starfront server made from the collaboration and keeps in step (T706); false for an old pasted join code.</summary>
        [JsonProperty("managed")] public bool Managed { get; set; }
        /// <summary>"ready" (a telescope on it can join), "pending" (the cloud project is still being made), "failed" (making it failed and is retried), or blank (not stacked, or a server that predates it). Shown, never routed on.</summary>
        [JsonProperty("state")] public string State { get; set; } = "";
        /// <summary>Why it failed; only the owner's own telescopes are told, blank for everyone else.</summary>
        [JsonProperty("problem")] public string Problem { get; set; } = "";
        /// <summary>The join code of a cloud project; not sent by the server inside the binding, filled in for old servers.</summary>
        [JsonIgnore] public string JoinCode { get; set; } = "";
    }

    /// <summary>An open collaboration as the server lists it for this telescope.</summary>
    public class ProjectListing {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("coordinator")] public string Coordinator { get; set; } = "";
        [JsonProperty("ownerId")] public string OwnerId { get; set; } = "";
        [JsonProperty("created")] public double Created { get; set; }
        [JsonProperty("region")] public Region Region { get; set; }
        [JsonProperty("kind")] public string Kind { get; set; } = "mosaic";
        [JsonProperty("requirements")] public Requirements Requirements { get; set; } = new();
        [JsonProperty("goals")] public Dictionary<string, double> Goals { get; set; } = new();
        [JsonProperty("notes")] public string Notes { get; set; } = "";
        /// <summary>Whether the lights are collected on the server for the coordinator to stack, and where to send them.</summary>
        [JsonProperty("collectFiles")] public bool CollectFiles { get; set; }
        [JsonProperty("filesUrl")] public string FilesUrl { get; set; } = "";
        /// <summary>Whether the lights are stacked as they land by Rapid Stacker Collab, and the code that gets this telescope into that project (handed to telescopes on the collaboration and to the owner's own; blank to the rest).</summary>
        [JsonProperty("stackingLinked")] public bool StackingLinked { get; set; }
        [JsonProperty("stackingJoinCode")] public string StackingJoinCode { get; set; } = "";
        /// <summary>Where the lights are stacked; null from servers that predate per-project backends (see <see cref="EffectiveStacking"/>).</summary>
        [JsonProperty("stacking")] public StackingBinding Stacking { get; set; }
        [JsonProperty("compatibility")] public Compatibility Compatibility { get; set; } = new();
        [JsonProperty("joined")] public bool Joined { get; set; }
        [JsonProperty("collected")] public Dictionary<string, double> Collected { get; set; } = new();
        [JsonProperty("progress")] public Dictionary<string, ProgressFigures> Progress { get; set; } = new();
        [JsonProperty("participants")] public int Participants { get; set; }
        [JsonProperty("participantsOnline")] public int ParticipantsOnline { get; set; }
        [JsonProperty("participantNames")] public List<string> ParticipantNames { get; set; } = new();

        /// <summary>
        /// The binding to act on: the server's, or for an old server that only
        /// sends a join code, a cloud binding carrying it. Never null; its
        /// <c>Backend</c> is blank when the collaboration is not stacked.
        /// </summary>
        public StackingBinding EffectiveStacking() {
            if (Stacking != null) return Stacking;
            var code = StackingJoinCode ?? "";
            return code.Length > 0
                ? new StackingBinding { Backend = StackingBinding.Cloud, JoinCode = code }
                : new StackingBinding();
        }
    }

    public class TelescopePresence {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("enrolledAs")] public string EnrolledAs { get; set; } = "";
        [JsonProperty("owner")] public string Owner { get; set; } = "";
        [JsonProperty("ownerName")] public string OwnerName { get; set; } = "";
        [JsonProperty("avatar")] public string Avatar { get; set; } = "";
        [JsonProperty("ra")] public double? Ra { get; set; }
        [JsonProperty("dec")] public double? Dec { get; set; }
        [JsonProperty("state")] public string State { get; set; } = "";
        [JsonProperty("target")] public string Target { get; set; } = "";
        [JsonProperty("project")] public string Project { get; set; } = "";
        [JsonProperty("ageSeconds")] public int AgeSeconds { get; set; }
        [JsonProperty("online")] public bool Online { get; set; }
    }

    public class PresenceAnswer {
        [JsonProperty("telescopes")] public List<TelescopePresence> Telescopes { get; set; } = new();
        [JsonProperty("online")] public int Online { get; set; }
        [JsonProperty("people")] public int People { get; set; }
        [JsonProperty("serverTime")] public double ServerTime { get; set; }
    }

    public class LoginBegun {
        [JsonProperty("code")] public string Code { get; set; } = "";
        [JsonProperty("url")] public string Url { get; set; } = "";
        [JsonProperty("expiresIn")] public double ExpiresIn { get; set; } = 600;
    }

    public class SignedInUser {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("avatar")] public string Avatar { get; set; } = "";
        [JsonProperty("admin")] public bool Admin { get; set; }
        [JsonProperty("canStart")] public bool CanStart { get; set; }
    }

    public class LoginPolled {
        [JsonProperty("state")] public string State { get; set; } = "";
        [JsonProperty("token")] public string Token { get; set; }
        [JsonProperty("user")] public SignedInUser User { get; set; }
    }

    public class HelloAnswer {
        [JsonProperty("agent")] public string Agent { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("protocol")] public int Protocol { get; set; }
        [JsonProperty("serverTime")] public double ServerTime { get; set; }
    }

    public class EnrolledAgent {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("owner")] public string Owner { get; set; } = "";
    }

    public class EnrolAnswer {
        [JsonProperty("agent")] public EnrolledAgent Agent { get; set; }
        [JsonProperty("token")] public string Token { get; set; } = "";
    }

    public class JoinAnswer {
        [JsonProperty("task")] public DealTask Task { get; set; }
        [JsonProperty("requirements")] public Requirements Requirements { get; set; }
        [JsonProperty("alreadyJoined")] public bool AlreadyJoined { get; set; }
    }

    public class RecordedRow {
        [JsonProperty("id")] public string Id { get; set; }
        [JsonProperty("accepted")] public bool Accepted { get; set; }
        [JsonProperty("duplicate")] public bool Duplicate { get; set; }
        [JsonProperty("verdict")] public JObject Verdict { get; set; }

        public string Summary => Verdict?["summary"]?.ToString() ?? "";
    }

    public class ReportAnswer {
        [JsonProperty("recorded")] public List<RecordedRow> Recorded { get; set; } = new();
    }

    /// <summary>An object the server found by name, for the start-a-collaboration form.</summary>
    public class LookupHit {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("type")] public string Type { get; set; } = "";
        [JsonProperty("ra")] public double Ra { get; set; }
        [JsonProperty("dec")] public double Dec { get; set; }
        [JsonProperty("sizeArcmin")] public double? SizeArcmin { get; set; }
        [JsonProperty("constellation")] public string Constellation { get; set; } = "";
        [JsonProperty("source")] public string Source { get; set; } = "";
    }

    public class LookupAnswer {
        [JsonProperty("results")] public List<LookupHit> Results { get; set; } = new();
        [JsonProperty("source")] public string Source { get; set; } = "";
    }

    public class HealthAnswer {
        [JsonProperty("ok")] public bool Ok { get; set; }
        [JsonProperty("protocol")] public int Protocol { get; set; }
        [JsonProperty("version")] public string Version { get; set; } = "";
        [JsonProperty("discord")] public bool Discord { get; set; }
    }
}

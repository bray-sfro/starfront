using System.Linq;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Starfront.Collab.Server {

    /// <summary>One scope as the server lists it for this agent.</summary>
    public sealed class ScopeInfo {
        [JsonProperty("id")] public string Id { get; set; } = "";
        [JsonProperty("name")] public string Name { get; set; } = "";
        [JsonProperty("displayName")] public string DisplayName { get; set; } = "";
        [JsonProperty("boundToThisAgent")] public bool BoundToThisAgent { get; set; }
        [JsonProperty("cameraSerial")] public string CameraSerial { get; set; } = "";
        [JsonProperty("cameraModel")] public string CameraModel { get; set; } = "";

        /// <summary>The label in the dropdown. The server's display name, else its name, else the id.</summary>
        public string Label =>
            !string.IsNullOrWhiteSpace(DisplayName) ? DisplayName.Trim()
            : !string.IsNullOrWhiteSpace(Name) ? Name.Trim()
            : (Id ?? "").Trim();
    }

    /// <summary>One row of the scope dropdown. The value stored is <see cref="Id"/>.</summary>
    public sealed class ScopeChoice {
        public string Id { get; init; } = "";
        public string DisplayName { get; init; } = "";
        public bool Bound { get; init; }
    }

    /// <summary>
    /// The scopes this agent last heard about, and the rules for listing them
    /// and for a 400/403/409 that belongs to one file.
    ///
    /// Nothing here makes up an id. Registering a scope is a person's action
    /// on the server.
    /// </summary>
    public static class ScopeDirectory {
        public const string RegisterUrl = "https://collab.starfront.space";

        private static readonly object Gate = new();
        private static List<ScopeInfo> items = new();

        /// <summary>Raised after <see cref="Replace"/>. The options page rebuilds its dropdown.</summary>
        public static event Action Changed;

        public static int BoundCount {
            get { lock (Gate) return items.Count(row => row.BoundToThisAgent); }
        }

        public static void Replace(IEnumerable<ScopeInfo> scopes) {
            var next = new List<ScopeInfo>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var row in scopes ?? Enumerable.Empty<ScopeInfo>()) {
                var id = (row?.Id ?? "").Trim();
                if (id.Length == 0 || !seen.Add(id)) continue;
                next.Add(row);
            }
            lock (Gate) items = next;
            try { Changed?.Invoke(); } catch (Exception) { }
        }

        public static List<ScopeInfo> Snapshot() {
            lock (Gate) return items.ToList();
        }

        /// <summary>Bound scopes first, then by display name. Ids are the server's.</summary>
        public static List<ScopeChoice> ChoicesOf(IEnumerable<ScopeInfo> scopes) {
            var rows = new List<ScopeChoice>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (var row in scopes ?? Enumerable.Empty<ScopeInfo>()) {
                var id = (row?.Id ?? "").Trim();
                if (id.Length == 0 || !seen.Add(id)) continue;
                rows.Add(new ScopeChoice { Id = id, DisplayName = row.Label, Bound = row.BoundToThisAgent });
            }
            rows.Sort((a, b) => {
                var bound = (a.Bound ? 0 : 1).CompareTo(b.Bound ? 0 : 1);
                if (bound != 0) return bound;
                var name = string.Compare(a.DisplayName, b.DisplayName, StringComparison.OrdinalIgnoreCase);
                return name != 0 ? name : string.Compare(a.Id, b.Id, StringComparison.Ordinal);
            });
            return rows;
        }

        public static List<ScopeChoice> Choices() => ChoicesOf(Snapshot());

        public static List<ScopeInfo> Read(JToken token) {
            if (token == null) return new List<ScopeInfo>();
            var array = token as JArray ?? token["scopes"] as JArray;
            if (array == null) return new List<ScopeInfo>();
            return array.ToObject<List<ScopeInfo>>() ?? new List<ScopeInfo>();
        }

        /// <summary>
        /// A per-file scope answer, as the status to show while the file is held.
        /// Null when this answer is something else (the collaboration is not
        /// taking uploads, the frame itself was refused, a resume).
        /// </summary>
        public static string HoldMessage(int code, JObject body) {
            if (code != 400 && code != 403 && code != 409) return null;
            if (body == null || body["offset"] != null) return null;
            var detail = (body.Value<string>("detail") ?? "").Trim();
            var low = detail.ToLowerInvariant();
            if (code == 409 && (low.Contains("auto-upload") || low.Contains("uploads are off")
                                || low.Contains("upload is off") || low.Contains("not taking"))) return null;
            var kind = (body.Value<string>("error") ?? body.Value<string>("code") ?? "").Trim().ToLowerInvariant();
            var known = kind is "not_bound" or "unknown_scope" or "foreign_scope"
                or "missing_scope" or "camera_miss" or "scope";
            var textual = low.Contains("scope") || low.Contains("not bound") || low.Contains("unbound")
                || low.Contains("instrume") || low.Contains("camera miss")
                || low.Contains("rasa-l") || low.Contains("rasa-a") || low.Contains("rasa-r");
            if (!known && !textual) return null;
            if (detail.Length > 0) return detail;
            return code switch {
                400 => "the server does not know that scope",
                403 => "that scope is not this telescope's",
                _ => "this file is not bound to a scope",
            };
        }
    }
}

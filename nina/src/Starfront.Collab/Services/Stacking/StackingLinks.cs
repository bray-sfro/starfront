using System.IO;
using Newtonsoft.Json;
using NINA.Core.Utility;
using RapidStackerCollab.Core.Api;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// The links on disk, one per Starfront collaboration, in the plugin's
    /// own folder beside the ledger and with its habits: written whole to a
    /// temporary file and moved into place, and a file that cannot be read
    /// is kept aside rather than overwritten.
    /// </summary>
    public sealed class StackingLinks : IHubLinkStore {
        private readonly object gate = new();
        private readonly string path;
        private Dictionary<string, StackingLink> links = new(StringComparer.Ordinal);

        public StackingLinks(string path) => this.path = path;

        public event EventHandler Changed;

        public void Load() {
            lock (gate) {
                try {
                    if (File.Exists(path)) {
                        var rows = JsonConvert.DeserializeObject<List<StackingLink>>(File.ReadAllText(path)) ?? new List<StackingLink>();
                        links = rows.Where(l => !string.IsNullOrEmpty(l.StarfrontProjectId)).ToDictionary(l => l.StarfrontProjectId, l => l, StringComparer.Ordinal);
                    }
                } catch (Exception ex) {
                    Logger.Error("Starfront stacking: the links file could not be read; starting afresh", ex);
                    try { File.Copy(path, path + ".broken", true); } catch (Exception) { }
                    links = new Dictionary<string, StackingLink>(StringComparer.Ordinal);
                }
            }
            try { Changed?.Invoke(this, EventArgs.Empty); } catch (Exception ex) { Logger.Error("Starfront stacking: a links handler failed", ex); }
        }

        private void Save() {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            var temporary = path + ".tmp";
            var rows = links.Values.OrderBy(l => l.StarfrontName, StringComparer.OrdinalIgnoreCase).ToList();
            File.WriteAllText(temporary, JsonConvert.SerializeObject(rows, Formatting.Indented));
            File.Move(temporary, path, true);
        }

        public StackingLink Lookup(string starfrontProjectId) {
            lock (gate) return !string.IsNullOrEmpty(starfrontProjectId) && links.TryGetValue(starfrontProjectId, out var link) ? link : null;
        }

        public StackingLink ByCollabProject(string collabProjectId) {
            lock (gate) return links.Values.FirstOrDefault(l => l.CollabProjectId == collabProjectId);
        }

        public IReadOnlyList<StackingLink> All() {
            lock (gate) return links.Values.OrderBy(l => l.StarfrontName, StringComparer.OrdinalIgnoreCase).ToList();
        }

        /// <summary>The project insists on stripped frames: the runner strips whatever the person's own switch says.</summary>
        public bool RequireStrip(string collabProjectId) => ByCollabProject(collabProjectId)?.RequireStrip ?? false;

        public void Upsert(StackingLink link) {
            lock (gate) {
                link.UpdatedUtc = DateTime.UtcNow;
                links[link.StarfrontProjectId] = link;
                try { Save(); } catch (Exception ex) { Logger.Error("Starfront stacking: the links file could not be written", ex); }
            }
            try { Changed?.Invoke(this, EventArgs.Empty); } catch (Exception ex) { Logger.Error("Starfront stacking: a links handler failed", ex); }
        }

        public void Remove(string starfrontProjectId) {
            lock (gate) {
                if (!links.Remove(starfrontProjectId)) return;
                try { Save(); } catch (Exception ex) { Logger.Error("Starfront stacking: the links file could not be written", ex); }
            }
            try { Changed?.Invoke(this, EventArgs.Empty); } catch (Exception ex) { Logger.Error("Starfront stacking: a links handler failed", ex); }
        }
    }
}

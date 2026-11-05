using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Filters;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Which of the stacking project's filters each of this wheel's filters
    /// delivers, worked out without asking: the plugin joins a project at a
    /// check-in, with nobody at the keyboard. What the person chose on the
    /// Starfront options page (the delivery table) is taken first; the rest
    /// is read from the filter's name by the stacking service's own table;
    /// a filter the project does not use, or one that cannot be read, is
    /// marked SKIP, which the service treats as "never upload this one". A
    /// mapping can be changed later on the Rapid Stacker Collab website.
    /// </summary>
    public static class StackingMapping {
        public const string Skip = "SKIP";

        /// <summary>Starfront's delivery codes to the stacking service's canonical filters.</summary>
        private static readonly Dictionary<string, string> Deliveries = new(StringComparer.OrdinalIgnoreCase) {
            ["L"] = "L", ["R"] = "R", ["G"] = "G", ["B"] = "B",
            ["H"] = "HA", ["O"] = "OIII", ["S"] = "SII",
            ["RGB"] = "OSC",
            ["HO"] = "DUO", ["SO"] = "DUO", ["HS"] = "DUO", ["HOS"] = "DUO",
            ["-"] = Skip,
        };

        private static readonly string[] BroadbandLetters = { "L", "R", "G", "B" };

        public sealed record Result(IReadOnlyList<ContributorMappingEntry> Entries, string Summary, bool AllSkipped);

        public static Result Build(IEnumerable<string> wheelNames, IReadOnlyList<string> declared, bool bayer,
                                   IReadOnlyDictionary<string, string> starfrontDeliveries, FilterNormalizer normalizer) {
            var wanted = new HashSet<string>(declared ?? Array.Empty<string>(), StringComparer.Ordinal);
            // The person's own table, where it names a filter this project wants.
            var own = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var kv in starfrontDeliveries ?? new Dictionary<string, string>()) {
                if (Deliveries.TryGetValue(kv.Value ?? "", out var canonical) && (canonical == Skip || wanted.Contains(canonical))) own[kv.Key.Trim()] = canonical;
            }

            var entries = new List<ContributorMappingEntry>();
            var summary = new List<string>();
            foreach (var name in (wheelNames ?? Array.Empty<string>()).Where(n => !string.IsNullOrWhiteSpace(n)).Select(n => n.Trim()).Distinct(StringComparer.Ordinal)) {
                var canonical = Skip;
                var guess = normalizer.Normalize(name, new FilterContext(declared, bayer, Contributor: own));
                if (!guess.IsKnown && guess.Canonical != Skip) {
                    // A bare L, R, G or B resolves only when the project declares
                    // it; ask again as if it did, to tell "broadband this project
                    // does not use" from "unreadable". Both end as SKIP here.
                    guess = normalizer.Normalize(name, new FilterContext(declared.Concat(BroadbandLetters).Distinct().ToList(), bayer, Contributor: own));
                }
                if (guess.IsKnown && wanted.Contains(guess.Canonical)) canonical = guess.Canonical;
                entries.Add(new ContributorMappingEntry { Raw = name, Canonical = canonical });
                summary.Add(canonical == Skip ? $"{name} skipped" : $"{name} = {canonical}");
            }
            var allSkipped = entries.Count == 0 || entries.All(e => e.Canonical == Skip);
            return new Result(entries, string.Join(", ", summary), allSkipped);
        }
    }
}

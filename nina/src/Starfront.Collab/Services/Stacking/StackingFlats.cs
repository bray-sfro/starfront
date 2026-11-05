using System.IO;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Frames;
using RapidStackerCollab.Core.Upload;
using RapidStackerCollab.Core.Util;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// What the flat router needs to know about the stacking projects. The
    /// uploader answers from the links and the ledger; the tests answer from a
    /// table. No N.I.N.A. here, so the router runs under the tests.
    /// </summary>
    public interface IFlatProjects {
        /// <summary>The project's name when it may take a flat of this wheel filter (linked and feeding, calibration not "none", the filter not skipped); otherwise null.</summary>
        string Eligible(string collabProjectId, string rawFilter);

        /// <summary>The stacking project of the collaboration this target name belongs to, as the ledger matches lights; null when none.</summary>
        string ProjectOfTarget(string objectName);

        /// <summary>The stacking project of the last light sent; null when none.</summary>
        string LastProject { get; }
    }

    public enum FlatPlanKind {
        /// <summary>Queue the flat for <see cref="FlatPlan.Projects"/> now.</summary>
        Route,
        /// <summary>No project has the night's lights yet; wait (an evening flat) until <see cref="FlatPlan.HoldUntil"/>.</summary>
        Hold,
        /// <summary>Nothing takes it; it is not sent.</summary>
        Skip,
    }

    /// <summary>Where a flat goes. <see cref="Projects"/> are stacking project ids.</summary>
    public sealed record FlatPlan(FlatPlanKind Kind, IReadOnlyList<string> Projects, string Reason, DateTimeOffset? HoldUntil = null);

    /// <summary>
    /// Sends each flat to the stacking projects that got the lights it
    /// calibrates. A flat carries no pointing, and an evening flat is taken
    /// before the night's first light, so a flat is matched to lights already
    /// queued by rig, filter and local night, and an unmatched one waits until
    /// a matching light is queued or, at local noon plus an hour, takes the
    /// fallbacks of <see cref="Plan"/>: lights in the same folder, the target
    /// name, lights of a night up to three away, the project of the last light
    /// sent. A night whose lights were all of another target sends its flats
    /// nowhere: those flats are that target's, and uploading them would only
    /// cost the person quota on a project that cannot use them.
    ///
    /// Ported from Rapid Stacker Collab's plugin
    /// (<c>RapidStackerCollab.Shell/Intake/FlatRouter.cs</c>); the active
    /// project there is the last light's project here, and its project catalog
    /// is the links.
    /// </summary>
    public sealed class StackingFlats {
        /// <summary>The farthest night, either side, whose lights the nearest-night fallback looks at.</summary>
        public const int NearestNightRadius = 3;

        private readonly UploadQueue queue;
        private readonly IFlatProjects projects;
        private readonly Func<string> rigId;
        private readonly IClock clock;
        private readonly Action<string> info;
        private readonly Action<string, Exception> error;
        private readonly object gate = new();

        public StackingFlats(UploadQueue queue, IFlatProjects projects, Func<string> rigId, IClock clock, TimeZoneInfo timeZone,
                             Action<string> info = null, Action<string, Exception> error = null) {
            this.queue = queue;
            this.projects = projects;
            this.rigId = rigId;
            this.clock = clock;
            TimeZone = timeZone;
            this.info = info ?? (_ => { });
            this.error = error ?? ((_, _) => { });
        }

        /// <summary>The PC's time zone: the only thing a night is computed from.</summary>
        public TimeZoneInfo TimeZone { get; }

        /// <summary>Raised when a flat was queued for a project, so the runner is woken.</summary>
        public event Action<QueueItem> Enqueued;

        /// <summary>Flats still waiting for lights, for the dock.</summary>
        public int Waiting => queue.CountHeldFlats();

        private string Rig() {
            var rig = rigId();
            return string.IsNullOrWhiteSpace(rig) ? null : rig;
        }

        // -- planning ----------------------------------------------------------

        /// <summary>What would happen to this flat now. <paramref name="immediate"/> skips the wait (the hold's end).</summary>
        public FlatPlan Plan(string path, InitHeader header, DateTimeOffset now, bool immediate) {
            var rig = Rig();
            var filterKey = FlatRouting.FilterKey(header.Filter);
            var obsUtc = FlatRouting.ParseDateObs(header.DateObs);
            var night = obsUtc is DateTime t ? FlatRouting.Format(FlatRouting.LocalNight(t, TimeZone)) : null;

            if (night != null) {
                var refs = queue.LightProjects(rig, filterKey, night, TimeZone);
                if (refs.Count > 0) {
                    var eligible = Eligible(refs, header.Filter);
                    return eligible.Count > 0
                        ? Route(eligible, $"lights of the same filter and night went to {Names(eligible)}")
                        : new FlatPlan(FlatPlanKind.Skip, Array.Empty<string>(), "the projects that got this night's lights take no calibration, or not this filter");
                }
            }

            var holdUntil = obsUtc is DateTime o ? FlatRouting.HoldUntil(o, TimeZone) : now;
            if (!immediate && now < holdUntil) return new FlatPlan(FlatPlanKind.Hold, Array.Empty<string>(), "waiting for this night's lights", holdUntil);

            // No project took lights of this filter tonight, and some of the night's lights were of another target: the flat is that target's.
            if (night != null && queue.CountOtherTargetLights(rig, night) > 0) {
                return new FlatPlan(FlatPlanKind.Skip, Array.Empty<string>(), "this night's lights of that filter were of another target, not of a linked collaboration");
            }

            return Fallbacks(path, header, rig, filterKey, night);
        }

        private FlatPlan Fallbacks(string path, InitHeader header, string rig, string filterKey, string night) {
            // 1. Lights in the same folder: <Target>/<date>/FLAT beside <Target>/<date>/LIGHT, or flats lying with the lights.
            var parent = SafeDirectory(path);
            var grand = parent == null ? null : SafeDirectory(parent);
            foreach (var folder in new[] { grand, parent }.Where(f => !string.IsNullOrEmpty(f)).Distinct(StringComparer.OrdinalIgnoreCase)) {
                var eligible = Eligible(queue.LightProjectsUnder(folder), header.Filter);
                if (eligible.Count > 0) return Route(eligible, $"lights in the same folder went to {Names(eligible)}");
            }

            // 2. The target name, as the ledger matches lights.
            if (projects.ProjectOfTarget(header.Object) is string named && Eligible(new[] { named }, header.Filter) is { Count: > 0 } byName) {
                return Route(byName, $"the target name is {byName[0].Name}'s");
            }

            // 3. Lights on a nearby night, nearest first.
            if (FlatRouting.ParseNight(night) is DateOnly flatNight) {
                var nights = queue.LightNights(rig, filterKey, TimeZone);
                for (var d = 1; d <= NearestNightRadius; d++) {
                    var wanted = new[] { FlatRouting.Format(flatNight.AddDays(-d)), FlatRouting.Format(flatNight.AddDays(d)) };
                    var refs = nights.Where(n => wanted.Contains(n.Night)).Select(n => n.ProjectRef).Distinct().ToList();
                    var eligible = Eligible(refs, header.Filter);
                    if (eligible.Count > 0) return Route(eligible, $"lights {d} night(s) away went to {Names(eligible)}");
                }
            }

            // 4. The project of the last light sent.
            if (projects.LastProject is string last && Eligible(new[] { last }, header.Filter) is { Count: > 0 } byLast) {
                return Route(byLast, $"the last light sent went to {byLast[0].Name}");
            }

            return new FlatPlan(FlatPlanKind.Skip, Array.Empty<string>(), "no linked project has lights for this flat's filter and night");
        }

        private sealed record Target(string ProjectId, string Name);

        private List<Target> Eligible(IEnumerable<string> projectRefs, string rawFilter) {
            var result = new List<Target>();
            foreach (var id in projectRefs.Distinct()) {
                if (string.IsNullOrEmpty(id)) continue;
                var name = projects.Eligible(id, rawFilter);
                if (name != null) result.Add(new Target(id, name));
            }
            return result;
        }

        // -- acting ------------------------------------------------------------

        /// <summary>Plan the flat, remember it, and queue it for the projects the plan names. Idempotent per project and path.</summary>
        public FlatPlan OnFlat(string path, InitHeader header, bool immediate) {
            lock (gate) {
                var now = clock.Now;
                var obsUtc = FlatRouting.ParseDateObs(header.DateObs);
                var night = obsUtc is DateTime t ? FlatRouting.Format(FlatRouting.LocalNight(t, TimeZone)) : "";
                var holdUntil = obsUtc is DateTime o ? FlatRouting.HoldUntil(o, TimeZone) : now;
                queue.AddFlatRoute(path, header, Rig(), FlatRouting.FilterKey(header.Filter), night, holdUntil);
                var plan = Plan(path, header, now, immediate);
                Apply(path, header, plan);
                return plan;
            }
        }

        /// <summary>A light was queued: every held or routed flat of its rig, filter and night also goes to its project.</summary>
        public void OnLightEnqueued(QueueItem light) {
            if (light.Kind != FrameKind.Light) return;
            try {
                lock (gate) {
                    var night = FlatRouting.NightOf(light.Header, TimeZone);
                    if (night == null) return;
                    foreach (var flat in queue.FlatsMatching(light.RigId, FlatRouting.FilterKey(light.Header.Filter), night)) {
                        if (flat.RoutedTo.Contains(light.ProjectRef)) continue;
                        var eligible = Eligible(new[] { light.ProjectRef }, flat.Header.Filter);
                        if (eligible.Count == 0) continue;
                        Apply(flat.Path, flat.Header, Route(eligible, $"lights of the same filter and night went to {eligible[0].Name}"));
                    }
                }
            } catch (Exception ex) {
                error("Starfront stacking: routing held flats to a new light failed", ex);
            }
        }

        /// <summary>Run the fallbacks for held flats whose wait is over. Returns how many were settled.</summary>
        public int Tick(DateTimeOffset now) {
            var settled = 0;
            try {
                lock (gate) {
                    foreach (var flat in queue.HeldFlats().Where(f => f.HoldUntil <= now)) {
                        Apply(flat.Path, flat.Header, Plan(flat.Path, flat.Header, now, immediate: true));
                        settled++;
                    }
                }
            } catch (Exception ex) {
                error("Starfront stacking: routing held flats failed", ex);
            }
            return settled;
        }

        private void Apply(string path, InitHeader header, FlatPlan plan) {
            var already = queue.GetFlatRoute(path)?.RoutedTo ?? Array.Empty<string>();
            var name = Path.GetFileName(path);
            switch (plan.Kind) {
                case FlatPlanKind.Route: {
                    var kind = FrameKinds.Classify(header.ImageTyp);
                    var night = FlatRouting.NightOf(header, TimeZone) ?? "";
                    var filterKey = FlatRouting.FilterKey(header.Filter);
                    foreach (var project in plan.Projects) {
                        var r = queue.Enqueue(new NewQueueItem(project, path, name, kind, header, Rig(), night, filterKey));
                        if (!r.Added) continue;
                        try { Enqueued?.Invoke(r.Item); } catch (Exception ex) { error("Starfront stacking: an enqueue handler failed", ex); }
                    }
                    queue.SetFlatRoute(path, FlatRouteStates.Routed, already.Union(plan.Projects).ToList(), plan.Reason);
                    info($"Starfront stacking: flat {name}: {plan.Reason}");
                    break;
                }
                case FlatPlanKind.Hold:
                    break; // stays as recorded: held, or already routed
                case FlatPlanKind.Skip:
                    if (already.Count == 0) queue.SetFlatRoute(path, FlatRouteStates.Skipped, Array.Empty<string>(), plan.Reason);
                    info($"Starfront stacking: not sending flat {name}: {plan.Reason}");
                    break;
            }
        }

        private static FlatPlan Route(IReadOnlyList<Target> to, string reason) => new(FlatPlanKind.Route, to.Select(t => t.ProjectId).ToList(), reason);

        private static string Names(IEnumerable<Target> targets) => string.Join(", ", targets.Select(t => t.Name));

        private static string SafeDirectory(string path) {
            try { return Path.GetDirectoryName(path); } catch (ArgumentException) { return null; }
        }
    }
}

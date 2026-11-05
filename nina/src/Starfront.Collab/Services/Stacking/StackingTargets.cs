using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Projects;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Whether a light shot for a linked collaboration is of the stacking
    /// project's target at all. The ledger matches a light to its
    /// collaboration by the target name N.I.N.A. stamped, which says what the
    /// sequence meant to shoot, not where the telescope was pointed; a light
    /// far from the stacking project's field (a typed target name reused, a
    /// mount that slewed somewhere else) is rejected by the stacking service
    /// as unplaceable and costs the person quota. The rule is Rapid Stacker
    /// Collab's own (<see cref="ProjectAttribution.IsOtherTarget"/>): the
    /// light's <c>OBJECT</c> does not name the project, and every position
    /// its header gives is farther from the grid centre than the grid's
    /// half-diagonal plus a degree. No N.I.N.A. here, so it runs under the tests.
    /// </summary>
    public static class StackingTargets {
        /// <summary>The stacking project as the rule sees it; null without a grid, when nothing can be judged.</summary>
        public static JoinedProject Project(string collabProjectId, string name, string targetName, Grid grid, bool gridPending) =>
            grid == null ? null : new JoinedProject(collabProjectId ?? "", "", name ?? "", targetName ?? "", grid, DateTimeOffset.MinValue, gridPending);

        /// <summary>Why the light is another target's, or null when it is the project's (or cannot be told).</summary>
        public static string OtherTargetReason(InitHeader header, JoinedProject project) {
            if (header == null || project == null) return null;
            var pointing = FramePointing.FromHeader(header);
            if (!ProjectAttribution.IsOtherTarget(pointing, project)) return null;
            var what = string.IsNullOrWhiteSpace(header.Object) ? "another target" : $"another target ('{header.Object.Trim()}')";
            return ProjectAttribution.SeparationDeg(pointing, project) is double sep
                ? $"it shows {what}: pointed {sep:0.#}° from the field of {project.Name}"
                : $"it shows {what}, not {project.Name}";
        }
    }
}

using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Upload;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>A project whose uploads the queue is holding, as the dock lists it, with the reference its Resume button lifts.</summary>
    public sealed record HeldProject(string ProjectRef, string Text);

    /// <summary>
    /// A project pause that a confirmed membership answers. Core pauses a project with no end when an upload is
    /// told <c>not_member</c> (the person has to act), and only a timed recheck for <c>membership_pending</c>.
    /// Nothing in Core ever lifts the first, and the queue that holds it outlives N.I.N.A. restarts and plugin
    /// reinstalls. On 2026-10-28 one <c>not_member</c> paused Bubble Nebula for good: the membership was read as
    /// active every 30 minutes for six days and the rig was joined again, while every light was queued behind the
    /// pause and none was ever tried. So a membership read as active after a pause of these kinds was set is the
    /// answer to it, and the pause goes.
    /// </summary>
    public static class MembershipPauses {
        /// <summary>The pauses a membership read as active answers; never the person's own pause, a full quota, or a project that is not open.</summary>
        public static bool AnsweredByActiveMembership(ProjectPause pause) =>
            !pause.ByUser && pause.Code is ApiCodes.NotMember or ApiCodes.MembershipPending;

        /// <summary>
        /// Lift the pauses of <paramref name="projectRef"/> that a membership read as active answers, if they were set
        /// before <paramref name="askedAt"/> (when the read was sent: a pause recorded while it was on its way is newer
        /// than the answer). Returns the pauses lifted, for the log.
        /// </summary>
        public static IReadOnlyList<ProjectPause> ResumeConfirmed(UploadQueue queue, string projectRef, DateTimeOffset askedAt) {
            if (queue == null || string.IsNullOrEmpty(projectRef)) return Array.Empty<ProjectPause>();
            var lifted = queue.ActivePauses()
                .Where(p => p.ProjectRef == projectRef && AnsweredByActiveMembership(p) && p.PausedAt <= askedAt)
                .ToList();
            foreach (var pause in lifted) queue.ResumeProject(pause.ProjectRef);
            return lifted;
        }

        /// <summary>
        /// The dock's line for a held project. A pause with no end of its own (not a member, or the person's own)
        /// says so, because only a membership read or the Resume button next to it lets that project's lights go.
        /// </summary>
        public static string DockText(ProjectPause pause, string name) {
            var label = string.IsNullOrWhiteSpace(name) ? pause.ProjectRef : name;
            var tried = pause.ResumeAt is DateTimeOffset at ? $", tried again at {at.ToLocalTime():HH:mm}" : pause.ByUser ? ", paused by you" : ", until resumed";
            var reason = string.IsNullOrWhiteSpace(pause.Reason) ? "" : $": {pause.Reason}";
            return $"{label} held since {pause.PausedAt.ToLocalTime():d MMM HH:mm}{tried}{reason}";
        }

        /// <summary>One line for the log about a pause still holding a project's uploads.</summary>
        public static string Describe(ProjectPause pause) {
            var until = pause.ResumeAt is DateTimeOffset at ? $"until {at.ToLocalTime():yyyy-MM-dd HH:mm}" : "with no end";
            var code = string.IsNullOrEmpty(pause.Code) ? "" : $" ({pause.Code})";
            return $"paused since {pause.PausedAt.ToLocalTime():yyyy-MM-dd HH:mm} {until}{code}: {pause.Reason}";
        }
    }
}

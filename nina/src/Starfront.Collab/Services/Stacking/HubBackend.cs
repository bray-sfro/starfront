using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Upload;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// What is the hub's alone. A hub that refuses a grant is a fact about that link, never about the Rapid
    /// Stacker Collab sign-in: the router turns the refusal into a project pause (Core's own "project blocked"
    /// outcome, rechecked on a timer) instead of letting Core read it as "sign in again", and the next check-in,
    /// which brings a fresh grant, lifts the pause early.
    /// </summary>
    public static class HubAuth {
        /// <summary>The pause's reason, and the message of the exception the engine reads; also how a pause of ours is recognised later.</summary>
        public const string PauseReason = "The hub refused this telescope's grant; uploads resume when the next check-in brings a new one.";

        /// <summary>The codes Core's engine turns into "sign in again" (the same four).</summary>
        public static bool IsAuthFailure(ApiException ex) =>
            ex.Code is ApiCodes.NotAuthenticated or ApiCodes.TokenInvalid or ApiCodes.SessionExpired or ApiCodes.TokenScope;

        /// <summary>
        /// What the engine sees instead of a hub's 401. <c>project_not_open</c> is Core's "project blocked" outcome:
        /// the item is released, the project (only) is paused, the rest of the queue carries on, and the runner's
        /// auth-required flag is left alone.
        /// </summary>
        public static ApiException AsProjectPause(ApiException original) =>
            new(ApiCodes.ProjectNotOpen, PauseReason, original.HttpStatus, original);

        /// <summary>Lift the pause of every hub project whose link holds a grant again (a check-in brought one). Returns how many.</summary>
        public static int ResumeRefreshed(UploadQueue queue, Func<string, StackingLink> byCollabProject) {
            var resumed = 0;
            foreach (var pause in queue.ActivePauses()) {
                if (pause.ByUser || pause.Reason != PauseReason) continue;
                var link = byCollabProject(pause.ProjectRef);
                if (link == null || !link.IsHub || link.Grant.Length == 0) continue;
                queue.ResumeProject(pause.ProjectRef);
                resumed++;
            }
            return resumed;
        }
    }

    /// <summary>Decision D001: one library per backend, so one rig id per backend. The cloud's lives in the profile settings; a hub's lives on its links.</summary>
    public static class RigIds {
        /// <summary>The first hub link that can carry rig-scoped calls.</summary>
        public static StackingLink DefaultHubLink(IEnumerable<StackingLink> links) =>
            links.FirstOrDefault(l => l.IsHub && l.Feeds && l.Grant.Length > 0 && l.ApiBase.Trim().Length > 0);

        /// <summary>A hub's rig id: the link's own, else a sibling link on the same hub (one telescope is one rig on a hub). Blank when there is none.</summary>
        public static string OfHub(StackingLink link, IEnumerable<StackingLink> all) {
            if (link == null) return "";
            if (link.RigId.Length > 0) return link.RigId;
            return all.FirstOrDefault(l => l.IsHub && l.HubId == link.HubId && l.RigId.Length > 0)?.RigId ?? "";
        }

        /// <summary>The rig id the default backend knows this telescope by, with no fallback to the other backend's. Blank when it has none yet.</summary>
        public static string ForDefaultBackend(string defaultBackend, string cloudRigId, IReadOnlyList<StackingLink> links) {
            if (string.Equals(defaultBackend, "hub", StringComparison.OrdinalIgnoreCase) && DefaultHubLink(links) is { } hub) return OfHub(hub, links);
            return cloudRigId ?? "";
        }

        /// <summary>
        /// The key lights and flats are grouped under locally: the default backend's rig id, else the cloud's,
        /// else any hub's, so a hub-only telescope still has one. It is a grouping key, not a server address.
        /// </summary>
        public static string LocalKey(string defaultBackend, string cloudRigId, IReadOnlyList<StackingLink> links) {
            var rig = ForDefaultBackend(defaultBackend, cloudRigId, links);
            if (rig.Length > 0) return rig;
            return links.FirstOrDefault(l => l.IsHub && l.RigId.Length > 0)?.RigId ?? "";
        }
    }
}

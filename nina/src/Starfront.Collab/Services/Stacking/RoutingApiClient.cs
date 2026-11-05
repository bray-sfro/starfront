using System.Collections.Concurrent;
using RapidStackerCollab.Core.Api;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>What the router needs to know about the world: the links, the default backend and the clients.</summary>
    public interface IStackingRoutes {
        /// <summary>The link whose stacking project this is (the id the engine and the queue carry), or null.</summary>
        StackingLink LinkOfProject(string project);
        /// <summary>"cloud" or "hub": where rig- and library-scoped calls go (decision D001).</summary>
        string DefaultBackend { get; }
        /// <summary>The Rapid Stacker Collab cloud client (the <c>rscu_</c> token).</summary>
        IApiClient Cloud { get; }
        /// <summary>The client a link talks through (its effective base and its token source).</summary>
        IApiClient ForLink(StackingLink link);
        /// <summary>The link whose hub carries rig-scoped calls when the default backend is the hub, or null when no hub link is usable.</summary>
        StackingLink DefaultHubLink();
        /// <summary>This telescope's rig id on a backend: a hub link's, or the cloud's when null. Blank when it has none yet.</summary>
        string RigIdOf(StackingLink hubOrNull);
        /// <summary>A hub refused the link's grant: drop it (the next check-in brings a fresh one). Never touches the cloud sign-in.</summary>
        void HubRefused(StackingLink link);
    }

    /// <summary>
    /// The one <see cref="IApiClient"/> the upload engine, the verdict poller, the joiner and the library
    /// sync hold, so Core stays verbatim while a night's frames go to more than one backend. Project-scoped
    /// calls go to the client of the link whose <c>CollabProjectId</c> is the argument; a project nobody
    /// linked goes to the cloud client, which is what every call did before hubs. Rig- and library-scoped
    /// calls go to the default backend (the first usable hub link's client when that is the hub, and the
    /// cloud when no hub link is usable). Device sign-in, revoking the token, the project lists and join-code
    /// resolution are the cloud's, and the panels file is a plain GET that carries no token.
    /// </summary>
    public sealed class RoutingApiClient : IApiClient {
        private readonly IStackingRoutes routes;

        // A frame started on one base finishes on it: init records the client it went through and complete,
        // get and withdraw for that frame use the same one even if the LAN short-cut switched in between.
        // Not persisted: after a restart the queue item re-inits, which the server answers resumed or duplicate.
        private readonly ConcurrentDictionary<(string Project, string Frame), Target> pinned = new();

        public RoutingApiClient(IStackingRoutes routes) => this.routes = routes;

        // A client together with the hub link behind it (null for the cloud), so a hub's 401 can be told from the cloud's.
        private readonly record struct Target(IApiClient Client, StackingLink Hub);

        private Target ForFrame(string project, string frameId) =>
            !string.IsNullOrEmpty(frameId) && pinned.TryGetValue((project ?? "", frameId), out var target) ? target : ForProject(project);

        private IApiClient Cloud => routes.Cloud;

        private Target ForProject(string project) {
            var link = string.IsNullOrEmpty(project) ? null : routes.LinkOfProject(project);
            if (link == null) return new Target(Cloud, null);
            return new Target(routes.ForLink(link), link.IsHub ? link : null);
        }

        private Target ForDefault() {
            if (string.Equals(routes.DefaultBackend, "hub", StringComparison.OrdinalIgnoreCase)) {
                var hub = routes.DefaultHubLink();
                if (hub != null) return new Target(routes.ForLink(hub), hub);
            }
            return new Target(Cloud, null);
        }

        /// <summary>The rig id to send to the backend a call goes to: that backend's own when it is known, else the one the caller held.</summary>
        private string RigFor(Target target, string passed) {
            var own = routes.RigIdOf(target.Hub);
            return string.IsNullOrWhiteSpace(own) ? passed : own;
        }

        // A hub that refuses the grant is that link's trouble: the grant goes and the call fails as a project
        // pause, so Core never reads it as "sign in to the cloud again". Project-scoped calls only; a rig- or
        // library-scoped call keeps its own error (the library says what it could not do) after the grant is dropped.
        private async Task<T> Guard<T>(Target target, Func<IApiClient, Task<T>> call, bool asPause = true) {
            try {
                return await call(target.Client).ConfigureAwait(false);
            } catch (ApiException ex) when (target.Hub != null && HubAuth.IsAuthFailure(ex)) {
                routes.HubRefused(target.Hub);
                if (asPause) throw HubAuth.AsProjectPause(ex);
                throw;
            }
        }


        // Device sign-in and the cloud token.
        public Task<DeviceStartResponse> DeviceStartAsync(DeviceStartRequest request, CancellationToken ct = default) => Cloud.DeviceStartAsync(request, ct);
        public Task<DevicePollResponse> DevicePollAsync(string deviceCode, CancellationToken ct = default) => Cloud.DevicePollAsync(deviceCode, ct);
        public Task RevokeOwnTokenAsync(CancellationToken ct = default) => Cloud.RevokeOwnTokenAsync(ct);

        // The cloud's directory.
        public Task<ProjectListResponse> ListProjectsAsync(string scope = "mine", int? limit = null, string cursor = null, CancellationToken ct = default) => Cloud.ListProjectsAsync(scope, limit, cursor, ct);
        public Task<ResolveJoinCodeResponse> ResolveJoinCodeAsync(string code, CancellationToken ct = default) => Cloud.ResolveJoinCodeAsync(code, ct);
        public Task<PanelsDocument> GetPanelsDocumentAsync(string url, CancellationToken ct = default) => Cloud.GetPanelsDocumentAsync(url, ct);

        // Rig- and library-scoped: the default backend.
        public Task<IReadOnlyList<Rig>> ListRigsAsync(CancellationToken ct = default) => Guard(ForDefault(), c => c.ListRigsAsync(ct), false);
        public Task<Rig> UpsertRigAsync(RigUpsertRequest request, CancellationToken ct = default) => Guard(ForDefault(), c => c.UpsertRigAsync(request, ct), false);
        public Task<Rig> PatchRigAsync(string rigId, RigPatchRequest request, CancellationToken ct = default) { var t = ForDefault(); return Guard(t, c => c.PatchRigAsync(RigFor(t, rigId), request, ct), false); }
        public Task<LibraryResponse> GetLibraryAsync(string rigId, int? limit = null, string cursor = null, CancellationToken ct = default) { var t = ForDefault(); return Guard(t, c => c.GetLibraryAsync(RigFor(t, rigId), limit, cursor, ct), false); }
        public Task<LibraryFrameInitResponse> InitLibraryFrameAsync(string rigId, LibraryFrameInitRequest request, CancellationToken ct = default) { var t = ForDefault(); return Guard(t, c => c.InitLibraryFrameAsync(RigFor(t, rigId), request, ct), false); }
        public Task<LibraryFrameCompleteResponse> CompleteLibraryFrameAsync(string rigId, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct = default) { var t = ForDefault(); return Guard(t, c => c.CompleteLibraryFrameAsync(RigFor(t, rigId), frameId, etags, ct), false); }
        public Task<WithdrawResponse> WithdrawLibraryFrameAsync(string rigId, string frameId, CancellationToken ct = default) { var t = ForDefault(); return Guard(t, c => c.WithdrawLibraryFrameAsync(RigFor(t, rigId), frameId, ct), false); }

        /// <summary>Names are normalised against a project when one is given (that project's backend), else against the default backend.</summary>
        public Task<NormalizeResponse> NormalizeFiltersAsync(IEnumerable<string> names, string project = null, bool bayer = false, CancellationToken ct = default) =>
            Guard(string.IsNullOrEmpty(project) ? ForDefault() : ForProject(project), c => c.NormalizeFiltersAsync(names, project, bayer, ct), !string.IsNullOrEmpty(project));

        // Project-scoped: the project's own backend.
        public Task<ProjectDetail> GetProjectAsync(string project, CancellationToken ct = default) => Guard(ForProject(project), c => c.GetProjectAsync(project, ct));
        public Task<Membership> JoinProjectAsync(string project, JoinRequest request, CancellationToken ct = default) => Guard(ForProject(project), c => c.JoinProjectAsync(project, request, ct));
        public Task<Membership> GetMembershipAsync(string project, CancellationToken ct = default) => Guard(ForProject(project), c => c.GetMembershipAsync(project, ct));
        public Task<Membership> UpdateMembershipAsync(string project, UpdateMembershipRequest request, CancellationToken ct = default) => Guard(ForProject(project), c => c.UpdateMembershipAsync(project, request, ct));
        public Task<ProjectStatusDoc> GetProjectStatusAsync(string project, CancellationToken ct = default) => Guard(ForProject(project), c => c.GetProjectStatusAsync(project, ct));
        public Task<MosaicState> GetMosaicAsync(string project, CancellationToken ct = default) => Guard(ForProject(project), c => c.GetMosaicAsync(project, ct));
        public Task<MosaicState> PutClaimsAsync(string project, IReadOnlyList<string> panels, CancellationToken ct = default) => Guard(ForProject(project), c => c.PutClaimsAsync(project, panels, ct));
        public async Task<FrameInitResponse> InitFrameAsync(string project, FrameInitRequest request, CancellationToken ct = default) {
            var target = ForProject(project);
            var hinted = request?.RigHint?.RigId;
            if (request != null && hinted != null && RigFor(target, hinted) != hinted) {
                // The queue item names the rig it was shot under (the cloud's, or the default backend's); the project's own backend knows it by its own id.
                request = new FrameInitRequest {
                    Sha256 = request.Sha256, Size = request.Size, Filename = request.Filename, Header = request.Header,
                    Stripped = request.Stripped, RigHint = new RigHint { RigId = RigFor(target, hinted) },
                };
            }
            var response = await Guard(target, c => c.InitFrameAsync(project, request, ct)).ConfigureAwait(false);
            if (pinned.Count > 1000) pinned.Clear(); // duplicates never complete; keep the map small
            if (response != null && !string.IsNullOrEmpty(response.FrameId)) pinned[(project ?? "", response.FrameId)] = target;
            return response;
        }

        public async Task<FrameCompleteResponse> CompleteFrameAsync(string project, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct = default) {
            var response = await Guard(ForFrame(project, frameId), c => c.CompleteFrameAsync(project, frameId, etags, ct)).ConfigureAwait(false);
            pinned.TryRemove((project ?? "", frameId ?? ""), out _);
            return response;
        }
        public Task<FrameListResponse> ListFramesAsync(string project, FrameListQuery query = null, CancellationToken ct = default) => Guard(ForProject(project), c => c.ListFramesAsync(project, query, ct));
        public Task<Frame> GetFrameAsync(string project, string frameId, CancellationToken ct = default) => Guard(ForFrame(project, frameId), c => c.GetFrameAsync(project, frameId, ct));
        public Task<WithdrawResponse> WithdrawFrameAsync(string project, string frameId, CancellationToken ct = default) => Guard(ForFrame(project, frameId), c => c.WithdrawFrameAsync(project, frameId, ct));
    }
}

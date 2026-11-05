// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Api;

/// <summary>
/// The contract routes the plugin uses (docs/api/openapi.yaml). Every method throws <see cref="ApiException"/> for a
/// contract error or an unusable response, and <see cref="OperationCanceledException"/> when cancelled.
/// <c>project</c> arguments are a <c>project_id</c> or a slug.
/// </summary>
public interface IApiClient
{
    // Device sign-in (no bearer token).
    Task<DeviceStartResponse> DeviceStartAsync(DeviceStartRequest request, CancellationToken ct = default);

    /// <summary>
    /// One poll. Returns <c>pending</c> or <c>approved</c>; <c>device_denied</c> / <c>device_expired</c> (HTTP 200,
    /// <c>ok:false</c>) and <c>slow_down</c> (429) are thrown as <see cref="ApiException"/>.
    /// </summary>
    Task<DevicePollResponse> DevicePollAsync(string deviceCode, CancellationToken ct = default);

    // Rigs
    Task<IReadOnlyList<Rig>> ListRigsAsync(CancellationToken ct = default);
    Task<Rig> UpsertRigAsync(RigUpsertRequest request, CancellationToken ct = default);
    Task<Rig> PatchRigAsync(string rigId, RigPatchRequest request, CancellationToken ct = default);

    // Filters
    Task<NormalizeResponse> NormalizeFiltersAsync(IEnumerable<string> names, string? project = null, bool bayer = false, CancellationToken ct = default);

    // Projects
    /// <param name="scope"><c>public</c> or <c>mine</c>.</param>
    Task<ProjectListResponse> ListProjectsAsync(string scope = "mine", int? limit = null, string? cursor = null, CancellationToken ct = default);
    Task<ResolveJoinCodeResponse> ResolveJoinCodeAsync(string code, CancellationToken ct = default);
    Task<ProjectDetail> GetProjectAsync(string project, CancellationToken ct = default);
    Task<Membership> JoinProjectAsync(string project, JoinRequest request, CancellationToken ct = default);
    Task<Membership> GetMembershipAsync(string project, CancellationToken ct = default);
    Task<Membership> UpdateMembershipAsync(string project, UpdateMembershipRequest request, CancellationToken ct = default);
    Task<ProjectStatusDoc> GetProjectStatusAsync(string project, CancellationToken ct = default);

    // Mosaic (contract 1.17.0)
    /// <summary><c>GET /projects/{project}/mosaic</c>: null for a single-field project.</summary>
    Task<MosaicState?> GetMosaicAsync(string project, CancellationToken ct = default);

    /// <summary><c>PUT /projects/{project}/mosaic/claims</c>: replaces the caller's claims; an empty list releases them all.</summary>
    Task<MosaicState?> PutClaimsAsync(string project, IReadOnlyList<string> panels, CancellationToken ct = default);

    /// <summary>
    /// Reads <c>panels.json</c> (panels.schema.json) from the <c>url</c> of the status document's <c>panels</c> index. A plain
    /// GET without the bearer token (the file is public or presigned); not a contract operation.
    /// </summary>
    Task<PanelsDocument> GetPanelsDocumentAsync(string url, CancellationToken ct = default);

    // Frames
    Task<FrameInitResponse> InitFrameAsync(string project, FrameInitRequest request, CancellationToken ct = default);
    Task<FrameCompleteResponse> CompleteFrameAsync(string project, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct = default);
    Task<FrameListResponse> ListFramesAsync(string project, FrameListQuery? query = null, CancellationToken ct = default);
    Task<Frame> GetFrameAsync(string project, string frameId, CancellationToken ct = default);
    Task<WithdrawResponse> WithdrawFrameAsync(string project, string frameId, CancellationToken ct = default);

    // Calibration library (contract 1.23.0)
    /// <summary><c>GET /rigs/{rig_id}/library</c>: usage, the frames (newest first, paged) and the masters last built.</summary>
    Task<LibraryResponse> GetLibraryAsync(string rigId, int? limit = null, string? cursor = null, CancellationToken ct = default);

    /// <summary><c>POST /rigs/{rig_id}/library/frames/init</c>: start or resume a library upload. A repeat of a <c>ready</c> frame is 409 <c>duplicate</c>.</summary>
    Task<LibraryFrameInitResponse> InitLibraryFrameAsync(string rigId, LibraryFrameInitRequest request, CancellationToken ct = default);

    Task<LibraryFrameCompleteResponse> CompleteLibraryFrameAsync(string rigId, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct = default);

    Task<WithdrawResponse> WithdrawLibraryFrameAsync(string rigId, string frameId, CancellationToken ct = default);

    // Auth
    /// <summary>
    /// <c>DELETE /auth/token</c>: revokes the token this client is signed in with (contract 1.8.0). A token that is already dead
    /// is <c>token_invalid</c>, which a caller that is signing out treats as done.
    /// </summary>
    Task RevokeOwnTokenAsync(CancellationToken ct = default);
}

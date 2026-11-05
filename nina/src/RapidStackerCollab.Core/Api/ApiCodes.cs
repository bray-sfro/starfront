// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Api;

/// <summary>
/// Every <c>code</c> of the contract's <c>ErrorCode</c> enum (openapi.yaml), plus the few the client invents for
/// failures that never produced a contract envelope. Clients branch on these, never on <c>message</c> and never on
/// the HTTP status alone. A code not listed here is a newer server: treat it as a generic failure.
/// </summary>
public static class ApiCodes
{
    public const string AdminOnly = "admin_only";
    public const string AlreadyMember = "already_member";
    public const string BadEtags = "bad_etags";
    public const string BadOrigin = "bad_origin";
    public const string DeviceDenied = "device_denied";
    public const string DeviceExpired = "device_expired";
    public const string DimensionsTooLarge = "dimensions_too_large";
    public const string DisplayNameTaken = "display_name_taken";
    public const string Duplicate = "duplicate";
    public const string EmailInUse = "email_in_use";
    public const string EmailNotVerified = "email_not_verified";
    public const string FrameState = "frame_state";
    public const string FrameTooLarge = "frame_too_large";
    public const string FrameWithdrawn = "frame_withdrawn";
    public const string GoldInProgress = "gold_in_progress";
    public const string GridLocked = "grid_locked";
    public const string GridRequired = "grid_required";
    public const string HeaderRejected = "header_rejected";
    public const string IngestPaused = "ingest_paused";
    public const string InternalError = "internal_error";
    public const string InvalidCode = "invalid_code";
    public const string InvalidCredentials = "invalid_credentials";
    public const string InvalidLink = "invalid_link";
    public const string InvalidRequest = "invalid_request";
    public const string InvalidTransition = "invalid_transition";
    public const string JobState = "job_state";
    public const string LastOwner = "last_owner";
    public const string LeaseHeld = "lease_held";
    public const string LeaseLost = "lease_lost";
    public const string MembershipPending = "membership_pending";
    public const string MembershipRemoved = "membership_removed";
    /// <summary>The project's grid is derived from its mosaic layout, so a grid cannot also be set (contract 1.17.0).</summary>
    public const string MosaicGrid = "mosaic_grid";
    /// <summary>The route needs a project with a mosaic layout (contract 1.17.0).</summary>
    public const string MosaicRequired = "mosaic_required";
    public const string NoNewFrames = "no_new_frames";
    public const string NotAuthenticated = "not_authenticated";
    public const string NotFound = "not_found";
    public const string NotMaintainer = "not_maintainer";
    public const string NotMember = "not_member";
    public const string NotOwner = "not_owner";
    /// <summary>A claim on a panel the owner assigned to someone else (contract 1.17.0).</summary>
    public const string PanelAssigned = "panel_assigned";
    public const string PolicyViolation = "policy_violation";
    /// <summary>The project is scheduled for deletion (contract 1.16.0): joins and uploads are refused until it is restored.</summary>
    public const string ProjectDeleted = "project_deleted";
    public const string ProjectNotOpen = "project_not_open";
    /// <summary>A restore came too late: the project's data is already being deleted (contract 1.16.0).</summary>
    public const string PurgeStarted = "purge_started";
    public const string QuotaExceeded = "quota_exceeded";
    public const string RateLimited = "rate_limited";
    public const string RawDownloadNotAllowed = "raw_download_not_allowed";
    public const string RawUnavailable = "raw_unavailable";
    public const string SessionExpired = "session_expired";
    public const string SessionRequired = "session_required";
    public const string SizeMismatch = "size_mismatch";
    public const string SlowDown = "slow_down";
    public const string SlugReserved = "slug_reserved";
    public const string SlugTaken = "slug_taken";
    public const string StaleCheckpoint = "stale_checkpoint";
    /// <summary><c>frames/init</c> of a project that requires stripped frames, without <c>stripped: true</c> (400).</summary>
    public const string StripRequired = "strip_required";
    public const string TermsOutdated = "terms_outdated";
    public const string TokenInvalid = "token_invalid";
    public const string TokenScope = "token_scope";
    public const string UnsupportedFrameType = "unsupported_frame_type";
    public const string UploadIncomplete = "upload_incomplete";
    public const string ValidationFailed = "validation_failed";
    public const string WorkerOnly = "worker_only";

    // Client-side only: no contract envelope was received.
    /// <summary>The request never got an HTTP response (DNS, connection, TLS, timeout).</summary>
    public const string NetworkError = "network_error";
    /// <summary>A response arrived but was not a JSON envelope (a CDN error page, a proxy, a truncated body).</summary>
    public const string BadResponse = "bad_response";

    /// <summary>The codes in <c>ErrorCode</c>, for the contract test that fails when a server adds one.</summary>
    public static readonly IReadOnlySet<string> Contract = new HashSet<string>
    {
        AdminOnly,
        AlreadyMember,
        BadEtags,
        BadOrigin,
        DeviceDenied,
        DeviceExpired,
        DimensionsTooLarge,
        DisplayNameTaken,
        Duplicate,
        EmailInUse,
        EmailNotVerified,
        FrameState,
        FrameTooLarge,
        FrameWithdrawn,
        GoldInProgress,
        GridLocked,
        GridRequired,
        HeaderRejected,
        IngestPaused,
        InternalError,
        InvalidCode,
        InvalidCredentials,
        InvalidLink,
        InvalidRequest,
        InvalidTransition,
        JobState,
        LastOwner,
        LeaseHeld,
        LeaseLost,
        MembershipPending,
        MembershipRemoved,
        MosaicGrid,
        MosaicRequired,
        NoNewFrames,
        NotAuthenticated,
        NotFound,
        NotMaintainer,
        NotMember,
        NotOwner,
        PanelAssigned,
        PolicyViolation,
        ProjectDeleted,
        ProjectNotOpen,
        PurgeStarted,
        QuotaExceeded,
        RateLimited,
        RawDownloadNotAllowed,
        RawUnavailable,
        SessionExpired,
        SessionRequired,
        SizeMismatch,
        SlowDown,
        SlugReserved,
        SlugTaken,
        StaleCheckpoint,
        StripRequired,
        TermsOutdated,
        TokenInvalid,
        TokenScope,
        UnsupportedFrameType,
        UploadIncomplete,
        ValidationFailed,
        WorkerOnly,
    };
}

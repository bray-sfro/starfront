// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Api;

/// <summary>One entry of <c>errors[]</c> (<c>validation_failed</c>, <c>header_rejected</c>).</summary>
public sealed record FieldError(string Field, string Message);

/// <summary>
/// A failed API call: either a contract error envelope (<c>ok:false</c> with a <c>code</c>) or one of the two
/// client-side codes <see cref="ApiCodes.NetworkError"/> / <see cref="ApiCodes.BadResponse"/>.
/// Callers branch on <see cref="Code"/>. <see cref="Message"/> is the server's text for people, never parsed.
/// </summary>
public sealed class ApiException : Exception
{
    public ApiException(string code, string message, int httpStatus, Exception? inner = null)
        : base(message, inner)
    {
        Code = code;
        HttpStatus = httpStatus;
    }

    public string Code { get; }

    /// <summary>The HTTP status, or 0 when there was no response. Informational only: 401 and 409 each cover many codes.</summary>
    public int HttpStatus { get; }

    /// <summary><c>Retry-After</c> (or <c>retry_after_s</c>) as a delay, when the server gave one.</summary>
    public TimeSpan? RetryAfter { get; init; }

    public IReadOnlyList<FieldError> Errors { get; init; } = Array.Empty<FieldError>();

    /// <summary><c>quota_exceeded</c>: <c>contributor_bytes</c>, <c>contributor_frames</c>, <c>project_bytes</c>, <c>library_bytes</c> or <c>library_frames</c>.</summary>
    public string? Quota { get; init; }

    /// <summary><c>token_scope</c>: the scope the route needs.</summary>
    public string? RequiredScope { get; init; }

    /// <summary><c>policy_violation</c>: a reject-reason value.</summary>
    public string? Reason { get; init; }

    /// <summary><c>frame_state</c>: the frame's current status.</summary>
    public string? FrameStatus { get; init; }

    /// <summary><c>terms_outdated</c>: the current terms version.</summary>
    public string? TermsVersion { get; init; }

    /// <summary><c>upload_incomplete</c>: the part numbers still missing.</summary>
    public IReadOnlyList<int> OutstandingParts { get; init; } = Array.Empty<int>();

    /// <summary><c>duplicate</c>: the caller's own earlier frame, when the server revealed it.</summary>
    public FrameSummary? ExistingFrame { get; init; }

    public bool NeedsVerification { get; init; }

    /// <summary>The token is missing, unknown, revoked, expired or the session lapsed: sign in again.</summary>
    public bool IsAuthenticationFailure =>
        Code is ApiCodes.NotAuthenticated or ApiCodes.TokenInvalid or ApiCodes.SessionExpired;

    /// <summary>
    /// A failure that may succeed if tried again later without changing anything: no response, a garbled
    /// response, rate limits, the ingest pause, or a 5xx.
    /// </summary>
    public bool IsTransient =>
        Code is ApiCodes.NetworkError or ApiCodes.BadResponse or ApiCodes.RateLimited
            or ApiCodes.IngestPaused or ApiCodes.SlowDown
        || (Code == ApiCodes.InternalError)
        || (HttpStatus >= 500 && !Code.Equals(ApiCodes.InvalidRequest, StringComparison.Ordinal));
}

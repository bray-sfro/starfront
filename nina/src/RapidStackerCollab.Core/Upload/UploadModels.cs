// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Upload;

/// <summary>One file to send. <see cref="FilePath"/> is the exact file whose bytes are hashed and uploaded (already stripped, if stripping applies).</summary>
public sealed record UploadRequest(
    string ProjectRef,
    string FilePath,
    string Filename,
    InitHeader Header,
    bool Stripped,
    string? RigId = null,
    string? KnownSha256 = null);

public enum UploadStage
{
    Hashing,
    Initialising,
    Uploading,
    Completing,
    Done,
}

/// <summary>Progress of one upload. <see cref="FrameId"/> appears from the moment <c>init</c> answered, so the queue can persist it for resume.</summary>
public sealed record UploadProgress(
    UploadStage Stage,
    string? Sha256,
    string? FrameId,
    long BytesSent,
    long TotalBytes,
    int PartsDone,
    int PartCount);

/// <summary>What the caller (the queue runner) must do next with the item.</summary>
public enum UploadOutcome
{
    /// <summary><c>complete</c> succeeded: the server holds the file and will prepare it.</summary>
    Uploaded,
    /// <summary>The server already has this exact file (409 <c>duplicate</c>). Done; nothing was sent.</summary>
    Duplicate,
    /// <summary>Not now: a rate limit, the ingest pause, a network or server failure. Try again, after <see cref="UploadResult.RetryAfter"/> when given.</summary>
    RetryLater,
    /// <summary>The whole project is blocked (quota, not open, membership pending or removed). Pause its queue; other projects continue.</summary>
    ProjectBlocked,
    /// <summary>The token is unusable. Stop everything and ask the user to sign in again.</summary>
    AuthRequired,
    /// <summary>This file will never upload as it is (rejected header, too large, unsupported type, policy). Do not retry.</summary>
    Failed,
}

public sealed record UploadResult(
    UploadOutcome Outcome,
    string? Sha256,
    string? FrameId = null,
    string? Status = null,
    string? Code = null,
    string Message = "",
    TimeSpan? RetryAfter = null,
    bool Resumed = false,
    int PartsSent = 0,
    string? Quota = null)
{
    public bool IsDone => Outcome is UploadOutcome.Uploaded or UploadOutcome.Duplicate;
}

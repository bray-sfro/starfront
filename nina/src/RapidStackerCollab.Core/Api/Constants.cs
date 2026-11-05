// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Api;

/// <summary>API-token scopes (openapi.yaml <c>Scope</c>). The device flow grants the first five.</summary>
public static class ApiScopes
{
    public const string ProjectsRead = "projects:read";
    public const string ProjectsJoin = "projects:join";
    public const string FramesRead = "frames:read";
    public const string FramesWrite = "frames:write";
    public const string MastersRead = "masters:read";
    public const string ProjectsManage = "projects:manage";

    /// <summary>What the plugin asks for. <c>projects:manage</c> is never requested: the device flow refuses it.</summary>
    public static readonly IReadOnlyList<string> PluginDefaults =
        new[] { ProjectsRead, ProjectsJoin, FramesRead, FramesWrite, MastersRead };
}

/// <summary>Frame statuses (<c>FrameStatus</c>). Strings, so an unknown future status survives a round trip.</summary>
public static class FrameStatuses
{
    public const string Uploading = "uploading";
    public const string Uploaded = "uploaded";
    public const string Preparing = "preparing";
    public const string Accepted = "accepted";
    public const string Rejected = "rejected";
    public const string Quarantined = "quarantined";
    public const string Accumulated = "accumulated";
    public const string InGold = "in_gold";
    public const string Failed = "failed";
    public const string Withdrawn = "withdrawn";

    /// <summary>Statuses that mean the server accepted the frame into a stack.</summary>
    public static bool IsAcceptedFamily(string? s) => s is Accepted or Accumulated or InGold;

    /// <summary>Statuses that are a final verdict the dock panel stops polling for.</summary>
    public static bool IsFinalVerdict(string? s) =>
        s is Accepted or Accumulated or InGold or Rejected or Quarantined or Failed or Withdrawn;
}

/// <summary>Project statuses (<c>ProjectStatus</c>).</summary>
public static class ProjectStatuses
{
    public const string Draft = "draft";
    public const string Open = "open";
    public const string Paused = "paused";
    public const string Closed = "closed";
    public const string Archived = "archived";
    /// <summary>Scheduled for deletion (contract 1.16.0); <c>delete_after</c> says when the purge may start.</summary>
    public const string Deleted = "deleted";
}

/// <summary>Member statuses (<c>MemberStatus</c>).</summary>
public static class MemberStatuses
{
    public const string Active = "active";
    public const string Pending = "pending";
    public const string Removed = "removed";
}

/// <summary>Quota names carried by 429 <c>quota_exceeded</c>.</summary>
public static class QuotaNames
{
    public const string ContributorBytes = "contributor_bytes";
    public const string ContributorFrames = "contributor_frames";
    public const string ProjectBytes = "project_bytes";
    /// <summary>The per-user calibration library quota (contract 1.23.0).</summary>
    public const string LibraryBytes = "library_bytes";
    public const string LibraryFrames = "library_frames";

    /// <summary>The library quotas refuse the library only; the others refuse a project.</summary>
    public static bool IsLibrary(string? quota) => quota is LibraryBytes or LibraryFrames;
}

/// <summary>The frame types a calibration library holds (<c>LibraryFrameType</c>, contract 1.23.0).</summary>
public static class LibraryFrameTypes
{
    public const string Bias = "bias";
    public const string Dark = "dark";
    public const string DarkFlat = "dark_flat";
    public const string MasterBias = "master_bias";
    public const string MasterDark = "master_dark";
    public const string MasterDarkFlat = "master_dark_flat";
}

/// <summary>Library frame statuses (<c>LibraryFrameStatus</c>).</summary>
public static class LibraryFrameStatuses
{
    public const string Uploading = "uploading";
    public const string Ready = "ready";
    public const string Withdrawn = "withdrawn";
}

/// <summary>Why the worker did not use a library frame (<c>LibraryFrame.problem</c>).</summary>
public static class LibraryProblems
{
    public const string Sha256Mismatch = "sha256_mismatch";
    public const string Unreadable = "unreadable";
    public const string WrongType = "wrong_type";
    public const string WrongCamera = "wrong_camera";
    public const string WrongSize = "wrong_size";
    public const string Superseded = "superseded";

    /// <summary>A plain-language sentence for an options page or the dock.</summary>
    public static string Describe(string? problem) => problem switch
    {
        Sha256Mismatch => "its bytes do not match what was announced; upload it again",
        Unreadable => "its header could not be read",
        WrongType => "it is not a bias, dark or dark flat",
        WrongCamera => "it was taken with a different camera than the rig",
        WrongSize => "its size or binning is not the rig's",
        Superseded => "a master built from your raw frames replaced this uploaded master",
        _ => problem ?? "",
    };
}

/// <summary>Values of <c>SKIP</c> in a contributor mapping: never upload frames with this wheel name.</summary>
public static class MappingValues
{
    public const string Skip = "SKIP";
}

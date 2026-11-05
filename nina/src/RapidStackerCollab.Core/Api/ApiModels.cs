// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text.Json.Serialization;

// Field-for-field with docs/api/openapi.yaml (contract 1.0.0). Names are snake_case through ApiJson.Options;
// the only explicit [JsonPropertyName]s are FITS cards inside InitHeader, which keep their FITS spelling.
// Enumerations are strings (see Constants.cs) so a value added by a newer server is carried, not lost.
// Response types are lenient: strings default to "", lists to empty, so a field the server omits never throws.
namespace RapidStackerCollab.Core.Api;

// ---- Device sign-in --------------------------------------------------------------------------------------------

public sealed class DeviceStartRequest
{
    public string ClientName { get; init; } = "";
    public IReadOnlyList<string>? Scopes { get; init; }
}

public sealed class DeviceStartResponse
{
    public string DeviceCode { get; init; } = "";
    public string UserCode { get; init; } = "";
    public string VerificationUri { get; init; } = "";
    public string VerificationUriComplete { get; init; } = "";
    public int ExpiresIn { get; init; }
    public int Interval { get; init; } = 5;
    public IReadOnlyList<string> Scopes { get; init; } = Array.Empty<string>();
}

public sealed class DevicePollRequest
{
    public string DeviceCode { get; init; } = "";
}

public sealed class DevicePollResponse
{
    /// <summary><c>pending</c> or <c>approved</c>.</summary>
    public string Status { get; init; } = "pending";
    /// <summary>Present once, only when approved. A lost response cannot be replayed.</summary>
    public string? Token { get; init; }
    public TokenInfo? Info { get; init; }
    public UserInfo? User { get; init; }
}

public sealed class TokenInfo
{
    public string TokenId { get; init; } = "";
    public string Name { get; init; } = "";
    public IReadOnlyList<string> Scopes { get; init; } = Array.Empty<string>();
    public string Hint { get; init; } = "";
    public string Source { get; init; } = "";
    public string? ClientName { get; init; }
    public DateTimeOffset CreatedAt { get; init; }
    public DateTimeOffset? LastUsedAt { get; init; }
    public DateTimeOffset ExpiresAt { get; init; }
}

public sealed class UserInfo
{
    public string UserId { get; init; } = "";
    public string Email { get; init; } = "";
    public string Name { get; init; } = "";
    public string Role { get; init; } = "user";
    public bool EmailVerified { get; init; }
}

// ---- Rigs ------------------------------------------------------------------------------------------------------

public sealed class Rig
{
    public string RigId { get; init; } = "";
    public string Name { get; init; } = "";
    public string Camera { get; init; } = "";
    public string Telescope { get; init; } = "";
    public double FocalLengthMm { get; init; }
    public double PixelSizeUm { get; init; }
    public int Binning { get; init; }
    public bool Osc { get; init; }
    public string? BayerPattern { get; init; }
    public double? PixelScaleArcsec { get; init; }
    public int? WidthPx { get; init; }
    public int? HeightPx { get; init; }
    public bool Confirmed { get; init; }
    public int FrameCount { get; init; }
    public DateTimeOffset? LastFrameAt { get; init; }
    /// <summary>The camera's last solved orientation (contract 1.21.0); null until a light carrying one is accepted.</summary>
    public RigOrientation? Orientation { get; init; }
    public DateTimeOffset CreatedAt { get; init; }
}

/// <summary>A rig's latest solved orientation (contract 1.21.0): what the website's grid forms offer as "use my camera".</summary>
public sealed class RigOrientation
{
    public double PositionAngleDeg { get; init; }
    public double? ScaleArcsecPx { get; init; }
    public DateTimeOffset? ObservedAt { get; init; }
    public DateTimeOffset MeasuredAt { get; init; }
}

public sealed class RigUpsertRequest
{
    public string Camera { get; init; } = "";
    public string Telescope { get; init; } = "";
    public double FocalLengthMm { get; init; }
    public double PixelSizeUm { get; init; }
    public int Binning { get; init; } = 1;
    /// <summary>RGGB, BGGR, GRBG, GBRG, or null for a mono sensor.</summary>
    public string? BayerPattern { get; init; }
    public int? WidthPx { get; init; }
    public int? HeightPx { get; init; }
    public string? Name { get; init; }
}

public sealed class RigPatchRequest
{
    public string? Name { get; init; }
    public bool? Confirmed { get; init; }
}

public sealed class RigResponse
{
    public Rig Rig { get; init; } = new();
}

public sealed class RigListResponse
{
    public IReadOnlyList<Rig> Rigs { get; init; } = Array.Empty<Rig>();
}

// ---- Projects --------------------------------------------------------------------------------------------------

/// <summary>The project frame: a TAN WCS with CRPIX at the centre.</summary>
public sealed class Grid
{
    public double CenterRaDeg { get; init; }
    public double CenterDecDeg { get; init; }
    /// <summary>Position angle of image-up, degrees east of north.</summary>
    public double RotationDeg { get; init; }
    public double ScaleArcsecPx { get; init; }
    public int WidthPx { get; init; }
    public int HeightPx { get; init; }
    // Up to 130 000 px on an edge for the grid derived from a mosaic layout (contract 1.17.0); nothing here limits it (int).
}

public sealed class ContributorMappingEntry
{
    public string Raw { get; init; } = "";
    /// <summary>A canonical filter, or <c>SKIP</c>.</summary>
    public string Canonical { get; init; } = "";
}

/// <summary>Project policy. Always complete on read; the plugin reads it and never writes it.</summary>
public sealed class ProjectPolicy
{
    public string Calibration { get; init; } = "preferred";
    public bool OscAllowed { get; init; }
    public string OscMode { get; init; } = "rgb_channels";
    public bool MergeOscChannels { get; init; }
    public bool MergeBandwidths { get; init; }
    public double MinExposureS { get; init; } = 5;
    public double AcceptScaleMin { get; init; } = 0.4;
    public double AcceptScaleMax { get; init; } = 2.5;
    public double MaxFwhmArcsec { get; init; } = 6;
    public double FwhmMedianFactor { get; init; } = 2.5;
    public double MaxEccentricity { get; init; } = 0.7;
    public double MinStarFraction { get; init; } = 0.3;
    /// <summary>When true, <c>init</c> refuses frames not marked <c>stripped</c>.</summary>
    public bool RequireCoordinateStrip { get; init; }
    public bool RequireApproval { get; init; }
    public bool RetainRaw { get; init; }
}

public sealed class ProjectCounts
{
    public int MemberCount { get; init; }
    public int FramesAccepted { get; init; }
    public double IntegrationS { get; init; }
    public DateTimeOffset? LastUpdate { get; init; }
}

public sealed class ProjectSummary
{
    public string ProjectId { get; init; } = "";
    public string Slug { get; init; } = "";
    public string Name { get; init; } = "";
    public string TargetName { get; init; } = "";
    public string Status { get; init; } = "";
    public string Visibility { get; init; } = "";
    public string? OwnerName { get; init; }
    public string Palette { get; init; } = "none";
    public IReadOnlyList<string> Filters { get; init; } = Array.Empty<string>();
    public ProjectCounts Counts { get; init; } = new();
    public string? CombinedPreviewUrl { get; init; }
    public DateTimeOffset CreatedAt { get; init; }
    /// <summary>Only in <c>scope=mine</c> items whose <c>Status</c> is <c>deleted</c> (contract 1.16.0).</summary>
    public DateTimeOffset? DeleteAfter { get; init; }

    // Only in <c>scope=mine</c> items (contract 1.8.0); the project catalog builds its entries from them and reads a project with
    // GET /projects/{project} only when they are missing (an older server).
    public Grid? Grid { get; init; }
    public ProjectPolicy? Policy { get; init; }
    public Membership? MyMembership { get; init; }
}

public sealed class Membership
{
    public string ProjectId { get; init; } = "";
    public string UserId { get; init; } = "";
    public string Role { get; init; } = "contributor";
    public string Status { get; init; } = "";
    public string DisplayName { get; init; } = "";
    public string? Country { get; init; }
    public string AcceptedTermsVersion { get; init; } = "";
    public DateTimeOffset? AcceptedTermsAt { get; init; }
    public bool AllowOwnerRawDownload { get; init; }
    public IReadOnlyList<ContributorMappingEntry> FilterMapping { get; init; } = Array.Empty<ContributorMappingEntry>();
    public MembershipUsage Usage { get; init; } = new();
    public MemberStats Stats { get; init; } = new();
    public DateTimeOffset JoinedAt { get; init; }
    /// <summary>Mosaic panels the owner assigned to this member (contract 1.17.0). Empty when the project has no layout or none assigned.</summary>
    public IReadOnlyList<string> Panels { get; init; } = Array.Empty<string>();
    /// <summary>Mosaic panels this member claimed (contract 1.17.0).</summary>
    public IReadOnlyList<string> Claims { get; init; } = Array.Empty<string>();
}

public sealed class MembershipUsage
{
    public long BytesUsed { get; init; }
    public long FramesUsed { get; init; }
    public long BytesCap { get; init; }
    public long FramesCap { get; init; }
}

public sealed class MemberStats
{
    public int FramesAccepted { get; init; }
    public int FramesRejected { get; init; }
    public double IntegrationS { get; init; }
    public DateTimeOffset? LastFrameAt { get; init; }
}

/// <summary>Project detail. <c>JoinCode</c>, <c>Notify</c> and <c>Quotas</c> appear only for callers entitled to them.</summary>
public sealed class ProjectDetail
{
    public string ProjectId { get; init; } = "";
    public string Slug { get; init; } = "";
    public string Name { get; init; } = "";
    public string TargetName { get; init; } = "";
    public string Description { get; init; } = "";
    public string Status { get; init; } = "";
    public string Visibility { get; init; } = "";
    public string Licence { get; init; } = "";
    public string Palette { get; init; } = "none";
    public IReadOnlyList<string> Filters { get; init; } = Array.Empty<string>();
    public Grid? Grid { get; init; }
    public ProjectPolicy Policy { get; init; } = new();
    public ProjectCounts Counts { get; init; } = new();
    public string OwnerName { get; init; } = "";
    public string TermsVersion { get; init; } = "";
    public Membership? MyMembership { get; init; }
    public string? JoinCode { get; init; }
    /// <summary>Only while <c>Status</c> is <c>deleted</c> (contract 1.16.0): when the purge may start.</summary>
    public DateTimeOffset? DeleteAfter { get; init; }
    public DateTimeOffset? DeletedAt { get; init; }
    /// <summary><c>owner</c> or <c>admin</c>; a role, never a person.</summary>
    public string? DeletedByRole { get; init; }
    /// <summary>The mosaic layout (contract 1.17.0), or null for a single-field project. <see cref="Grid"/> is derived from it.</summary>
    public Mosaic? Mosaic { get; init; }
    /// <summary>True while the grid's rotation waits for the first light (contract 1.24.0): <see cref="Grid"/>'s rotation is a placeholder until then.</summary>
    public bool? RotationFromFirstLight { get; init; }
    public DateTimeOffset CreatedAt { get; init; }
    public DateTimeOffset UpdatedAt { get; init; }
}

// ---- Mosaic (contract 1.17.0) ----------------------------------------------------------------------------------

/// <summary>A rectangular layout of <c>Rows</c> x <c>Cols</c> panels (up to 25 x 25) about <see cref="Centre"/>.</summary>
public sealed class Mosaic
{
    public int Rows { get; init; }
    public int Cols { get; init; }
    public double OverlapPct { get; init; }
    public double PanelWDeg { get; init; }
    public double PanelHDeg { get; init; }
    public double RotationDeg { get; init; }
    public double ScaleArcsecPx { get; init; }
    public MosaicCentre Centre { get; init; } = new();
}

public sealed class MosaicCentre
{
    public double RaDeg { get; init; }
    public double DecDeg { get; init; }
}

/// <summary>One derived panel. Display names only.</summary>
public sealed class MosaicPanel
{
    public string PanelId { get; init; } = "";
    public int Row { get; init; }
    public int Col { get; init; }
    public double RaDeg { get; init; }
    public double DecDeg { get; init; }
    /// <summary>Position angle of the layout's up direction at the panel's centre, degrees east of north.</summary>
    public double PaDeg { get; init; }
    public string? AssignedTo { get; init; }
    public IReadOnlyList<string> ClaimedBy { get; init; } = Array.Empty<string>();
}

/// <summary>Target hours per filter. Keys are canonical filters; an override of 0 means no target.</summary>
public sealed class MosaicTargets
{
    public Dictionary<string, double> Default { get; init; } = new();
    public Dictionary<string, Dictionary<string, double>> Panels { get; init; } = new();
}

public sealed class MosaicState
{
    public Mosaic Layout { get; init; } = new();
    public Grid Grid { get; init; } = new();
    public bool Locked { get; init; }
    public int Revision { get; init; }
    public IReadOnlyList<MosaicPanel> Panels { get; init; } = Array.Empty<MosaicPanel>();
    public MosaicTargets Targets { get; init; } = new();
    public IReadOnlyList<string> MyPanels { get; init; } = Array.Empty<string>();
    public IReadOnlyList<string> MyClaims { get; init; } = Array.Empty<string>();
}

/// <summary><c>Mosaic</c> is null for a single-field project.</summary>
public sealed class MosaicResponse
{
    public MosaicState? Mosaic { get; init; }
}

/// <summary><c>PUT /projects/{project}/mosaic/claims</c>: replaces the caller's claims (an empty list releases them all).</summary>
public sealed class ClaimsRequest
{
    public IReadOnlyList<string> Panels { get; init; } = Array.Empty<string>();
}

public sealed class ProjectResponse
{
    public ProjectDetail Project { get; init; } = new();
}

public sealed class ProjectListResponse
{
    public IReadOnlyList<ProjectSummary> Projects { get; init; } = Array.Empty<ProjectSummary>();
    public bool DirectoryEnabled { get; init; }
    public string? NextCursor { get; init; }
}

public sealed class ResolveJoinCodeRequest
{
    public string Code { get; init; } = "";
}

public sealed class ResolveJoinCodeResponse
{
    public ProjectSummary Project { get; init; } = new();
    public Grid? Grid { get; init; }
    public ProjectPolicy Policy { get; init; } = new();
    public string TermsVersion { get; init; } = "";
    public string TermsUrl { get; init; } = "";
    public bool RequiresApproval { get; init; }
    public bool AlreadyMember { get; init; }
}

public sealed class JoinRequest
{
    public string Code { get; init; } = "";
    public string DisplayName { get; init; } = "";
    public string? Country { get; init; }
    public string AcceptedTermsVersion { get; init; } = "";
    /// <summary>Plan §13 answer 2: off unless the contributor opts in, here or later.</summary>
    public bool AllowOwnerRawDownload { get; init; }
    public IReadOnlyList<ContributorMappingEntry>? FilterMapping { get; init; }
}

public sealed class JoinResponse
{
    public Membership Membership { get; init; } = new();
}

public sealed class MembershipResponse
{
    public Membership Membership { get; init; } = new();
}

public sealed class UpdateMembershipRequest
{
    public string? DisplayName { get; init; }
    public string? Country { get; init; }
    public bool? AllowOwnerRawDownload { get; init; }
    public IReadOnlyList<ContributorMappingEntry>? FilterMapping { get; init; }
    public string? AcceptedTermsVersion { get; init; }
}

// ---- Filters ---------------------------------------------------------------------------------------------------

public sealed class FilterResolution
{
    public string Raw { get; init; } = "";
    /// <summary>A canonical filter or <c>UNKNOWN</c>.</summary>
    public string Canonical { get; init; } = "UNKNOWN";
    public string? Confidence { get; init; }
    public string Source { get; init; } = "none";
    public double? BandwidthNm { get; init; }
    public string? Vendor { get; init; }
    public IReadOnlyList<string>? StreamKeys { get; init; }
    public string? QuarantineReason { get; init; }
}

public sealed class NormalizeResponse
{
    public IReadOnlyList<FilterResolution> Results { get; init; } = Array.Empty<FilterResolution>();
    public string AliasesVersion { get; init; } = "";
}

// ---- Status ----------------------------------------------------------------------------------------------------

/// <summary>
/// The parts of <c>status.schema.json</c> the dock panel shows. Unknown members are ignored, so schema-1
/// additions never break it.
/// </summary>
public sealed class ProjectStatusDoc
{
    public int SchemaVersion { get; init; }
    public DateTimeOffset GeneratedAt { get; init; }
    public string Access { get; init; } = "public";
    public StatusTotals Totals { get; init; } = new();
    public IReadOnlyList<StatusStream> Streams { get; init; } = Array.Empty<StatusStream>();
    public StatusCombined Combined { get; init; } = new();
    public IReadOnlyList<StatusContributor> Contributors { get; init; } = Array.Empty<StatusContributor>();
    /// <summary>Present only for a mosaic project (contract 1.17.0): where <c>panels.json</c> is.</summary>
    public StatusPanels? Panels { get; init; }
}

/// <summary>The <c>panels</c> index of the status document.</summary>
public sealed class StatusPanels
{
    public string Url { get; init; } = "";
    public DateTimeOffset UpdatedAt { get; init; }
    public int WithData { get; init; }
    public int Total { get; init; }
}

public sealed class StatusTotals
{
    public int FramesAccepted { get; init; }
    public int FramesRejected { get; init; }
    public double IntegrationS { get; init; }
    public int Streams { get; init; }
    public int Contributors { get; init; }
    public int MemberCount { get; init; }
    public DateTimeOffset? LastUpdate { get; init; }
}

public sealed class PreviewSet
{
    public string Full { get; init; } = "";
    public string Thumb { get; init; } = "";
    public DateTimeOffset UpdatedAt { get; init; }
}

public sealed class StatusPreviews
{
    public PreviewSet? Live { get; init; }
    public PreviewSet? Gold { get; init; }
}

public sealed class StatusStream
{
    public string StreamKey { get; init; } = "";
    public string Filter { get; init; } = "";
    public string SensorKind { get; init; } = "mono";
    public double? BandwidthNm { get; init; }
    public int FramesAccepted { get; init; }
    public int FramesRejected { get; init; }
    public double IntegrationS { get; init; }
    public int Contributors { get; init; }
    public int? GoldSeq { get; init; }
    public int LiveN { get; init; }
    public double? MedianFwhmArcsec { get; init; }
    public StatusPreviews PreviewUrls { get; init; } = new();
    public IReadOnlyList<StatusByContributor> ByContributor { get; init; } = Array.Empty<StatusByContributor>();
}

public sealed class StatusByContributor
{
    public string DisplayName { get; init; } = "";
    public int FramesAccepted { get; init; }
    public double IntegrationS { get; init; }
}

public sealed class StatusCombined
{
    public string Palette { get; init; } = "none";
    public PreviewSet? Live { get; init; }
    public PreviewSet? Gold { get; init; }
}

public sealed class StatusContributor
{
    public int Rank { get; init; }
    public string DisplayName { get; init; } = "";
    public string? Country { get; init; }
    public double IntegrationS { get; init; }
    public int FramesAccepted { get; init; }
    public IReadOnlyList<string> Streams { get; init; } = Array.Empty<string>();
}

public sealed class ProjectStatusResponse
{
    public ProjectStatusDoc StatusDoc { get; init; } = new();
}

// ---- Frames ----------------------------------------------------------------------------------------------------

/// <summary>
/// The header allow-list of <c>frames/init</c>. Anything else, in particular <c>SITELAT</c>, <c>SITELONG</c>,
/// <c>SITEELEV</c> and <c>OBSERVER</c>, makes the server answer 400 <c>header_rejected</c>. Because this is a typed
/// class, a key outside the list cannot be serialised by accident; <c>FitsHeaderReader</c> is the only producer.
/// </summary>
public sealed class InitHeader
{
    [JsonPropertyName("FILTER")] public string? Filter { get; set; }
    [JsonPropertyName("IMAGETYP")] public string? ImageTyp { get; set; }
    [JsonPropertyName("EXPTIME")] public double? ExpTime { get; set; }
    [JsonPropertyName("GAIN")] public double? Gain { get; set; }
    [JsonPropertyName("OFFSET")] public double? Offset { get; set; }
    [JsonPropertyName("XBINNING")] public int? XBinning { get; set; }
    [JsonPropertyName("YBINNING")] public int? YBinning { get; set; }
    [JsonPropertyName("INSTRUME")] public string? Instrume { get; set; }
    [JsonPropertyName("TELESCOP")] public string? Telescop { get; set; }
    [JsonPropertyName("FOCALLEN")] public double? FocalLen { get; set; }
    [JsonPropertyName("XPIXSZ")] public double? XPixSz { get; set; }
    [JsonPropertyName("YPIXSZ")] public double? YPixSz { get; set; }
    [JsonPropertyName("BAYERPAT")] public string? BayerPat { get; set; }
    [JsonPropertyName("DATE-OBS")] public string? DateObs { get; set; }
    [JsonPropertyName("OBJECT")] public string? Object { get; set; }
    /// <summary>Degrees (a number) or a sexagesimal string.</summary>
    [JsonPropertyName("RA")] public object? Ra { get; set; }
    [JsonPropertyName("DEC")] public object? Dec { get; set; }
    [JsonPropertyName("OBJCTRA")] public string? ObjctRa { get; set; }
    [JsonPropertyName("OBJCTDEC")] public string? ObjctDec { get; set; }
    [JsonPropertyName("PIXSCALE")] public double? PixScale { get; set; }
    [JsonPropertyName("SET-TEMP")] public double? SetTemp { get; set; }
    [JsonPropertyName("CALSTAT")] public string? CalStat { get; set; }
    [JsonPropertyName("NAXIS1")] public int? Naxis1 { get; set; }
    [JsonPropertyName("NAXIS2")] public int? Naxis2 { get; set; }
    [JsonPropertyName("BITPIX")] public int? BitPix { get; set; }

    /// <summary>The 25 FITS keywords the contract accepts, in contract order.</summary>
    public static readonly IReadOnlyList<string> AllowedKeys = new[]
    {
        "FILTER", "IMAGETYP", "EXPTIME", "GAIN", "OFFSET", "XBINNING", "YBINNING", "INSTRUME", "TELESCOP",
        "FOCALLEN", "XPIXSZ", "YPIXSZ", "BAYERPAT", "DATE-OBS", "OBJECT", "RA", "DEC", "OBJCTRA", "OBJCTDEC",
        "PIXSCALE", "SET-TEMP", "CALSTAT", "NAXIS1", "NAXIS2", "BITPIX",
    };
}

public sealed class RigHint
{
    public string? RigId { get; init; }
}

public sealed class FrameInitRequest
{
    public string Sha256 { get; init; } = "";
    /// <summary>Bytes of the file as uploaded, after any header strip.</summary>
    public long Size { get; init; }
    public string Filename { get; init; } = "";
    public InitHeader Header { get; init; } = new();
    public RigHint? RigHint { get; init; }
    /// <summary>True when the plugin removed the observer-location cards before hashing.</summary>
    public bool Stripped { get; init; }
}

public sealed class UploadPart
{
    public int PartNumber { get; init; }
    public string Url { get; init; } = "";
    public long Size { get; init; }
}

public sealed class CompletedPart
{
    public int PartNumber { get; init; }
    public string Etag { get; init; } = "";
}

public sealed class UploadInstructions
{
    public string Type { get; init; } = "multipart";
    public string UploadId { get; init; } = "";
    public long PartSize { get; init; }
    public int PartCount { get; init; }
    /// <summary>Outstanding parts only. On a resume, parts already stored are omitted.</summary>
    public IReadOnlyList<UploadPart> PartUrls { get; init; } = Array.Empty<UploadPart>();
    public IReadOnlyList<CompletedPart> CompletedParts { get; init; } = Array.Empty<CompletedPart>();
    public DateTimeOffset ExpiresAt { get; init; }
}

public sealed class FrameInitResponse
{
    public string FrameId { get; init; } = "";
    public string Status { get; init; } = FrameStatuses.Uploading;
    public bool Resumed { get; init; }
    public string FrameType { get; init; } = "";
    public string Filter { get; init; } = "";
    public FilterResolution? Resolution { get; init; }
    public string RigId { get; init; } = "";
    public UploadInstructions Upload { get; init; } = new();
}

public sealed class FrameCompleteRequest
{
    public IReadOnlyList<CompletedPart> Etags { get; init; } = Array.Empty<CompletedPart>();
}

public sealed class FrameCompleteResponse
{
    public string FrameId { get; init; } = "";
    public string Status { get; init; } = "";
    public bool AlreadyComplete { get; init; }
}

public sealed class FrameMetrics
{
    public double? FwhmArcsec { get; init; }
    public double? Eccentricity { get; init; }
    public int? NStars { get; init; }
    public double? BgMad { get; init; }
    public double? SigmaNorm { get; init; }
    public double? SolveRmsPx { get; init; }
    public double? RejectFrac { get; init; }
    public double? Weight { get; init; }
    public int? StarsReached { get; init; }
    public double? GridOverlap { get; init; }
    public double? ScaleRatio { get; init; }
    /// <summary>The sub's up direction, degrees east of north, as <c>Grid.rotation_deg</c> (contract 1.21.0).</summary>
    public double? PositionAngleDeg { get; init; }
    /// <summary>The sky position of the sub's centre pixel, degrees (contract 1.25.0): where the camera pointed.</summary>
    public double? CentreRaDeg { get; init; }
    public double? CentreDecDeg { get; init; }
    public double? FitSystematicPx { get; init; }
    public double? FitSystematicSigmaPx { get; init; }
    public double? FitWorstCellPx { get; init; }
    public double? FitStarNoisePx { get; init; }
    public double? ZeroPointMag { get; init; }
    public int? ZeroPointStars { get; init; }
    public double? ZeroPointScatterMag { get; init; }
    public string? PhotometryMethod { get; init; }
}

public sealed class FrameSummary
{
    public string FrameId { get; init; } = "";
    public string Status { get; init; } = "";
    public string Filter { get; init; } = "";
    public DateTimeOffset? UploadedAt { get; init; }
}

public sealed class Frame
{
    public string FrameId { get; init; } = "";
    public string ProjectId { get; init; } = "";
    public string Status { get; init; } = "";
    public string FrameType { get; init; } = "";
    public string Filter { get; init; } = "";
    public string? FilterRaw { get; init; }
    public IReadOnlyList<string> TargetStreams { get; init; } = Array.Empty<string>();
    public string Filename { get; init; } = "";
    public long Size { get; init; }
    public string Sha256 { get; init; } = "";
    public double? ExposureS { get; init; }
    public string? DateObs { get; init; }
    public string? RigId { get; init; }
    public string? RejectReason { get; init; }
    public string? RejectDetail { get; init; }
    public string? QuarantineReason { get; init; }
    public IReadOnlyList<string> Flags { get; init; } = Array.Empty<string>();
    public FrameMetrics Metrics { get; init; } = new();
    /// <summary>Which masters the frame was calibrated with (contract 1.11.0; members' frame routes only, absent elsewhere).</summary>
    public FrameCalibration? Calibration { get; init; }
    public int? GoldSeq { get; init; }
    public DateTimeOffset CreatedAt { get; init; }
    public DateTimeOffset? UploadedAt { get; init; }
    public DateTimeOffset UpdatedAt { get; init; }
}

/// <summary>The calibration a light got (contract 1.11.0): each master kind, or null when none was applied.</summary>
public sealed class FrameCalibration
{
    public bool Precalibrated { get; init; }
    public AppliedMaster? Bias { get; init; }
    public AppliedMaster? Dark { get; init; }
    public AppliedMaster? Flat { get; init; }
    public double? DarkScale { get; init; }
}

/// <summary>One applied master: built from raw frames or uploaded, its night, filter (flats), frame count and exposure (darks).</summary>
public sealed class AppliedMaster
{
    public string? Source { get; init; }
    public string? Night { get; init; }
    public string? Filter { get; init; }
    public int? Frames { get; init; }
    public double? ExposureS { get; init; }
    /// <summary>For a flat, the earliest <c>DATE-OBS</c> of the cluster of flats it was built from (UTC; contract 1.22.0).</summary>
    public DateTimeOffset? TakenFrom { get; init; }
    /// <summary>For a flat, the latest <c>DATE-OBS</c> of its cluster (UTC; contract 1.22.0).</summary>
    public DateTimeOffset? TakenTo { get; init; }
    /// <summary>The master came from the rig's calibration library rather than the project's own calibration frames (contract 1.23.0); false for a frame prepared before.</summary>
    public bool Library { get; init; }
}

public sealed class FrameResponse
{
    public Frame Frame { get; init; } = new();
}

public sealed class FrameListResponse
{
    public IReadOnlyList<Frame> Frames { get; init; } = Array.Empty<Frame>();
    public string? NextCursor { get; init; }
}

/// <summary>An answer with nothing in it but <c>ok</c>.</summary>
public sealed class AckResponse
{
}

public sealed class WithdrawResponse
{
    public string FrameId { get; init; } = "";
    public string Status { get; init; } = "";
    public DateTimeOffset RawDeleteAfter { get; init; }
}

/// <summary>Query for <c>GET …/frames</c>.</summary>
public sealed class FrameListQuery
{
    public bool Mine { get; init; } = true;
    public string? Status { get; init; }
    public string? Filter { get; init; }
    public DateTimeOffset? Since { get; init; }
    public int? Limit { get; init; }
    public string? Cursor { get; init; }
}


// ---- panels.json (contract 1.17.0, panels.schema.json) ---------------------------------------------------------

/// <summary>
/// The per-panel progress document of a mosaic project. Sparse: a panel without data is absent. Read tolerantly (unknown
/// members ignored). Display names only.
/// </summary>
public sealed class PanelsDocument
{
    public int SchemaVersion { get; init; }
    public DateTimeOffset GeneratedAt { get; init; }
    public string Access { get; init; } = "public";
    public string ProjectId { get; init; } = "";
    public string Attribution { get; init; } = "nearest_centre";
    public PanelsLayout Layout { get; init; } = new();
    public MosaicTargets Targets { get; init; } = new();
    public PanelsTotals Totals { get; init; } = new();
    public IReadOnlyList<PanelProgress> Panels { get; init; } = Array.Empty<PanelProgress>();
}

public sealed class PanelsLayout
{
    public int Rows { get; init; }
    public int Cols { get; init; }
}

public sealed class PanelsTotals
{
    public int PanelsWithData { get; init; }
    public int FramesAccepted { get; init; }
    public double IntegrationS { get; init; }
}

public sealed class PanelProgress
{
    public string PanelId { get; init; } = "";
    public int FramesAccepted { get; init; }
    public double IntegrationS { get; init; }
    public IReadOnlyList<string> Contributors { get; init; } = Array.Empty<string>();
    /// <summary>Stream key (<c>HA</c>, <c>HA@3nm</c>, <c>R@osc</c>) to that stream's figures in this panel.</summary>
    public Dictionary<string, PanelStreamProgress> Streams { get; init; } = new();
}

public sealed class PanelStreamProgress
{
    public int FramesAccepted { get; init; }
    public double IntegrationS { get; init; }
    public double? MedianFwhmArcsec { get; init; }
    public IReadOnlyList<string> Contributors { get; init; } = Array.Empty<string>();
    public double? Coverage { get; init; }
    public double? TargetS { get; init; }
}


// ---- Calibration library (contract 1.23.0, README open points 98 to 104) -------------------------------------------

/// <summary>Body of <c>POST /rigs/{rig_id}/library/frames/init</c>. Unlike a project frame there is no rig hint (the rig is in the path), and <c>stripped</c> must be true.</summary>
public sealed class LibraryFrameInitRequest
{
    public string Sha256 { get; init; } = "";
    /// <summary>Bytes of the stripped file as uploaded.</summary>
    public long Size { get; init; }
    public string Filename { get; init; } = "";
    public InitHeader Header { get; init; } = new();
    /// <summary>Always true: a library frame is always uploaded with its location cards removed (open point 104).</summary>
    public bool Stripped { get; init; } = true;
}

public sealed class LibraryFrameInitResponse
{
    public string FrameId { get; init; } = "";
    public string Status { get; init; } = FrameStatuses.Uploading;
    public bool Resumed { get; init; }
    /// <summary>One of <see cref="LibraryFrameTypes"/>.</summary>
    public string FrameType { get; init; } = "";
    public string RigId { get; init; } = "";
    public UploadInstructions Upload { get; init; } = new();
}

public sealed class LibraryFrameCompleteResponse
{
    public string FrameId { get; init; } = "";
    /// <summary>One of <see cref="LibraryFrameStatuses"/>.</summary>
    public string Status { get; init; } = "";
    public bool AlreadyComplete { get; init; }
}

/// <summary>One frame of a rig's library as its owner sees it.</summary>
public sealed class LibraryFrame
{
    public string FrameId { get; init; } = "";
    public string FrameType { get; init; } = "";
    public string Status { get; init; } = "";
    public string Sha256 { get; init; } = "";
    public long Size { get; init; }
    public string Filename { get; init; } = "";
    public double? ExposureS { get; init; }
    public double? Gain { get; init; }
    public double? Offset { get; init; }
    public int? Binning { get; init; }
    public double? SetTempC { get; init; }
    public string? DateObs { get; init; }
    public DateTimeOffset? UploadedAt { get; init; }
    /// <summary>Null until the worker has built the library with this frame in view; then whether it was used.</summary>
    public bool? Usable { get; init; }
    /// <summary>One of <see cref="LibraryProblems"/> when <see cref="Usable"/> is false.</summary>
    public string? Problem { get; init; }
}

/// <summary>One master the worker built (or took as uploaded) from the library.</summary>
public sealed class LibraryMaster
{
    /// <summary><c>bias</c>, <c>dark</c> or <c>dark_flat</c>.</summary>
    public string Kind { get; init; } = "";
    /// <summary><c>built</c> or <c>uploaded</c>.</summary>
    public string Source { get; init; } = "";
    public double? ExposureS { get; init; }
    public double? Gain { get; init; }
    public double? Offset { get; init; }
    public int? Binning { get; init; }
    public double? SetTempC { get; init; }
    public int? Frames { get; init; }
}

public sealed class LibraryUsage
{
    public long BytesUsed { get; init; }
    public long FramesUsed { get; init; }
    public long MaxBytes { get; init; }
    public long MaxFrames { get; init; }
}

public sealed class LibraryResponse
{
    public string RigId { get; init; } = "";
    public LibraryUsage Usage { get; init; } = new();
    public IReadOnlyList<LibraryFrame> Frames { get; init; } = Array.Empty<LibraryFrame>();
    public IReadOnlyList<LibraryMaster> Masters { get; init; } = Array.Empty<LibraryMaster>();
    public DateTimeOffset? BuiltAt { get; init; }
    public string? NextCursor { get; init; }
}

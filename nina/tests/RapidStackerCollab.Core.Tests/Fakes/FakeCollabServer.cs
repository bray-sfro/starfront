// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Tests.Fakes;

public sealed record RecordedRequest(string Method, string Path, string Query, string? Body, string? Authorization, long Seq, string? UserAgent = null);

/// <summary>A stored frame as the fake backend holds it.</summary>
public sealed class FakeFrame
{
    public string FrameId { get; init; } = "";
    public string ProjectId { get; init; } = "";
    public string Sha256 { get; init; } = "";
    public long Size { get; init; }
    public string Filename { get; init; } = "";
    public string UploadId { get; init; } = "";
    public string Status { get; set; } = "uploading";
    public JsonObject Header { get; init; } = new();
    public bool Stripped { get; init; }
    public int PartCount { get; init; }
    public long PartSize { get; init; }
    public byte[]? Object { get; set; }
    public string? RejectReason { get; set; }
}

/// <summary>
/// An in-process stand-in for the Rapid Stacker Collab API and for the S3 presigned part URLs it hands out, on one
/// loopback Kestrel port: <c>/api/*</c> follows docs/api/openapi.yaml (envelope, codes, upload flow) and
/// <c>/s3/{upload_id}/{part}</c> accepts the part PUTs and returns an <c>ETag</c>. Behaviour a test needs to bend is
/// either a property (part size, quota) or an interceptor that answers a matching request before the default does.
/// </summary>
public sealed class FakeCollabServer : IAsyncDisposable
{
    public const string ProjectId = "prj_7k3m9q2x5v8d1hn4t6wz";
    public const string Slug = "horsehead-sho";
    public const string RigId = "rig_9c1d4e7a02bb";
    public const string UserCode = "K7QM-3XWD";
    public static readonly string DeviceCode = "rscd_" + new string('d', 64);
    public static readonly string Token = "rscu_" + new string('a', 64);

    private readonly WebApplication app;
    private readonly object gate = new();
    private readonly List<RecordedRequest> requests = new();
    private readonly List<Interceptor> interceptors = new();
    private readonly Dictionary<string, FakeFrame> framesById = new();
    private readonly Dictionary<string, Dictionary<int, (string Etag, byte[] Data)>> parts = new();
    private readonly Dictionary<int, int> partPutCounts = new();
    private long seq;
    private int frameCounter;
    private bool deviceApproved;
    private bool deviceDenied;
    private bool deviceExpired;

    private FakeCollabServer(WebApplication app, Uri baseAddress)
    {
        this.app = app;
        BaseAddress = baseAddress;
    }

    public Uri BaseAddress { get; }
    public Uri ApiBase => new(BaseAddress, "api/");

    /// <summary>Bytes per upload part the server asks for. Real servers say 16 MiB; tests use something small.</summary>
    public long PartSize { get; set; } = 64 * 1024;

    /// <summary>Remaining bytes the contributor may still reserve; <c>init</c> answers 429 <c>quota_exceeded</c> beyond it.</summary>
    public long QuotaBytesRemaining { get; set; } = long.MaxValue;

    public int DevicePollIntervalSeconds { get; set; } = 5;

    /// <summary>The project's <c>require_coordinate_strip</c>: <c>init</c> answers 400 <c>strip_required</c> unless the request says <c>stripped: true</c>.</summary>
    public bool RequireCoordinateStrip { get; set; }

    /// <summary>Each part PUT sleeps this long before answering, so parallel PUTs overlap and <see cref="PeakConcurrentPuts"/> means something.</summary>
    public TimeSpan PutDelay { get; set; } = TimeSpan.Zero;

    private int activePuts;
    private int peakPuts;

    /// <summary>The most part PUTs the fake ever saw in flight at once.</summary>
    public int PeakConcurrentPuts => Volatile.Read(ref peakPuts);

    /// <summary>The set of project terms the fake advertises and demands on join.</summary>
    /// <summary>The <c>mosaic</c> member of <c>GET .../mosaic</c> (contract 1.17.0); null answers <c>"mosaic": null</c>, a single field.</summary>
    public JsonObject? Mosaic { get; set; }

    /// <summary>Served at <c>/public/panels.json</c> when set; the status document then carries a <c>panels</c> index pointing at it.</summary>
    public JsonObject? PanelsDoc { get; set; }

    public string TermsVersion { get; set; } = "2026-10-13-draft";

    /// <summary>When true, <c>GET /projects?scope=mine</c> answers as a server before contract 1.8.0 did: summaries only.</summary>
    public bool LegacyProjectList { get; set; }

    /// <summary>The token the fake accepts; anything else is 401 <c>token_invalid</c>.</summary>
    public string AcceptedToken { get; set; } = Token;

    /// <summary>How many times the client revoked its own token (<c>DELETE /auth/token</c>); the fake then rejects that token.</summary>
    public int SelfRevocations { get; private set; }

    public IReadOnlyList<RecordedRequest> Requests
    {
        get { lock (gate) return requests.ToList(); }
    }

    public IReadOnlyList<RecordedRequest> RequestsTo(string method, string pathContains)
    {
        lock (gate) return requests.Where(r => r.Method == method && r.Path.Contains(pathContains, StringComparison.Ordinal)).ToList();
    }

    public IReadOnlyList<FakeFrame> Frames
    {
        get { lock (gate) return framesById.Values.ToList(); }
    }

    /// <summary>How many times part <paramref name="partNumber"/> was PUT, whether or not the PUT succeeded.</summary>
    public int PartPuts(int partNumber)
    {
        lock (gate) return partPutCounts.GetValueOrDefault(partNumber);
    }

    public int TotalPartPuts
    {
        get { lock (gate) return partPutCounts.Values.Sum(); }
    }

    public static async Task<FakeCollabServer> StartAsync()
    {
        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        builder.WebHost.ConfigureKestrel(k => k.Limits.MaxRequestBodySize = 256L * 1024 * 1024);
        var app = builder.Build();
        FakeCollabServer? server = null;
        app.Run(ctx => server!.HandleAsync(ctx));
        await app.StartAsync();
        var address = app.Services.GetRequiredService<IServer>().Features.Get<IServerAddressesFeature>()!.Addresses.First();
        server = new FakeCollabServer(app, new Uri(address.EndsWith('/') ? address : address + "/"));
        return server;
    }

    public async ValueTask DisposeAsync()
    {
        await app.StopAsync();
        await app.DisposeAsync();
    }

    // ---- Test controls -----------------------------------------------------------------------------------------

    public void ApproveDevice() => deviceApproved = true;
    public void DenyDevice() => deviceDenied = true;
    public void ExpireDevice() => deviceExpired = true;

    /// <summary>
    /// Answer requests for which <paramref name="match"/> is true with <paramref name="respond"/> instead of the default
    /// handling, <paramref name="times"/> times (default: once). Interceptors run in registration order.
    /// </summary>
    public void Intercept(Func<RecordedRequest, bool> match, Func<HttpContext, Task> respond, int times = 1)
    {
        lock (gate) interceptors.Add(new Interceptor(match, respond, times));
    }

    public void InterceptError(Func<RecordedRequest, bool> match, int status, string code, string message = "scripted",
        int? retryAfterSeconds = null, int times = 1, JsonObject? extra = null) =>
        Intercept(match, ctx => WriteErrorAsync(ctx, status, code, message, retryAfterSeconds, extra), times);

    public static Func<RecordedRequest, bool> Route(string method, string pathContains) =>
        r => r.Method == method && r.Path.Contains(pathContains, StringComparison.Ordinal);

    /// <summary>Insert a frame as if an earlier run had already finished an upload of this content.</summary>
    public FakeFrame SeedFrame(string sha256, string status, long size = 1)
    {
        lock (gate)
        {
            var f = NewFrame(sha256, size, "seed.fits", new JsonObject(), false, 1, PartSize);
            f.Status = status;
            return f;
        }
    }

    // ---- Dispatch ----------------------------------------------------------------------------------------------

    private sealed class Interceptor
    {
        public Interceptor(Func<RecordedRequest, bool> match, Func<HttpContext, Task> respond, int times)
        {
            Match = match;
            Respond = respond;
            Remaining = times;
        }

        public Func<RecordedRequest, bool> Match { get; }
        public Func<HttpContext, Task> Respond { get; }
        public int Remaining { get; set; }
    }

    private async Task HandleAsync(HttpContext ctx)
    {
        var req = ctx.Request;
        string? bodyText = null;
        byte[]? bodyBytes = null;
        if (req.ContentLength is > 0 || req.Headers.ContainsKey("Transfer-Encoding"))
        {
            using var ms = new MemoryStream();
            await req.Body.CopyToAsync(ms);
            bodyBytes = ms.ToArray();
            if ((req.ContentType ?? "").Contains("json", StringComparison.OrdinalIgnoreCase))
                bodyText = Encoding.UTF8.GetString(bodyBytes);
        }
        ctx.Items["bodyBytes"] = bodyBytes;
        var rec = new RecordedRequest(req.Method, req.Path.Value ?? "", req.QueryString.Value ?? "", bodyText,
            req.Headers.Authorization.ToString(), Interlocked.Increment(ref seq), req.Headers.UserAgent.ToString());
        Interceptor? hit = null;
        lock (gate)
        {
            requests.Add(rec);
            if (rec.Method == "PUT" && rec.Path.StartsWith("/s3/", StringComparison.Ordinal)
                && int.TryParse(rec.Path.Split('/').ElementAtOrDefault(3), out var putPart))
                partPutCounts[putPart] = partPutCounts.GetValueOrDefault(putPart) + 1; // counted even when an interceptor answers
            foreach (var i in interceptors)
            {
                if (i.Remaining > 0 && i.Match(rec))
                {
                    i.Remaining--;
                    hit = i;
                    break;
                }
            }
        }
        if (hit is not null)
        {
            await hit.Respond(ctx);
            return;
        }
        try
        {
            await RouteAsync(ctx, rec, bodyText, bodyBytes);
        }
        catch (Exception ex)
        {
            await WriteErrorAsync(ctx, 500, "internal_error", "fake server bug: " + ex);
        }
    }

    private async Task RouteAsync(HttpContext ctx, RecordedRequest rec, string? bodyText, byte[]? bodyBytes)
    {
        var path = rec.Path;
        if (path.StartsWith("/s3/", StringComparison.Ordinal))
        {
            await HandlePartPutAsync(ctx, path, bodyBytes ?? Array.Empty<byte>());
            return;
        }
        if (path == "/public/panels.json" && PanelsDoc is not null)
        {
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync(PanelsDoc.ToJsonString());
            return;
        }
        if (!path.StartsWith("/api/", StringComparison.Ordinal))
        {
            ctx.Response.StatusCode = 404;
            ctx.Response.ContentType = "text/html";
            await ctx.Response.WriteAsync("<html>Not found</html>");
            return;
        }
        var route = path["/api/".Length..];
        var m = rec.Method;
        var json = string.IsNullOrEmpty(bodyText) ? null : JsonNode.Parse(bodyText)?.AsObject();

        // Anonymous routes.
        if (route == "device/start" && m == "POST") { await DeviceStartAsync(ctx); return; }
        if (route == "device/poll" && m == "POST") { await DevicePollAsync(ctx, json); return; }
        if (route == "filters/normalize" && m == "GET" && !rec.Query.Contains("project=", StringComparison.Ordinal))
        {
            await NormalizeAsync(ctx, rec.Query);
            return;
        }

        // Everything else needs the token.
        if (rec.Authorization != "Bearer " + AcceptedToken)
        {
            await WriteErrorAsync(ctx, 401, string.IsNullOrEmpty(rec.Authorization) ? "not_authenticated" : "token_invalid", "Sign in again.");
            return;
        }

        switch (route)
        {
            case "auth/token" when m == "DELETE":
                SelfRevocations++;
                AcceptedToken = "";
                await WriteOkAsync(ctx, new JsonObject());
                return;
            case "rigs" when m == "GET": await WriteOkAsync(ctx, new JsonObject { ["rigs"] = new JsonArray(RigJson()) }); return;
            case "rigs" when m == "POST": await WriteOkAsync(ctx, new JsonObject { ["rig"] = RigJson(json) }); return;
            case var r when r.StartsWith("rigs/", StringComparison.Ordinal) && r.Contains("/library", StringComparison.Ordinal):
                await LibraryRouteAsync(ctx, rec, r, json);
                return;
            case var r when r.StartsWith("rigs/", StringComparison.Ordinal) && m == "PATCH": await WriteOkAsync(ctx, new JsonObject { ["rig"] = RigJson(json) }); return;
            case "filters/normalize": await NormalizeAsync(ctx, rec.Query); return;
            case "projects" when m == "GET": await WriteOkAsync(ctx, new JsonObject { ["projects"] = new JsonArray(MineItemJson()), ["directory_enabled"] = false, ["next_cursor"] = null }); return;
            case "projects/resolve-join-code" when m == "POST": await ResolveJoinCodeAsync(ctx, json); return;
        }

        if (route.StartsWith("projects/", StringComparison.Ordinal))
        {
            var rest = route["projects/".Length..].Split('/');
            var project = rest[0];
            if (project != ProjectId && project != Slug)
            {
                await WriteErrorAsync(ctx, 400, "not_found", "No such project.");
                return;
            }
            var tail = string.Join('/', rest.Skip(1));
            switch (tail)
            {
                case "" when m == "GET": await WriteOkAsync(ctx, new JsonObject { ["project"] = ProjectDetailJson() }); return;
                case "join" when m == "POST": await JoinAsync(ctx, json); return;
                case "membership" when m == "GET": await WriteOkAsync(ctx, new JsonObject { ["membership"] = MembershipJson() }); return;
                case "membership" when m == "PUT": await WriteOkAsync(ctx, new JsonObject { ["membership"] = MembershipJson(json) }); return;
                case "status" when m == "GET":
                    var statusDoc = StatusDocJson();
                    if (Mosaic is not null)
                        statusDoc["panels"] = new JsonObject { ["url"] = new Uri(BaseAddress, "public/panels.json").AbsoluteUri, ["updated_at"] = "2026-10-13T12:00:00.000Z", ["with_data"] = 1, ["total"] = 12 };
                    await WriteOkAsync(ctx, new JsonObject { ["status_doc"] = statusDoc });
                    return;
                case "mosaic" when m == "GET": await WriteOkAsync(ctx, new JsonObject { ["mosaic"] = Mosaic?.DeepClone() }); return;
                case "mosaic/claims" when m == "PUT":
                    if (Mosaic is null) { await WriteErrorAsync(ctx, 409, "mosaic_required", "This project has no mosaic."); return; }
                    Mosaic["my_claims"] = json?["panels"]?.DeepClone() ?? new JsonArray();
                    await WriteOkAsync(ctx, new JsonObject { ["mosaic"] = Mosaic.DeepClone() });
                    return;
                case "frames/init" when m == "POST": await FrameInitAsync(ctx, json!); return;
                case "frames" when m == "GET": await FrameListAsync(ctx); return;
            }
            if (rest.Length >= 3 && rest[1] == "frames")
            {
                var frameId = rest[2];
                var action = rest.Length > 3 ? rest[3] : "";
                if (action == "complete" && m == "POST") { await FrameCompleteAsync(ctx, frameId, json!); return; }
                if (action == "" && m == "GET") { await FrameGetAsync(ctx, frameId); return; }
                if (action == "withdraw" && m == "POST") { await FrameWithdrawAsync(ctx, frameId); return; }
            }
        }
        await WriteErrorAsync(ctx, 400, "invalid_request", $"fake: no route for {m} {route}");
    }

    // ---- Device ------------------------------------------------------------------------------------------------

    private Task DeviceStartAsync(HttpContext ctx) => WriteOkAsync(ctx, new JsonObject
    {
        ["device_code"] = DeviceCode,
        ["user_code"] = UserCode,
        ["verification_uri"] = "https://collab.astro.ekeservices.com/device",
        ["verification_uri_complete"] = "https://collab.astro.ekeservices.com/device?code=" + UserCode,
        ["expires_in"] = 600,
        ["interval"] = DevicePollIntervalSeconds,
        ["scopes"] = new JsonArray("projects:read", "projects:join", "frames:read", "frames:write", "masters:read"),
    });

    private async Task DevicePollAsync(HttpContext ctx, JsonObject? body)
    {
        if (body?["device_code"]?.GetValue<string>() != DeviceCode)
        {
            await WriteErrorAsync(ctx, 400, "invalid_code", "Unknown device code.");
            return;
        }
        if (deviceDenied) { await WriteErrorAsync(ctx, 200, "device_denied", "Denied."); return; }
        if (deviceExpired) { await WriteErrorAsync(ctx, 200, "device_expired", "Expired."); return; }
        if (!deviceApproved)
        {
            await WriteOkAsync(ctx, new JsonObject { ["status"] = "pending" });
            return;
        }
        await WriteOkAsync(ctx, new JsonObject
        {
            ["status"] = "approved",
            ["token"] = Token,
            ["info"] = new JsonObject
            {
                ["token_id"] = "tok_0123456789abcdef", ["name"] = "N.I.N.A. plugin", ["scopes"] = new JsonArray("frames:write"),
                ["hint"] = "rscu_aaaa", ["source"] = "device", ["client_name"] = "N.I.N.A. plugin",
                ["created_at"] = "2026-10-13T12:00:00.000Z", ["last_used_at"] = null, ["expires_at"] = "2027-04-11T12:00:00.000Z",
            },
            ["user"] = new JsonObject { ["user_id"] = "3f2b8c1e-5a47-4d9b-9c0e-1a2b3c4d5e6f", ["email"] = "ed@example.com", ["name"] = "Ed", ["role"] = "user", ["email_verified"] = true },
        });
    }

    // ---- Filters / rigs / projects -----------------------------------------------------------------------------

    private static Task NormalizeAsync(HttpContext ctx, string query)
    {
        var names = Uri.UnescapeDataString(query.Split("names=").ElementAtOrDefault(1)?.Split('&')[0] ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries);
        var results = new JsonArray();
        foreach (var n in names)
            results.Add(new JsonObject { ["raw"] = n, ["canonical"] = n.StartsWith("H", StringComparison.OrdinalIgnoreCase) ? "HA" : "UNKNOWN", ["confidence"] = "pattern", ["source"] = "pattern", ["bandwidth_nm"] = null, ["vendor"] = null });
        return WriteOkAsync(ctx, new JsonObject { ["results"] = results, ["aliases_version"] = "fake-1" });
    }

    /// <summary>The fake rig's camera (<c>GET /rigs</c>); a library frame whose <c>INSTRUME</c> differs is refused at <c>library/frames/init</c>.</summary>
    public string RigCamera { get; set; } = "ASI2600MM";

    /// <summary>The fake rig's sensor size; null reports and checks none.</summary>
    public int? RigWidthPx { get; set; } = 6248;

    public int? RigHeightPx { get; set; } = 4176;

    public int RigBinning { get; set; } = 1;

    private JsonObject RigJson(JsonObject? req = null) => new()
    {
        ["rig_id"] = RigId,
        ["name"] = req?["name"]?.GetValue<string>() ?? "Main rig",
        ["camera"] = req?["camera"]?.GetValue<string>() ?? RigCamera,
        ["telescope"] = req?["telescope"]?.GetValue<string>() ?? "RedCat 51",
        ["focal_length_mm"] = 250,
        ["pixel_size_um"] = 3.76,
        ["binning"] = RigBinning,
        ["osc"] = false,
        ["bayer_pattern"] = null,
        ["pixel_scale_arcsec"] = 3.1,
        ["width_px"] = RigWidthPx,
        ["height_px"] = RigHeightPx,
        ["confirmed"] = true,
        ["frame_count"] = 0,
        ["last_frame_at"] = null,
        ["created_at"] = "2026-10-13T12:00:00.000Z",
    };

    private JsonObject ProjectSummaryJson() => new()
    {
        ["project_id"] = ProjectId, ["slug"] = Slug, ["name"] = "Horsehead SHO", ["target_name"] = "Horsehead Nebula",
        ["status"] = "open", ["visibility"] = "unlisted", ["owner_name"] = "Ed", ["palette"] = "SHO",
        ["filters"] = new JsonArray("HA", "OIII", "SII"),
        ["counts"] = new JsonObject { ["member_count"] = 3, ["frames_accepted"] = 120, ["integration_s"] = 36000.5, ["last_update"] = "2026-10-12T09:00:00.000Z" },
        ["combined_preview_url"] = null, ["created_at"] = "2026-10-01T00:00:00.000Z",
    };

    /// <summary>A <c>scope=mine</c> item: the summary plus grid, policy and own membership (contract 1.8.0), unless <see cref="LegacyProjectList"/>.</summary>
    private JsonObject MineItemJson()
    {
        var item = ProjectSummaryJson();
        if (LegacyProjectList) return item;
        item["grid"] = GridJson();
        item["policy"] = PolicyJson();
        item["my_membership"] = MembershipJson();
        return item;
    }

    private JsonObject GridJson() => new()
    {
        ["center_ra_deg"] = 85.25, ["center_dec_deg"] = -2.458, ["rotation_deg"] = 0, ["scale_arcsec_px"] = 1.5, ["width_px"] = 3000, ["height_px"] = 2000,
    };

    private JsonObject PolicyJson() => new() { ["calibration"] = "preferred", ["osc_allowed"] = false, ["require_coordinate_strip"] = RequireCoordinateStrip, ["min_exposure_s"] = 5 };

    private JsonObject MembershipJson(JsonObject? patch = null) => new()
    {
        ["project_id"] = ProjectId, ["user_id"] = "3f2b8c1e-5a47-4d9b-9c0e-1a2b3c4d5e6f", ["role"] = "contributor", ["status"] = "active",
        ["display_name"] = patch?["display_name"]?.GetValue<string>() ?? "Ed", ["country"] = null, ["accepted_terms_version"] = TermsVersion,
        ["accepted_terms_at"] = "2026-10-13T12:00:00.000Z",
        ["allow_owner_raw_download"] = patch?["allow_owner_raw_download"]?.GetValue<bool>() ?? false,
        ["filter_mapping"] = patch?["filter_mapping"]?.DeepClone() ?? new JsonArray(),
        ["usage"] = new JsonObject { ["bytes_used"] = 0, ["frames_used"] = 0, ["bytes_cap"] = 64424509440L, ["frames_cap"] = 2000 },
        ["stats"] = new JsonObject { ["frames_accepted"] = 0, ["frames_rejected"] = 0, ["integration_s"] = 0, ["last_frame_at"] = null },
        ["joined_at"] = "2026-10-13T12:00:00.000Z",
    };

    private JsonObject ProjectDetailJson() => new()
    {
        ["project_id"] = ProjectId, ["slug"] = Slug, ["name"] = "Horsehead SHO", ["target_name"] = "Horsehead Nebula", ["description"] = "",
        ["status"] = "open", ["visibility"] = "unlisted", ["licence"] = "members_only", ["palette"] = "SHO",
        ["filters"] = new JsonArray("HA", "OIII", "SII"), ["grid"] = GridJson(), ["policy"] = PolicyJson(),
        ["counts"] = ProjectSummaryJson()["counts"]!.DeepClone(), ["owner_name"] = "Ed", ["terms_version"] = TermsVersion,
        ["my_membership"] = MembershipJson(), ["created_at"] = "2026-10-01T00:00:00.000Z", ["updated_at"] = "2026-10-12T00:00:00.000Z",
    };

    private async Task ResolveJoinCodeAsync(HttpContext ctx, JsonObject? body)
    {
        var code = (body?["code"]?.GetValue<string>() ?? "").Replace("-", "").ToUpperInvariant();
        if (code != "H4RT92BKWNPC")
        {
            await WriteErrorAsync(ctx, 400, "invalid_code", "Unknown join code.");
            return;
        }
        await WriteOkAsync(ctx, new JsonObject
        {
            ["project"] = ProjectSummaryJson(), ["grid"] = GridJson(), ["policy"] = PolicyJson(), ["terms_version"] = TermsVersion,
            ["terms_url"] = "https://collab.astro.ekeservices.com/terms", ["requires_approval"] = false, ["already_member"] = false,
        });
    }

    private async Task JoinAsync(HttpContext ctx, JsonObject? body)
    {
        if (body?["accepted_terms_version"]?.GetValue<string>() != TermsVersion)
        {
            await WriteErrorAsync(ctx, 409, "terms_outdated", "Terms changed.", extra: new JsonObject { ["terms_version"] = TermsVersion });
            return;
        }
        await WriteOkAsync(ctx, new JsonObject { ["membership"] = MembershipJson(body) });
    }

    private static JsonObject StatusDocJson() => new()
    {
        ["schema_version"] = 1, ["generated_at"] = "2026-10-13T12:00:00.000Z", ["access"] = "public",
        ["totals"] = new JsonObject { ["frames_accepted"] = 120, ["frames_rejected"] = 4, ["integration_s"] = 36000.5, ["streams"] = 3, ["sessions"] = 5, ["contributors"] = 3, ["member_count"] = 3, ["last_update"] = "2026-10-12T09:00:00.000Z", ["gold_seq_max"] = null, ["rejected_by_reason"] = new JsonObject() },
        ["streams"] = new JsonArray(),
        ["combined"] = new JsonObject { ["palette"] = "SHO", ["live"] = null, ["gold"] = null },
        ["contributors"] = new JsonArray(),
        ["timeline"] = new JsonArray(),
    };

    // ---- Frames ------------------------------------------------------------------------------------------------

    private static readonly HashSet<string> AllowedHeader = new(InitHeader.AllowedKeys);

    private FakeFrame NewFrame(string sha, long size, string filename, JsonObject header, bool stripped, int partCount, long partSize, string? owner = null)
    {
        var n = Interlocked.Increment(ref frameCounter);
        var id = "frm_" + IdPrefix + n.ToString("D6");
        var frame = new FakeFrame
        {
            FrameId = id, ProjectId = owner ?? ProjectId, Sha256 = sha, Size = size, Filename = filename, UploadId = "upl" + n,
            Header = header, Stripped = stripped, PartCount = partCount, PartSize = partSize,
        };
        framesById[id] = frame;
        parts[frame.UploadId] = new();
        return frame;
    }

    // 20 + 6 characters: the 26 of a ULID, all valid lower-case Crockford base32.
    private const string IdPrefix = "01hzx8k3m9q2x5v8d1hn";

    private async Task FrameInitAsync(HttpContext ctx, JsonObject body)
    {
        var header = body["header"]?.AsObject() ?? new JsonObject();
        var bad = header.Select(kv => kv.Key).Where(k => !AllowedHeader.Contains(k)).ToList();
        if (bad.Count > 0)
        {
            var errors = new JsonArray(bad.Select(k => (JsonNode)new JsonObject { ["field"] = "header." + k, ["message"] = "not allowed" }).ToArray());
            await WriteErrorAsync(ctx, 400, "header_rejected", "Header contains keys outside the allow-list.", extra: new JsonObject { ["errors"] = errors });
            return;
        }
        var sha = body["sha256"]!.GetValue<string>();
        var size = body["size"]!.GetValue<long>();
        var filename = body["filename"]!.GetValue<string>();
        var stripped = body["stripped"]?.GetValue<bool>() ?? false;
        if (RequireCoordinateStrip && !stripped)
        {
            await WriteErrorAsync(ctx, 400, "strip_required", "This project requires location cards to be stripped before upload.",
                extra: new JsonObject { ["errors"] = new JsonArray(new JsonObject { ["field"] = "stripped", ["message"] = "must be true for this project" }) });
            return;
        }

        FakeFrame? frame = null;
        var resumed = false;
        JsonObject? failure = null;
        int failureStatus = 0;
        string failureCode = "";
        lock (gate)
        {
            var existing = framesById.Values.FirstOrDefault(f => f.ProjectId == ProjectId && f.Sha256 == sha);
            if (existing is not null && existing.Status != "uploading")
            {
                failureStatus = 409;
                failureCode = "duplicate";
                failure = new JsonObject
                {
                    ["existing_frame"] = new JsonObject { ["frame_id"] = existing.FrameId, ["status"] = existing.Status, ["filter"] = "HA", ["uploaded_at"] = null },
                };
            }
            else if (existing is not null)
            {
                frame = existing;
                resumed = true;
            }
            else if (size > QuotaBytesRemaining)
            {
                failureStatus = 429;
                failureCode = "quota_exceeded";
                failure = new JsonObject { ["quota"] = "contributor_bytes" };
            }
            else
            {
                if (QuotaBytesRemaining != long.MaxValue) QuotaBytesRemaining -= size;
                var partCount = (int)Math.Max(1, (size + PartSize - 1) / PartSize);
                frame = NewFrame(sha, size, filename, header, stripped, partCount, PartSize);
            }
        }
        if (frame is null)
        {
            await WriteErrorAsync(ctx, failureStatus, failureCode, failureCode, extra: failure);
            return;
        }

        JsonArray completed, urls;
        lock (gate)
        {
            var done = parts[frame.UploadId];
            completed = new JsonArray(done.OrderBy(kv => kv.Key)
                .Select(kv => (JsonNode)new JsonObject { ["part_number"] = kv.Key, ["etag"] = kv.Value.Etag }).ToArray());
            urls = new JsonArray();
            for (var p = 1; p <= frame.PartCount; p++)
            {
                if (done.ContainsKey(p)) continue;
                var partSize = p < frame.PartCount ? frame.PartSize : frame.Size - frame.PartSize * (frame.PartCount - 1);
                urls.Add(new JsonObject { ["part_number"] = p, ["url"] = new Uri(BaseAddress, $"s3/{frame.UploadId}/{p}?sig=fake").AbsoluteUri, ["size"] = partSize });
            }
        }
        await WriteOkAsync(ctx, new JsonObject
        {
            ["frame_id"] = frame.FrameId, ["status"] = "uploading", ["resumed"] = resumed, ["frame_type"] = "light", ["filter"] = "HA",
            ["resolution"] = new JsonObject { ["raw"] = header["FILTER"]?.GetValue<string>() ?? "", ["canonical"] = "HA", ["confidence"] = "exact", ["source"] = "alias_table" },
            ["rig_id"] = "rig_9c1d4e7a02bb",
            ["upload"] = new JsonObject
            {
                ["type"] = "multipart", ["upload_id"] = frame.UploadId, ["part_size"] = frame.PartSize, ["part_count"] = frame.PartCount,
                ["part_urls"] = urls, ["completed_parts"] = completed, ["expires_at"] = "2026-10-13T13:00:00.000Z",
            },
        });
    }

    private async Task HandlePartPutAsync(HttpContext ctx, string path, byte[] body)
    {
        var seg = path["/s3/".Length..].Split('/');
        var uploadId = seg[0];
        var part = int.Parse(seg[1]);
        if (ctx.Request.Method != "PUT") { ctx.Response.StatusCode = 405; return; }
        var now = Interlocked.Increment(ref activePuts);
        int seen;
        while ((seen = Volatile.Read(ref peakPuts)) < now && Interlocked.CompareExchange(ref peakPuts, now, seen) != seen) { }
        try
        {
            if (PutDelay > TimeSpan.Zero) await Task.Delay(PutDelay, ctx.RequestAborted);
        }
        finally
        {
            Interlocked.Decrement(ref activePuts);
        }
        string? etag = null;
        lock (gate)
        {
            if (parts.TryGetValue(uploadId, out var store))
            {
                etag = "\"" + Convert.ToHexString(MD5.HashData(body)).ToLowerInvariant() + "\"";
                store[part] = (etag, body);
            }
        }
        if (etag is null) { ctx.Response.StatusCode = 403; return; }
        ctx.Response.Headers.ETag = etag;
        ctx.Response.StatusCode = 200;
        await Task.CompletedTask;
    }

    private async Task FrameCompleteAsync(HttpContext ctx, string frameId, JsonObject body)
    {
        FakeFrame? frame;
        lock (gate) framesById.TryGetValue(frameId, out frame);
        if (frame is null) { await WriteErrorAsync(ctx, 400, "not_found", "No such frame."); return; }
        var sent = body["etags"]!.AsArray().ToDictionary(n => n!["part_number"]!.GetValue<int>(), n => n!["etag"]!.GetValue<string>());
        List<int> missing;
        var sizeMismatch = false;
        var already = false;
        lock (gate)
        {
            already = frame.Status != "uploading";
            var have = parts[frame.UploadId];
            missing = Enumerable.Range(1, frame.PartCount).Where(p => !have.ContainsKey(p) || !sent.TryGetValue(p, out var e) || e != have[p].Etag).ToList();
            if (!already && missing.Count == 0)
            {
                var total = have.Values.Sum(v => (long)v.Data.Length);
                if (total != frame.Size)
                {
                    sizeMismatch = true;
                    parts[frame.UploadId] = new();
                }
                else
                {
                    frame.Object = have.OrderBy(kv => kv.Key).SelectMany(kv => kv.Value.Data).ToArray();
                    frame.Status = "uploaded";
                }
            }
        }
        if (already)
        {
            await WriteOkAsync(ctx, new JsonObject { ["frame_id"] = frameId, ["status"] = frame.Status, ["already_complete"] = true });
            return;
        }
        if (missing.Count > 0)
        {
            await WriteErrorAsync(ctx, 409, "upload_incomplete", "Parts are missing.", extra: new JsonObject { ["outstanding_parts"] = new JsonArray(missing.Select(p => (JsonNode)p).ToArray()) });
            return;
        }
        if (sizeMismatch)
        {
            await WriteErrorAsync(ctx, 409, "size_mismatch", "Size differs; start again.");
            return;
        }
        await WriteOkAsync(ctx, new JsonObject { ["frame_id"] = frameId, ["status"] = "uploaded", ["already_complete"] = false });
    }

    private static JsonObject FrameJson(FakeFrame f) => new()
    {
        ["frame_id"] = f.FrameId, ["project_id"] = f.ProjectId, ["status"] = f.Status, ["frame_type"] = "light", ["filter"] = "HA",
        ["filter_raw"] = f.Header["FILTER"]?.GetValue<string>(), ["target_streams"] = new JsonArray("HA"), ["filename"] = f.Filename,
        ["size"] = f.Size, ["sha256"] = f.Sha256, ["exposure_s"] = f.Header["EXPTIME"]?.GetValue<double>(), ["date_obs"] = null, ["rig_id"] = "rig_9c1d4e7a02bb",
        ["reject_reason"] = f.RejectReason, ["reject_detail"] = null, ["quarantine_reason"] = null, ["flags"] = new JsonArray(), ["metrics"] = new JsonObject(),
        ["gold_seq"] = null, ["created_at"] = "2026-10-13T12:00:00.000Z", ["uploaded_at"] = null, ["updated_at"] = "2026-10-13T12:00:00.000Z",
    };

    private async Task FrameListAsync(HttpContext ctx)
    {
        JsonArray arr;
        lock (gate) arr = new JsonArray(framesById.Values.Where(f => f.ProjectId == ProjectId).Select(f => (JsonNode)FrameJson(f)).ToArray());
        await WriteOkAsync(ctx, new JsonObject { ["frames"] = arr, ["next_cursor"] = null });
    }

    private async Task FrameGetAsync(HttpContext ctx, string frameId)
    {
        FakeFrame? f;
        lock (gate) framesById.TryGetValue(frameId, out f);
        if (f is null) { await WriteErrorAsync(ctx, 400, "not_found", "No such frame."); return; }
        await WriteOkAsync(ctx, new JsonObject { ["frame"] = FrameJson(f) });
    }

    private async Task FrameWithdrawAsync(HttpContext ctx, string frameId)
    {
        FakeFrame? f;
        lock (gate) framesById.TryGetValue(frameId, out f);
        if (f is null) { await WriteErrorAsync(ctx, 400, "not_found", "No such frame."); return; }
        f.Status = "withdrawn";
        await WriteOkAsync(ctx, new JsonObject { ["frame_id"] = frameId, ["status"] = "withdrawn", ["raw_delete_after"] = "2026-10-20T12:00:00.000Z" });
    }

    // ---- Calibration library (contract 1.23.0) -----------------------------------------------------------------

    /// <summary>The per-user library quota the fake enforces (defaults of the contract: 30 GiB and 1 500 frames).</summary>
    public long LibraryMaxBytes { get; set; } = 30L << 30;

    public long LibraryMaxFrames { get; set; } = 1500;

    /// <summary>The most frames one inventory page returns, whatever <c>limit</c> says; small values exercise <c>next_cursor</c>.</summary>
    public int LibraryPageCap { get; set; } = int.MaxValue;

    /// <summary>Problems the fake reports for a frame once "built": sha256 -> problem code (<c>usable</c> becomes false).</summary>
    public Dictionary<string, string> LibraryFrameProblems { get; } = new();

    public IReadOnlyList<FakeFrame> LibraryFrames
    {
        get { lock (gate) return framesById.Values.Where(f => f.ProjectId.StartsWith("lib:", StringComparison.Ordinal)).ToList(); }
    }

    private static string LibraryTypeOf(string? imageTyp)
    {
        var key = new string((imageTyp ?? "").Where(char.IsLetter).ToArray()).ToLowerInvariant().Replace("frame", "");
        return key switch
        {
            "dark" => "dark", "bias" or "offset" => "bias", "darkflat" or "flatdark" => "dark_flat",
            "masterdark" => "master_dark", "masterbias" or "masteroffset" => "master_bias", "masterdarkflat" => "master_dark_flat",
            _ => "",
        };
    }

    private async Task LibraryRouteAsync(HttpContext ctx, RecordedRequest rec, string route, JsonObject? body)
    {
        var seg = route.Split('/'); // rigs/{rig}/library[/frames/init | /frames/{id}/complete | /frames/{id}/withdraw]
        if (seg.Length < 3 || seg[2] != "library") { await WriteErrorAsync(ctx, 400, "invalid_request", "fake: no such library route"); return; }
        if (seg[1] != RigId) { await WriteErrorAsync(ctx, 400, "not_found", "No such rig."); return; }
        var m = rec.Method;
        if (seg.Length == 3 && m == "GET") { await LibraryGetAsync(ctx, rec.Query); return; }
        if (seg.Length == 5 && seg[3] == "frames" && seg[4] == "init" && m == "POST") { await LibraryInitAsync(ctx, body!); return; }
        if (seg.Length == 6 && seg[3] == "frames" && seg[5] == "complete" && m == "POST") { await LibraryCompleteAsync(ctx, seg[4], body!); return; }
        if (seg.Length == 6 && seg[3] == "frames" && seg[5] == "withdraw" && m == "POST") { await LibraryWithdrawAsync(ctx, seg[4]); return; }
        await WriteErrorAsync(ctx, 400, "invalid_request", $"fake: no route for {m} {route}");
    }

    private (long Bytes, long Frames) LibraryUsageNow()
    {
        lock (gate)
        {
            var ready = framesById.Values.Where(f => f.ProjectId.StartsWith("lib:", StringComparison.Ordinal) && f.Status == "ready").ToList();
            return (ready.Sum(f => f.Size), ready.Count);
        }
    }

    private async Task LibraryGetAsync(HttpContext ctx, string query)
    {
        var limit = 50;
        string? cursor = null;
        foreach (var pair in query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var kv = pair.Split('=', 2);
            if (kv[0] == "limit" && int.TryParse(kv[1], out var l)) limit = l;
            if (kv[0] == "cursor") cursor = Uri.UnescapeDataString(kv[1]);
        }
        // The real route validates `limit` against the contract (1-100) and refuses anything else; the fake does the same, so a client
        // that asks for more fails here and not only against the real server (plugin 0.2.0.0 asked for 200).
        if (limit < 1 || limit > 100)
        {
            await WriteErrorAsync(ctx, 400, "invalid_request", "Invalid parameter 'limit': must be at most 100.");
            return;
        }
        limit = Math.Min(limit, LibraryPageCap);
        List<FakeFrame> all;
        lock (gate) all = framesById.Values.Where(f => f.ProjectId.StartsWith("lib:", StringComparison.Ordinal) && f.Status != "withdrawn").Reverse().ToList();
        var start = cursor is null ? 0 : int.Parse(cursor);
        var page = all.Skip(start).Take(limit).ToList();
        var (bytes, frames) = LibraryUsageNow();
        var arr = new JsonArray(page.Select(f =>
        {
            LibraryFrameProblems.TryGetValue(f.Sha256, out var problem);
            var exp = f.Header["EXPTIME"]?.GetValue<double>();
            return (JsonNode)new JsonObject
            {
                ["frame_id"] = f.FrameId, ["frame_type"] = LibraryTypeOf(f.Header["IMAGETYP"]?.GetValue<string>()), ["status"] = f.Status, ["sha256"] = f.Sha256,
                ["size"] = f.Size, ["filename"] = f.Filename, ["exposure_s"] = exp, ["gain"] = f.Header["GAIN"]?.GetValue<double>(), ["offset"] = f.Header["OFFSET"]?.GetValue<double>(),
                ["binning"] = f.Header["XBINNING"]?.GetValue<int>(), ["set_temp_c"] = f.Header["SET-TEMP"]?.GetValue<double>(), ["date_obs"] = f.Header["DATE-OBS"]?.GetValue<string>(),
                ["uploaded_at"] = "2026-10-13T12:00:00.000Z", ["usable"] = problem is null ? null : false, ["problem"] = problem,
            };
        }).ToArray());
        await WriteOkAsync(ctx, new JsonObject
        {
            ["rig_id"] = RigId,
            ["usage"] = new JsonObject { ["bytes_used"] = bytes, ["frames_used"] = frames, ["max_bytes"] = LibraryMaxBytes, ["max_frames"] = LibraryMaxFrames },
            ["frames"] = arr, ["masters"] = new JsonArray(), ["built_at"] = null,
            ["next_cursor"] = start + page.Count < all.Count ? (start + page.Count).ToString() : null,
        });
    }

    private async Task LibraryInitAsync(HttpContext ctx, JsonObject body)
    {
        var header = body["header"]?.AsObject() ?? new JsonObject();
        var bad = header.Select(kv => kv.Key).Where(k => !AllowedHeader.Contains(k)).ToList();
        if (bad.Count > 0)
        {
            await WriteErrorAsync(ctx, 400, "header_rejected", "Header contains keys outside the allow-list.");
            return;
        }
        if (body["stripped"]?.GetValue<bool>() != true)
        {
            await WriteErrorAsync(ctx, 400, "strip_required", "A library frame is always uploaded stripped.");
            return;
        }
        var type = LibraryTypeOf(header["IMAGETYP"]?.GetValue<string>());
        if (type == "") { await WriteErrorAsync(ctx, 400, "unsupported_frame_type", "Only bias, dark and dark flat frames go in the library."); return; }
        var camera = header["INSTRUME"]?.GetValue<string>();
        if (camera is null || !string.Equals(camera.Trim(), RigCamera.Trim(), StringComparison.OrdinalIgnoreCase))
        {
            await WriteErrorAsync(ctx, 400, "validation_failed", "The header does not name the rig's camera.");
            return;
        }
        if ((header["XBINNING"]?.GetValue<int>() ?? 1) != RigBinning) { await WriteErrorAsync(ctx, 400, "validation_failed", "Binning differs from the rig's."); return; }
        if (RigWidthPx is { } rw && RigHeightPx is { } rh && (header["NAXIS1"]?.GetValue<int>() != rw || header["NAXIS2"]?.GetValue<int>() != rh))
        {
            await WriteErrorAsync(ctx, 400, "validation_failed", "Size differs from the rig's.");
            return;
        }
        var sha = body["sha256"]!.GetValue<string>();
        var size = body["size"]!.GetValue<long>();
        var filename = body["filename"]!.GetValue<string>();

        FakeFrame? frame = null;
        var resumed = false;
        string? failCode = null;
        int failStatus = 0;
        string? quota = null;
        lock (gate)
        {
            var existing = framesById.Values.FirstOrDefault(f => f.ProjectId == "lib:" + RigId && f.Sha256 == sha && f.Status != "withdrawn");
            if (existing is { Status: "ready" }) { failCode = "duplicate"; failStatus = 409; }
            else if (existing is not null) { frame = existing; resumed = true; }
            else
            {
                var (bytes, frames) = LibraryUsageNow();
                if (bytes + size > LibraryMaxBytes) { failCode = "quota_exceeded"; failStatus = 429; quota = "library_bytes"; }
                else if (frames + 1 > LibraryMaxFrames) { failCode = "quota_exceeded"; failStatus = 429; quota = "library_frames"; }
                else
                {
                    var partCount = (int)Math.Max(1, (size + PartSize - 1) / PartSize);
                    frame = NewFrame(sha, size, filename, header, true, partCount, PartSize, "lib:" + RigId);
                }
            }
        }
        if (frame is null)
        {
            await WriteErrorAsync(ctx, failStatus, failCode!, failCode!, extra: quota is null ? null : new JsonObject { ["quota"] = quota });
            return;
        }
        JsonArray completed, urls;
        lock (gate)
        {
            var done = parts[frame.UploadId];
            completed = new JsonArray(done.OrderBy(kv => kv.Key).Select(kv => (JsonNode)new JsonObject { ["part_number"] = kv.Key, ["etag"] = kv.Value.Etag }).ToArray());
            urls = new JsonArray();
            for (var p = 1; p <= frame.PartCount; p++)
            {
                if (done.ContainsKey(p)) continue;
                var partSize = p < frame.PartCount ? frame.PartSize : frame.Size - frame.PartSize * (frame.PartCount - 1);
                urls.Add(new JsonObject { ["part_number"] = p, ["url"] = new Uri(BaseAddress, $"s3/{frame.UploadId}/{p}?sig=fake").AbsoluteUri, ["size"] = partSize });
            }
        }
        await WriteOkAsync(ctx, new JsonObject
        {
            ["frame_id"] = frame.FrameId, ["status"] = "uploading", ["resumed"] = resumed, ["frame_type"] = type, ["rig_id"] = RigId,
            ["upload"] = new JsonObject
            {
                ["type"] = "multipart", ["upload_id"] = frame.UploadId, ["part_size"] = frame.PartSize, ["part_count"] = frame.PartCount,
                ["part_urls"] = urls, ["completed_parts"] = completed, ["expires_at"] = "2026-10-13T13:00:00.000Z",
            },
        });
    }

    private async Task LibraryCompleteAsync(HttpContext ctx, string frameId, JsonObject body)
    {
        FakeFrame? frame;
        lock (gate) framesById.TryGetValue(frameId, out frame);
        if (frame is null || !frame.ProjectId.StartsWith("lib:", StringComparison.Ordinal)) { await WriteErrorAsync(ctx, 400, "not_found", "No such library frame."); return; }
        var sent = body["etags"]!.AsArray().ToDictionary(n => n!["part_number"]!.GetValue<int>(), n => n!["etag"]!.GetValue<string>());
        List<int> missing;
        var already = false;
        var sizeMismatch = false;
        var overQuota = false;
        lock (gate)
        {
            already = frame.Status == "ready";
            var have = parts[frame.UploadId];
            missing = Enumerable.Range(1, frame.PartCount).Where(p => !have.ContainsKey(p) || !sent.TryGetValue(p, out var e) || e != have[p].Etag).ToList();
            if (!already && missing.Count == 0)
            {
                var total = have.Values.Sum(v => (long)v.Data.Length);
                var (bytes, frames) = LibraryUsageNow();
                if (total != frame.Size) { sizeMismatch = true; parts[frame.UploadId] = new(); }
                else if (bytes + total > LibraryMaxBytes || frames + 1 > LibraryMaxFrames) overQuota = true;
                else
                {
                    frame.Object = have.OrderBy(kv => kv.Key).SelectMany(kv => kv.Value.Data).ToArray();
                    frame.Status = "ready";
                }
            }
        }
        if (already) { await WriteOkAsync(ctx, new JsonObject { ["frame_id"] = frameId, ["status"] = "ready", ["already_complete"] = true }); return; }
        if (missing.Count > 0) { await WriteErrorAsync(ctx, 409, "upload_incomplete", "Parts are missing.", extra: new JsonObject { ["outstanding_parts"] = new JsonArray(missing.Select(p => (JsonNode)p).ToArray()) }); return; }
        if (sizeMismatch) { await WriteErrorAsync(ctx, 409, "size_mismatch", "Size differs; start again."); return; }
        if (overQuota) { await WriteErrorAsync(ctx, 429, "quota_exceeded", "The library is full.", extra: new JsonObject { ["quota"] = "library_bytes" }); return; }
        await WriteOkAsync(ctx, new JsonObject { ["frame_id"] = frameId, ["status"] = "ready", ["already_complete"] = false });
    }

    private async Task LibraryWithdrawAsync(HttpContext ctx, string frameId)
    {
        FakeFrame? f;
        lock (gate) framesById.TryGetValue(frameId, out f);
        if (f is null || !f.ProjectId.StartsWith("lib:", StringComparison.Ordinal)) { await WriteErrorAsync(ctx, 400, "not_found", "No such library frame."); return; }
        f.Status = "withdrawn";
        await WriteOkAsync(ctx, new JsonObject { ["frame_id"] = frameId, ["status"] = "withdrawn", ["raw_delete_after"] = "2026-10-20T12:00:00.000Z" });
    }

    // ---- Envelope writers --------------------------------------------------------------------------------------

    public static async Task WriteOkAsync(HttpContext ctx, JsonObject fields)
    {
        var obj = new JsonObject { ["ok"] = true };
        foreach (var kv in fields.ToList())
        {
            fields.Remove(kv.Key);
            obj[kv.Key] = kv.Value;
        }
        ctx.Response.StatusCode = 200;
        ctx.Response.ContentType = "application/json";
        await ctx.Response.WriteAsync(obj.ToJsonString());
    }

    public static async Task WriteErrorAsync(HttpContext ctx, int status, string code, string message,
        int? retryAfterSeconds = null, JsonObject? extra = null)
    {
        var obj = new JsonObject { ["ok"] = false, ["code"] = code, ["message"] = message };
        if (retryAfterSeconds is { } ra)
        {
            obj["retry_after_s"] = ra;
            ctx.Response.Headers.RetryAfter = ra.ToString();
        }
        if (extra is not null)
            foreach (var kv in extra.ToList())
            {
                extra.Remove(kv.Key);
                obj[kv.Key] = kv.Value;
            }
        ctx.Response.StatusCode = status;
        ctx.Response.ContentType = "application/json";
        await ctx.Response.WriteAsync(obj.ToJsonString());
    }
}

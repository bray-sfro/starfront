// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using RapidStackerCollab.Core.Auth;

namespace RapidStackerCollab.Core.Api;

/// <summary>Settings for <see cref="ApiClient"/>.</summary>
public sealed class ApiClientOptions
{
    /// <summary>The API root, ending in <c>/api/</c>. Defaults to production.</summary>
    public Uri BaseUri { get; init; } = new("https://collab.astro.ekeservices.com/api/");

    /// <summary>Sent as <c>User-Agent</c>, for example <c>RapidStackerCollab-NINA/0.2.2</c>.</summary>
    public string UserAgent { get; init; } = "RapidStackerCollab-NINA/0.1";
}

/// <summary>
/// The REST client of docs/api/openapi.yaml over a caller-owned <see cref="HttpClient"/>.
/// It injects the bearer token per request (the shared client's default headers are never touched), parses the
/// <c>{ok, code, message}</c> envelope, and turns every failure into an <see cref="ApiException"/> whose
/// <see cref="ApiException.Code"/> the caller branches on. The server's <c>message</c> is surfaced, never parsed.
/// </summary>
public sealed class ApiClient : IApiClient
{
    private readonly HttpClient http;
    private readonly ITokenStore tokens;
    private readonly Uri baseUri;
    private readonly string userAgent;

    public ApiClient(HttpClient http, ITokenStore tokens, ApiClientOptions? options = null)
    {
        this.http = http;
        this.tokens = tokens;
        options ??= new ApiClientOptions();
        var b = options.BaseUri.AbsoluteUri;
        baseUri = new Uri(b.EndsWith('/') ? b : b + "/");
        userAgent = options.UserAgent;
    }

    // ---- Device sign-in ----------------------------------------------------------------------------------------

    public Task<DeviceStartResponse> DeviceStartAsync(DeviceStartRequest request, CancellationToken ct = default) =>
        SendAsync<DeviceStartResponse>(HttpMethod.Post, "device/start", request, Auth.None, ct);

    public Task<DevicePollResponse> DevicePollAsync(string deviceCode, CancellationToken ct = default) =>
        SendAsync<DevicePollResponse>(HttpMethod.Post, "device/poll", new DevicePollRequest { DeviceCode = deviceCode }, Auth.None, ct);

    // ---- Rigs --------------------------------------------------------------------------------------------------

    public async Task<IReadOnlyList<Rig>> ListRigsAsync(CancellationToken ct = default) =>
        (await SendAsync<RigListResponse>(HttpMethod.Get, "rigs", null, Auth.Required, ct).ConfigureAwait(false)).Rigs;

    public async Task<Rig> UpsertRigAsync(RigUpsertRequest request, CancellationToken ct = default) =>
        (await SendAsync<RigResponse>(HttpMethod.Post, "rigs", request, Auth.Required, ct).ConfigureAwait(false)).Rig;

    public async Task<Rig> PatchRigAsync(string rigId, RigPatchRequest request, CancellationToken ct = default) =>
        (await SendAsync<RigResponse>(HttpMethod.Patch, $"rigs/{Esc(rigId)}", request, Auth.Required, ct).ConfigureAwait(false)).Rig;

    // ---- Filters -----------------------------------------------------------------------------------------------

    public Task<NormalizeResponse> NormalizeFiltersAsync(IEnumerable<string> names, string? project = null, bool bayer = false, CancellationToken ct = default)
    {
        var list = names.ToList();
        var q = new List<(string, string?)> { ("names", string.Join(",", list.Select(n => n.Replace(",", " ")))) };
        if (project is not null) q.Add(("project", project));
        if (bayer) q.Add(("bayer", "1"));
        // Optional authentication: the member-specific resolution only applies when a project is named.
        return SendAsync<NormalizeResponse>(HttpMethod.Get, "filters/normalize" + Query(q), null,
            project is null ? Auth.None : Auth.Required, ct);
    }

    // ---- Projects ----------------------------------------------------------------------------------------------

    public Task<ProjectListResponse> ListProjectsAsync(string scope = "mine", int? limit = null, string? cursor = null, CancellationToken ct = default) =>
        SendAsync<ProjectListResponse>(HttpMethod.Get,
            "projects" + Query(("scope", scope), ("limit", limit?.ToString(CultureInfo.InvariantCulture)), ("cursor", cursor)),
            null, Auth.Required, ct);

    public Task<ResolveJoinCodeResponse> ResolveJoinCodeAsync(string code, CancellationToken ct = default) =>
        SendAsync<ResolveJoinCodeResponse>(HttpMethod.Post, "projects/resolve-join-code", new ResolveJoinCodeRequest { Code = code }, Auth.Required, ct);

    public async Task<ProjectDetail> GetProjectAsync(string project, CancellationToken ct = default) =>
        (await SendAsync<ProjectResponse>(HttpMethod.Get, $"projects/{Esc(project)}", null, Auth.Required, ct).ConfigureAwait(false)).Project;

    public async Task<Membership> JoinProjectAsync(string project, JoinRequest request, CancellationToken ct = default) =>
        (await SendAsync<JoinResponse>(HttpMethod.Post, $"projects/{Esc(project)}/join", request, Auth.Required, ct).ConfigureAwait(false)).Membership;

    public async Task<Membership> GetMembershipAsync(string project, CancellationToken ct = default) =>
        (await SendAsync<MembershipResponse>(HttpMethod.Get, $"projects/{Esc(project)}/membership", null, Auth.Required, ct).ConfigureAwait(false)).Membership;

    public async Task<Membership> UpdateMembershipAsync(string project, UpdateMembershipRequest request, CancellationToken ct = default) =>
        (await SendAsync<MembershipResponse>(HttpMethod.Put, $"projects/{Esc(project)}/membership", request, Auth.Required, ct).ConfigureAwait(false)).Membership;

    public async Task<ProjectStatusDoc> GetProjectStatusAsync(string project, CancellationToken ct = default) =>
        (await SendAsync<ProjectStatusResponse>(HttpMethod.Get, $"projects/{Esc(project)}/status", null, Auth.Required, ct).ConfigureAwait(false)).StatusDoc;

    // ---- Mosaic (contract 1.17.0) ------------------------------------------------------------------------------

    public async Task<MosaicState?> GetMosaicAsync(string project, CancellationToken ct = default) =>
        (await SendAsync<MosaicResponse>(HttpMethod.Get, $"projects/{Esc(project)}/mosaic", null, Auth.Required, ct).ConfigureAwait(false)).Mosaic;

    public async Task<MosaicState?> PutClaimsAsync(string project, IReadOnlyList<string> panels, CancellationToken ct = default) =>
        (await SendAsync<MosaicResponse>(HttpMethod.Put, $"projects/{Esc(project)}/mosaic/claims", new ClaimsRequest { Panels = panels }, Auth.Required, ct).ConfigureAwait(false)).Mosaic;

    public async Task<PanelsDocument> GetPanelsDocumentAsync(string url, CancellationToken ct = default)
    {
        // The status document's urls are absolute (public) or presigned; a relative one is resolved against the site root.
        var target = Uri.TryCreate(url, UriKind.Absolute, out var abs) ? abs : new Uri(new Uri(baseUri.GetLeftPart(UriPartial.Authority)), url);
        using var request = new HttpRequestMessage(HttpMethod.Get, target);
        request.Headers.UserAgent.ParseAdd(userAgent);
        string text;
        int status;
        try
        {
            using var response = await http.SendAsync(request, HttpCompletionOption.ResponseContentRead, ct).ConfigureAwait(false);
            status = (int)response.StatusCode;
            text = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
                throw new ApiException(ApiCodes.BadResponse, $"panels.json answered HTTP {status}.", status);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or IOException)
        {
            throw new ApiException(ApiCodes.NetworkError, ex.Message, 0, ex);
        }
        try
        {
            return ApiJson.Deserialize<PanelsDocument>(text) ?? throw new ApiException(ApiCodes.BadResponse, "Empty panels.json.", status);
        }
        catch (JsonException ex)
        {
            throw new ApiException(ApiCodes.BadResponse, "panels.json does not match the contract: " + ex.Message, status, ex);
        }
    }

    // ---- Frames ------------------------------------------------------------------------------------------------

    public Task<FrameInitResponse> InitFrameAsync(string project, FrameInitRequest request, CancellationToken ct = default) =>
        SendAsync<FrameInitResponse>(HttpMethod.Post, $"projects/{Esc(project)}/frames/init", request, Auth.Required, ct);

    public Task<FrameCompleteResponse> CompleteFrameAsync(string project, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct = default) =>
        SendAsync<FrameCompleteResponse>(HttpMethod.Post, $"projects/{Esc(project)}/frames/{Esc(frameId)}/complete",
            new FrameCompleteRequest { Etags = etags }, Auth.Required, ct);

    public Task<FrameListResponse> ListFramesAsync(string project, FrameListQuery? query = null, CancellationToken ct = default)
    {
        query ??= new FrameListQuery();
        var q = Query(
            ("mine", query.Mine ? "1" : "0"),
            ("status", query.Status),
            ("filter", query.Filter),
            ("since", query.Since?.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture)),
            ("limit", query.Limit?.ToString(CultureInfo.InvariantCulture)),
            ("cursor", query.Cursor));
        return SendAsync<FrameListResponse>(HttpMethod.Get, $"projects/{Esc(project)}/frames{q}", null, Auth.Required, ct);
    }

    public async Task<Frame> GetFrameAsync(string project, string frameId, CancellationToken ct = default) =>
        (await SendAsync<FrameResponse>(HttpMethod.Get, $"projects/{Esc(project)}/frames/{Esc(frameId)}", null, Auth.Required, ct).ConfigureAwait(false)).Frame;

    public Task<WithdrawResponse> WithdrawFrameAsync(string project, string frameId, CancellationToken ct = default) =>
        SendAsync<WithdrawResponse>(HttpMethod.Post, $"projects/{Esc(project)}/frames/{Esc(frameId)}/withdraw", null, Auth.Required, ct);

    // ---- Calibration library (contract 1.23.0) -----------------------------------------------------------------

    public Task<LibraryResponse> GetLibraryAsync(string rigId, int? limit = null, string? cursor = null, CancellationToken ct = default) =>
        SendAsync<LibraryResponse>(HttpMethod.Get,
            $"rigs/{Esc(rigId)}/library" + Query(("limit", limit?.ToString(CultureInfo.InvariantCulture)), ("cursor", cursor)),
            null, Auth.Required, ct);

    public Task<LibraryFrameInitResponse> InitLibraryFrameAsync(string rigId, LibraryFrameInitRequest request, CancellationToken ct = default) =>
        SendAsync<LibraryFrameInitResponse>(HttpMethod.Post, $"rigs/{Esc(rigId)}/library/frames/init", request, Auth.Required, ct);

    public Task<LibraryFrameCompleteResponse> CompleteLibraryFrameAsync(string rigId, string frameId, IReadOnlyList<CompletedPart> etags, CancellationToken ct = default) =>
        SendAsync<LibraryFrameCompleteResponse>(HttpMethod.Post, $"rigs/{Esc(rigId)}/library/frames/{Esc(frameId)}/complete",
            new FrameCompleteRequest { Etags = etags }, Auth.Required, ct);

    public Task<WithdrawResponse> WithdrawLibraryFrameAsync(string rigId, string frameId, CancellationToken ct = default) =>
        SendAsync<WithdrawResponse>(HttpMethod.Post, $"rigs/{Esc(rigId)}/library/frames/{Esc(frameId)}/withdraw", null, Auth.Required, ct);

    public Task RevokeOwnTokenAsync(CancellationToken ct = default) =>
        SendAsync<AckResponse>(HttpMethod.Delete, "auth/token", null, Auth.Required, ct);

    // ---- Plumbing ----------------------------------------------------------------------------------------------

    private enum Auth { None, Required }

    private static string Esc(string segment) => Uri.EscapeDataString(segment);

    private static string Query(params (string Key, string? Value)[] pairs) => Query((IEnumerable<(string, string?)>)pairs);

    private static string Query(IEnumerable<(string Key, string? Value)> pairs)
    {
        var sb = new StringBuilder();
        foreach (var (k, v) in pairs)
        {
            if (v is null) continue;
            sb.Append(sb.Length == 0 ? '?' : '&').Append(Uri.EscapeDataString(k)).Append('=').Append(Uri.EscapeDataString(v));
        }
        return sb.ToString();
    }

    private async Task<T> SendAsync<T>(HttpMethod method, string relative, object? body, Auth auth, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(method, new Uri(baseUri, relative));
        request.Headers.UserAgent.ParseAdd(userAgent);
        request.Headers.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        if (auth == Auth.Required)
        {
            var token = tokens.Load();
            if (string.IsNullOrEmpty(token))
                throw new ApiException(ApiCodes.NotAuthenticated, "Not signed in.", 0);
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        }
        if (body is not null)
            request.Content = new StringContent(ApiJson.Serialize(body), Encoding.UTF8, "application/json");

        HttpResponseMessage response;
        string text;
        try
        {
            response = await http.SendAsync(request, HttpCompletionOption.ResponseContentRead, ct).ConfigureAwait(false);
            text = await response.Content.ReadAsStringAsync(ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or IOException)
        {
            // A timeout surfaces as TaskCanceledException without the caller's token being cancelled.
            throw new ApiException(ApiCodes.NetworkError, ex.Message, 0, ex);
        }

        using (response)
        {
            var status = (int)response.StatusCode;
            var retryAfter = RetryAfterOf(response);
            JsonDocument? doc = null;
            try
            {
                try
                {
                    doc = JsonDocument.Parse(text);
                }
                catch (JsonException ex)
                {
                    throw new ApiException(ApiCodes.BadResponse,
                        $"The server answered HTTP {status} with something that is not JSON.", status, ex) { RetryAfter = retryAfter };
                }
                if (doc.RootElement.ValueKind != JsonValueKind.Object)
                    throw new ApiException(ApiCodes.BadResponse, $"HTTP {status}: the response is not a JSON object.", status) { RetryAfter = retryAfter };

                var root = doc.RootElement;
                var okFlag = root.TryGetProperty("ok", out var okEl) && okEl.ValueKind is JsonValueKind.True or JsonValueKind.False
                    ? okEl.GetBoolean()
                    : (bool?)null;
                var success = response.IsSuccessStatusCode && okFlag != false;
                if (!success)
                    throw ToException(root, status, retryAfter);

                try
                {
                    return ApiJson.Deserialize<T>(text) ?? throw new ApiException(ApiCodes.BadResponse, "Empty response body.", status);
                }
                catch (JsonException ex)
                {
                    throw new ApiException(ApiCodes.BadResponse, "The response does not match the contract: " + ex.Message, status, ex);
                }
            }
            finally
            {
                doc?.Dispose();
            }
        }
    }

    private static TimeSpan? RetryAfterOf(HttpResponseMessage response)
    {
        var ra = response.Headers.RetryAfter;
        if (ra is null) return null;
        if (ra.Delta is { } delta) return delta < TimeSpan.Zero ? TimeSpan.Zero : delta;
        if (ra.Date is { } date)
        {
            var d = date - DateTimeOffset.UtcNow;
            return d < TimeSpan.Zero ? TimeSpan.Zero : d;
        }
        return null;
    }

    private static ApiException ToException(JsonElement root, int status, TimeSpan? headerRetryAfter)
    {
        string? Str(string name) => root.TryGetProperty(name, out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;

        var code = Str("code");
        var message = Str("message") ?? $"HTTP {status}";
        if (string.IsNullOrEmpty(code))
            return new ApiException(ApiCodes.BadResponse, $"HTTP {status} without an error code: {message}", status) { RetryAfter = headerRetryAfter };

        var retryAfter = headerRetryAfter;
        if (retryAfter is null && root.TryGetProperty("retry_after_s", out var ras) && ras.TryGetDouble(out var secs))
            retryAfter = TimeSpan.FromSeconds(Math.Max(0, secs));

        var errors = new List<FieldError>();
        if (root.TryGetProperty("errors", out var errs) && errs.ValueKind == JsonValueKind.Array)
            foreach (var e in errs.EnumerateArray())
                errors.Add(new FieldError(
                    e.TryGetProperty("field", out var f) ? f.GetString() ?? "" : "",
                    e.TryGetProperty("message", out var m) ? m.GetString() ?? "" : ""));

        var outstanding = new List<int>();
        if (root.TryGetProperty("outstanding_parts", out var op) && op.ValueKind == JsonValueKind.Array)
            foreach (var n in op.EnumerateArray())
                if (n.TryGetInt32(out var i)) outstanding.Add(i);

        FrameSummary? existing = null;
        if (root.TryGetProperty("existing_frame", out var ef) && ef.ValueKind == JsonValueKind.Object)
            existing = ApiJson.Deserialize<FrameSummary>(ef.GetRawText());

        return new ApiException(code, message, status)
        {
            RetryAfter = retryAfter,
            Errors = errors,
            Quota = Str("quota"),
            RequiredScope = Str("required_scope"),
            Reason = Str("reason"),
            FrameStatus = Str("status"),
            TermsVersion = Str("terms_version"),
            OutstandingParts = outstanding,
            ExistingFrame = existing,
            NeedsVerification = root.TryGetProperty("needs_verification", out var nv) && nv.ValueKind == JsonValueKind.True,
        };
    }
}

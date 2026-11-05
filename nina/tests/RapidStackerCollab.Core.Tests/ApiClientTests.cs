// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Net;
using System.Net.Sockets;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Tests.Fakes;

namespace RapidStackerCollab.Core.Tests;

public class ApiClientTests
{
    private FakeCollabServer server = null!;
    private ApiClient api = null!;

    [SetUp]
    public async Task SetUp()
    {
        server = await FakeCollabServer.StartAsync();
        api = TestApi.Client(server);
    }

    [TearDown]
    public async Task TearDown() => await server.DisposeAsync();

    private static string Sha(char c) => new(c, 64);

    // ---- routes ------------------------------------------------------------------------------------------------

    [Test]
    public async Task Device_start_and_poll_do_not_send_a_bearer_token()
    {
        var start = await api.DeviceStartAsync(new DeviceStartRequest { ClientName = "N.I.N.A. plugin on TEST", Scopes = ApiScopes.PluginDefaults });
        Assert.That(start.UserCode, Is.EqualTo("K7QM-3XWD"));
        Assert.That(start.Interval, Is.EqualTo(5));
        Assert.That(start.ExpiresIn, Is.EqualTo(600));
        var pending = await api.DevicePollAsync(start.DeviceCode);
        Assert.That(pending.Status, Is.EqualTo("pending"));
        server.ApproveDevice();
        var approved = await api.DevicePollAsync(start.DeviceCode);
        Assert.That(approved.Status, Is.EqualTo("approved"));
        Assert.That(approved.Token, Is.EqualTo(FakeCollabServer.Token));
        Assert.That(approved.User!.Email, Is.EqualTo("ed@example.com"));
        Assert.That(server.Requests.Where(r => r.Path.StartsWith("/api/device")).Select(r => r.Authorization), Is.All.Empty);
        var body = JsonNode.Parse(server.RequestsTo("POST", "device/start").Single().Body!)!;
        Assert.That(body["client_name"]!.GetValue<string>(), Is.EqualTo("N.I.N.A. plugin on TEST"));
        Assert.That(body["scopes"]!.AsArray().Select(n => n!.GetValue<string>()), Does.Not.Contain("projects:manage"));
    }

    [Test]
    public async Task Rigs_projects_and_membership_round_trip()
    {
        var rig = await api.UpsertRigAsync(new RigUpsertRequest { Camera = "ASI2600MM", Telescope = "RedCat 51", FocalLengthMm = 250, PixelSizeUm = 3.76 });
        Assert.That(rig.RigId, Is.EqualTo("rig_9c1d4e7a02bb"));
        Assert.That((await api.ListRigsAsync()).Single().Camera, Is.EqualTo("ASI2600MM"));
        Assert.That((await api.PatchRigAsync(rig.RigId, new RigPatchRequest { Confirmed = true })).Confirmed, Is.True);

        var projects = await api.ListProjectsAsync("mine");
        Assert.That(projects.Projects.Single().Slug, Is.EqualTo("horsehead-sho"));
        Assert.That(server.RequestsTo("GET", "/api/projects").Single().Query, Is.EqualTo("?scope=mine"));

        var resolved = await api.ResolveJoinCodeAsync("h4rt-92bk-wnpc");
        Assert.That(resolved.Grid!.ScaleArcsecPx, Is.EqualTo(1.5));
        Assert.That(resolved.TermsVersion, Is.EqualTo(server.TermsVersion));
        Assert.That(resolved.AlreadyMember, Is.False);

        var detail = await api.GetProjectAsync(FakeCollabServer.Slug);
        Assert.That(detail.ProjectId, Is.EqualTo(FakeCollabServer.ProjectId));
        Assert.That(detail.Filters, Is.EqualTo(new[] { "HA", "OIII", "SII" }));
        Assert.That(detail.MyMembership!.Status, Is.EqualTo("active"));

        var joined = await api.JoinProjectAsync(detail.ProjectId, new JoinRequest
        {
            Code = "H4RT-92BK-WNPC", DisplayName = "Ed", AcceptedTermsVersion = server.TermsVersion, AllowOwnerRawDownload = true,
            FilterMapping = new[] { new ContributorMappingEntry { Raw = "Ha 3nm", Canonical = "HA" }, new ContributorMappingEntry { Raw = "Dark", Canonical = MappingValues.Skip } },
        });
        Assert.That(joined.AllowOwnerRawDownload, Is.True);
        Assert.That(joined.FilterMapping.Select(m => (m.Raw, m.Canonical)), Is.EqualTo(new[] { ("Ha 3nm", "HA"), ("Dark", "SKIP") }));
        var joinBody = JsonNode.Parse(server.RequestsTo("POST", "/join").Single().Body!)!;
        Assert.That(joinBody["allow_owner_raw_download"]!.GetValue<bool>(), Is.True);
        Assert.That(joinBody["accepted_terms_version"]!.GetValue<string>(), Is.EqualTo(server.TermsVersion));
        Assert.That(joinBody.AsObject().ContainsKey("country"), Is.False);

        Assert.That((await api.GetMembershipAsync(detail.ProjectId)).DisplayName, Is.EqualTo("Ed"));
        var updated = await api.UpdateMembershipAsync(detail.ProjectId, new UpdateMembershipRequest { AllowOwnerRawDownload = false });
        Assert.That(updated.AllowOwnerRawDownload, Is.False);

        var status = await api.GetProjectStatusAsync(detail.ProjectId);
        Assert.That(status.Totals.FramesAccepted, Is.EqualTo(120));
        Assert.That(status.Totals.IntegrationS, Is.EqualTo(36000.5));
        Assert.That(status.Combined.Palette, Is.EqualTo("SHO"));
    }

    [Test]
    public async Task Terms_outdated_carries_the_current_version()
    {
        var ex = Assert.ThrowsAsync<ApiException>(() => api.JoinProjectAsync(FakeCollabServer.ProjectId, new JoinRequest
            { Code = "H4RT-92BK-WNPC", DisplayName = "Ed", AcceptedTermsVersion = "1999-01-01" }))!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.TermsOutdated));
        Assert.That(ex.TermsVersion, Is.EqualTo(server.TermsVersion));
        await Task.CompletedTask;
    }

    [Test]
    public async Task Filter_normalisation_url_encodes_names_and_sends_the_token_only_when_project_scoped()
    {
        var r = await api.NormalizeFiltersAsync(new[] { "Ha 3nm", "H-α" });
        Assert.That(r.Results.Select(x => x.Raw), Is.EqualTo(new[] { "Ha 3nm", "H-α" }));
        Assert.That(r.AliasesVersion, Is.EqualTo("fake-1"));
        var anon = server.RequestsTo("GET", "filters/normalize").Single();
        Assert.That(anon.Query, Does.Contain("names=Ha%203nm%2CH-%CE%B1"));
        Assert.That(anon.Authorization, Is.Empty);
        await api.NormalizeFiltersAsync(new[] { "Ha" }, project: FakeCollabServer.Slug, bayer: true);
        var scoped = server.RequestsTo("GET", "filters/normalize").Last();
        Assert.That(scoped.Query, Does.Contain("project=horsehead-sho").And.Contain("bayer=1"));
        Assert.That(scoped.Authorization, Is.EqualTo("Bearer " + FakeCollabServer.Token));
    }

    [Test]
    public async Task Frame_routes_init_complete_list_get_and_withdraw()
    {
        var init = await api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('a'), 10));
        Assert.That(init.FrameId, Does.Match("^frm_[0-9a-hjkmnp-tv-z]{26}$"));
        Assert.That(init.Upload.PartUrls, Has.Count.EqualTo(1));
        Assert.That(init.Resumed, Is.False);

        using var put = new HttpClient();
        var resp = await put.PutAsync(init.Upload.PartUrls[0].Url, new ByteArrayContent(new byte[10]));
        var etag = resp.Headers.GetValues("ETag").Single();
        var done = await api.CompleteFrameAsync(FakeCollabServer.ProjectId, init.FrameId, new[] { new CompletedPart { PartNumber = 1, Etag = etag } });
        Assert.That(done.Status, Is.EqualTo("uploaded"));

        var again = await api.CompleteFrameAsync(FakeCollabServer.ProjectId, init.FrameId, new[] { new CompletedPart { PartNumber = 1, Etag = etag } });
        Assert.That(again.AlreadyComplete, Is.True);

        var list = await api.ListFramesAsync(FakeCollabServer.ProjectId, new FrameListQuery { Mine = true, Status = "accepted", Limit = 20 });
        Assert.That(list.Frames.Single().FrameId, Is.EqualTo(init.FrameId));
        Assert.That(server.RequestsTo("GET", "/frames").Single().Query, Is.EqualTo("?mine=1&status=accepted&limit=20"));
        Assert.That((await api.GetFrameAsync(FakeCollabServer.ProjectId, init.FrameId)).Sha256, Is.EqualTo(Sha('a')));
        var w = await api.WithdrawFrameAsync(FakeCollabServer.ProjectId, init.FrameId);
        Assert.That(w.Status, Is.EqualTo("withdrawn"));
    }

    // ---- auth --------------------------------------------------------------------------------------------------

    [Test]
    public void Without_a_stored_token_a_protected_call_fails_locally_and_sends_nothing()
    {
        var anon = TestApi.Client(server, new InMemoryTokenStore());
        var ex = Assert.ThrowsAsync<ApiException>(() => anon.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.NotAuthenticated));
        Assert.That(ex.IsAuthenticationFailure, Is.True);
        Assert.That(server.Requests, Is.Empty);
    }

    [Test]
    public void A_rejected_token_is_an_authentication_failure_and_is_not_cleared_by_the_client()
    {
        var store = new InMemoryTokenStore("rscu_" + new string('b', 64));
        var bad = TestApi.Client(server, store);
        var ex = Assert.ThrowsAsync<ApiException>(() => bad.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.TokenInvalid));
        Assert.That(ex.IsAuthenticationFailure, Is.True);
        Assert.That(ex.HttpStatus, Is.EqualTo(401));
        Assert.That(store.Load(), Is.Not.Null, "deciding to sign out is the caller's job");
    }

    // ---- error mapping -----------------------------------------------------------------------------------------

    [Test]
    public void Retry_after_header_wins_over_the_body_and_branching_is_on_code_not_message()
    {
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 429, "rate_limited", "Slow down please", retryAfterSeconds: 17);
        var ex = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('a'), 1)))!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.RateLimited));
        Assert.That(ex.RetryAfter, Is.EqualTo(TimeSpan.FromSeconds(17)));
        Assert.That(ex.IsTransient, Is.True);

        // Same code with a different, misleading message: still rate_limited.
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 429, "rate_limited", "quota exceeded (not really)", retryAfterSeconds: 3);
        var ex2 = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('b'), 1)))!;
        Assert.That(ex2.Code, Is.EqualTo(ApiCodes.RateLimited));
    }

    [Test]
    public void Retry_after_falls_back_to_the_body_field_and_to_http_dates()
    {
        server.Intercept(FakeCollabServer.Route("POST", "frames/init"), async ctx =>
        {
            ctx.Response.StatusCode = 503;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync("""{"ok":false,"code":"ingest_paused","message":"paused","retry_after_s":120}""");
        });
        var a = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('a'), 1)))!;
        Assert.That(a.Code, Is.EqualTo(ApiCodes.IngestPaused));
        Assert.That(a.RetryAfter, Is.EqualTo(TimeSpan.FromSeconds(120)));
        Assert.That(a.HttpStatus, Is.EqualTo(503));

        server.Intercept(FakeCollabServer.Route("POST", "frames/init"), async ctx =>
        {
            ctx.Response.StatusCode = 503;
            ctx.Response.Headers.RetryAfter = DateTimeOffset.UtcNow.AddSeconds(90).ToString("R");
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync("""{"ok":false,"code":"ingest_paused","message":"paused"}""");
        });
        var b = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('b'), 1)))!;
        Assert.That(b.RetryAfter!.Value.TotalSeconds, Is.InRange(80, 91));
    }

    [Test]
    public void Quota_duplicate_and_incomplete_carry_their_extras()
    {
        server.QuotaBytesRemaining = 5;
        var q = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('a'), 100)))!;
        Assert.That(q.Code, Is.EqualTo(ApiCodes.QuotaExceeded));
        Assert.That(q.Quota, Is.EqualTo(QuotaNames.ContributorBytes));
        Assert.That(q.IsTransient, Is.False);

        server.SeedFrame(Sha('c'), "accepted");
        var d = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('c'), 1)))!;
        Assert.That(d.Code, Is.EqualTo(ApiCodes.Duplicate));
        Assert.That(d.ExistingFrame!.Status, Is.EqualTo("accepted"));

        server.QuotaBytesRemaining = long.MaxValue;
        var init = api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('e'), 70_000)).GetAwaiter().GetResult();
        var inc = Assert.ThrowsAsync<ApiException>(() => api.CompleteFrameAsync(FakeCollabServer.ProjectId, init.FrameId,
            new[] { new CompletedPart { PartNumber = 1, Etag = "x" } }))!;
        Assert.That(inc.Code, Is.EqualTo(ApiCodes.UploadIncomplete));
        Assert.That(inc.OutstandingParts, Is.EqualTo(new[] { 1, 2 }));
    }

    [Test]
    public void A_header_outside_the_allow_list_is_rejected_with_field_errors()
    {
        // Bypass the typed InitHeader on purpose: this is what a buggy client would send.
        using var http = new HttpClient();
        var req = new HttpRequestMessage(HttpMethod.Post, new Uri(server.ApiBase, $"projects/{FakeCollabServer.ProjectId}/frames/init"))
        {
            Content = new StringContent("""{"sha256":"%s","size":1,"filename":"x.fits","header":{"FILTER":"Ha","SITELAT":"41.2"}}""".Replace("%s", Sha('a')),
                System.Text.Encoding.UTF8, "application/json"),
        };
        req.Headers.Authorization = new("Bearer", FakeCollabServer.Token);
        var resp = http.Send(req);
        Assert.That((int)resp.StatusCode, Is.EqualTo(400));
        Assert.That(resp.Content.ReadAsStringAsync().Result, Does.Contain("header_rejected").And.Contain("header.SITELAT"));
    }

    [Test]
    public void Header_rejected_is_parsed_into_field_errors()
    {
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 400, "header_rejected", "no",
            extra: new JsonObject { ["errors"] = new JsonArray(new JsonObject { ["field"] = "header.SITELAT", ["message"] = "not allowed" }) });
        var ex = Assert.ThrowsAsync<ApiException>(() => api.InitFrameAsync(FakeCollabServer.ProjectId, TestApi.InitRequest(Sha('a'), 1)))!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.HeaderRejected));
        Assert.That(ex.Errors, Is.EqualTo(new[] { new FieldError("header.SITELAT", "not allowed") }));
    }

    [Test]
    public async Task Device_denied_and_expired_arrive_as_http_200_with_ok_false()
    {
        server.DenyDevice();
        var ex = Assert.ThrowsAsync<ApiException>(() => api.DevicePollAsync(FakeCollabServer.DeviceCode))!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.DeviceDenied));
        Assert.That(ex.HttpStatus, Is.EqualTo(200));
        await Task.CompletedTask;
    }

    [Test]
    public void An_unknown_code_is_kept_verbatim_for_the_caller_to_treat_as_generic()
    {
        server.InterceptError(FakeCollabServer.Route("GET", "/rigs"), 409, "brand_new_code_2027");
        var ex = Assert.ThrowsAsync<ApiException>(() => api.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo("brand_new_code_2027"));
        Assert.That(ApiCodes.Contract, Does.Not.Contain(ex.Code));
    }

    [Test]
    public void A_cdn_html_error_page_is_a_transient_bad_response()
    {
        server.Intercept(FakeCollabServer.Route("GET", "/rigs"), async ctx =>
        {
            ctx.Response.StatusCode = 502;
            ctx.Response.ContentType = "text/html";
            await ctx.Response.WriteAsync("<html><body>502 Bad Gateway</body></html>");
        });
        var ex = Assert.ThrowsAsync<ApiException>(() => api.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.BadResponse));
        Assert.That(ex.HttpStatus, Is.EqualTo(502));
        Assert.That(ex.IsTransient, Is.True);
    }

    [Test]
    public void A_json_error_without_a_code_is_a_bad_response_not_a_crash()
    {
        server.Intercept(FakeCollabServer.Route("GET", "/rigs"), async ctx =>
        {
            ctx.Response.StatusCode = 500;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync("""{"message":"Internal Server Error"}""");
        });
        var ex = Assert.ThrowsAsync<ApiException>(() => api.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.BadResponse));
        Assert.That(ex.IsTransient, Is.True);
    }

    [Test]
    public void A_refused_connection_is_a_network_error()
    {
        var port = FreePort();
        var offline = new ApiClient(new HttpClient(), new InMemoryTokenStore("rscu_" + new string('a', 64)),
            new ApiClientOptions { BaseUri = new Uri($"http://127.0.0.1:{port}/api/") });
        var ex = Assert.ThrowsAsync<ApiException>(() => offline.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.NetworkError));
        Assert.That(ex.HttpStatus, Is.Zero);
        Assert.That(ex.IsTransient, Is.True);
    }

    [Test]
    public void A_timeout_is_a_network_error_but_a_caller_cancel_is_a_cancel()
    {
        server.Intercept(FakeCollabServer.Route("GET", "/rigs"), async ctx => await Task.Delay(2000, ctx.RequestAborted), times: 2);
        var impatient = TestApi.Client(server, http: new HttpClient { Timeout = TimeSpan.FromMilliseconds(150) });
        var ex = Assert.ThrowsAsync<ApiException>(() => impatient.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.NetworkError));

        using var cts = new CancellationTokenSource(100);
        Assert.CatchAsync<OperationCanceledException>(() => api.ListRigsAsync(cts.Token));
    }

    [Test]
    public void Unexpected_field_types_in_a_success_body_are_a_bad_response()
    {
        server.Intercept(FakeCollabServer.Route("GET", "/rigs"), async ctx =>
        {
            ctx.Response.StatusCode = 200;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync("""{"ok":true,"rigs":"not a list"}""");
        });
        var ex = Assert.ThrowsAsync<ApiException>(() => api.ListRigsAsync())!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.BadResponse));
    }

    [Test]
    public async Task The_user_agent_names_the_plugin()
    {
        await api.ListRigsAsync();
        Assert.That(server.Requests.Single().UserAgent, Does.StartWith("RapidStackerCollab-NINA/"));
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}

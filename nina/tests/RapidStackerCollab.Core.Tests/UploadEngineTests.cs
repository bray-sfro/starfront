// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using Microsoft.AspNetCore.Http;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Upload;
using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Tests;

public class UploadEngineTests
{
    private const long Part = 64 * 1024;

    private FakeCollabServer server = null!;
    private FakeClock clock = null!;
    private string dir = null!;
    private HttpClient http = null!;

    [SetUp]
    public async Task SetUp()
    {
        server = await FakeCollabServer.StartAsync();
        server.PartSize = Part;
        clock = new FakeClock();
        dir = Directory.CreateTempSubdirectory("rsc-up-").FullName;
        http = new HttpClient();
    }

    [TearDown]
    public async Task TearDown()
    {
        http.Dispose();
        await server.DisposeAsync();
        TempDirs.Delete(dir);
    }

    private UploadEngine Engine(UploadEngineOptions? o = null, BandwidthLimiter? limiter = null, IClock? c = null) =>
        new(TestApi.Client(server, http: http), http, c ?? clock, limiter, o);

    /// <summary>A file of <paramref name="length"/> deterministic bytes: 300,000 is four full 64 KiB parts and a 37,856-byte tail.</summary>
    private (string Path, byte[] Bytes) File(int length = 300_000, string name = "light.fits")
    {
        var bytes = FitsBuilder.Pattern(length, length);
        var path = Path.Combine(dir, name);
        System.IO.File.WriteAllBytes(path, bytes);
        return (path, bytes);
    }

    private static UploadRequest Req(string path, string? sha = null, bool stripped = false) => new(
        FakeCollabServer.ProjectId, path, Path.GetFileName(path),
        new InitHeader { Filter = "Ha", ImageTyp = "LIGHT", ExpTime = 300, XBinning = 1 }, stripped, "rig_9c1d4e7a02bb", sha);

    private static string Sha(byte[] b) => Convert.ToHexString(SHA256.HashData(b)).ToLowerInvariant();

    // ---- happy path ------------------------------------------------------------------------------------------

    [Test]
    public async Task Happy_path_sends_every_part_once_and_the_server_reassembles_the_exact_bytes()
    {
        var (path, bytes) = File();
        var events = new List<UploadProgress>();
        var result = await Engine().UploadAsync(Req(path, stripped: true), new Progress<UploadProgress>(events.Add));
        await Task.Delay(50); // Progress<T> posts to the thread pool

        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(result.Sha256, Is.EqualTo(Sha(bytes)));
        Assert.That(result.PartsSent, Is.EqualTo(5));
        Assert.That(result.Resumed, Is.False);
        Assert.That(Enumerable.Range(1, 5).Select(server.PartPuts), Is.All.EqualTo(1));
        var frame = server.Frames.Single();
        Assert.That(frame.Status, Is.EqualTo("uploaded"));
        Assert.That(frame.Object, Is.EqualTo(bytes));

        var init = JsonNode.Parse(server.RequestsTo("POST", "frames/init").Single().Body!)!;
        Assert.That(init["sha256"]!.GetValue<string>(), Is.EqualTo(Sha(bytes)));
        Assert.That(init["size"]!.GetValue<long>(), Is.EqualTo(300_000));
        Assert.That(init["filename"]!.GetValue<string>(), Is.EqualTo("light.fits"));
        Assert.That(init["stripped"]!.GetValue<bool>(), Is.True);
        Assert.That(init["header"]!["FILTER"]!.GetValue<string>(), Is.EqualTo("Ha"));
        Assert.That(init["rig_hint"]!["rig_id"]!.GetValue<string>(), Is.EqualTo("rig_9c1d4e7a02bb"));

        Assert.That(events.Select(e => e.Stage), Does.Contain(UploadStage.Hashing).And.Contain(UploadStage.Uploading).And.Contain(UploadStage.Done));
        Assert.That(events.Max(e => e.BytesSent), Is.EqualTo(300_000));
        Assert.That(events.Where(e => e.FrameId is not null).Select(e => e.FrameId).Distinct().Single(), Is.EqualTo(frame.FrameId));
    }

    [Test]
    public async Task The_part_puts_carry_no_credentials_and_the_parts_add_up_to_the_file()
    {
        var (path, bytes) = File();
        await Engine().UploadAsync(Req(path));
        var puts = server.RequestsTo("PUT", "/s3/");
        Assert.That(puts, Has.Count.EqualTo(5));
        Assert.That(puts.Select(r => r.Authorization), Is.All.Empty, "the presigned URL is the credential; the bearer token must not leak to storage");
        Assert.That(server.Frames.Single().Object!.Length, Is.EqualTo(bytes.Length));
    }

    [Test]
    public async Task At_most_four_parts_are_in_flight_and_more_than_one_is()
    {
        var (path, _) = File(10 * (int)Part);
        server.PutDelay = TimeSpan.FromMilliseconds(80);
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(server.PeakConcurrentPuts, Is.InRange(2, 4));
    }

    [Test]
    public async Task A_known_hash_skips_rehashing()
    {
        var (path, bytes) = File();
        var events = new List<UploadProgress>();
        var p = new Progress<UploadProgress>(events.Add);
        await Engine().UploadAsync(Req(path, Sha(bytes)), p);
        await Task.Delay(50);
        Assert.That(events.Select(e => e.Stage), Does.Not.Contain(UploadStage.Hashing));
    }

    // ---- resume ------------------------------------------------------------------------------------------------

    [Test]
    public async Task A_crash_mid_upload_resumes_with_only_the_outstanding_parts()
    {
        var (path, bytes) = File();
        // Run 1 is killed (cancelled) while the third part is in flight: parts 1 and 2 are stored, nothing else.
        using var cts = new CancellationTokenSource();
        server.Intercept(r => r.Method == "PUT" && r.Path.EndsWith("/3"), async ctx =>
        {
            cts.Cancel();
            await Task.Delay(5000, ctx.RequestAborted);
        });
        var first = Engine(new UploadEngineOptions { ParallelParts = 1 });
        Assert.CatchAsync<OperationCanceledException>(() => first.UploadAsync(Req(path), ct: cts.Token));
        Assert.That(server.RequestsTo("POST", "/complete"), Is.Empty, "a cancelled upload is never completed");
        var framesAfterCrash = server.Frames;
        Assert.That(framesAfterCrash.Single().Status, Is.EqualTo("uploading"));
        var putsBefore = server.TotalPartPuts;

        // "Restart": a brand new engine, a brand new API client, the same file.
        var result = await Engine().UploadAsync(Req(path));

        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(result.Resumed, Is.True);
        Assert.That(result.PartsSent, Is.EqualTo(3), "parts 3, 4 and 5 only");
        Assert.That(server.TotalPartPuts - putsBefore, Is.EqualTo(3));
        Assert.That(server.PartPuts(1), Is.EqualTo(1));
        Assert.That(server.PartPuts(2), Is.EqualTo(1));
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
        Assert.That(server.Frames, Has.Count.EqualTo(1), "the same frame, not a second one");
        var secondInit = JsonNode.Parse(server.RequestsTo("POST", "frames/init").Last().Body!)!;
        Assert.That(secondInit["sha256"]!.GetValue<string>(), Is.EqualTo(Sha(bytes)));
    }

    [Test]
    public async Task A_part_that_keeps_failing_hands_the_item_back_and_a_later_run_finishes_only_that_part()
    {
        var (path, bytes) = File();
        server.InterceptError(r => r.Method == "PUT" && r.Path.EndsWith("/4"), 500, "internal_error", times: 3);
        var first = await Engine(new UploadEngineOptions { MaxPartAttempts = 3 }).UploadAsync(Req(path));
        Assert.That(first.Outcome, Is.EqualTo(UploadOutcome.RetryLater));
        Assert.That(first.Code, Is.EqualTo("part_failed"));
        Assert.That(server.PartPuts(4), Is.EqualTo(3));
        Assert.That(server.RequestsTo("POST", "/complete"), Is.Empty);

        var second = await Engine().UploadAsync(Req(path));
        Assert.That(second.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(second.Resumed, Is.True);
        Assert.That(second.PartsSent, Is.EqualTo(1));
        Assert.That(server.PartPuts(4), Is.EqualTo(4));
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task A_transient_part_failure_is_retried_with_jittered_backoff_and_then_succeeds()
    {
        var (path, bytes) = File();
        server.InterceptError(r => r.Method == "PUT" && r.Path.EndsWith("/2"), 500, "internal_error", times: 2);
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(server.PartPuts(2), Is.EqualTo(3), "two failures and the success");
        Assert.That(server.PartPuts(1), Is.EqualTo(1));
        Assert.That(clock.Delays, Has.Count.EqualTo(2));
        Assert.That(clock.Delays[0].TotalSeconds, Is.InRange(1, 2));   // base 2 s, 50 % jitter
        Assert.That(clock.Delays[1].TotalSeconds, Is.InRange(2, 4));   // doubled
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task A_dropped_connection_on_a_part_is_a_retry_not_a_failure()
    {
        var (path, bytes) = File();
        server.Intercept(r => r.Method == "PUT" && r.Path.EndsWith("/1"), ctx =>
        {
            ctx.Abort();
            return Task.CompletedTask;
        });
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(server.PartPuts(1), Is.EqualTo(2));
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task An_expired_presigned_url_re_inits_and_gets_fresh_urls_for_only_what_is_left()
    {
        var (path, bytes) = File();
        server.Intercept(r => r.Method == "PUT" && r.Path.EndsWith("/3"), ctx =>
        {
            ctx.Response.StatusCode = 403; // S3: Request has expired
            return Task.CompletedTask;
        });
        var result = await Engine(new UploadEngineOptions { ParallelParts = 1 }).UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(result.Resumed, Is.True);
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(2));
        Assert.That(server.PartPuts(1), Is.EqualTo(1));
        Assert.That(server.PartPuts(2), Is.EqualTo(1));
        Assert.That(server.PartPuts(3), Is.EqualTo(2), "once refused, once with a fresh URL (a 403 is not retried on the same URL)");
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task Upload_incomplete_on_complete_triggers_a_resume_from_init()
    {
        var (path, bytes) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "/complete"), 409, "upload_incomplete", "parts missing",
            extra: new JsonObject { ["outstanding_parts"] = new JsonArray(2) });
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(2));
        Assert.That(server.RequestsTo("POST", "/complete"), Has.Count.EqualTo(2));
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task Size_mismatch_starts_again_from_init()
    {
        var (path, bytes) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "/complete"), 409, "size_mismatch", "assembled size differs");
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(2));
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task Resumes_are_bounded_so_a_persistently_refusing_server_cannot_loop_forever()
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "/complete"), 409, "upload_incomplete", "no", times: 1000);
        var result = await Engine(new UploadEngineOptions { MaxResumes = 2 }).UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.RetryLater));
        Assert.That(result.Code, Is.EqualTo(ApiCodes.UploadIncomplete));
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(3));
    }

    // ---- server verdicts ---------------------------------------------------------------------------------------

    [Test]
    public async Task Duplicate_means_done_and_nothing_is_sent()
    {
        var (path, bytes) = File();
        var existing = server.SeedFrame(Sha(bytes), "accepted", 300_000);
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Duplicate));
        Assert.That(result.IsDone);
        Assert.That(result.FrameId, Is.EqualTo(existing.FrameId));
        Assert.That(result.Status, Is.EqualTo("accepted"));
        Assert.That(server.TotalPartPuts, Is.Zero);
        Assert.That(server.RequestsTo("POST", "/complete"), Is.Empty);
    }

    [Test]
    public async Task Quota_exceeded_blocks_the_project_and_names_the_quota()
    {
        var (path, _) = File();
        server.QuotaBytesRemaining = 10;
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.ProjectBlocked));
        Assert.That(result.Code, Is.EqualTo(ApiCodes.QuotaExceeded));
        Assert.That(result.Quota, Is.EqualTo(QuotaNames.ContributorBytes));
        Assert.That(server.TotalPartPuts, Is.Zero);
    }

    [Test]
    public async Task Ingest_paused_is_retry_later_with_the_servers_retry_after()
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 503, "ingest_paused", "maintenance", retryAfterSeconds: 900);
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.RetryLater));
        Assert.That(result.Code, Is.EqualTo(ApiCodes.IngestPaused));
        Assert.That(result.RetryAfter, Is.EqualTo(TimeSpan.FromSeconds(900)));
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(1), "not hammered inline");
        Assert.That(clock.Delays, Is.Empty);
    }

    [Test]
    public async Task Rate_limited_is_retry_later_and_a_missing_retry_after_gets_a_default()
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 429, "rate_limited", "slow", retryAfterSeconds: 42);
        Assert.That((await Engine().UploadAsync(Req(path))).RetryAfter, Is.EqualTo(TimeSpan.FromSeconds(42)));

        server.Intercept(FakeCollabServer.Route("POST", "frames/init"), async ctx =>
        {
            ctx.Response.StatusCode = 429;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync("""{"ok":false,"code":"rate_limited","message":"x"}""");
        });
        var r = await Engine().UploadAsync(Req(path));
        Assert.That(r.Outcome, Is.EqualTo(UploadOutcome.RetryLater));
        Assert.That(r.RetryAfter, Is.EqualTo(TimeSpan.FromSeconds(60)));
    }

    [Test]
    public async Task Server_errors_on_init_are_retried_inline_with_jitter_then_handed_back()
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 500, "internal_error", times: 2);
        var ok = await Engine().UploadAsync(Req(path));
        Assert.That(ok.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(3));
        Assert.That(clock.Delays, Has.Count.EqualTo(2));

        var (path2, _) = File(1000, "other.fits");
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 502, "internal_error", times: 100);
        var before = server.RequestsTo("POST", "frames/init").Count;
        var bad = await Engine().UploadAsync(Req(path2));
        Assert.That(bad.Outcome, Is.EqualTo(UploadOutcome.RetryLater));
        Assert.That(server.RequestsTo("POST", "frames/init").Count - before, Is.EqualTo(4), "MaxApiAttempts");
    }

    [Test]
    public async Task A_rejected_token_means_auth_required_and_sends_nothing_to_storage()
    {
        var (path, _) = File();
        var eng = new UploadEngine(TestApi.Client(server, new InMemoryTokenStore("rscu_" + new string('f', 64)), http), http, clock);
        var result = await eng.UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.AuthRequired));
        Assert.That(result.Code, Is.EqualTo(ApiCodes.TokenInvalid));
        Assert.That(server.TotalPartPuts, Is.Zero);
    }

    [TestCase("header_rejected", 400)]
    [TestCase("frame_too_large", 400)]
    [TestCase("unsupported_frame_type", 400)]
    [TestCase("policy_violation", 409)]
    [TestCase("validation_failed", 400)]
    public async Task Verdicts_that_retrying_cannot_change_are_permanent_failures(string code, int status)
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), status, code, "no");
        var result = await Engine().UploadAsync(Req(path));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Failed));
        Assert.That(result.Code, Is.EqualTo(code));
    }

    [Test]
    public async Task A_project_that_requires_stripped_frames_refuses_a_request_that_does_not_declare_it()
    {
        // strip_required (open point 28) is its own code, not header_rejected; the file is not retried unchanged.
        var (path, _) = File();
        server.RequireCoordinateStrip = true;
        var refused = await Engine().UploadAsync(Req(path));
        Assert.That(refused.Outcome, Is.EqualTo(UploadOutcome.Failed));
        Assert.That(refused.Code, Is.EqualTo(ApiCodes.StripRequired));
        Assert.That(server.TotalPartPuts, Is.Zero, "nothing went to storage");

        var declared = await Engine().UploadAsync(Req(path) with { Stripped = true });
        Assert.That(declared.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
    }

    [TestCase("project_not_open", 409)]
    [TestCase("project_deleted", 409)] // scheduled for deletion (contract 1.16.0): blocked, rechecked later (it can be restored)
    [TestCase("membership_pending", 409)]
    [TestCase("not_member", 401)]
    public async Task Project_level_refusals_block_the_project_not_just_the_file(string code, int status)
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), status, code, "no");
        Assert.That((await Engine().UploadAsync(Req(path))).Outcome, Is.EqualTo(UploadOutcome.ProjectBlocked));
    }

    [Test]
    public async Task An_unknown_future_code_is_a_permanent_failure_not_an_endless_retry()
    {
        var (path, _) = File();
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 409, "brand_new_code");
        var r = await Engine().UploadAsync(Req(path));
        Assert.That(r.Outcome, Is.EqualTo(UploadOutcome.Failed));
        Assert.That(r.Code, Is.EqualTo("brand_new_code"));
    }

    [Test]
    public async Task A_missing_or_empty_file_fails_without_calling_the_server()
    {
        var gone = await Engine().UploadAsync(Req(Path.Combine(dir, "nope.fits")));
        Assert.That(gone.Outcome, Is.EqualTo(UploadOutcome.Failed));
        Assert.That(gone.Code, Is.EqualTo("file_unreadable"));
        var empty = Path.Combine(dir, "empty.fits");
        System.IO.File.WriteAllBytes(empty, Array.Empty<byte>());
        Assert.That((await Engine().UploadAsync(Req(empty))).Code, Is.EqualTo("file_empty"));
        Assert.That(server.Requests, Is.Empty);
    }

    // ---- cancellation, bandwidth ---------------------------------------------------------------------------------

    [Test]
    public void Cancelling_before_the_start_throws_and_sends_nothing()
    {
        var (path, _) = File();
        using var cts = new CancellationTokenSource();
        cts.Cancel();
        Assert.CatchAsync<OperationCanceledException>(() => Engine().UploadAsync(Req(path), ct: cts.Token));
        Assert.That(server.Requests, Is.Empty);
    }

    [Test]
    public async Task The_bandwidth_cap_paces_the_upload_in_real_time()
    {
        var (path, bytes) = File(600_000);
        var limiter = new BandwidthLimiter(SystemClock.Instance, kilobytesPerSecond: 400); // 600 kB at 400 kB/s: about 1.5 s
        var sw = Stopwatch.StartNew();
        var result = await Engine(limiter: limiter, c: SystemClock.Instance).UploadAsync(Req(path, Sha(bytes)));
        sw.Stop();
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(sw.Elapsed.TotalSeconds, Is.GreaterThan(1.0), "unthrottled this takes milliseconds");
        Assert.That(sw.Elapsed.TotalSeconds, Is.LessThan(6));
        Assert.That(server.Frames.Single().Object, Is.EqualTo(bytes));
    }

    [Test]
    public async Task Raising_the_cap_to_unlimited_mid_run_speeds_up_the_next_upload()
    {
        var (path, bytes) = File(600_000);
        var limiter = new BandwidthLimiter(clock, 100);
        var eng = Engine(limiter: limiter);
        await eng.UploadAsync(Req(path, Sha(bytes)));
        var slowDelays = clock.Delays.Count;
        Assert.That(slowDelays, Is.GreaterThan(0));
        limiter.SetLimit(0);
        var (path2, bytes2) = File(500_000, "second.fits");
        await eng.UploadAsync(Req(path2, Sha(bytes2)));
        Assert.That(clock.Delays.Count, Is.EqualTo(slowDelays));
    }
}

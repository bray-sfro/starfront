// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Upload;
using RapidStackerCollab.Core.Util;


namespace RapidStackerCollab.Core.Tests;

public class UploadRunnerTests
{
    private FakeCollabServer server = null!;
    private FakeClock clock = null!;
    private string dir = null!;
    private string temp = null!;
    private string data = null!;
    private HttpClient http = null!;
    private InMemoryTokenStore tokens = null!;
    private UploadQueue queue = null!;
    private UploadPolicySettings settings = new(UploadMode.Immediately);
    private ImagingState imaging = new();
    private int fileCounter;

    [SetUp]
    public async Task SetUp()
    {
        server = await FakeCollabServer.StartAsync();
        server.PartSize = 8 * 1024; // a 64x48 16-bit frame is 8,640 bytes of data plus headers: several parts
        clock = new FakeClock();
        dir = Directory.CreateTempSubdirectory("rsc-run-").FullName;
        temp = Path.Combine(dir, "tmp");
        data = Path.Combine(dir, "data");
        Directory.CreateDirectory(data);
        http = new HttpClient();
        tokens = new InMemoryTokenStore(FakeCollabServer.Token);
        queue = new UploadQueue(Path.Combine(dir, "queue.db"), clock);
        // NUnit reuses one fixture instance for every test, so per-test state is reset here.
        settings = new UploadPolicySettings(UploadMode.Immediately);
        imaging = new ImagingState();
    }

    [TearDown]
    public async Task TearDown()
    {
        queue.Dispose();
        http.Dispose();
        await server.DisposeAsync();
        TempDirs.Delete(dir);
    }

    private UploadRunner Runner(UploadRunnerOptions? o = null, UploadEngineOptions? eo = null, UploadQueue? q = null) => new(
        q ?? queue,
        new UploadEngine(new ApiClient(http, tokens, new ApiClientOptions { BaseUri = server.ApiBase }), http, clock, null, eo),
        new UploadPolicy(clock), () => settings, () => imaging, clock,
        o ?? new UploadRunnerOptions { TempDirectory = temp });

    private (string Path, byte[] Bytes) Fits(string name, FitsBuilder? b = null)
    {
        // Distinct content per file (the exposure differs), or the server would call the second one a duplicate.
        var bytes = (b ?? FitsBuilder.NinaLight("Ha", 300 + ++fileCounter, withLocation: true)).Build();
        var path = Path.Combine(data, name);
        File.WriteAllBytes(path, bytes);
        return (path, bytes);
    }

    private QueueItem Enqueue(string project, string path, FrameKind kind = FrameKind.Light)
    {
        var header = path.EndsWith(".fits") && File.Exists(path) ? FitsHeaderReader.Read(path) : new InitHeader { Filter = "Ha" };
        return queue.Enqueue(new NewQueueItem(project, path, Path.GetFileName(path), kind, header, "rig_9c1d4e7a02bb")).Item;
    }

    private const string P = FakeCollabServer.ProjectId;

    // ---- the privacy path -----------------------------------------------------------------------------------------

    [Test]
    public async Task Uploaded_files_have_no_observer_location_and_the_originals_are_untouched()
    {
        var files = Enumerable.Range(1, 3).Select(i => Fits($"light{i}.fits", FitsBuilder.NinaLight("Ha", 300 + i, withLocation: true))).ToList();
        var items = files.Select(f => Enqueue(P, f.Path)).ToList();

        var n = await Runner().RunOnceAsync();

        Assert.That(n, Is.EqualTo(3));
        Assert.That(queue.CountsByState()[QueueStates.Uploaded], Is.EqualTo(3));
        var objects = server.Frames.Select(f => f.Object!).ToList();
        Assert.That(objects, Has.Count.EqualTo(3));
        foreach (var o in objects)
        {
            var text = Encoding.Latin1.GetString(o);
            Assert.That(text, Does.Not.Contain("SITELAT").And.Not.Contain("SITELONG").And.Not.Contain("SITEELEV").And.Not.Contain("OBSERVER")
                .And.Not.Contain("41.2345").And.Not.Contain("Jane Q."));
            Assert.That(text, Does.Contain("FILTER").And.Contain("Horsehead"));
        }
        foreach (var (path, bytes) in files) Assert.That(File.ReadAllBytes(path), Is.EqualTo(bytes), "the user's file is exactly as it was");
        Assert.That(Directory.GetFiles(temp), Is.Empty, "the runner deletes its own temp copies");

        // What was uploaded is what init was told: hash, size and the stripped flag.
        foreach (var f in server.Frames)
        {
            Assert.That(f.Stripped, Is.True);
            Assert.That(f.Sha256, Is.EqualTo(Convert.ToHexString(SHA256.HashData(f.Object!)).ToLowerInvariant()));
            Assert.That(f.Size, Is.EqualTo(f.Object!.Length));
        }
        var initHeader = JsonNode.Parse(server.RequestsTo("POST", "frames/init").First().Body!)!["header"]!.AsObject();
        Assert.That(initHeader.Select(kv => kv.Key), Is.SubsetOf(InitHeader.AllowedKeys));
        Assert.That(items.Select(i => queue.Get(i.Id)!.FrameId), Is.All.Not.Null);
    }

    [Test]
    public async Task With_stripping_off_the_original_bytes_go_up_and_the_flag_says_so()
    {
        var f = Fits("raw.fits");
        Enqueue(P, f.Path);
        await Runner(new UploadRunnerOptions { TempDirectory = temp, StripLocation = false }).RunOnceAsync();
        Assert.That(server.Frames.Single().Object, Is.EqualTo(f.Bytes));
        Assert.That(server.Frames.Single().Stripped, Is.False);
    }

    [Test]
    public async Task Xisf_is_stripped_like_fits_and_uploaded_as_is_when_stripping_is_off()
    {
        var path = Path.Combine(data, "x.xisf");
        File.WriteAllBytes(path, Support.XisfBuilder.Build("""<?xml version="1.0"?><xisf version="1.0"><Image geometry="64:48:1" sampleFormat="UInt16" location="attachment:{0}:6144"><FITSKeyword name="FILTER" value="'Ha'" comment=""/><FITSKeyword name="SITELAT" value="41.2" comment=""/></Image></xisf>""", new byte[6144]));
        var item = Enqueue(P, path);

        await Runner().RunOnceAsync();
        Assert.That(queue.Get(item.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
        var sent = server.Frames.Single();
        Assert.That(sent.Stripped, Is.True);
        Assert.That(Encoding.UTF8.GetString(sent.Object!), Does.Contain("FILTER").And.Not.Contain("SITELAT"));
        Assert.That(File.ReadAllText(path), Does.Contain("SITELAT"), "the user's file is untouched");

        var second = Path.Combine(data, "y.xisf");
        File.WriteAllBytes(second, Support.XisfBuilder.Build("""<?xml version="1.0"?><xisf version="1.0"><Image geometry="64:48:1" sampleFormat="UInt16" location="attachment:{0}:6144"><FITSKeyword name="SITELAT" value="41.3" comment=""/></Image></xisf>""", new byte[6144]));
        var again = Enqueue(P, second);
        await Runner(new UploadRunnerOptions { TempDirectory = temp, StripLocation = false }).RunOnceAsync();
        Assert.That(queue.Get(again.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
        Assert.That(Encoding.UTF8.GetString(server.Frames.Last().Object!), Does.Contain("SITELAT"));
    }

    [Test]
    public async Task An_xisf_that_cannot_be_cut_safely_fails_that_item_and_sends_nothing()
    {
        var path = Path.Combine(data, "bad.xisf");
        var xml = Encoding.UTF8.GetBytes("<?xml version=\"1.0\"?><xisf><Image><FITSKeyword name=\"SITELAT\" value=\"1\"></xisf>"); // not well-formed
        File.WriteAllBytes(path, Encoding.ASCII.GetBytes("XISF0100").Concat(BitConverter.GetBytes((uint)xml.Length)).Concat(new byte[4]).Concat(xml).Concat(new byte[64]).ToArray());
        var item = Enqueue(P, path);

        await Runner().RunOnceAsync();

        Assert.That(queue.Get(item.Id)!.State, Is.EqualTo(QueueStates.Failed));
        Assert.That(server.Requests, Is.Empty, "nothing about the file reached the server");
    }

    [Test]
    public async Task A_missing_or_unsupported_file_fails_that_item_only()
    {
        var gone = Enqueue(P, Path.Combine(data, "gone.fits"));
        var png = Path.Combine(data, "x.png");
        File.WriteAllBytes(png, new byte[] { 0x89, 0x50, 0x4E, 0x47, 0, 0, 0, 0, 0, 0 });
        var notFits = Enqueue(P, png);
        var ok = Enqueue(P, Fits("ok.fits").Path);

        await Runner().RunOnceAsync();

        Assert.That(queue.Get(gone.Id)!.LastErrorCode, Is.EqualTo("file_missing"));
        Assert.That(queue.Get(notFits.Id)!.LastErrorCode, Is.EqualTo("unsupported_file_type"));
        Assert.That(queue.Get(ok.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
    }

    // ---- policy -----------------------------------------------------------------------------------------------------

    [Test]
    public async Task The_policy_gates_starting_an_upload_and_the_reason_is_exposed()
    {
        Enqueue(P, Fits("a.fits").Path);
        settings = new UploadPolicySettings(UploadMode.WhenIdle);
        imaging = new ImagingState(ExposureInProgress: true);
        var runner = Runner();

        Assert.That(await runner.RunOnceAsync(), Is.Zero);
        Assert.That(runner.LastDecision!.Allow, Is.False);
        Assert.That(runner.LastDecision.Reason, Does.Contain("exposure"));
        Assert.That(server.Requests, Is.Empty);

        imaging = new ImagingState();
        Assert.That(await runner.RunOnceAsync(), Is.EqualTo(1));
        Assert.That(queue.CountsByState()[QueueStates.Uploaded], Is.EqualTo(1));
    }

    [Test]
    public async Task A_policy_that_closes_mid_queue_stops_before_the_next_file()
    {
        Enqueue(P, Fits("a.fits").Path);
        clock.Advance(TimeSpan.FromSeconds(1));
        Enqueue(P, Fits("b.fits").Path);
        var runner = Runner();
        runner.OnItemFinished = (_, _) => imaging = new ImagingState(ExposureInProgress: true); // the next exposure starts
        settings = new UploadPolicySettings(UploadMode.WhenIdle);

        Assert.That(await runner.RunOnceAsync(), Is.EqualTo(1));
        Assert.That(queue.CountsByState()[QueueStates.Pending], Is.EqualTo(1));
    }

    // ---- outcomes -----------------------------------------------------------------------------------------------------

    [Test]
    public async Task Duplicate_is_recorded_as_done_without_sending_bytes()
    {
        var f = Fits("dup.fits");
        // Hash what the runner will upload: the stripped copy.
        var stripped = Path.Combine(dir, "pre.tmp");
        FitsHeaderStripper.StripToFile(f.Path, stripped);
        var sha = Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(stripped))).ToLowerInvariant();
        server.SeedFrame(sha, "accepted");
        var item = Enqueue(P, f.Path);

        await Runner().RunOnceAsync();

        var row = queue.Get(item.Id)!;
        Assert.That(row.State, Is.EqualTo(QueueStates.Duplicate));
        Assert.That(row.Verdict, Is.EqualTo("accepted"));
        Assert.That(server.TotalPartPuts, Is.Zero);
    }

    [Test]
    public async Task A_quota_pauses_that_project_and_the_other_projects_carry_on()
    {
        var a = Enqueue(P, Fits("a.fits").Path);
        clock.Advance(TimeSpan.FromSeconds(1));
        // Same server, so for the second "project" reuse the real one under its slug.
        var b = Enqueue(FakeCollabServer.Slug, Fits("b.fits").Path);
        var runner = Runner();

        // Make only the first project's init fail with a quota error.
        server.InterceptError(r => r.Method == "POST" && r.Path.Contains(P) && r.Path.EndsWith("frames/init"), 429, "quota_exceeded", "Storage quota reached",
            extra: new JsonObject { ["quota"] = "contributor_bytes" });

        await runner.RunOnceAsync();

        var paused = queue.ActivePauses().Single();
        Assert.That(paused.ProjectRef, Is.EqualTo(P));
        Assert.That(paused.Code, Is.EqualTo(ApiCodes.QuotaExceeded));
        Assert.That(paused.ResumeAt, Is.EqualTo(clock.Now + TimeSpan.FromHours(6)));
        Assert.That(paused.ByUser, Is.False);
        var rowA = queue.Get(a.Id)!;
        Assert.That(rowA.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(rowA.Attempts, Is.Zero, "a quota is not the file's fault");
        Assert.That(queue.Get(b.Id)!.State, Is.EqualTo(QueueStates.Uploaded));

        // Later, when the pause lapses, the file goes.
        clock.Advance(TimeSpan.FromHours(7));
        await runner.RunOnceAsync();
        Assert.That(queue.Get(a.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
    }

    [Test]
    public async Task Rate_limited_holds_the_whole_run_until_the_time_the_server_named()
    {
        var a = Enqueue(P, Fits("a.fits").Path);
        clock.Advance(TimeSpan.FromSeconds(1));
        var b = Enqueue(P, Fits("b.fits").Path);
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 429, "rate_limited", "slow down", retryAfterSeconds: 120);
        var runner = Runner();
        var t0 = clock.Now;

        Assert.That(await runner.RunOnceAsync(), Is.EqualTo(1), "the second file is not tried into the same limit");
        Assert.That(server.RequestsTo("POST", "frames/init"), Has.Count.EqualTo(1));
        var rowA = queue.Get(a.Id)!;
        Assert.That(rowA.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(rowA.NextAttemptAt, Is.EqualTo(t0 + TimeSpan.FromSeconds(120)));
        Assert.That(runner.HeldUntil, Is.EqualTo(t0 + TimeSpan.FromSeconds(120)));
        Assert.That(queue.Get(b.Id)!.State, Is.EqualTo(QueueStates.Pending));

        Assert.That(await runner.RunOnceAsync(), Is.Zero, "still inside Retry-After");
        clock.Advance(TimeSpan.FromSeconds(121));
        Assert.That(await runner.RunOnceAsync(), Is.EqualTo(2));
        Assert.That(queue.CountsByState()[QueueStates.Uploaded], Is.EqualTo(2));
    }

    [Test]
    public async Task Ingest_paused_is_honoured_the_same_way()
    {
        Enqueue(P, Fits("a.fits").Path);
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 503, "ingest_paused", "maintenance", retryAfterSeconds: 600);
        var runner = Runner();
        await runner.RunOnceAsync();
        Assert.That(runner.HeldUntil, Is.EqualTo(clock.Now + TimeSpan.FromSeconds(600)));
        Assert.That(queue.List().Single().LastErrorCode, Is.EqualTo(ApiCodes.IngestPaused));
    }

    [Test]
    public async Task A_rejected_token_stops_everything_until_the_user_signs_in_again()
    {
        Enqueue(P, Fits("a.fits").Path);
        clock.Advance(TimeSpan.FromSeconds(1));
        Enqueue(P, Fits("b.fits").Path);
        tokens.Save("rscu_" + new string('0', 64));
        var runner = Runner();
        var told = 0;
        runner.OnAuthRequired = () => told++;

        Assert.That(await runner.RunOnceAsync(), Is.EqualTo(1));
        Assert.That(runner.AuthRequired);
        Assert.That(told, Is.EqualTo(1));
        Assert.That(queue.CountsByState()[QueueStates.Pending], Is.EqualTo(2), "nothing was lost");
        Assert.That(queue.List().Select(i => i.Attempts), Is.All.Zero);
        Assert.That(await runner.RunOnceAsync(), Is.Zero);

        tokens.Save(FakeCollabServer.Token);
        runner.ClearAuthRequired();
        Assert.That(await runner.RunOnceAsync(), Is.EqualTo(2));
        Assert.That(queue.CountsByState()[QueueStates.Uploaded], Is.EqualTo(2));
    }

    [Test]
    public async Task A_file_the_server_will_never_take_fails_once_with_the_reason_and_does_not_hold_up_the_rest()
    {
        var bad = Enqueue(P, Fits("bad.fits").Path);
        clock.Advance(TimeSpan.FromSeconds(1));
        var good = Enqueue(P, Fits("good.fits").Path);
        server.InterceptError(FakeCollabServer.Route("POST", "frames/init"), 400, "unsupported_frame_type", "IMAGETYP is not one we take");

        await Runner().RunOnceAsync();

        var row = queue.Get(bad.Id)!;
        Assert.That(row.State, Is.EqualTo(QueueStates.Failed));
        Assert.That(row.LastErrorCode, Is.EqualTo("unsupported_frame_type"));
        Assert.That(row.LastError, Does.Contain("IMAGETYP"));
        Assert.That(queue.Get(good.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
    }

    [Test]
    public async Task A_part_that_keeps_failing_is_retried_with_backoff_and_finally_failed()
    {
        var item = Enqueue(P, Fits("flaky.fits").Path);
        server.InterceptError(r => r.Method == "PUT", 500, "internal_error", times: 10_000);
        var runner = Runner(new UploadRunnerOptions { TempDirectory = temp, MaxAttempts = 3 }, new UploadEngineOptions { MaxPartAttempts = 2 });

        await runner.RunOnceAsync();
        var first = queue.Get(item.Id)!;
        Assert.That((first.State, first.Attempts, first.LastErrorCode), Is.EqualTo((QueueStates.Pending, 1, "part_failed")));
        Assert.That(first.NextAttemptAt, Is.GreaterThan(clock.Now));
        Assert.That(runner.HeldUntil, Is.Null, "a flaky file does not hold up other files");

        clock.Advance(TimeSpan.FromMinutes(31));
        await runner.RunOnceAsync();
        Assert.That(queue.Get(item.Id)!.Attempts, Is.EqualTo(2));
        clock.Advance(TimeSpan.FromMinutes(31));
        await runner.RunOnceAsync();
        var last = queue.Get(item.Id)!;
        Assert.That(last.State, Is.EqualTo(QueueStates.Failed));
        Assert.That(last.LastError, Does.Contain("Gave up after 3 attempts"));
        Assert.That(Directory.GetFiles(temp), Is.Empty);
    }

    // ---- restart, cancellation --------------------------------------------------------------------------------------

    [Test]
    public async Task After_a_crash_the_next_start_recovers_the_item_and_resumes_the_same_frame()
    {
        var f = Fits("big.fits", FitsBuilder.NinaLight(withLocation: true, width: 200, height: 150)); // ~60 kB: eight 8 KiB parts
        var item = Enqueue(P, f.Path);

        // Run 1 dies while part 3 is in flight (we cancel the token and then abandon the process state).
        using var cts = new CancellationTokenSource();
        server.Intercept(r => r.Method == "PUT" && r.Path.EndsWith("/3"), async ctx =>
        {
            cts.Cancel();
            await Task.Delay(5000, ctx.RequestAborted);
        });
        var r1 = Runner(eo: new UploadEngineOptions { ParallelParts = 1 });
        Assert.CatchAsync<OperationCanceledException>(() => r1.RunOnceAsync(cts.Token));
        var frameId = queue.Get(item.Id)!.FrameId;
        Assert.That(frameId, Is.Not.Null, "the frame id was persisted as soon as init answered");
        // Make the row look the way a hard kill leaves it: claimed and never released.
        queue.Dispose();
        using (var c = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={Path.Combine(dir, "queue.db")};Pooling=False"))
        {
            c.Open();
            using var cmd = c.CreateCommand();
            cmd.CommandText = "UPDATE items SET state = 'uploading'";
            cmd.ExecuteNonQuery();
        }
        var putsBefore = server.TotalPartPuts;
        Directory.CreateDirectory(temp);
        File.WriteAllText(Path.Combine(temp, "rsc-upload-99.fits.tmp"), "orphan from the crashed run");

        // "Restart": reopen the queue, a fresh runner, RunAsync does recovery then works.
        queue = new UploadQueue(Path.Combine(dir, "queue.db"), clock);
        Assert.That(queue.Get(item.Id)!.State, Is.EqualTo(QueueStates.Uploading));
        var r2 = Runner(eo: new UploadEngineOptions { ParallelParts = 1 });
        using var stop = new CancellationTokenSource();
        r2.OnItemFinished = (_, _) => stop.Cancel();
        await r2.RunAsync(stop.Token);

        var row = queue.Get(item.Id)!;
        Assert.That(row.State, Is.EqualTo(QueueStates.Uploaded));
        Assert.That(row.FrameId, Is.EqualTo(frameId), "the same frame, resumed");
        Assert.That(server.Frames, Has.Count.EqualTo(1));
        Assert.That(server.Frames.Single().Status, Is.EqualTo("uploaded"));
        var parts = server.Frames.Single().PartCount;
        Assert.That(server.TotalPartPuts - putsBefore, Is.LessThan(parts), "the finished parts were not sent again");
        Assert.That(server.PartPuts(1), Is.EqualTo(1));
        Assert.That(server.PartPuts(2), Is.EqualTo(1));
        Assert.That(File.Exists(Path.Combine(temp, "rsc-upload-99.fits.tmp")), Is.False, "orphan temp copies are swept at start");
        Assert.That(Encoding.Latin1.GetString(server.Frames.Single().Object!), Does.Not.Contain("SITELAT"));
    }

    [Test]
    public async Task Cancelling_mid_upload_puts_the_item_back_without_counting_an_attempt_and_cleans_up()
    {
        var item = Enqueue(P, Fits("c.fits").Path);
        using var cts = new CancellationTokenSource();
        server.Intercept(r => r.Method == "PUT", async ctx =>
        {
            cts.Cancel();
            await Task.Delay(5000, ctx.RequestAborted);
        });
        Assert.CatchAsync<OperationCanceledException>(() => Runner().RunOnceAsync(cts.Token));
        var row = queue.Get(item.Id)!;
        Assert.That((row.State, row.Attempts), Is.EqualTo((QueueStates.Pending, 0)));
        Assert.That(Directory.GetFiles(temp), Is.Empty);
        await Task.CompletedTask;
    }

    [Test]
    public async Task RunAsync_wakes_for_new_work_and_stops_on_cancel()
    {
        var real = new RealtimeRunner(this);
        await real.RunAsync();
    }

    private sealed class RealtimeRunner
    {
        private readonly UploadRunnerTests t;
        public RealtimeRunner(UploadRunnerTests t) => this.t = t;

        public async Task RunAsync()
        {
            // The real clock: the runner must sleep, then wake when told, not busy-loop.
            var q = new UploadQueue(Path.Combine(t.dir, "wake.db"), SystemClock.Instance);
            var engine = new UploadEngine(new ApiClient(t.http, t.tokens, new ApiClientOptions { BaseUri = t.server.ApiBase }), t.http, SystemClock.Instance);
            var runner = new UploadRunner(q, engine, new UploadPolicy(SystemClock.Instance), () => new UploadPolicySettings(UploadMode.Immediately),
                () => new ImagingState(), SystemClock.Instance, new UploadRunnerOptions { TempDirectory = Path.Combine(t.dir, "t2"), IdlePoll = TimeSpan.FromHours(1) });
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            var done = new TaskCompletionSource();
            runner.OnItemFinished = (_, _) => done.TrySetResult();
            var loop = runner.RunAsync(cts.Token);

            await Task.Delay(300);
            Assert.That(t.server.Requests, Is.Empty, "idle: nothing sent while the queue is empty");
            var path = Path.Combine(t.data, "w.fits");
            File.WriteAllBytes(path, FitsBuilder.NinaLight().Build());
            q.Enqueue(new NewQueueItem(P, path, "w.fits", FrameKind.Light, FitsHeaderReader.Read(path)));
            runner.Wake();

            await done.Task.WaitAsync(TimeSpan.FromSeconds(10));
            cts.Cancel();
            await loop.WaitAsync(TimeSpan.FromSeconds(5));
            Assert.That(q.CountsByState()[QueueStates.Uploaded], Is.EqualTo(1));
            q.Dispose();
        }
    }
}

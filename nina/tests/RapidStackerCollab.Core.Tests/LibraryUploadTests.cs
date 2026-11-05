// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Security.Cryptography;
using System.Text;
using Microsoft.Data.Sqlite;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Upload;

namespace RapidStackerCollab.Core.Tests;

/// <summary>The upload path to a rig's calibration library: engine target, runner strip, pausing, the verdict poll and the queue's schema 3.</summary>
public class LibraryUploadTests
{
    private const string Rig = FakeCollabServer.RigId;
    private static readonly string Ref = LibraryRef.For(Rig);

    private FakeCollabServer server = null!;
    private FakeClock clock = null!;
    private string dir = null!;
    private string data = null!;
    private HttpClient http = null!;
    private UploadQueue queue = null!;
    private InMemoryTokenStore tokens = null!;

    [SetUp]
    public async Task SetUp()
    {
        server = await FakeCollabServer.StartAsync();
        server.PartSize = 4 * 1024;
        server.RigWidthPx = 64;
        server.RigHeightPx = 48;
        clock = new FakeClock();
        dir = Directory.CreateTempSubdirectory("rsc-libup-").FullName;
        data = Path.Combine(dir, "data");
        Directory.CreateDirectory(data);
        http = new HttpClient();
        tokens = new InMemoryTokenStore(FakeCollabServer.Token);
        queue = new UploadQueue(Path.Combine(dir, "queue.db"), clock);
    }

    [TearDown]
    public async Task TearDown()
    {
        queue.Dispose();
        SqliteConnection.ClearAllPools();
        http.Dispose();
        await server.DisposeAsync();
        TempDirs.Delete(dir);
    }

    private ApiClient Api() => new(http, tokens, new ApiClientOptions { BaseUri = server.ApiBase });

    private UploadRunner Runner(Func<QueueItem, bool>? strip = null) => new(queue, new UploadEngine(Api(), http, clock), new UploadPolicy(clock),
        () => new UploadPolicySettings(UploadMode.Immediately), () => new ImagingState(), clock,
        new UploadRunnerOptions { TempDirectory = Path.Combine(dir, "tmp"), StripPredicate = strip ?? (_ => false), StripLocation = false });

    private QueueItem EnqueueLibrary(string name, int seed, string imageTyp = "Dark")
    {
        var path = FitsBuilder.Calibration(imageTyp, seed: seed).WriteTo(data, name);
        return queue.Enqueue(new NewQueueItem(Ref, path, name, FrameKinds.Classify(imageTyp), FitsHeaderReader.Read(path), Rig)).Item;
    }

    [Test]
    public void A_library_ref_names_its_rig_and_a_project_ref_does_not()
    {
        Assert.That(LibraryRef.For("rig_1"), Is.EqualTo("lib:rig_1"));
        Assert.That(LibraryRef.RigOf("lib:rig_1"), Is.EqualTo("rig_1"));
        Assert.That(LibraryRef.RigOf(FakeCollabServer.ProjectId), Is.Null);
        Assert.That(LibraryRef.IsLibrary("lib:"), Is.True);
        Assert.That(LibraryRef.RigOf("lib:"), Is.Null);
    }

    [Test]
    public async Task A_library_item_is_uploaded_to_the_rigs_library_stripped_even_when_the_user_turned_stripping_off()
    {
        var item = EnqueueLibrary("dark.fits", 1);
        await Runner(strip: _ => false).RunOnceAsync();

        Assert.That(queue.Get(item.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
        Assert.That(server.RequestsTo("POST", $"/api/rigs/{Rig}/library/frames/init"), Has.Count.EqualTo(1));
        Assert.That(server.RequestsTo("POST", $"/api/rigs/{Rig}/library/frames/{queue.Get(item.Id)!.FrameId}/complete"), Has.Count.EqualTo(1));
        Assert.That(server.RequestsTo("POST", "/frames/init").Where(r => r.Path.Contains("/projects/")), Is.Empty, "no project route was used");

        var frame = server.LibraryFrames.Single();
        Assert.That(frame.Status, Is.EqualTo("ready"));
        Assert.That(frame.Stripped, Is.True);
        var text = Encoding.Latin1.GetString(frame.Object!);
        Assert.That(text, Does.Not.Contain("SITELAT").And.Not.Contain("OBSERVER").And.Not.Contain("Jane Q."), "the location is gone");
        Assert.That(File.ReadAllText(Path.Combine(data, "dark.fits"), Encoding.Latin1), Does.Contain("SITELAT"), "the user's file is untouched");
        Assert.That(frame.Sha256, Is.EqualTo(Convert.ToHexString(SHA256.HashData(frame.Object!)).ToLowerInvariant()));
    }

    [Test]
    public async Task A_duplicate_in_the_library_counts_as_done()
    {
        var item = EnqueueLibrary("dark.fits", 1);
        await Runner().RunOnceAsync();
        queue.Requeue(item.Id); // as if the queue forgot, while the server holds it
        await Runner().RunOnceAsync();
        Assert.That(queue.Get(item.Id)!.State, Is.EqualTo(QueueStates.Duplicate));
        Assert.That(server.LibraryFrames, Has.Count.EqualTo(1));
    }

    [Test]
    public async Task A_library_quota_refusal_pauses_only_the_library_and_other_projects_still_upload()
    {
        server.LibraryMaxFrames = 1;
        var first = EnqueueLibrary("a.fits", 1);
        var second = EnqueueLibrary("b.fits", 2);
        var light = FitsBuilder.NinaLight("Ha", 300).WriteTo(data, "light.fits");
        var project = queue.Enqueue(new NewQueueItem(FakeCollabServer.ProjectId, light, "light.fits", FrameKind.Light, FitsHeaderReader.Read(light), Rig)).Item;

        await Runner().RunOnceAsync();

        Assert.That(queue.Get(first.Id)!.State, Is.EqualTo(QueueStates.Uploaded));
        Assert.That(queue.Get(second.Id)!.State, Is.EqualTo(QueueStates.Pending), "released, not failed");
        Assert.That(queue.Get(project.Id)!.State, Is.EqualTo(QueueStates.Uploaded), "the project's upload was not held up");
        var pause = queue.ActivePauses().Single();
        Assert.That(pause.ProjectRef, Is.EqualTo(Ref));
        Assert.That(pause.Code, Is.EqualTo(ApiCodes.QuotaExceeded));
        Assert.That(queue.IsPaused(FakeCollabServer.ProjectId), Is.False);
    }

    [Test]
    public async Task A_frame_the_rig_does_not_match_fails_for_that_file_only()
    {
        server.RigCamera = "QHY268M";
        var item = EnqueueLibrary("dark.fits", 1);
        await Runner().RunOnceAsync();
        var row = queue.Get(item.Id)!;
        Assert.That(row.State, Is.EqualTo(QueueStates.Failed).Or.EqualTo(QueueStates.Pending));
        Assert.That(row.LastErrorCode, Is.EqualTo(ApiCodes.ValidationFailed));
        Assert.That(queue.ActivePauses(), Is.Empty);
    }

    [Test]
    public async Task The_engine_refuses_an_unstripped_library_request_without_calling_the_server()
    {
        var path = FitsBuilder.Calibration(seed: 1).WriteTo(data, "d.fits");
        var engine = new UploadEngine(Api(), http, clock);
        var result = await engine.UploadAsync(new UploadRequest(Ref, path, "d.fits", FitsHeaderReader.Read(path), Stripped: false, Rig));
        Assert.That(result.Outcome, Is.EqualTo(UploadOutcome.Failed));
        Assert.That(result.Code, Is.EqualTo(ApiCodes.StripRequired));
        Assert.That(server.Requests.Where(r => r.Path.Contains("/library")), Is.Empty);
    }

    [Test]
    public async Task An_interrupted_library_upload_resumes_with_the_same_frame()
    {
        var path = FitsBuilder.Calibration(seed: 3, width: 64, height: 48).WriteTo(data, "d.fits");
        var engine = new UploadEngine(Api(), http, clock);
        var stripped = Path.Combine(dir, "stripped.fits");
        FitsHeaderStripper.StripToFile(path, stripped);
        var failing = true;
        var put = FakeCollabServer.Route("PUT", "/s3/");
        server.InterceptError(r => failing && put(r), 500, "internal_error", times: 1000);
        var request = new UploadRequest(Ref, stripped, "d.fits", FitsHeaderReader.Read(path), Stripped: true, Rig);
        var failed = await engine.UploadAsync(request);
        Assert.That(failed.Outcome, Is.EqualTo(UploadOutcome.RetryLater));
        var first = server.LibraryFrames.Single();
        Assert.That(first.Status, Is.EqualTo("uploading"));

        failing = false;
        var again = await new UploadEngine(Api(), http, clock).UploadAsync(request);
        Assert.That(again.Outcome, Is.EqualTo(UploadOutcome.Uploaded));
        Assert.That(again.FrameId, Is.EqualTo(first.FrameId), "same sha, same frame");
        Assert.That(server.LibraryFrames.Single().Status, Is.EqualTo("ready"));
    }

    [Test]
    public void The_library_header_reader_takes_CCD_TEMP_as_the_set_point_only_when_SET_TEMP_is_missing()
    {
        var onlyCcd = FitsBuilder.Calibration(seed: 1, setTemp: null, ccdTemp: -9.8).WriteTo(data, "ccd.fits");
        Assert.That(LibraryHeaderReader.Read(onlyCcd).SetTemp, Is.EqualTo(-9.8));
        var both = FitsBuilder.Calibration(seed: 2, setTemp: -10, ccdTemp: -9.8).WriteTo(data, "both.fits");
        Assert.That(LibraryHeaderReader.Read(both).SetTemp, Is.EqualTo(-10));
        var neither = FitsBuilder.Calibration(seed: 3, setTemp: null).WriteTo(data, "none.fits");
        Assert.That(LibraryHeaderReader.Read(neither).SetTemp, Is.Null);
        Assert.That(FitsHeaderReader.Read(onlyCcd).SetTemp, Is.Null, "the live reader is unchanged");
    }

    // ---- verdicts -------------------------------------------------------------------------------------------------

    [Test]
    public void Library_rows_are_never_polled_for_a_verdict()
    {
        var item = EnqueueLibrary("a.fits", 1);
        queue.MarkUploaded(item.Id, "frm_x", "ready");
        var light = queue.Enqueue(new NewQueueItem(FakeCollabServer.ProjectId, "/d/l.fits", "l.fits", FrameKind.Light, new InitHeader { Filter = "Ha" }, Rig)).Item;
        queue.MarkUploaded(light.Id, "frm_y", "uploaded");
        Assert.That(queue.AwaitingVerdict().Select(i => i.Id), Is.EqualTo(new[] { light.Id }));
    }

    // ---- queue schema 3 -------------------------------------------------------------------------------------------

    [Test]
    public void A_v2_database_migrates_to_v3_keeping_every_row_and_gaining_the_hash_cache()
    {
        var db = Path.Combine(dir, "v2", "queue.db");
        using (var q = new UploadQueue(db, clock))
        {
            q.Enqueue(new NewQueueItem("prj_a", "/d/1.fits", "1.fits", FrameKind.Light, new InitHeader { Filter = "Ha" }, "rig_1", "2026-10-13", "ha"));
            q.Enqueue(new NewQueueItem(Ref, "/lib/d.fits", "d.fits", FrameKind.Dark, new InitHeader(), Rig));
            q.AddFlatRoute("/f/a.fits", new InitHeader { Filter = "Ha" }, "rig_1", "ha", "2026-10-13", clock.Now.AddHours(1));
        }
        SqliteConnection.ClearAllPools();
        using (var c = new SqliteConnection($"Data Source={db};Pooling=False"))
        {
            c.Open();
            using var cmd = c.CreateCommand();
            cmd.CommandText = "DROP TABLE library_files; UPDATE meta SET value = '2' WHERE key = 'schema_version'";
            cmd.ExecuteNonQuery();
        }

        using var reopened = new UploadQueue(db, clock);
        Assert.That(reopened.List(limit: 10), Has.Count.EqualTo(2), "no queue row is lost");
        Assert.That(reopened.CountHeldFlats(), Is.EqualTo(1), "nor a flat route");
        reopened.PutLibraryFile(new LibraryFileEntry("/lib/d.fits", 10, 20, new string('a', 64)));
        Assert.That(reopened.GetLibraryFile("/lib/d.fits")!.StrippedSha256, Is.EqualTo(new string('a', 64)));
        Assert.That(reopened.LibraryCounts(Rig)[QueueStates.Pending], Is.EqualTo(1));
        Assert.That(reopened.LibraryCounts("rig_other")[QueueStates.Pending], Is.Zero);
    }

    [Test]
    public async Task The_hasher_strips_and_hashes_a_file_once_and_matches_the_hash_the_runner_uploads()
    {
        var path = FitsBuilder.Calibration(seed: 5).WriteTo(data, "d.fits");
        var info = new FileInfo(path);
        var candidate = new LibraryCandidate(path, info.Length, new DateTimeOffset(info.LastWriteTimeUtc, TimeSpan.Zero).ToUnixTimeMilliseconds(), FitsHeaderReader.Read(path));
        var hasher = new LibraryHasher(queue, Path.Combine(dir, "tmp"));
        var sha = await hasher.StrippedSha256Async(candidate);

        // Replace the file by one of different content behind the same size and time: the cache answers, so nothing was read again.
        var cached = await hasher.StrippedSha256Async(candidate);
        Assert.That(cached, Is.EqualTo(sha));
        Assert.That(queue.GetLibraryFile(path)!.StrippedSha256, Is.EqualTo(sha));
        Assert.That(Directory.GetFiles(Path.Combine(dir, "tmp")), Is.Empty, "its temp copy is deleted");

        var item = queue.Enqueue(new NewQueueItem(Ref, path, "d.fits", FrameKind.Dark, candidate.Header, Rig)).Item;
        await Runner().RunOnceAsync();
        Assert.That(server.LibraryFrames.Single().Sha256, Is.EqualTo(sha));
        Assert.That(queue.Get(item.Id)!.Sha256, Is.EqualTo(sha));
    }

    [Test]
    public async Task A_changed_file_is_hashed_again()
    {
        var path = FitsBuilder.Calibration(seed: 5).WriteTo(data, "d.fits");
        var hasher = new LibraryHasher(queue, Path.Combine(dir, "tmp"));
        LibraryCandidate Of() { var i = new FileInfo(path); return new LibraryCandidate(path, i.Length, new DateTimeOffset(i.LastWriteTimeUtc, TimeSpan.Zero).ToUnixTimeMilliseconds(), new InitHeader()); }
        var before = await hasher.StrippedSha256Async(Of());
        FitsBuilder.Calibration(seed: 6).WriteTo(data, "d.fits");
        File.SetLastWriteTimeUtc(path, DateTime.UtcNow.AddMinutes(-3));
        Assert.That(await hasher.StrippedSha256Async(Of()), Is.Not.EqualTo(before));
    }

    [Test]
    public void The_hash_cache_forgets_files_that_left_the_folder_and_a_finished_row_can_be_requeued_once()
    {
        queue.PutLibraryFile(new LibraryFileEntry("/lib/a.fits", 1, 1, "x"));
        queue.PutLibraryFile(new LibraryFileEntry("/lib/b.fits", 1, 1, "y"));
        Assert.That(queue.PruneLibraryFiles(new HashSet<string> { "/lib/a.fits" }), Is.EqualTo(1));
        Assert.That(queue.GetLibraryFile("/lib/b.fits"), Is.Null);

        var item = EnqueueLibrary("a.fits", 1);
        Assert.That(queue.RequeueFinished(item.Id), Is.False, "a pending row is not touched");
        queue.MarkUploaded(item.Id, "frm_1", "ready");
        Assert.That(queue.RequeueFinished(item.Id), Is.True);
        var row = queue.Get(item.Id)!;
        Assert.That(row.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(row.FrameId, Is.Null);
    }
}

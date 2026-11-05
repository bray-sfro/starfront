// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Collections.Concurrent;
using Microsoft.Data.Sqlite;
using NUnit.Framework;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Upload;

namespace RapidStackerCollab.Core.Tests;

public class UploadQueueTests
{
    private string dir = null!;
    private string db = null!;
    private FakeClock clock = null!;

    [SetUp]
    public void SetUp()
    {
        dir = Directory.CreateTempSubdirectory("rsc-queue-").FullName;
        db = Path.Combine(dir, "nested", "queue.db"); // the folder does not exist yet
        clock = new FakeClock();
    }

    [TearDown]
    public void TearDown()
    {
        SqliteConnection.ClearAllPools();
        TempDirs.Delete(dir);
    }

    private UploadQueue Open() => new(db, clock);

    private static NewQueueItem Item(string project, string name, object? ra = null) => new(project, "/data/" + name, name, FrameKind.Light,
        new InitHeader { Filter = "Ha", ImageTyp = "LIGHT", ExpTime = 300, Ra = ra ?? 85.25, ObjctRa = "05 41 00" }, "rig_9c1d4e7a02bb");

    [Test]
    public void Enqueue_is_idempotent_per_project_and_path_and_keeps_the_header()
    {
        using var q = Open();
        var (a, added) = q.Enqueue(Item("prj_a", "1.fits"));
        Assert.That(added);
        Assert.That(a.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(a.Kind, Is.EqualTo(FrameKind.Light));
        Assert.That(a.RigId, Is.EqualTo("rig_9c1d4e7a02bb"));
        var (again, addedAgain) = q.Enqueue(Item("prj_a", "1.fits"));
        Assert.That(addedAgain, Is.False);
        Assert.That(again.Id, Is.EqualTo(a.Id));
        Assert.That(q.Enqueue(Item("prj_b", "1.fits")).Added, Is.True, "the same file may go to another project");
        Assert.That(ApiJson.Serialize(a.Header), Is.EqualTo(ApiJson.Serialize(Item("prj_a", "1.fits").Header)));
    }

    [Test]
    public void Active_lists_the_item_in_flight_first_and_Finished_lists_the_newest_first()
    {
        using var q = Open();
        var ids = Enumerable.Range(1, 5).Select(i => q.Enqueue(Item("prj_a", $"{i}.fits")).Item.Id).ToList();
        var claimed = q.ClaimNext()!;
        Assert.That(claimed.Id, Is.EqualTo(ids[0]));
        q.MarkRetry(ids[1], clock.Now.AddMinutes(10), "network_error", "later"); // pending, but not due for a while

        Assert.That(q.Active().Select(i => i.Id), Is.EqualTo(new[] { ids[0], ids[2], ids[3], ids[4], ids[1] }));
        Assert.That(q.Active(2), Has.Count.EqualTo(2));

        clock.Advance(TimeSpan.FromSeconds(1));
        q.MarkUploaded(ids[0], "frm_1", "uploaded");
        clock.Advance(TimeSpan.FromSeconds(1));
        q.MarkFailed(ids[2], "x", "bad");
        clock.Advance(TimeSpan.FromSeconds(1));
        q.MarkDuplicate(ids[3], "frm_3", "accepted");
        Assert.That(q.Finished().Select(i => i.Id), Is.EqualTo(new[] { ids[3], ids[2], ids[0] }));
        Assert.That(q.Finished(1), Has.Count.EqualTo(1));
        Assert.That(q.Active().Select(i => i.Id), Is.EqualTo(new[] { ids[4], ids[1] }));
    }

    [Test]
    public void A_sexagesimal_ra_survives_the_database_round_trip()
    {
        using var q = Open();
        var (item, _) = q.Enqueue(Item("prj_a", "s.fits", ra: "05 35 17.3"));
        Assert.That(ApiJson.Serialize(q.Get(item.Id)!.Header), Does.Contain("\"RA\":\"05 35 17.3\""));
    }

    [Test]
    public void State_survives_closing_and_reopening_the_database()
    {
        long doneId, retryId, failedId, pendingId, uploadingId;
        using (var q = Open())
        {
            doneId = q.Enqueue(Item("prj_a", "done.fits")).Item.Id;
            retryId = q.Enqueue(Item("prj_a", "retry.fits")).Item.Id;
            failedId = q.Enqueue(Item("prj_a", "failed.fits")).Item.Id;
            pendingId = q.Enqueue(Item("prj_a", "pending.fits")).Item.Id;
            uploadingId = q.Enqueue(Item("prj_b", "mid.fits")).Item.Id;

            q.MarkUploaded(doneId, "frm_x", "uploaded");
            q.MarkRetry(retryId, clock.Now.AddMinutes(10), "rate_limited", "slow down");
            q.MarkFailed(failedId, "header_rejected", "no");
            q.PauseProject("prj_c", "Storage quota reached", ApiCodes.QuotaExceeded);
            q.PauseProject("prj_d", "until later", null, resumeAt: clock.Now.AddHours(2), byUser: true);
            // The oldest claimable rows are pending.fits then mid.fits (the others are done, not yet due, or failed).
            var first = q.ClaimNext()!;
            Assert.That(first.Id, Is.EqualTo(pendingId));
            Assert.That(q.ClaimNext()!.Id, Is.EqualTo(uploadingId));
            q.Release(first.Id);
            q.RecordProgress(uploadingId, new string('a', 64), 12345, "frm_mid");
        }

        using var again = Open();
        Assert.That(again.Get(doneId)!.State, Is.EqualTo(QueueStates.Uploaded));
        Assert.That(again.Get(doneId)!.FrameId, Is.EqualTo("frm_x"));
        var retry = again.Get(retryId)!;
        Assert.That(retry.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(retry.Attempts, Is.EqualTo(1));
        Assert.That(retry.LastErrorCode, Is.EqualTo("rate_limited"));
        Assert.That(retry.LastError, Is.EqualTo("slow down"));
        Assert.That(retry.NextAttemptAt, Is.EqualTo(clock.Now.AddMinutes(10)));
        Assert.That(again.Get(failedId)!.State, Is.EqualTo(QueueStates.Failed));
        Assert.That(again.Get(pendingId)!.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(again.ActivePauses().Select(p => p.ProjectRef), Is.EquivalentTo(new[] { "prj_c", "prj_d" }));
        Assert.That(again.ActivePauses().Single(p => p.ProjectRef == "prj_c").Code, Is.EqualTo(ApiCodes.QuotaExceeded));
        Assert.That(again.ActivePauses().Single(p => p.ProjectRef == "prj_d").ByUser);

        // The item that was mid-upload when the process died is still marked uploading ...
        var mid = again.Get(uploadingId)!;
        Assert.That(mid.State, Is.EqualTo(QueueStates.Uploading));
        // ... until recovery, which keeps what a resume needs.
        Assert.That(again.RecoverInterrupted(), Is.EqualTo(1));
        mid = again.Get(uploadingId)!;
        Assert.That(mid.State, Is.EqualTo(QueueStates.Pending));
        Assert.That(mid.Sha256, Is.EqualTo(new string('a', 64)));
        Assert.That(mid.FrameId, Is.EqualTo("frm_mid"));
        Assert.That(mid.Size, Is.EqualTo(12345));
        Assert.That(mid.Attempts, Is.Zero, "a crash is not a failed attempt");
    }

    [Test]
    public void Items_are_claimed_oldest_first_once_each()
    {
        using var q = Open();
        var ids = new List<long>();
        for (var i = 0; i < 4; i++)
        {
            ids.Add(q.Enqueue(Item("prj_a", $"{i}.fits")).Item.Id);
            clock.Advance(TimeSpan.FromSeconds(1));
        }
        Assert.That(Enumerable.Range(0, 4).Select(_ => q.ClaimNext()!.Id), Is.EqualTo(ids));
        Assert.That(q.ClaimNext(), Is.Null);
        Assert.That(q.Get(ids[0])!.State, Is.EqualTo(QueueStates.Uploading));
    }

    [Test]
    public void A_retry_is_not_claimed_before_it_is_due()
    {
        using var q = Open();
        var id = q.Enqueue(Item("prj_a", "a.fits")).Item.Id;
        q.ClaimNext();
        q.MarkRetry(id, clock.Now.AddMinutes(5), "rate_limited", "x");
        Assert.That(q.ClaimNext(), Is.Null);
        Assert.That(q.NextDueAt(), Is.EqualTo(clock.Now.AddMinutes(5)));
        clock.Advance(TimeSpan.FromMinutes(5));
        Assert.That(q.ClaimNext()!.Id, Is.EqualTo(id));
    }

    [Test]
    public void A_paused_project_is_skipped_while_other_projects_continue_and_a_timed_pause_lapses()
    {
        using var q = Open();
        var a = q.Enqueue(Item("prj_a", "a.fits")).Item.Id;
        clock.Advance(TimeSpan.FromSeconds(1));
        var b = q.Enqueue(Item("prj_b", "b.fits")).Item.Id;
        q.PauseProject("prj_a", "quota", ApiCodes.QuotaExceeded);

        Assert.That(q.ClaimNext()!.Id, Is.EqualTo(b));
        Assert.That(q.ClaimNext(), Is.Null, "a is paused");
        Assert.That(q.NextDueAt(), Is.Null, "an open-ended pause has no wake-up time");
        q.ResumeProject("prj_a");
        Assert.That(q.ClaimNext()!.Id, Is.EqualTo(a));

        q.Enqueue(Item("prj_a", "c.fits"));
        q.PauseProject("prj_a", "later", resumeAt: clock.Now.AddMinutes(30));
        Assert.That(q.ClaimNext(), Is.Null);
        Assert.That(q.NextDueAt(), Is.EqualTo(clock.Now.AddMinutes(30)));
        clock.Advance(TimeSpan.FromMinutes(31));
        Assert.That(q.ClaimNext(), Is.Not.Null, "the pause expired by itself");
        Assert.That(q.IsPaused("prj_a"), Is.False);
    }

    [Test]
    public void Concurrent_claimers_never_receive_the_same_item()
    {
        using var q = Open();
        for (var i = 0; i < 60; i++) q.Enqueue(Item("prj_a", $"{i:D3}.fits"));
        var got = new ConcurrentBag<long>();
        Parallel.For(0, 8, _ =>
        {
            while (q.ClaimNext() is { } c) got.Add(c.Id);
        });
        Assert.That(got, Has.Count.EqualTo(60));
        Assert.That(got.Distinct().Count(), Is.EqualTo(60));
    }

    [Test]
    public void Failed_and_cancelled_items_can_be_requeued_and_a_cancelled_file_is_not_queued_twice()
    {
        using var q = Open();
        var f = q.Enqueue(Item("prj_a", "f.fits")).Item.Id;
        var c = q.Enqueue(Item("prj_a", "c.fits")).Item.Id;
        q.ClaimNext(); q.ClaimNext();
        q.MarkFailed(f, "header_rejected", "bad");
        q.Cancel(c);
        Assert.That(q.Enqueue(Item("prj_a", "c.fits")).Added, Is.False, "a rescan must not resurrect a cancelled file");
        Assert.That(q.ClaimNext(), Is.Null);
        q.Requeue(f);
        q.Requeue(c);
        var item = q.Get(f)!;
        Assert.That((item.State, item.Attempts, item.LastError), Is.EqualTo((QueueStates.Pending, 0, (string?)null)));
        Assert.That(q.ClaimNext(), Is.Not.Null);
    }

    [Test]
    public void Counts_verdicts_and_purge()
    {
        using var q = Open();
        var a = q.Enqueue(Item("prj_a", "a.fits")).Item.Id;
        var b = q.Enqueue(Item("prj_a", "b.fits")).Item.Id;
        var c = q.Enqueue(Item("prj_a", "c.fits")).Item.Id;
        q.MarkUploaded(a, "frm_a", "uploaded");
        q.MarkDuplicate(b, "frm_b", "accepted");
        var counts = q.CountsByState();
        Assert.That(counts[QueueStates.Uploaded], Is.EqualTo(1));
        Assert.That(counts[QueueStates.Duplicate], Is.EqualTo(1));
        Assert.That(counts[QueueStates.Pending], Is.EqualTo(1));
        Assert.That(counts[QueueStates.Failed], Is.Zero);

        Assert.That(q.AwaitingVerdict().Select(i => i.Id), Is.EqualTo(new[] { a }), "b already has a final verdict");
        q.SetVerdict(a, "rejected", "no_solution");
        Assert.That(q.AwaitingVerdict(), Is.Empty);
        Assert.That(q.Get(a)!.VerdictDetail, Is.EqualTo("no_solution"));

        clock.Advance(TimeSpan.FromDays(40));
        Assert.That(q.Purge(clock.Now.AddDays(-30)), Is.EqualTo(2));
        Assert.That(q.Get(c), Is.Not.Null, "pending rows are never purged");
        Assert.That(q.Get(a), Is.Null);
    }

    [Test]
    public void Lights_of_another_target_are_counted_by_rig_and_night_once_per_file_and_forgotten_with_the_history()
    {
        using var q = Open();
        q.AddOtherTargetLight("/d/bubble1.fits", "rig_1", "HA", "2026-10-20", "Bubble Nebula");
        q.AddOtherTargetLight("/d/bubble1.fits", "rig_1", "HA", "2026-10-20", "Bubble Nebula");
        q.AddOtherTargetLight("/d/bubble2.fits", "rig_1", "OIII", "2026-10-20", null);
        q.AddOtherTargetLight("/d/bubble3.fits", null, "HA", "2026-10-20", null);
        Assert.That(q.CountOtherTargetLights("rig_1", "2026-10-20"), Is.EqualTo(2), "any filter, once per path");
        Assert.That(q.CountOtherTargetLights("rig_1", "2026-10-21"), Is.Zero);
        Assert.That(q.CountOtherTargetLights(null, "2026-10-20"), Is.EqualTo(1), "no rig is a rig of its own");

        clock.Advance(TimeSpan.FromDays(40));
        q.Purge(clock.Now.AddDays(-30));
        Assert.That(q.CountOtherTargetLights("rig_1", "2026-10-20"), Is.Zero);
    }

    [Test]
    public void List_filters_by_state_and_project()
    {
        using var q = Open();
        q.Enqueue(Item("prj_a", "1.fits"));
        q.Enqueue(Item("prj_b", "2.fits"));
        var c = q.ClaimNext()!;
        Assert.That(q.List(QueueStates.Uploading).Select(i => i.Id), Is.EqualTo(new[] { c.Id }));
        Assert.That(q.List(projectRef: "prj_b"), Has.Count.EqualTo(1));
        Assert.That(q.List(limit: 1), Has.Count.EqualTo(1));
    }

    [Test]
    public void A_database_from_a_newer_plugin_is_refused_rather_than_damaged()
    {
        using (var q = Open()) { }
        using (var c = new SqliteConnection($"Data Source={db};Pooling=False"))
        {
            c.Open();
            using var cmd = c.CreateCommand();
            cmd.CommandText = "UPDATE meta SET value = '99' WHERE key = 'schema_version'";
            cmd.ExecuteNonQuery();
        }
        Assert.Throws<InvalidOperationException>(() => Open());
    }

    private void CreateV1Database(params (string Project, string Path, string Kind, string Header)[] rows)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(db)!);
        using var c = new SqliteConnection($"Data Source={db};Pooling=False");
        c.Open();
        using var cmd = c.CreateCommand();
        cmd.CommandText = """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta (key, value) VALUES ('schema_version', '1');
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_ref TEXT NOT NULL, path TEXT NOT NULL, filename TEXT NOT NULL,
                frame_kind TEXT NOT NULL, header_json TEXT NOT NULL, rig_id TEXT NULL, size INTEGER NULL, sha256 TEXT NULL, frame_id TEXT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NULL, last_error_code TEXT NULL,
                next_attempt_at INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                uploaded_at INTEGER NULL, verdict TEXT NULL, verdict_detail TEXT NULL, UNIQUE (project_ref, path));
            CREATE INDEX items_due ON items (state, next_attempt_at);
            CREATE TABLE project_pauses (project_ref TEXT PRIMARY KEY, reason TEXT NOT NULL, code TEXT NULL, paused_at INTEGER NOT NULL,
                resume_at INTEGER NULL, by_user INTEGER NOT NULL DEFAULT 0);
            """;
        cmd.ExecuteNonQuery();
        foreach (var (project, path, kind, header) in rows)
        {
            using var ins = c.CreateCommand();
            ins.CommandText = "INSERT INTO items (project_ref, path, filename, frame_kind, header_json, rig_id, state, created_at, updated_at) " +
                              "VALUES ($p, $path, 'f.fits', $k, $h, 'rig_1', 'uploaded', 1, 1)";
            ins.Parameters.AddWithValue("$p", project);
            ins.Parameters.AddWithValue("$path", path);
            ins.Parameters.AddWithValue("$k", kind);
            ins.Parameters.AddWithValue("$h", header);
            ins.ExecuteNonQuery();
        }
    }

    [Test]
    public void A_v1_database_migrates_to_v2_keeping_every_row_and_derives_the_flat_keys_from_the_headers()
    {
        CreateV1Database(
            ("prj_a", "/d/1.fits", "Light", """{"FILTER":"Ha","IMAGETYP":"LIGHT","DATE-OBS":"2026-10-14T03:00:00"}"""),
            ("prj_b", "/d/2.fits", "Light", """{"FILTER":" HA ","IMAGETYP":"LIGHT","DATE-OBS":"2026-10-14T04:00:00"}"""),
            ("prj_a", "/d/3.fits", "Light", """{"FILTER":"OIII","IMAGETYP":"LIGHT","DATE-OBS":"2026-10-14T03:30:00"}"""),
            ("prj_a", "/d/4.fits", "Light", """{"FILTER":"Ha","IMAGETYP":"LIGHT"}"""),
            ("prj_a", "/d/dark.fits", "Dark", """{"FILTER":"Ha","IMAGETYP":"DARK","DATE-OBS":"2026-10-14T03:00:00"}"""));

        using var q = Open();
        Assert.That(q.List(limit: 100), Has.Count.EqualTo(5), "no row is lost");
        Assert.That(q.List(projectRef: "prj_a", limit: 100).Select(i => i.Path), Is.EquivalentTo(new[] { "/d/1.fits", "/d/3.fits", "/d/4.fits", "/d/dark.fits" }));
        Assert.That(q.CountHeldFlats(), Is.Zero, "flat_routes exists and is empty");

        // UTC-8: 03:00 and 04:00 UTC on 14 Oct are the evening of 13 Oct, one night.
        var tz = TimeZoneInfo.CreateCustomTimeZone("utc-8", TimeSpan.FromHours(-8), "UTC-8", "UTC-8");
        Assert.That(q.LightProjects("rig_1", "ha", "2026-10-13", tz), Is.EqualTo(new[] { "prj_a", "prj_b" }), "derived lazily, filter trimmed and case-folded");
        Assert.That(q.LightProjects("rig_1", "oiii", "2026-10-13", tz), Is.EqualTo(new[] { "prj_a" }));
        Assert.That(q.LightProjects("rig_other", "ha", "2026-10-13", tz), Is.Empty);
        Assert.That(q.LightProjects("rig_1", "ha", "2026-10-14", tz), Is.Empty);
        Assert.That(q.LightNights("rig_1", "ha", tz).Select(n => n.Night).Distinct(), Is.EqualTo(new[] { "2026-10-13" }), "the light without DATE-OBS has no night");
    }

    [Test]
    public void A_migrated_database_reopens_as_v2_and_new_rows_carry_their_keys()
    {
        CreateV1Database(("prj_a", "/d/1.fits", "Light", """{"FILTER":"Ha","DATE-OBS":"2026-10-14T03:00:00"}"""));
        using (var first = Open()) { }
        using var q = Open();
        q.Enqueue(Item("prj_a", "new.fits") with { NightLocal = "2026-10-13", FilterKey = "ha" });
        var tz = TimeZoneInfo.Utc;
        Assert.That(q.LightProjects("rig_9c1d4e7a02bb", "ha", "2026-10-13", tz), Is.EqualTo(new[] { "prj_a" }));
        Assert.That(q.List(limit: 10), Has.Count.EqualTo(2));
    }

    [Test]
    public void Flat_routes_are_recorded_once_per_path_and_counted_while_held()
    {
        using var q = Open();
        var h = new InitHeader { Filter = "Ha", ImageTyp = "FLAT" };
        var a = q.AddFlatRoute("/f/a.fits", h, "rig_1", "ha", "2026-10-13", clock.Now.AddHours(10));
        Assert.That(a.State, Is.EqualTo(FlatRouteStates.Held));
        q.AddFlatRoute("/f/a.fits", h, "rig_1", "ha", "2026-10-13", clock.Now.AddHours(99));
        q.AddFlatRoute("/f/b.fits", h, "rig_1", "oiii", "2026-10-13", clock.Now.AddHours(10));
        Assert.That(q.CountHeldFlats(), Is.EqualTo(2));
        Assert.That(q.GetFlatRoute("/f/a.fits")!.HoldUntil, Is.EqualTo(a.HoldUntil), "the first hold stands");
        q.SetFlatRoute("/f/a.fits", FlatRouteStates.Routed, new[] { "prj_a" }, "why");
        Assert.That(q.CountHeldFlats(), Is.EqualTo(1));
        Assert.That(q.FlatsMatching("rig_1", "ha", "2026-10-13").Single().RoutedTo, Is.EqualTo(new[] { "prj_a" }));
        Assert.That(q.FlatsMatching("rig_1", "oiii", "2026-10-13").Single().State, Is.EqualTo(FlatRouteStates.Held));
    }

    [Test]
    public void The_folder_lookup_finds_lights_below_a_folder_on_either_separator_style()
    {
        using var q = Open();
        q.Enqueue(Item("prj_a", "x.fits") with { });
        q.Enqueue(new NewQueueItem("prj_b", @"D:\Astro\M42\2026-10-13\LIGHT\a.fits", "a.fits", FrameKind.Light, new InitHeader { Filter = "Ha" }, "rig_1", "", "ha"));
        Assert.That(q.LightProjectsUnder(@"D:\Astro\M42\2026-10-13"), Is.EqualTo(new[] { "prj_b" }));
        Assert.That(q.LightProjectsUnder(@"d:\astro\m42\2026-10-13\"), Is.EqualTo(new[] { "prj_b" }), "case-insensitive, trailing separator ignored");
        Assert.That(q.LightProjectsUnder(@"D:\Astro\M42\2026-10-1"), Is.Empty, "a prefix of a name is not a parent");
        Assert.That(q.LightProjectsUnder("/data"), Is.EqualTo(new[] { "prj_a" }));
    }

    [Test]
    public void The_database_file_is_released_on_dispose_so_the_folder_can_be_deleted()
    {
        var q = Open();
        q.Enqueue(Item("prj_a", "a.fits"));
        q.Dispose();
        Assert.DoesNotThrow(() => File.Delete(db));
    }
}

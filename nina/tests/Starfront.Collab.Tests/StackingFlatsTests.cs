using Microsoft.Data.Sqlite;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Frames;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Upload;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// Where a flat goes. A flat that misses its lights leaves them
    /// uncorrected (dust rings, vignetting in the stack); a flat sent to a
    /// project that never got that night's lights costs the person quota for
    /// nothing. The PC's zone here is UTC-8 and the clock 2026-10-14 02:00 UTC
    /// (18:00 local on the 13th), so lights at 05:00 UTC and flats at 01:30
    /// UTC (evening) or 15:00 UTC (next morning) are all the night of
    /// 2026-10-13, and an evening flat's wait ends at 2026-10-14 21:00 UTC.
    /// </summary>
    public class StackingFlatsTests {
        private const string EveningFlat = "2026-10-14T01:30:00";
        private const string NightLight = "2026-10-14T05:00:00";
        private const string MorningFlat = "2026-10-14T15:00:00";
        private const string Rig = "rig_1";
        private const string M33 = "prj_m33";
        private const string Veil = "prj_veil";
        private static readonly DateTimeOffset HoldEnds = new(2026, 10, 14, 21, 0, 0, TimeSpan.Zero);
        private static readonly TimeZoneInfo Zone = TimeZoneInfo.CreateCustomTimeZone("test-utc-8", TimeSpan.FromHours(-8), "UTC-8", "UTC-8");

        private string dir;
        private FakeClock clock;
        private UploadQueue queue;
        private Projects projects;
        private StackingFlats flats;
        private int counter;

        /// <summary>The links, as a table: which projects take flats of which filters, which target names are whose, the last light's project.</summary>
        private sealed class Projects : IFlatProjects {
            public readonly Dictionary<string, string> Names = new() { [M33] = "M33", [Veil] = "Veil" };
            public readonly HashSet<(string Project, string Filter)> Skipped = new();
            public readonly HashSet<string> NoCalibration = new();
            public readonly Dictionary<string, string> Targets = new(StringComparer.Ordinal);
            public string LastProject { get; set; }

            public string Eligible(string collabProjectId, string rawFilter) =>
                Names.TryGetValue(collabProjectId, out var name) && !NoCalibration.Contains(collabProjectId) && !Skipped.Contains((collabProjectId, rawFilter)) ? name : null;

            public string ProjectOfTarget(string objectName) => objectName != null && Targets.TryGetValue(objectName, out var p) ? p : null;
        }

        [SetUp]
        public void SetUp() {
            dir = Directory.CreateTempSubdirectory("sf-flats-").FullName;
            clock = new FakeClock(new DateTimeOffset(2026, 10, 14, 2, 0, 0, TimeSpan.Zero));
            queue = new UploadQueue(Path.Combine(dir, "queue.db"), clock);
            projects = new Projects();
            flats = new StackingFlats(queue, projects, () => Rig, clock, Zone);
        }

        [TearDown]
        public void TearDown() {
            queue.Dispose();
            SqliteConnection.ClearAllPools();
            try { Directory.Delete(dir, true); } catch (IOException) { }
        }

        private string Write(string imageTyp, string filter, string dateObs, string obj = null, string folder = null) {
            var cards = new List<string> {
                FitsBuilder.Str("IMAGETYP", imageTyp), FitsBuilder.Num("EXPTIME", imageTyp == "LIGHT" ? 300 : 2),
                FitsBuilder.Str("DATE-OBS", dateObs), FitsBuilder.Str("FILTER", filter),
            };
            if (obj != null) cards.Add(FitsBuilder.Str("OBJECT", obj));
            var where = folder ?? Path.Combine(dir, "frames");
            Directory.CreateDirectory(where);
            return new FitsBuilder().Primary(cards, 16, new[] { 8, 6 }).WriteTo(where, $"{++counter}-{imageTyp.ToLowerInvariant()}.fits");
        }

        /// <summary>Queue a light for a project as the intake does, and tell the router.</summary>
        private void Light(string project, string filter = "Ha", string dateObs = NightLight, string folder = null) {
            var path = Write("LIGHT", filter, dateObs, "M33", folder);
            var header = FitsHeaderReader.Read(path);
            var r = queue.Enqueue(new NewQueueItem(project, path, Path.GetFileName(path), FrameKind.Light, header, Rig,
                                                   FlatRouting.NightOf(header, Zone) ?? "", FlatRouting.FilterKey(header.Filter)));
            flats.OnLightEnqueued(r.Item);
        }

        /// <summary>A light of another target that night: not queued, only remembered, as the intake does.</summary>
        private void OtherTargetLight(string filter, string dateObs = NightLight) {
            var path = Write("LIGHT", filter, dateObs, "Bubble Nebula");
            var header = FitsHeaderReader.Read(path);
            queue.AddOtherTargetLight(path, Rig, FlatRouting.FilterKey(filter), FlatRouting.NightOf(header, Zone), header.Object);
        }

        private FlatPlan Flat(string path) => flats.OnFlat(path, FitsHeaderReader.Read(path), immediate: false);

        private IReadOnlyList<QueueItem> FlatRows() => queue.List(limit: 1000).Where(i => i.Kind == FrameKind.Flat).ToList();

        private string[] FlatProjects() => FlatRows().Select(i => i.ProjectRef).Order().ToArray();

        /// <summary>Hold the flat, let its wait run out, and say where it went.</summary>
        private string[] AfterTheWait(string path) {
            Assert.That(Flat(path).Kind, Is.EqualTo(FlatPlanKind.Hold), "held first");
            Assert.That(flats.Tick(HoldEnds - TimeSpan.FromSeconds(1)), Is.Zero, "not yet");
            clock.Now = HoldEnds;
            Assert.That(flats.Tick(clock.Now), Is.EqualTo(1));
            return FlatProjects();
        }

        [Test]
        public void An_evening_flat_waits_for_the_nights_lights_and_goes_only_to_the_projects_that_got_its_filter() {
            var ha = Write("FLAT", "Ha", EveningFlat);
            var oiii = Write("FLAT", "OIII", EveningFlat);
            Assert.That(Flat(ha).Kind, Is.EqualTo(FlatPlanKind.Hold));
            Assert.That(Flat(oiii).Kind, Is.EqualTo(FlatPlanKind.Hold));
            Assert.That(flats.Waiting, Is.EqualTo(2));
            Assert.That(FlatRows(), Is.Empty, "no lights yet: nothing queued");

            var queued = 0;
            flats.Enqueued += _ => queued++;
            Light(M33);
            Assert.That(FlatProjects(), Is.EqualTo(new[] { M33 }));
            Assert.That(FlatRows().Single().Path, Is.EqualTo(ha));
            Assert.That(queued, Is.EqualTo(1), "the runner is woken");
            Assert.That(flats.Waiting, Is.EqualTo(1), "the OIII flat still waits");

            Light(Veil);
            Assert.That(FlatProjects(), Is.EqualTo(new[] { M33, Veil }));
            Assert.That(FlatRows().All(r => r.RigId == Rig && r.Path == ha), Is.True);
        }

        [Test]
        public void A_morning_flat_goes_at_once_to_every_project_that_got_the_nights_lights_and_only_once() {
            Light(M33);
            Light(Veil);
            var flat = Write("FLAT", "Ha", MorningFlat);
            var plan = Flat(flat);
            Assert.That(plan.Kind, Is.EqualTo(FlatPlanKind.Route));
            Assert.That(plan.Reason, Does.Contain("M33").And.Contain("Veil"));
            Assert.That(FlatProjects(), Is.EqualTo(new[] { M33, Veil }));
            Flat(flat);
            Light(M33);
            Assert.That(FlatRows(), Has.Count.EqualTo(2));
            Assert.That(queue.GetFlatRoute(flat).State, Is.EqualTo(FlatRouteStates.Routed));
        }

        [Test]
        public void Lights_of_another_night_filter_or_rig_do_not_take_a_flat() {
            Flat(Write("FLAT", "Ha", EveningFlat));
            Light(M33, dateObs: "2026-10-16T05:00:00");
            Light(M33, filter: "OIII");
            Assert.That(FlatRows(), Is.Empty);
            Assert.That(flats.Waiting, Is.EqualTo(1));
        }

        [Test]
        public void The_flats_of_a_night_spent_on_another_target_stay_here() {
            projects.LastProject = M33;
            Light(M33, dateObs: "2026-10-13T05:00:00");      // the night before: the nearest-night fallback would have taken it there
            OtherTargetLight("Ha");
            var flat = Write("FLAT", "Ha", EveningFlat, folder: Path.Combine(dir, "elsewhere", "FLAT"));
            Assert.That(AfterTheWait(flat), Is.Empty);
            var route = queue.GetFlatRoute(flat);
            Assert.That(route.State, Is.EqualTo(FlatRouteStates.Skipped));
            Assert.That(route.Detail, Does.Contain("another target"));
        }

        [Test]
        public void On_a_night_split_between_targets_each_filter_follows_its_own_lights() {
            projects.LastProject = M33;
            Light(M33, filter: "Ha");
            OtherTargetLight("OIII");
            var ha = Write("FLAT", "Ha", MorningFlat);
            var oiii = Write("FLAT", "OIII", MorningFlat);
            Assert.That(Flat(ha).Kind, Is.EqualTo(FlatPlanKind.Route));
            Assert.That(AfterTheWait(oiii), Is.EqualTo(new[] { M33 }), "only the Ha flat went");
            Assert.That(FlatRows().Single().Path, Is.EqualTo(ha));
            Assert.That(queue.GetFlatRoute(oiii).State, Is.EqualTo(FlatRouteStates.Skipped));
        }

        [Test]
        public void Lights_of_another_target_on_another_night_leave_the_fallbacks_alone() {
            projects.LastProject = Veil;
            OtherTargetLight("Ha", dateObs: "2026-10-16T05:00:00");
            Assert.That(AfterTheWait(Write("FLAT", "Ha", EveningFlat, folder: Path.Combine(dir, "elsewhere", "FLAT"))), Is.EqualTo(new[] { Veil }));
        }

        [Test]
        public void When_the_wait_ends_the_same_folder_beats_the_target_name_the_nearest_night_and_the_last_light() {
            var night = Path.Combine(dir, "M33", "2026-10-13");
            Light(Veil, filter: "OIII", folder: Path.Combine(night, "LIGHT"));   // another filter, so the night does not match
            projects.Targets["M33"] = M33;
            projects.LastProject = M33;
            Assert.That(AfterTheWait(Write("FLAT", "Ha", EveningFlat, obj: "M33", folder: Path.Combine(night, "FLAT"))), Is.EqualTo(new[] { Veil }));
        }

        [Test]
        public void When_the_wait_ends_the_target_name_beats_the_nearest_night_and_the_last_light() {
            Light(M33, dateObs: "2026-10-13T05:00:00");
            projects.Targets["Veil Nebula"] = Veil;
            projects.LastProject = M33;
            Assert.That(AfterTheWait(Write("FLAT", "Ha", EveningFlat, obj: "Veil Nebula", folder: Path.Combine(dir, "elsewhere", "FLAT"))), Is.EqualTo(new[] { Veil }));
        }

        [Test]
        public void When_the_wait_ends_the_nearest_night_within_three_beats_the_last_light() {
            Light(Veil, dateObs: "2026-10-13T05:00:00");                       // one night away
            Light(M33, dateObs: "2026-10-17T05:00:00");                        // three nights away
            projects.LastProject = M33;
            Assert.That(AfterTheWait(Write("FLAT", "Ha", EveningFlat, folder: Path.Combine(dir, "elsewhere", "FLAT"))), Is.EqualTo(new[] { Veil }));
        }

        [Test]
        public void When_nothing_else_answers_the_flat_follows_the_last_light_sent_and_with_no_last_light_it_stays_here() {
            Light(Veil, dateObs: "2026-10-18T05:00:00");                       // four nights away: too far
            projects.LastProject = M33;
            Assert.That(AfterTheWait(Write("FLAT", "Ha", EveningFlat, folder: Path.Combine(dir, "elsewhere", "FLAT"))), Is.EqualTo(new[] { M33 }));

            clock.Now = new DateTimeOffset(2026, 10, 14, 2, 0, 0, TimeSpan.Zero);
            projects.LastProject = null;
            var lonely = Write("FLAT", "SII", EveningFlat, folder: Path.Combine(dir, "elsewhere", "FLAT"));
            Flat(lonely);
            clock.Now = HoldEnds;
            flats.Tick(clock.Now);
            Assert.That(queue.GetFlatRoute(lonely).State, Is.EqualTo(FlatRouteStates.Skipped));
        }

        [Test]
        public void A_project_that_takes_no_calibration_or_skips_the_filter_is_not_given_the_flat() {
            projects.NoCalibration.Add(Veil);
            projects.Skipped.Add((M33, "Ha"));
            Light(M33);
            Light(Veil);
            var plan = Flat(Write("FLAT", "Ha", MorningFlat));
            Assert.That(plan.Kind, Is.EqualTo(FlatPlanKind.Skip));
            Assert.That(FlatRows(), Is.Empty);
        }

        [Test]
        public void A_flat_without_DATE_OBS_does_not_wait() {
            projects.LastProject = M33;
            Directory.CreateDirectory(Path.Combine(dir, "frames"));
            var path = new FitsBuilder().Primary(new[] { FitsBuilder.Str("IMAGETYP", "FLAT"), FitsBuilder.Str("FILTER", "Ha") }, 16, new[] { 8, 6 })
                .WriteTo(Path.Combine(dir, "frames"), "nodate.fits");
            Assert.That(Flat(path).Kind, Is.EqualTo(FlatPlanKind.Route));
            Assert.That(FlatProjects(), Is.EqualTo(new[] { M33 }));
        }
    }
}

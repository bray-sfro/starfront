using Microsoft.Data.Sqlite;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Tests.Support;
using RapidStackerCollab.Core.Upload;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// The calibration library kept in step with a folder, against the
    /// upload engine's fake Rapid Stacker Collab server, its real queue and
    /// its real runner. What matters: only what the server lacks goes up, a
    /// second pass sends nothing, nothing happens without a rig, a folder and
    /// a sign-in, and a missing folder is a problem to show, not a crash.
    /// </summary>
    public class StackingLibraryTests {
        private const string Rig = FakeCollabServer.RigId;

        private sealed class Settings : ILibrarySettings {
            public string RigId { get; set; } = Rig;
            public string LibraryFolder { get; set; } = "";
            public int LibrarySyncHours { get; set; } = 24;
            public bool LibraryPreferRaw { get; set; }
        }

        private FakeCollabServer server;
        private FakeClock clock;
        private HttpClient http;
        private UploadQueue queue;
        private Settings settings;
        private StackingLibrary library;
        private string dir;
        private string folder;
        private bool signedIn = true;
        private int seed;

        [SetUp]
        public async Task SetUp() {
            server = await FakeCollabServer.StartAsync();
            server.PartSize = 4 * 1024;
            server.RigWidthPx = 64;
            server.RigHeightPx = 48;
            clock = new FakeClock();
            http = new HttpClient();
            dir = Directory.CreateTempSubdirectory("sf-library-").FullName;
            folder = Path.Combine(dir, "calibration");
            Directory.CreateDirectory(folder);
            queue = new UploadQueue(Path.Combine(dir, "queue.db"), clock);
            settings = new Settings { LibraryFolder = folder };
            signedIn = true;
            library = new StackingLibrary(Api, queue, settings, clock, () => signedIn, Path.Combine(dir, "tmp"));
        }

        [TearDown]
        public async Task TearDown() {
            queue.Dispose();
            SqliteConnection.ClearAllPools();
            http.Dispose();
            await server.DisposeAsync();
            try { Directory.Delete(dir, true); } catch (IOException) { }
        }

        private IApiClient Api() => new ApiClient(http, new InMemoryTokenStore(FakeCollabServer.Token), new ApiClientOptions { BaseUri = server.ApiBase });

        private UploadRunner Runner() => new(queue, new UploadEngine(Api(), http, clock), new UploadPolicy(clock),
            () => new UploadPolicySettings(UploadMode.Immediately), () => new ImagingState(), clock,
            new UploadRunnerOptions { TempDirectory = Path.Combine(dir, "tmp"), StripPredicate = _ => true, StripLocation = true });

        private void Calib(string name, string type = "Dark", double exposure = 120) {
            FitsBuilder.Calibration(type, exposure, ++seed).WriteTo(folder, name);
            File.SetLastWriteTimeUtc(Path.Combine(folder, name), (clock.Now - TimeSpan.FromMinutes(10)).UtcDateTime);
        }

        [Test]
        public async Task Only_what_the_server_lacks_goes_up_and_a_second_pass_sends_nothing() {
            for (var i = 1; i <= 5; i++) Calib($"d{i}.fits");
            Calib("master_bias.fits", "Master Bias", 0);
            Calib("light.fits", "Light", 300);
            var woken = 0;
            library.Queued += () => woken++;

            var first = await library.SyncAsync();
            Assert.That(first.State, Is.EqualTo(LibrarySyncState.Done), first.Message);
            Assert.That(first.Queued, Is.EqualTo(6), "five raw darks and the master bias");
            Assert.That(first.Ignored, Is.EqualTo(1), "the light");
            Assert.That(woken, Is.EqualTo(1), "the runner is woken");
            Assert.That(queue.LibraryCounts(Rig)[QueueStates.Pending], Is.EqualTo(6));
            Assert.That(await Runner().RunOnceAsync(), Is.EqualTo(6));
            Assert.That(server.LibraryFrames.Count(f => f.Status == "ready"), Is.EqualTo(6));

            var second = await library.SyncAsync();
            Assert.That(second.Queued, Is.Zero);
            Assert.That(second.OnServer, Is.EqualTo(6));
            Assert.That(second.Message, Is.EqualTo("Your calibration library is up to date."));
            Assert.That(library.DetailText, Does.Contain("6 already on the server"));
            Assert.That(library.DockText(), Does.StartWith("Calibration library: up to date"));
        }

        [Test]
        public async Task Nothing_happens_without_a_rig_a_folder_or_a_sign_in() {
            Calib("d1.fits");
            settings.RigId = "";
            Assert.That((await library.SyncAsync()).State, Is.EqualTo(LibrarySyncState.NotConfigured));
            Assert.That(library.StatusText, Does.Contain("rig"));
            settings.RigId = Rig;
            settings.LibraryFolder = "";
            Assert.That((await library.SyncAsync()).State, Is.EqualTo(LibrarySyncState.NotConfigured));
            Assert.That(library.StatusText, Does.Contain("folder"));
            settings.LibraryFolder = folder;
            signedIn = false;
            Assert.That((await library.SyncAsync()).State, Is.EqualTo(LibrarySyncState.NotConfigured));
            Assert.That(library.StatusText, Does.Contain("Sign in"));
            Assert.That(queue.List(limit: 10), Is.Empty);
            Assert.That(server.RequestsTo("GET", $"/api/rigs/{Rig}/library"), Is.Empty);
        }

        [Test]
        public async Task A_missing_folder_is_a_problem_to_show_and_not_an_exception() {
            settings.LibraryFolder = Path.Combine(dir, "not-there");
            var status = await library.SyncAsync();
            Assert.That(status.State, Is.EqualTo(LibrarySyncState.Problem));
            Assert.That(library.StatusText, Does.Contain("does not exist"));
        }

        [Test]
        public async Task A_master_wins_over_the_raw_frames_and_prefer_raw_turns_that_around() {
            for (var i = 1; i <= 3; i++) Calib($"d{i}.fits");
            Calib("master_dark.fits", "Master Dark");
            Assert.That((await library.SyncAsync()).Queued, Is.EqualTo(1), "the master alone");
            settings.LibraryPreferRaw = true;
            library.SettingsChanged();
            Assert.That((await library.SyncAsync()).Queued, Is.EqualTo(3), "the raw darks too, the master already queued");
        }
    }
}

using System.IO;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Net.Http;
using System.Security.Cryptography;
using Newtonsoft.Json.Linq;
using Starfront.Collab.Server;
using Starfront.Collab.Settings;
using Starfront.Collab.Upload;
using Xunit;

namespace Starfront.Collab.Tests {

    /// <summary>The server's resumable agent API, in memory, with switches for failures.</summary>
    internal sealed class FakeServer : IUploadWire {
        public readonly ConcurrentDictionary<string, (string Name, string Sha, long Bytes, MemoryStream Data)> Sessions = new();
        public readonly ConcurrentDictionary<string, (byte[] Data, DateTime At)> Files = new();
        public readonly ConcurrentQueue<string> Calls = new();
        public WireAnswer? ForceStart;
        public int FailPutNumber = -1;
        public JObject LastStart;
        private int puts;
        private int next;

        public async Task<WireAnswer> SendAsync(HttpMethod method, string path, HttpContent content, CancellationToken token) {
            Calls.Enqueue($"{method} {path}");
            if (method == HttpMethod.Post && path == "/api/v1/agent/uploads") {
                if (ForceStart is WireAnswer forced) return forced;
                var body = JObject.Parse(await content.ReadAsStringAsync(token));
                LastStart = body;
                var sha = body.Value<string>("sha256");
                if (Files.Values.Any(f => Hex(f.Data) == sha)) return Ok(new JObject { ["already"] = true, ["status"] = "ready" });
                foreach (var (sid, s) in Sessions) if (s.Sha == sha) return Ok(new JObject { ["already"] = false, ["id"] = sid, ["offset"] = s.Data.Length, ["chunk"] = 100_000 });
                var id = $"s{Interlocked.Increment(ref next)}";
                Sessions[id] = (body.Value<string>("name"), sha, body.Value<long>("bytes"), new MemoryStream());
                return Ok(new JObject { ["already"] = false, ["id"] = id, ["offset"] = 0, ["chunk"] = 100_000 });
            }
            if (method == HttpMethod.Put) {
                var n = Interlocked.Increment(ref puts);
                if (n == FailPutNumber) return new WireAnswer(0, new JObject { ["detail"] = "connection reset" }, null);
                var parts = path.Split('/').Last().Split("?offset=");
                var s = Sessions[parts[0]];
                if (long.Parse(parts[1]) != s.Data.Length) return new WireAnswer(409, new JObject { ["offset"] = s.Data.Length }, null);
                var bytes = await content.ReadAsByteArrayAsync(token);
                s.Data.Write(bytes);
                return Ok(new JObject { ["offset"] = s.Data.Length });
            }
            if (path.EndsWith("/finish")) {
                var sid = path.Split('/')[^2];
                Sessions.TryRemove(sid, out var s);
                var data = s.Data.ToArray();
                if (Hex(data) != s.Sha) return new WireAnswer(422, new JObject { ["detail"] = "sha" }, null);
                Files[s.Name] = (data, DateTime.UtcNow);
                return Ok(new JObject { ["name"] = s.Name, ["status"] = "pending" });
            }
            return new WireAnswer(404, new JObject(), null);
        }

        private static WireAnswer Ok(JObject body) => new(200, body, null);
        public static string Hex(byte[] data) => Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
    }

    public sealed class LightUploaderTests : IDisposable {
        private readonly string root = Path.Combine(Path.GetTempPath(), "sf-upload-tests-" + Guid.NewGuid().ToString("N"));

        public LightUploaderTests() {
            Directory.CreateDirectory(root);
            LightUploader.SettleTime = TimeSpan.FromMilliseconds(300);
        }

        public void Dispose() { try { Directory.Delete(root, true); } catch (Exception) { } }

        private string Light(string name, int size = 350_000) {
            var path = Path.Combine(root, name);
            var data = new byte[size];
            Random.Shared.NextBytes(data);
            File.WriteAllBytes(path, data);
            return path;
        }

        private static void Until(Func<bool> done, double seconds = 10) {
            var clock = Stopwatch.StartNew();
            while (!done() && clock.Elapsed.TotalSeconds < seconds) Thread.Sleep(25);
        }

        [Fact]
        public void Upload_is_on_by_default() {
            Assert.True(StarfrontSettings.AutoUploadDefault);
        }

        [Fact]
        public void Each_saved_light_reaches_the_server_within_seconds_of_its_save() {
            var server = new FakeServer();
            using var up = new LightUploader(Path.Combine(root, "q"), () => true, server);
            up.Start();
            Thread.Sleep(300);   // idle worker, waiting on its wake event
            var lags = new List<double>();
            var enqueueMs = new List<double>();
            for (var n = 0; n < 3; n++) {
                var path = Light($"M42_G_300s_{n:0000}.fits");
                var saved = DateTime.UtcNow;
                var clock = Stopwatch.StartNew();
                Assert.True(up.Enqueue(path, "p1", "t1"));
                enqueueMs.Add(clock.Elapsed.TotalMilliseconds);
                var name = Path.GetFileName(path);
                Until(() => server.Files.ContainsKey(name));
                Assert.True(server.Files.ContainsKey(name), $"{name} did not arrive");
                lags.Add((server.Files[name].At - saved).TotalSeconds);
                Assert.Equal(File.ReadAllBytes(path), server.Files[name].Data);
            }
            Assert.All(lags, lag => Assert.True(lag < 3.0, $"arrived {lag:0.0} s after its save"));
            Assert.All(enqueueMs, ms => Assert.True(ms < 200, $"the save hook took {ms:0} ms"));
            // Only the upload calls: nothing scans a folder or asks the server for a list.
            Assert.All(server.Calls, c => Assert.StartsWith(c.StartsWith("PUT") ? "PUT /api/v1/agent/uploads/" : "POST /api/v1/agent/uploads", c));
        }

        [Fact]
        public void A_dropped_chunk_is_retried_and_resumes_at_the_servers_offset() {
            var server = new FakeServer { FailPutNumber = 3 };
            var path = Light("M42_R_0001.fits", 450_000);
            var before = (FakeServer.Hex(File.ReadAllBytes(path)), File.GetLastWriteTimeUtc(path));
            Thread.Sleep(400);
            using var up = new LightUploader(Path.Combine(root, "q"), () => true, server);
            up.Enqueue(path, "p1", "t1");
            up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
            var item = up.Snapshot().Single();
            Assert.Equal("retry", item.State);
            Assert.Contains("connection reset", item.Error);
            Assert.False(server.Files.ContainsKey("M42_R_0001.fits"));
            // Make it due now and step again: it carries on, not from 0.
            var queue = Path.Combine(root, "q", "queue.json");
            var json = JArray.Parse(File.ReadAllText(queue));
            json[0]["NextTryUtc"] = DateTime.UtcNow.AddSeconds(-1);
            File.WriteAllText(queue, json.ToString());
            using var again = new LightUploader(Path.Combine(root, "q"), () => true, server);
            again.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
            Assert.Equal("sent", again.Snapshot().Single().State);
            Assert.Equal(File.ReadAllBytes(path), server.Files["M42_R_0001.fits"].Data);
            Assert.Contains(server.Calls, c => c.Contains("offset=200000"));
            Assert.Equal(before, (FakeServer.Hex(File.ReadAllBytes(path)), File.GetLastWriteTimeUtc(path)));
        }

        [Fact]
        public void Switched_off_nothing_is_queued() {
            var on = false;
            using var up = new LightUploader(Path.Combine(root, "q"), () => on, new FakeServer());
            Assert.False(up.Enqueue(Light("a.fits"), "p1", "t1"));
            Assert.Empty(up.Snapshot());
            on = true;
            Assert.True(up.Enqueue(Light("b.fits"), "p1", "t1"));
        }

        [Fact]
        public void The_queue_survives_a_restart_and_a_light_is_queued_once() {
            var path = Light("c.fits");
            using (var up = new LightUploader(Path.Combine(root, "q"), () => true, new FakeServer())) {
                Assert.True(up.Enqueue(path, "p1", "t1"));
                Assert.False(up.Enqueue(path, "p1", "t1"));
            }
            using var again = new LightUploader(Path.Combine(root, "q"), () => true, new FakeServer());
            Assert.Single(again.Snapshot());
        }

        [Fact]
        public void Server_answers_are_obeyed() {
            Thread.Sleep(10);
            var path = Light("d.fits");
            Thread.Sleep(400);
            var off = new FakeServer { ForceStart = new WireAnswer(409, new JObject { ["detail"] = "auto-upload is off on that collaboration" }, null) };
            using (var up = new LightUploader(Path.Combine(root, "q409"), () => true, off)) {
                up.Enqueue(path, "p1", "t1");
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                Assert.Equal("queued", up.Snapshot().Single().State);
                var calls = off.Calls.Count;
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                Assert.Equal(calls, off.Calls.Count);   // that collaboration is left alone for a while
            }
            var wrong = new FakeServer { ForceStart = new WireAnswer(403, new JObject { ["detail"] = "not a light of this collaboration: wrong field" }, null) };
            using (var up = new LightUploader(Path.Combine(root, "q403"), () => true, wrong)) {
                up.Enqueue(path, "p1", "t1");
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                var item = up.Snapshot().Single();
                Assert.Equal("refused", item.State);
                Assert.Contains("wrong field", item.Error);
            }
            var full = new FakeServer { ForceStart = new WireAnswer(507, new JObject { ["detail"] = "full" }, TimeSpan.FromHours(6)) };
            using (var up = new LightUploader(Path.Combine(root, "q507"), () => true, full)) {
                up.Enqueue(path, "p1", "t1");
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                Assert.Contains("paused", up.StatusLine);
                Assert.True(File.Exists(path));
            }
        }

        [Fact]
        public void A_frame_still_being_written_waits_only_to_settle() {
            var server = new FakeServer();
            using var up = new LightUploader(Path.Combine(root, "q"), () => true, server);
            var path = Light("e.fits");
            up.Enqueue(path, "p1", "t1");
            up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
            var item = up.Snapshot().Single();
            Assert.Equal("queued", item.State);
            Assert.Equal(0, item.Attempts);
            Assert.True(item.NextTryUtc - DateTime.UtcNow < TimeSpan.FromSeconds(1));
        }

        [Fact]
        public void Scope_id_is_sent_only_when_the_agent_has_more_than_one_bind() {
            var multiServer = new FakeServer();
            var multiPath = Light("rasa-l.fits");
            var onePath = Light("one.fits");
            var offPath = Light("off.fits");
            Thread.Sleep(400);
            using (var up = new LightUploader(Path.Combine(root, "qscope"), () => true, multiServer, scopeId: () => "scope-l", boundScopes: () => 3)) {
                up.Enqueue(multiPath, "p1", "t1");
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                Assert.Equal("scope-l", multiServer.LastStart.Value<string>("scopeId"));
                Assert.Null(multiServer.LastStart["telescope"]);
            }
            var one = new FakeServer();
            using (var up = new LightUploader(Path.Combine(root, "qone"), () => true, one, scopeId: () => "scope-l", boundScopes: () => 1)) {
                up.Enqueue(onePath, "p1", "t1");
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                Assert.Null(one.LastStart["scopeId"]);
                Assert.Null(one.LastStart["telescope"]);
            }
            var off = new FakeServer();
            using (var up = new LightUploader(Path.Combine(root, "qoff"), () => true, off, scopeId: () => "", boundScopes: () => 3)) {
                up.Enqueue(offPath, "p1", "t1");
                up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
                Assert.Null(off.LastStart["scopeId"]);
            }
        }

        [Fact]
        public void A_scope_409_holds_that_file_and_leaves_the_others() {
            var server = new FakeServer();
            var held = 0;
            var path = Light("held.fits");
            Thread.Sleep(400);
            server.ForceStart = new WireAnswer(409, new JObject { ["detail"] = "this agent is not bound to that scope" }, null);
            using var up = new LightUploader(Path.Combine(root, "qhold"), () => true, server, scopeId: () => "scope-l", boundScopes: () => 3);
            up.Enqueue(path, "p1", "t1");
            up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
            var item = up.Snapshot().Single();
            Assert.Equal("held", item.State);
            Assert.Contains("not bound", item.Error);
            Assert.Contains("held", up.StatusLine);
            // The next file is a different answer. ForceStart still 409s everything,
            // so a camera miss and the 400/403 forms are each their own uploader.
            Assert.Equal("held", item.State);
            held = up.Snapshot().Count(i => i.State == "held");
            Assert.Equal(1, held);
        }

        [Theory]
        [InlineData(409, "no scope matches this camera (RASA)")]
        [InlineData(403, "that scope belongs to another agent")]
        [InlineData(400, "unknown scope")]
        [InlineData(400, "missing scopeId")]
        public void Scope_errors_hold_the_file(int code, string detail) {
            var path = Light($"e{code}-{detail.Length}.fits");
            Thread.Sleep(400);
            var server = new FakeServer { ForceStart = new WireAnswer(code, new JObject { ["detail"] = detail }, null) };
            using var up = new LightUploader(Path.Combine(root, "q" + code + detail.Length), () => true, server,
                scopeId: () => "scope-a", boundScopes: () => 3);
            up.Enqueue(path, "p1", "t1");
            up.StepAsync(CancellationToken.None).GetAwaiter().GetResult();
            var item = up.Snapshot().Single();
            Assert.Equal("held", item.State);
            Assert.Contains(detail, item.Error);
            Assert.NotEqual("refused", item.State);
        }

        [Fact]
        public void Dropdown_lists_bound_scopes_first_by_display_name() {
            var choices = ScopeDirectory.ChoicesOf(new[] {
                new ScopeInfo { Id = "scope-fsq", DisplayName = "FSQ-106", BoundToThisAgent = false },
                new ScopeInfo { Id = "scope-l", Name = "old", DisplayName = "RASA-L", BoundToThisAgent = true },
                new ScopeInfo { Id = "scope-r", DisplayName = "RASA-R", BoundToThisAgent = true },
                new ScopeInfo { Id = "scope-a", DisplayName = "RASA-A", BoundToThisAgent = true },
            });
            Assert.Equal(new[] { "scope-a", "scope-l", "scope-r", "scope-fsq" }, choices.Select(c => c.Id).ToArray());
            Assert.Equal("RASA-L", choices.Single(c => c.Id == "scope-l").DisplayName);
            Assert.Null(ScopeDirectory.HoldMessage(409, new JObject { ["detail"] = "auto-upload is off on that collaboration" }));
            Assert.Null(ScopeDirectory.HoldMessage(403, new JObject { ["detail"] = "not a light of this collaboration: wrong field" }));
            Assert.Equal("unknown scope", ScopeDirectory.HoldMessage(400, new JObject { ["detail"] = "unknown scope" }));
        }
    }
}

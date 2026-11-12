using System.IO;
using System.Collections.Concurrent;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Starfront.Collab.Server;

namespace Starfront.Collab.Upload {

    /// <summary>One file waiting to go to the server, as kept in queue.json.</summary>
    public sealed class UploadItem {
        public string Path { get; set; } = "";
        public string Project { get; set; } = "";
        public string Task { get; set; } = "";
        public string Kind { get; set; } = "light";
        public string State { get; set; } = "queued";     // queued | retry | sent | already | refused | held
        public int Attempts { get; set; }
        public DateTime NextTryUtc { get; set; } = DateTime.MinValue;
        public DateTime AddedUtc { get; set; } = DateTime.UtcNow;
        public DateTime? DoneUtc { get; set; }
        public string Sha256 { get; set; } = "";
        public long Bytes { get; set; }
        public string UploadId { get; set; } = "";
        public string Error { get; set; } = "";
        public string Status { get; set; } = "";
        public bool Open => State == "queued" || State == "retry";
    }

    /// <summary>The server's answer to one call: status code (0 = not reached), body, Retry-After.</summary>
    public readonly record struct WireAnswer(int Code, JObject Body, TimeSpan? RetryAfter);

    /// <summary>The HTTP the uploader speaks; swapped for an in-memory server in the tests.</summary>
    public interface IUploadWire {
        Task<WireAnswer> SendAsync(HttpMethod method, string path, HttpContent content, CancellationToken token);
    }

    /// <summary>
    /// Sends every collaboration light N.I.N.A. saves to the collaboration
    /// server, the moment it is saved.
    ///
    /// The trigger is N.I.N.A.'s own image-saved event (IImageSaveMediator.
    /// ImageSaved, through <see cref="Services.FrameReporter"/>): each saved
    /// light of a collaboration panel is handed to <see cref="Enqueue"/>,
    /// which appends it to a small JSON queue and wakes the worker - no folder
    /// scan, no batch at dawn. The worker is a thread of its own at below-normal
    /// priority, so nothing here ever waits inside the capture or save path.
    ///
    /// The wire is the server's resumable agent API (`/api/v1/agent/uploads`:
    /// start with size and sha256, chunks at the offsets the server confirms,
    /// finish), the same one the Starfront program uses. A dropped connection
    /// resumes where it stopped, with back-off; 409 (that collaboration is not
    /// taking uploads) pauses that collaboration for an hour and keeps the item;
    /// 429 and 507 wait the server's Retry-After; a refusal of the frame itself
    /// takes it off the queue with the reason. Local files are opened read-only
    /// and are never moved, changed or deleted.
    /// </summary>
    public sealed class LightUploader : IDisposable {
        public static TimeSpan SettleTime = TimeSpan.FromSeconds(3);
        public static readonly TimeSpan BackoffMin = TimeSpan.FromSeconds(30);
        public static readonly TimeSpan BackoffMax = TimeSpan.FromHours(1);
        public static readonly TimeSpan OffPause = TimeSpan.FromHours(1);
        public static readonly TimeSpan KeepDone = TimeSpan.FromDays(14);
        public const int DefaultChunk = 8 * 1024 * 1024;

        private readonly Func<bool> enabled;
        private readonly IUploadWire wire;
        private readonly Action<string, string> log;
        private readonly Func<string> scopeId;
        private readonly Func<int> boundScopes;
        private readonly string queuePath;
        private readonly object gate = new();
        private readonly List<UploadItem> items;
        private readonly AutoResetEvent wake = new(false);
        private readonly ConcurrentDictionary<string, DateTime> projectPaused = new(StringComparer.Ordinal);
        private CancellationTokenSource stop;
        private Thread thread;
        private DateTime pausedUntilUtc = DateTime.MinValue;

        public LightUploader(string folder, Func<bool> enabled, IUploadWire wire, Action<string, string> log = null,
                             Func<string> scopeId = null, Func<int> boundScopes = null) {
            Directory.CreateDirectory(folder);
            queuePath = System.IO.Path.Combine(folder, "queue.json");
            this.enabled = enabled;
            this.wire = wire;
            this.log = log ?? ((_, _) => { });
            // scopeId is sent only when this agent holds more than one bind.
            // A single bind is resolved by the server. A null func sends nothing,
            // which is the previous path.
            this.scopeId = scopeId ?? (() => "");
            this.boundScopes = boundScopes ?? (() => 0);
            items = Load();
        }

        public string LastError { get; private set; } = "";
        public long SentBytes { get; private set; }

        public List<UploadItem> Snapshot() { lock (gate) return items.Select(i => i.MemberwiseCloneItem()).ToList(); }

        /// <summary>A one-line state for the options page.</summary>
        public string StatusLine {
            get {
                lock (gate) {
                    if (!enabled()) return "Off: lights are not uploaded";
                    var waiting = items.Count(i => i.Open);
                    var sent = items.Count(i => i.State == "sent" || i.State == "already");
                    var refused = items.Count(i => i.State == "refused");
                    var held = items.Count(i => i.State == "held");
                    var text = $"{sent} sent, {waiting} waiting" + (held > 0 ? $", {held} held" : "") + (refused > 0 ? $", {refused} refused" : "");
                    if (pausedUntilUtc > DateTime.UtcNow) text += $" - paused until {pausedUntilUtc.ToLocalTime():HH:mm}";
                    return text;
                }
            }
        }

        // -- what the image-saved hook calls ----------------------------------

        /// <summary>
        /// A light of a collaboration task is on disk. Queue it and wake the
        /// worker; returns at once. Never throws.
        /// </summary>
        public bool Enqueue(string path, string project, string task, string kind = "light") {
            try {
                if (!enabled() || string.IsNullOrWhiteSpace(path) || string.IsNullOrWhiteSpace(project)) return false;
                lock (gate) {
                    if (items.Any(i => i.Open && i.Project == project && i.Kind == kind
                                       && string.Equals(i.Path, path, StringComparison.OrdinalIgnoreCase))) return false;
                    items.Add(new UploadItem { Path = path, Project = project, Task = task ?? "", Kind = kind });
                    Save();
                }
                wake.Set();
                return true;
            } catch (Exception ex) {
                log("warn", $"Starfront upload: could not queue {System.IO.Path.GetFileName(path)} - {ex.Message}");
                return false;
            }
        }

        // -- the worker ---------------------------------------------------------

        public void Start() {
            lock (gate) {
                if (thread != null && thread.IsAlive) return;
                stop = new CancellationTokenSource();
                var token = stop.Token;
                thread = new Thread(() => Run(token)) { IsBackground = true, Name = "Starfront upload", Priority = ThreadPriority.BelowNormal };
                thread.Start();
            }
        }

        public void Stop() {
            try { stop?.Cancel(); } catch (ObjectDisposedException) { }
            wake.Set();
        }

        private void Run(CancellationToken token) {
            while (!token.IsCancellationRequested) {
                TimeSpan wait;
                try {
                    wait = StepAsync(token).GetAwaiter().GetResult();
                } catch (OperationCanceledException) {
                    return;
                } catch (Exception ex) {
                    LastError = ex.Message;
                    wait = TimeSpan.FromSeconds(30);
                }
                if (wait > TimeSpan.Zero) wake.WaitOne(wait);
            }
        }

        /// <summary>One unit of work; how long to wait before the next (a saved frame wakes it sooner).</summary>
        public async Task<TimeSpan> StepAsync(CancellationToken token) {
            if (!enabled()) return TimeSpan.FromMinutes(5);
            var now = DateTime.UtcNow;
            if (pausedUntilUtc > now) return Min(pausedUntilUtc - now, TimeSpan.FromMinutes(10));
            var item = Due(now);
            if (item == null) return NextWait(now);
            await SendAsync(item, token).ConfigureAwait(false);
            return TimeSpan.Zero;
        }

        private UploadItem Due(DateTime now) {
            lock (gate) {
                foreach (var item in items) {
                    if (!item.Open || item.NextTryUtc > now) continue;
                    if (projectPaused.TryGetValue(item.Project, out var until) && until > now) continue;
                    return item;
                }
            }
            return null;
        }

        private TimeSpan NextWait(DateTime now) {
            var soonest = TimeSpan.FromMinutes(5);
            lock (gate) {
                foreach (var item in items.Where(i => i.Open)) {
                    var at = item.NextTryUtc;
                    if (projectPaused.TryGetValue(item.Project, out var until) && until > at) at = until;
                    if (at > now && at - now < soonest) soonest = at - now;
                }
            }
            return Max(soonest, TimeSpan.FromMilliseconds(200));
        }

        private void Mark(UploadItem item, Action<UploadItem> change) {
            lock (gate) { change(item); Save(); }
        }

        private async Task SendAsync(UploadItem item, CancellationToken token) {
            var name = System.IO.Path.GetFileName(item.Path);
            try {
                var info = new FileInfo(item.Path);
                if (!info.Exists) throw new Refused("the file is no longer on this PC");
                var age = DateTime.UtcNow - info.LastWriteTimeUtc;
                if (age < SettleTime) {
                    // Just saved: go the moment it has settled. Not a failure.
                    Mark(item, i => i.NextTryUtc = DateTime.UtcNow + (SettleTime - age) + TimeSpan.FromMilliseconds(100));
                    return;
                }
                if (string.IsNullOrEmpty(item.Sha256) || item.Bytes != info.Length) {
                    var sha = await Task.Run(() => HashOf(item.Path), token).ConfigureAwait(false);
                    Mark(item, i => { i.Sha256 = sha; i.Bytes = info.Length; i.UploadId = ""; });
                }
                await UploadAsync(item, name, token).ConfigureAwait(false);
            } catch (OperationCanceledException) when (token.IsCancellationRequested) {
                throw;
            } catch (Refused ex) {
                Mark(item, i => { i.State = "refused"; i.Error = ex.Message; i.DoneUtc = DateTime.UtcNow; });
                log("warn", $"Starfront upload: {name} not sent - {ex.Message}");
            } catch (Held ex) {
                // The scope will not take this file. It stays in the queue with
                // the reason. The other files are not paused and this one is not dropped.
                Mark(item, i => { i.State = "held"; i.Error = ex.Message; });
                log("warn", $"Starfront upload: {name} held - {ex.Message}");
            } catch (Pause ex) {
                var until = DateTime.UtcNow + ex.For;
                if (ex.ProjectOnly) projectPaused[item.Project] = until; else pausedUntilUtc = until;
                Mark(item, i => { i.State = "queued"; i.Error = ex.Message; });
                log("info", $"Starfront upload: paused {(ex.ProjectOnly ? "for that collaboration " : "")}until {until.ToLocalTime():HH:mm} - {ex.Message}");
            } catch (Exception ex) {
                // A dropped connection, a timeout, a file still locked: try again later, never lose it.
                var attempts = item.Attempts + 1;
                var seconds = Math.Min(BackoffMax.TotalSeconds, BackoffMin.TotalSeconds * Math.Pow(2, attempts - 1));
                seconds *= 0.8 + 0.4 * Random.Shared.NextDouble();
                Mark(item, i => { i.State = "retry"; i.Attempts = attempts; i.Error = ex.Message; i.NextTryUtc = DateTime.UtcNow.AddSeconds(seconds); });
                LastError = ex.Message;
                log("warn", $"Starfront upload: {name} will be tried again in {seconds:0} s - {ex.Message}");
            }
        }

        private async Task UploadAsync(UploadItem item, string name, CancellationToken token) {
            var start = new JObject {
                ["project"] = item.Project, ["name"] = name, ["kind"] = item.Kind,
                ["bytes"] = item.Bytes, ["sha256"] = item.Sha256, ["task"] = item.Task ?? "",
            };
            // The profile name is not a filing key. With more than one bind the
            // scope id says which camera this light belongs to.
            var chosen = (scopeId() ?? "").Trim();
            var binds = 0;
            try { binds = boundScopes(); } catch (Exception) { binds = 0; }
            if (chosen.Length > 0 && binds > 1) start["scopeId"] = chosen;
            var answer = await wire.SendAsync(HttpMethod.Post, "/api/v1/agent/uploads", Json(start), token).ConfigureAwait(false);
            Obey(answer);
            if (answer.Body.Value<bool?>("already") == true) {
                Mark(item, i => { i.State = "already"; i.Status = answer.Body.Value<string>("status") ?? ""; i.DoneUtc = DateTime.UtcNow; i.Error = ""; });
                return;
            }
            var id = answer.Body.Value<string>("id") ?? throw new InvalidOperationException("the server did not open an upload");
            long offset = answer.Body.Value<long?>("offset") ?? 0;
            var chunk = (int)Math.Clamp(answer.Body.Value<long?>("chunk") ?? DefaultChunk, 64 * 1024, 32 * 1024 * 1024);
            Mark(item, i => i.UploadId = id);
            var buffer = new byte[chunk];
            using (var file = new FileStream(item.Path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) {
                while (offset < item.Bytes) {
                    token.ThrowIfCancellationRequested();
                    if (!enabled()) throw new InvalidOperationException("auto-upload was switched off");
                    file.Seek(offset, SeekOrigin.Begin);
                    var want = (int)Math.Min(chunk, item.Bytes - offset);
                    var got = 0;
                    while (got < want) {
                        var n = await file.ReadAsync(buffer.AsMemory(got, want - got), token).ConfigureAwait(false);
                        if (n == 0) break;
                        got += n;
                    }
                    if (got != want) throw new IOException("the file is shorter than when it was hashed");
                    var body = new ByteArrayContent(buffer, 0, got);
                    body.Headers.ContentType = new MediaTypeHeaderValue("application/octet-stream");
                    var put = await wire.SendAsync(HttpMethod.Put, $"/api/v1/agent/uploads/{Uri.EscapeDataString(id)}?offset={offset}", body, token).ConfigureAwait(false);
                    if (put.Code == 409 && put.Body["offset"] != null) { offset = put.Body.Value<long>("offset"); continue; }
                    if (put.Code == 404) { Mark(item, i => i.UploadId = ""); throw new IOException("the upload session expired; starting again"); }
                    Obey(put);
                    offset = put.Body.Value<long?>("offset") ?? offset + got;
                    SentBytes += got;
                }
            }
            var done = await wire.SendAsync(HttpMethod.Post, $"/api/v1/agent/uploads/{Uri.EscapeDataString(id)}/finish", null, token).ConfigureAwait(false);
            if (done.Code == 409 && done.Body["offset"] != null) throw new IOException(done.Body.Value<string>("detail") ?? "the server is missing part of the file");
            if (done.Code == 422) { Mark(item, i => { i.Sha256 = ""; i.UploadId = ""; }); throw new IOException(done.Body.Value<string>("detail") ?? "sha256 mismatch"); }
            Obey(done);
            Mark(item, i => { i.State = "sent"; i.Status = done.Body.Value<string>("status") ?? ""; i.DoneUtc = DateTime.UtcNow; i.Error = ""; });
            log("info", $"Starfront upload: {name} sent ({done.Body.Value<string>("status") ?? "ok"})");
        }

        private static void Obey(WireAnswer answer) {
            if (answer.Code >= 200 && answer.Code < 300) return;
            var hold = ScopeDirectory.HoldMessage(answer.Code, answer.Body);
            if (hold != null) throw new Held(hold);
            var detail = answer.Body?.Value<string>("detail") ?? $"HTTP {answer.Code}";
            switch (answer.Code) {
                case 0: throw new IOException(detail);
                case 429: throw new Pause(answer.RetryAfter ?? TimeSpan.FromMinutes(1), detail, false);
                case 507: throw new Pause(answer.RetryAfter ?? TimeSpan.FromHours(6), detail, false);
                case 409: throw new Pause(OffPause, detail, true);
                case 401: throw new Pause(OffPause, "this telescope is not enrolled on the collaboration server", false);
                case 400: case 403: case 404: case 413: throw new Refused(detail);
                default: throw new IOException(detail);
            }
        }

        // -- the queue file -------------------------------------------------------

        private List<UploadItem> Load() {
            try {
                if (File.Exists(queuePath))
                    return (JsonConvert.DeserializeObject<List<UploadItem>>(File.ReadAllText(queuePath)) ?? new List<UploadItem>())
                        .Where(i => !string.IsNullOrEmpty(i.Path)).ToList();
            } catch (Exception ex) {
                log("warn", $"Starfront upload: the queue could not be read, starting a new one ({ex.Message})");
                try { File.Copy(queuePath, queuePath + ".broken", true); } catch (Exception) { }
            }
            return new List<UploadItem>();
        }

        private void Save() {
            var now = DateTime.UtcNow;
            items.RemoveAll(i => !i.Open && i.DoneUtc.HasValue && now - i.DoneUtc.Value > KeepDone);
            var temporary = queuePath + ".tmp";
            File.WriteAllText(temporary, JsonConvert.SerializeObject(items, Formatting.Indented));
            File.Move(temporary, queuePath, true);
        }

        public static string HashOf(string path) {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 20);
            using var sha = SHA256.Create();
            return Convert.ToHexString(sha.ComputeHash(stream)).ToLowerInvariant();
        }

        private static StringContent Json(JObject body) => new(body.ToString(Formatting.None), Encoding.UTF8, "application/json");
        private static TimeSpan Min(TimeSpan a, TimeSpan b) => a < b ? a : b;
        private static TimeSpan Max(TimeSpan a, TimeSpan b) => a > b ? a : b;

        public void Dispose() {
            Stop();
            try { thread?.Join(TimeSpan.FromSeconds(2)); } catch (Exception) { }
            stop?.Dispose();
        }

        private sealed class Refused : Exception { public Refused(string m) : base(m) { } }

        private sealed class Held : Exception { public Held(string m) : base(m) { } }

        private sealed class Pause : Exception {
            public TimeSpan For { get; }
            public bool ProjectOnly { get; }
            public Pause(TimeSpan duration, string m, bool projectOnly) : base(m) { For = duration; ProjectOnly = projectOnly; }
        }
    }

    internal static class UploadItemCopy {
        public static UploadItem MemberwiseCloneItem(this UploadItem item) =>
            JsonConvert.DeserializeObject<UploadItem>(JsonConvert.SerializeObject(item));
    }

    /// <summary>The real wire: HTTPS to the collaboration server on the telescope's own token.</summary>
    public sealed class HttpUploadWire : IUploadWire {
        private static readonly HttpClient Http = Make();
        private readonly Func<string> baseUrl;
        private readonly Func<string> agentToken;

        public HttpUploadWire(Func<string> baseUrl, Func<string> agentToken) {
            this.baseUrl = baseUrl;
            this.agentToken = agentToken;
        }

        private static HttpClient Make() {
            // Its own client and a long timeout: an 8 MB chunk on a slow uplink
            // must not trip the 25 s limit the control calls use.
            var client = new HttpClient(new HttpClientHandler()) { Timeout = TimeSpan.FromMinutes(3) };
            var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0";
            client.DefaultRequestHeaders.UserAgent.ParseAdd($"Starfront-NINA/{version}");
            client.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            return client;
        }

        public async Task<WireAnswer> SendAsync(HttpMethod method, string path, HttpContent content, CancellationToken token) {
            var bearer = agentToken();
            if (string.IsNullOrEmpty(bearer)) return new WireAnswer(401, new JObject { ["detail"] = "not enrolled" }, null);
            using var request = new HttpRequestMessage(method, baseUrl().TrimEnd('/') + path) { Content = content };
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
            try {
                using var response = await Http.SendAsync(request, token).ConfigureAwait(false);
                var text = await response.Content.ReadAsStringAsync(token).ConfigureAwait(false);
                JObject body;
                try { body = string.IsNullOrWhiteSpace(text) ? new JObject() : JObject.Parse(text); } catch (Exception) { body = new JObject { ["detail"] = text.Length > 300 ? text[..300] : text }; }
                TimeSpan? retry = response.Headers.RetryAfter?.Delta;
                return new WireAnswer((int)response.StatusCode, body, retry);
            } catch (OperationCanceledException) when (token.IsCancellationRequested) {
                throw;
            } catch (Exception ex) {
                return new WireAnswer(0, new JObject { ["detail"] = $"the collaboration server could not be reached ({ex.Message})" }, null);
            }
        }
    }
}

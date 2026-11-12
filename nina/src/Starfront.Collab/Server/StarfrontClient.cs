using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Reflection;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using NINA.Core.Utility;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Server {

    /// <summary>The server said no, or could not be reached. Status 0 is "could not be reached".</summary>
    public sealed class ServerException : Exception {
        public int Status { get; }
        public ServerException(int status, string detail) : base(detail) { Status = status; }
    }

    /// <summary>
    /// The collaboration server's API, one method per call. Two credentials:
    /// a person's token for signing in and enrolling a telescope, the
    /// telescope's own for the night's work. Nothing here retries or blocks
    /// for long - a poll of a few hundred bytes that hangs is a thread that
    /// hangs, and the night must never wait on the server.
    /// </summary>
    public sealed class StarfrontClient {
        public const int Protocol = 1;

        private static readonly HttpClient Http = Make();
        private readonly StarfrontSettings settings;

        public StarfrontClient(StarfrontSettings settings) {
            this.settings = settings;
        }

        private static HttpClient Make() {
            var client = new HttpClient(new HttpClientHandler { AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate }) {
                Timeout = TimeSpan.FromSeconds(25),
            };
            var version = Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0";
            client.DefaultRequestHeaders.UserAgent.ParseAdd($"Starfront-NINA/{version}");
            client.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            return client;
        }

        private string Base => settings.ServerUrl;

        private async Task<T> Call<T>(HttpMethod method, string path, object body, string bearer, CancellationToken token) where T : class {
            using var request = new HttpRequestMessage(method, Base + path);
            if (!string.IsNullOrEmpty(bearer)) request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
            if (body != null) {
                request.Content = new StringContent(JsonConvert.SerializeObject(body), Encoding.UTF8, "application/json");
            }
            HttpResponseMessage response;
            string text;
            try {
                response = await Http.SendAsync(request, token).ConfigureAwait(false);
                text = await response.Content.ReadAsStringAsync(token).ConfigureAwait(false);
            } catch (OperationCanceledException) when (token.IsCancellationRequested) {
                throw;
            } catch (Exception ex) {
                throw new ServerException(0, $"the collaboration server could not be reached ({ex.Message})");
            }
            using (response) {
                if (!response.IsSuccessStatusCode) {
                    var detail = text;
                    try { detail = JObject.Parse(text)["detail"]?.ToString() ?? text; } catch (Exception) { }
                    if (string.IsNullOrWhiteSpace(detail)) detail = response.ReasonPhrase ?? "error";
                    if (detail.Length > 400) detail = detail.Substring(0, 400);
                    throw new ServerException((int)response.StatusCode, $"{(int)response.StatusCode}: {detail}");
                }
                if (string.IsNullOrWhiteSpace(text)) return typeof(T) == typeof(JObject) ? (T)(object)new JObject() : Activator.CreateInstance<T>();
                try {
                    return JsonConvert.DeserializeObject<T>(text);
                } catch (Exception ex) {
                    throw new ServerException((int)response.StatusCode, $"the server's answer could not be read ({ex.Message})");
                }
            }
        }

        private static string Q(string value) => Uri.EscapeDataString(value ?? "");

        // -- open to anyone ----------------------------------------------------

        public Task<HealthAnswer> HealthAsync(CancellationToken token) =>
            Call<HealthAnswer>(HttpMethod.Get, "/api/v1/health", null, null, token);

        public Task<JObject> AuthStatusAsync(CancellationToken token) =>
            Call<JObject>(HttpMethod.Get, "/api/v1/auth", null, null, token);

        public Task<LoginBegun> BeginLoginAsync(CancellationToken token) =>
            Call<LoginBegun>(HttpMethod.Post, "/api/v1/auth/login", null, null, token);

        public Task<LoginPolled> PollLoginAsync(string code, CancellationToken token) =>
            Call<LoginPolled>(HttpMethod.Get, $"/api/v1/auth/poll?code={Q(code)}", null, null, token);

        // -- a person ----------------------------------------------------------

        public Task<SignedInUser> MeAsync(string userToken, CancellationToken token) =>
            Call<SignedInUser>(HttpMethod.Get, "/api/v1/auth/me", null, userToken, token);

        public Task<JObject> LogoutAsync(string userToken, CancellationToken token) =>
            Call<JObject>(HttpMethod.Post, "/api/v1/auth/logout", null, userToken, token);

        public Task<EnrolAnswer> EnrolTelescopeAsync(string userToken, string name, string owner, CancellationToken token) =>
            Call<EnrolAnswer>(HttpMethod.Post, "/api/v1/agents", new { name, owner }, userToken, token);

        // -- a person starting and running a collaboration --------------------

        public Task<LookupAnswer> LookupAsync(string userToken, string name, CancellationToken token) =>
            Call<LookupAnswer>(HttpMethod.Get, $"/api/v1/lookup?name={Q(name)}", null, userToken, token);

        public Task<JObject> CreateProjectAsync(string userToken, JObject body, CancellationToken token) =>
            Call<JObject>(HttpMethod.Post, "/api/v1/projects", body, userToken, token);

        public Task<JObject> UpdateProjectAsync(string userToken, string projectId, JObject body, CancellationToken token) =>
            Call<JObject>(HttpMethod.Put, $"/api/v1/projects/{Q(projectId)}", body, userToken, token);

        public Task<JObject> RedealProjectAsync(string userToken, string projectId, CancellationToken token) =>
            Call<JObject>(HttpMethod.Post, $"/api/v1/projects/{Q(projectId)}/redeal", null, userToken, token);

        // -- a telescope: the three calls, and the browsing around them ------

        /// <summary>The scopes this agent can see. An agent token is enough; binding is not.</summary>
        public async Task<List<ScopeInfo>> ScopesAsync(string agentToken, CancellationToken token) {
            var answer = await Call<JObject>(HttpMethod.Get, "/api/v1/scopes", null, agentToken, token).ConfigureAwait(false);
            return ScopeDirectory.Read(answer);
        }

        public Task<HelloAnswer> HelloAsync(string agentToken, JObject profile, JObject presence, CancellationToken token) {
            var body = new JObject { ["protocol"] = Protocol, ["profile"] = profile };
            if (presence != null) body["presence"] = presence;
            return Call<HelloAnswer>(HttpMethod.Post, "/api/v1/agent/hello", body, agentToken, token);
        }

        public Task<TaskAnswer> TaskAsync(string agentToken, string night, double? moon, double? moonUp, CancellationToken token) {
            var query = $"night={Q(night)}";
            if (moon.HasValue && moonUp.HasValue) {
                query += $"&moon={moon.Value.ToString("0.000", System.Globalization.CultureInfo.InvariantCulture)}" +
                         $"&moonUp={moonUp.Value.ToString("0.000", System.Globalization.CultureInfo.InvariantCulture)}";
            }
            return Call<TaskAnswer>(HttpMethod.Get, $"/api/v1/agent/task?{query}", null, agentToken, token);
        }

        public async Task<List<ProjectListing>> ProjectsAsync(string agentToken, CancellationToken token) {
            var answer = await Call<JObject>(HttpMethod.Get, "/api/v1/agent/projects", null, agentToken, token).ConfigureAwait(false);
            return answer["projects"]?.ToObject<List<ProjectListing>>() ?? new List<ProjectListing>();
        }

        public Task<PresenceAnswer> PresenceAsync(string agentToken, CancellationToken token) =>
            Call<PresenceAnswer>(HttpMethod.Get, "/api/v1/presence", null, agentToken, token);

        public Task<JoinAnswer> JoinAsync(string agentToken, string projectId, JObject body, CancellationToken token) =>
            Call<JoinAnswer>(HttpMethod.Post, $"/api/v1/agent/projects/{Q(projectId)}/join", body, agentToken, token);

        public Task<JObject> SetTaskStateAsync(string agentToken, string taskId, string state, CancellationToken token) =>
            Call<JObject>(HttpMethod.Post, $"/api/v1/agent/task/{Q(taskId)}", new { state }, agentToken, token);

        public Task<ReportAnswer> ReportAsync(string agentToken, List<JObject> contributions, CancellationToken token) =>
            Call<ReportAnswer>(HttpMethod.Post, "/api/v1/agent/report", new JObject { ["contributions"] = new JArray(contributions) }, agentToken, token);

        public Task<JObject> DepthAsync(string agentToken, string projectId, CancellationToken token) =>
            Call<JObject>(HttpMethod.Get, $"/api/v1/agent/projects/{Q(projectId)}/depth", null, agentToken, token);

        // -- a telescope: personal projects (not offered in the UI yet) --------
        //
        // A personal project is the owner's own imaging of one target, private
        // to them. The server makes one per target on first use and the frames
        // then go through the same resumable upload as a collaboration's
        // (/api/v1/agent/uploads), with the personal project's id as the
        // project. The server answers 403 while personal projects are not open
        // and 402 when the owner has no plan for them: both mean "do not send",
        // never "try again".

        public Task<PersonalStatus> PersonalStatusAsync(string agentToken, CancellationToken token) =>
            Call<PersonalStatus>(HttpMethod.Get, "/api/v1/agent/personal", null, agentToken, token);

        // A mosaic is one target with its panels under it. Pass the panel when
        // the sequence knows it; otherwise the server reads it from the name
        // N.I.N.A. gives the target ("M31 Panel 3"). Either way the returned
        // Project is the panel's, to upload into, and Target is the mosaic's.
        public Task<PersonalProject> PersonalProjectAsync(string agentToken, string target, double? raDegrees, double? decDegrees, CancellationToken token, string panel = null) {
            var body = new JObject { ["target"] = target ?? "" };
            if (raDegrees.HasValue && decDegrees.HasValue) {
                body["ra"] = raDegrees.Value;
                body["dec"] = decDegrees.Value;
            }
            if (!string.IsNullOrWhiteSpace(panel)) body["panel"] = panel.Trim();
            return Call<PersonalProject>(HttpMethod.Post, "/api/v1/agent/personal", body, agentToken, token);
        }

        /// <summary>Log a server call's failure once, at the right level: being offline is a warning, a refusal is an error.</summary>
        public static void Log(string what, Exception ex) {
            if (ex is ServerException se && se.Status == 0) Logger.Warning($"Starfront: {what} - {ex.Message}");
            else Logger.Error($"Starfront: {what} - {ex.Message}");
        }
    }
}

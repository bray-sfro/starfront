using System.Net;
using System.Net.Http;
using System.Reflection;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using NINA.Core.Utility;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// The connections to Rapid Stacker Collab: one handler, an API client
    /// with a timeout and a part-upload client without one (a 16 MB part on a
    /// slow link takes as long as it takes; the engine has its own per-part
    /// limit). Made when live stacking is turned on and thrown away when it is
    /// turned off, so a plugin with the setting off holds no connection. The
    /// API clients are a cache, one per (base URL, token source): the cloud
    /// one, and one per hub link with that link's grant.
    /// </summary>
    internal sealed class StackingClients : IDisposable {
        public StackingClients(StarfrontSettings settings, StackingLinks links) {
            var handler = new SocketsHttpHandler {
                PooledConnectionLifetime = TimeSpan.FromMinutes(5),
                AutomaticDecompression = DecompressionMethods.All,
            };
            ApiHttp = new HttpClient(handler, disposeHandler: true) { Timeout = TimeSpan.FromSeconds(60) };
            S3Http = new HttpClient(handler, disposeHandler: false) { Timeout = Timeout.InfiniteTimeSpan };
            cloudTokens = new SettingsTokenStore(settings);
            this.settings = settings;
            this.links = links;
            userAgent = "Starfront-NINA/" + (Assembly.GetExecutingAssembly().GetName().Version?.ToString() ?? "0");
            Lan = new LanProbe(ApiHttp, () => settings.StackingHubTrustLan, line => Logger.Info(line), TimeSpan.FromSeconds(2));
            Cloud = CloudClient();
            Router = new RoutingApiClient(new Routes(this));
        }

        private readonly StarfrontSettings settings;
        private readonly StackingLinks links;
        private readonly string userAgent;
        private readonly ClientCache<IApiClient> cache = new();
        private readonly SettingsTokenStore cloudTokens;

        private IApiClient CloudClient() => cache.For(settings.StackingApiBase, "cloud", () => Make(settings.StackingApiBase, cloudTokens));

        private IApiClient Make(string baseUrl, ITokenStore tokens) =>
            new ApiClient(ApiHttp, tokens, new ApiClientOptions { BaseUri = new Uri(baseUrl), UserAgent = userAgent });

        /// <summary>The client for a link: a cloud link uses the cloud base and the <c>rscu_</c> token, a hub link its own base and its grant.</summary>
        public IApiClient For(StackingLink link) {
            if (link == null || !link.IsHub || string.IsNullOrWhiteSpace(link.ApiBase)) return CloudClient();
            var id = link.StarfrontProjectId;
            var effective = Lan.EffectiveBase(link);
            return cache.For(effective, "grant:" + id, () => Make(effective, new GrantTokenStore(id, links.Lookup, links.Upsert)));
        }

        private sealed class Routes : IStackingRoutes {
            private readonly StackingClients owner;
            public Routes(StackingClients owner) => this.owner = owner;
            public StackingLink LinkOfProject(string project) => owner.links.ByCollabProject(project);
            public string DefaultBackend => owner.settings.StackingDefaultBackend;
            public IApiClient Cloud => owner.CloudClient();
            public IApiClient ForLink(StackingLink link) => owner.For(link);
            public StackingLink DefaultHubLink() => RigIds.DefaultHubLink(owner.links.All());
            public string RigIdOf(StackingLink hubOrNull) => hubOrNull == null ? owner.settings.StackingRigId : RigIds.OfHub(hubOrNull, owner.links.All());
            public void HubRefused(StackingLink link) {
                new GrantTokenStore(link.StarfrontProjectId, owner.links.Lookup, owner.links.Upsert).Clear();
                Logger.Info($"Starfront stacking: {link.StarfrontName}: the hub refused this telescope's grant; its uploads wait for the next check-in");
            }
        }

        /// <summary>The LAN short-cut: which hub links answer on their own network right now.</summary>
        public LanProbe Lan { get; }
        public HttpClient ApiHttp { get; }
        public HttpClient S3Http { get; }
        /// <summary>The cloud client: sign-in and the token, and what a project nobody linked goes to.</summary>
        public IApiClient Cloud { get; }
        /// <summary>What the engine, the poller, the joiner and the library hold: every call goes to the backend it belongs to.</summary>
        public IApiClient Router { get; }

        public void Dispose() {
            S3Http.Dispose();
            ApiHttp.Dispose();
        }
    }
}

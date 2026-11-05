using System.Net.Http;
using Newtonsoft.Json.Linq;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// The LAN short-cut to a hub. At each check-in the probe asks every hub link that has a LAN address
    /// whether the hub answers there: <c>GET {LanApiBase}/api/hub/v1/health</c> with a two second limit. The
    /// answer counts only when it is a 2xx JSON body whose <c>hub_id</c> is the link's <c>HubId</c>, so
    /// another machine that happens to hold the address is never sent a frame. While it answers, the link's
    /// effective base is the LAN URL, otherwise the link's own <c>ApiBase</c> (the Starfront server's
    /// <c>/stack/api/</c>). Both accept the same grant. Nothing here touches NINA, so the tests link it.
    /// </summary>
    public sealed class LanProbe {
        private readonly HttpClient http;
        private readonly Func<bool> trustLan;
        private readonly Action<string> log;
        private readonly TimeSpan timeout;
        private readonly object gate = new();
        private readonly Dictionary<string, string> onLan = new(StringComparer.Ordinal);

        /// <param name="http">Made over any handler; the probe sets its own per-request limit.</param>
        /// <param name="trustLan">The <c>StackingHubTrustLan</c> setting, read each time.</param>
        /// <param name="log">Receives the one line written when a link switches between the LAN and the server.</param>
        public LanProbe(HttpClient http, Func<bool> trustLan, Action<string> log = null, TimeSpan? timeout = null) {
            this.http = http;
            this.trustLan = trustLan;
            this.log = log;
            this.timeout = timeout ?? TimeSpan.FromSeconds(2);
        }

        /// <summary>The LAN API base of a link (ending in a slash), or null when it has no usable LAN address.</summary>
        public static string LanBase(StackingLink link) {
            var lan = (link?.LanApiBase ?? "").Trim().TrimEnd('/');
            if (lan.Length == 0 || !Uri.TryCreate(lan, UriKind.Absolute, out var uri) || (uri.Scheme != "http" && uri.Scheme != "https")) return null;
            return lan + "/api/";
        }

        /// <summary>The base the link's calls go to now: the LAN URL while the last probe was answered, else <c>ApiBase</c>.</summary>
        public string EffectiveBase(StackingLink link) {
            if (link == null) return "";
            var lan = LanBase(link);
            if (lan != null && trustLan()) {
                lock (gate) {
                    if (onLan.TryGetValue(link.StarfrontProjectId, out var probed) && string.Equals(probed, lan, StringComparison.OrdinalIgnoreCase)) return lan;
                }
            }
            return link.ApiBase;
        }

        public bool IsOnLan(StackingLink link) => link != null && !string.Equals(EffectiveBase(link), link.ApiBase, StringComparison.Ordinal);

        /// <summary>Probe every hub link with a LAN address (or none, when the setting is off) and log each switch.</summary>
        public async Task CheckAsync(IEnumerable<StackingLink> links, CancellationToken ct = default) {
            var trust = trustLan();
            foreach (var link in links) {
                if (link == null || !link.IsHub) continue;
                var lan = LanBase(link);
                var wasOn = IsOnLan(link);
                string answered = null;
                if (trust && lan != null) answered = await ProbeAsync(link, lan, ct).ConfigureAwait(false) ? lan : null;
                lock (gate) {
                    if (answered != null) onLan[link.StarfrontProjectId] = answered; else onLan.Remove(link.StarfrontProjectId);
                }
                var isOn = IsOnLan(link);
                if (isOn != wasOn) {
                    try {
                        log?.Invoke(isOn
                            ? $"Starfront stacking: {link.StarfrontName} now uploads over the LAN ({lan})"
                            : $"Starfront stacking: {link.StarfrontName} now uploads through the Starfront server");
                    } catch (Exception) { }
                }
            }
        }

        private async Task<bool> ProbeAsync(StackingLink link, string lanBase, CancellationToken ct) {
            if (string.IsNullOrEmpty(link.HubId)) return false;
            try {
                using var limit = CancellationTokenSource.CreateLinkedTokenSource(ct);
                limit.CancelAfter(timeout);
                using var request = new HttpRequestMessage(HttpMethod.Get, lanBase + "hub/v1/health");
                using var response = await http.SendAsync(request, limit.Token).ConfigureAwait(false);
                if (!response.IsSuccessStatusCode) return false;
                var body = await response.Content.ReadAsStringAsync(limit.Token).ConfigureAwait(false);
                var id = JObject.Parse(body)["hub_id"];
                return id != null && id.Type == JTokenType.String && string.Equals((string)id, link.HubId, StringComparison.Ordinal);
            } catch (Exception) {
                return false;
            }
        }
    }
}

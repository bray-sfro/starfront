namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// One client per (effective base URL, token source). The same pair always gets the same client, so a
    /// route that is asked for a hundred times in a night opens one connection setup, not a hundred, and
    /// two links on one hub with one grant share a client. Generic so the test project can use it without
    /// the engine's HTTP stack.
    /// </summary>
    public sealed class ClientCache<TClient> where TClient : class {
        private readonly object gate = new();
        private readonly Dictionary<(string Base, string Source), TClient> clients = new();

        /// <summary>The client for this pair, made by <paramref name="make"/> the first time it is asked for.</summary>
        public TClient For(string baseUrl, string tokenSource, Func<TClient> make) {
            var key = (Normalise(baseUrl), tokenSource ?? "");
            lock (gate) {
                if (!clients.TryGetValue(key, out var client)) {
                    client = make();
                    clients[key] = client;
                }
                return client;
            }
        }

        public int Count { get { lock (gate) return clients.Count; } }

        private static string Normalise(string url) => (url ?? "").Trim().TrimEnd('/').ToLowerInvariant();
    }
}

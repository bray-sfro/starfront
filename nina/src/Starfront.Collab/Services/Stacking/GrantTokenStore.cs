using RapidStackerCollab.Core.Auth;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// The token source of a hub link: the grant the link holds now, read on every request, so a grant the
    /// joiner fetched at a check-in is used at once. It never touches the Rapid Stacker Collab cloud token.
    /// When the hub refuses the grant the engine calls <see cref="Clear"/>: the grant is dropped and the link
    /// is marked <see cref="LinkStatus.GrantRefresh"/> (it still feeds: lights are queued and held), so the next check-in fetches a fresh grant instead of this one being
    /// retried (a grant the hub has refused is not going to start working).
    /// </summary>
    public sealed class GrantTokenStore : ITokenStore {
        private readonly string starfrontProjectId;
        private readonly Func<string, StackingLink> lookup;
        private readonly Action<StackingLink> persist;

        /// <param name="lookup">The link by Starfront project id; looked up each time because the joiner may replace the object.</param>
        /// <param name="persist">Writes the link back (the links store's <c>Upsert</c>).</param>
        public GrantTokenStore(string starfrontProjectId, Func<string, StackingLink> lookup, Action<StackingLink> persist) {
            this.starfrontProjectId = starfrontProjectId;
            this.lookup = lookup;
            this.persist = persist;
        }

        public string Load() {
            var grant = lookup(starfrontProjectId)?.Grant;
            return string.IsNullOrEmpty(grant) ? null : grant;
        }

        public void Save(string token) {
            var link = lookup(starfrontProjectId);
            if (link == null) return;
            link.Grant = token ?? "";
            persist(link);
        }

        public void Clear() {
            var link = lookup(starfrontProjectId);
            if (link == null) return;
            link.Grant = "";
            link.GrantExpiresUtc = null;
            link.Status = LinkStatus.GrantRefresh;
            link.Message = "The hub refused this telescope's grant; a new one is fetched at the next check-in.";
            link.NextAttemptUtc = null;
            persist(link);
        }
    }
}

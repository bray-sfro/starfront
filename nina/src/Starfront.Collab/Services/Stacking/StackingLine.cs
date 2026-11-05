using Starfront.Collab.Server;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// The one line a collaboration's card gives about live stacking: where its
    /// lights are stacked, and how this telescope stands with that.
    ///
    /// Why: the line used to hang on <c>stackingLinked</c>, which the server sets
    /// only for a Rapid Stacker Collab join code. A collaboration stacked on a
    /// hub had no line at all, one that was stacked showed "Rapid Stacker Collab"
    /// whatever did the stacking, and while the server was still making the
    /// cloud project (or Collab had refused it, say on an expired account link)
    /// the card said nothing and the plugin skipped it without a word. The line
    /// now reads the listing's <c>stacking</c> object, with its <c>state</c>
    /// and, for the owner's own telescopes, the <c>problem</c>. No WPF here, so
    /// the tests compile it in.
    /// </summary>
    public static class StackingLine {
        /// <param name="enabled">Live stacking is on in this plugin's options (and the plugin is ready).</param>
        /// <param name="link">This telescope's link to the stacking project, or null when it has none yet.</param>
        /// <param name="joined">This telescope is on the collaboration.</param>
        public static string Describe(ProjectListing listing, bool enabled, StackingLink link, bool joined) {
            if (listing == null) return "";
            var binding = listing.EffectiveStacking();
            var backend = binding.Backend ?? "";
            // An old server sends no stacking object, and a browsing telescope
            // no code: its yes-or-no is all there is, and that was always the cloud.
            if (backend.Length == 0 && listing.Stacking == null && listing.StackingLinked) backend = StackingBinding.Cloud;
            if (backend.Length == 0) return "";

            var hub = string.Equals(backend, StackingBinding.Hub, StringComparison.OrdinalIgnoreCase);
            var where = !hub ? "Live-stacked on Rapid Stacker Collab"
                : string.IsNullOrWhiteSpace(binding.HubName) ? "Live-stacked on a hub"
                : $"Live-stacked on the hub {binding.HubName.Trim()}";

            switch (binding.State ?? "") {
                case "pending":
                    return $"{where}; the stacking project is being set up, and lights join once it is ready.";
                case "failed":
                    var problem = (binding.Problem ?? "").Trim().TrimEnd('.');
                    if (problem.Length > 0) return $"{where}, but the stacking project could not be set up yet: {problem}.";
                    return hub
                        ? $"{where}, but that hub is no longer registered; the collaboration's owner can choose another."
                        : $"{where}, but the stacking project could not be set up yet; the owner's Starfront Collab tab says why.";
            }
            if (!enabled) return where + "; turn on Live stacking in the options to send yours.";
            if (link != null) return $"{where}: {link.Describe()}.";
            return joined ? where + "; joining at the next check-in." : where + ".";
        }
    }
}

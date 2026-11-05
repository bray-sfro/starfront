namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Which addresses from Rapid Stacker Collab the plugin hands to the
    /// browser. The approval page and the terms page come from the server's
    /// answers, and handing a string to the shell runs whatever is registered
    /// for it (a <c>file:</c> path, a program, another app's scheme), so only
    /// web pages get through: any https page, and plain http only on this PC's
    /// own loopback address. That second case is the local stack (and an SSH
    /// tunnel to one), which serves http on 127.0.0.1; a loopback page never
    /// crosses the network, so http there exposes nothing that https would
    /// protect. Before this, a local sign-in meant typing the address by hand.
    /// </summary>
    public static class BrowserUrls {
        public static bool CanOpen(string url) {
            if (string.IsNullOrWhiteSpace(url) || !Uri.TryCreate(url.Trim(), UriKind.Absolute, out var uri)) return false;
            if (uri.Scheme == Uri.UriSchemeHttps) return true;
            return uri.Scheme == Uri.UriSchemeHttp && IsLoopback(uri);
        }

        // Spelt out rather than Uri.IsLoopback, which takes "localhost" but not
        // "LOCALHOST": any 127.x.x.x, ::1, or localhost in any case.
        private static bool IsLoopback(Uri uri) {
            if (string.Equals(uri.Host, "localhost", StringComparison.OrdinalIgnoreCase)) return true;
            return uri.HostNameType is UriHostNameType.IPv4 or UriHostNameType.IPv6
                && System.Net.IPAddress.TryParse(uri.Host.Trim('[', ']'), out var address)
                && System.Net.IPAddress.IsLoopback(address);
        }
    }
}

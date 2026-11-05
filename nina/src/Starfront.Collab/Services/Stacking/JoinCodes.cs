namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// A Rapid Stacker Collab join code as that service spells it: twelve
    /// characters of 2-9 and A-Z without I, L and O, written XXXX-XXXX-XXXX.
    /// The same rule as the Starfront server's, so a code the form accepts is
    /// one the server stores and one the stacking service recognises.
    /// </summary>
    public static class JoinCodes {
        public const string Alphabet = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";
        public const string Hint = "A Rapid Stacker Collab join code is twelve characters, like H4RT-92BK-WNPC";

        /// <summary>Upper case, letters and digits only: "h4rt 92bk-wnpc" is "H4RT92BKWNPC".</summary>
        public static string Clean(string text) => new((text ?? "").Where(char.IsLetterOrDigit).Select(char.ToUpperInvariant).ToArray());

        /// <summary>A typed code in canonical form, or false when it cannot be a join code. Blank is canonical "".</summary>
        public static bool TryNormalize(string typed, out string canonical) {
            var clean = Clean(typed);
            canonical = "";
            if (clean.Length == 0) return true;
            if (clean.Length != 12 || clean.Any(c => !Alphabet.Contains(c))) return false;
            canonical = Format(clean);
            return true;
        }

        public static string Format(string clean) => clean.Length == 12 ? $"{clean[..4]}-{clean[4..8]}-{clean[8..]}" : clean;
    }
}

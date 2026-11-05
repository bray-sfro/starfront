using RapidStackerCollab.Core.Auth;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// The Rapid Stacker Collab token, kept where the plugin's other credentials
    /// are: in the settings file, protected for this Windows account. The engine
    /// reads it on every request, so a sign-out takes effect at once.
    /// </summary>
    internal sealed class SettingsTokenStore : ITokenStore {
        private readonly StarfrontSettings settings;

        public SettingsTokenStore(StarfrontSettings settings) => this.settings = settings;

        public string Load() {
            var token = settings.StackingToken;
            return token.Length > 0 ? token : null;
        }

        public void Save(string token) => settings.StackingToken = token ?? "";

        /// <summary>Forget the sign-in: the token and what was learnt about the person with it.</summary>
        public void Clear() => settings.ForgetStackingCredentials();
    }
}

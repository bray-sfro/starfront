using System.Diagnostics;
using System.Net.Http;
using NINA.Core.Utility;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Util;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Signing in to Rapid Stacker Collab, the way the Starfront sign-in works:
    /// a code is shown, the browser opens the approval page, and the plugin
    /// polls until the person approves it there. No password is ever typed
    /// into N.I.N.A. The token is shown by the server once, so it is saved the
    /// moment it arrives, into the settings file beside the other credentials.
    /// </summary>
    public sealed class StackingSignIn : BaseINPC {
        private const string ClientName = "Starfront for N.I.N.A.";

        private readonly StarfrontSettings settings;
        private readonly Func<IApiClient> api;
        private readonly object gate = new();
        private CancellationTokenSource cts;
        private bool busy;
        private string userCode = "";
        private string verificationUrl = "";
        private string status = "";
        private bool authRequired;

        public StackingSignIn(StarfrontSettings settings, Func<IApiClient> api) {
            this.settings = settings;
            this.api = api;
            settings.PropertyChanged += (_, e) => {
                if (e.PropertyName == nameof(StarfrontSettings.StackingToken) || string.IsNullOrEmpty(e.PropertyName)) {
                    RaisePropertyChanged(nameof(SignedIn));
                    RaisePropertyChanged(nameof(AccountText));
                    RaisePropertyChanged(nameof(ExpiryText));
                }
            };
        }

        /// <summary>Raised on a worker thread once a token is stored, or after a sign-out.</summary>
        public event EventHandler Changed;

        /// <summary>A sign-in is under way: asking for a code, or waiting for the person to approve it.</summary>
        public bool Busy { get => busy; private set { busy = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(Waiting)); } }

        /// <summary>The code on screen, like <c>K7QM-3XWD</c>, while the approval page is waiting for it.</summary>
        public string UserCode { get => userCode; private set { userCode = value; RaisePropertyChanged(); RaisePropertyChanged(nameof(Waiting)); } }

        public string VerificationUrl { get => verificationUrl; private set { verificationUrl = value; RaisePropertyChanged(); } }

        /// <summary>What the options page says under the button.</summary>
        public string Status { get => status; private set { status = value; RaisePropertyChanged(); } }

        public bool Waiting => Busy && UserCode.Length > 0;

        public bool SignedIn => settings.StackingSignedIn;

        /// <summary>The engine was told the stored token is no longer accepted. Cleared by the next sign-in.</summary>
        public bool AuthRequired { get => authRequired; private set { authRequired = value; RaisePropertyChanged(); } }

        public string AccountText {
            get {
                if (!SignedIn) return "";
                var name = settings.StackingUserName;
                var email = settings.StackingUserEmail;
                if (name.Length == 0 && email.Length == 0) return "Signed in";
                if (email.Length == 0) return $"Signed in as {name}";
                return name.Length == 0 ? $"Signed in as {email}" : $"Signed in as {name} ({email})";
            }
        }

        /// <summary>How long the token has left. There is no refresh; a lapsed one means signing in again.</summary>
        public string ExpiryText {
            get {
                if (!SignedIn || settings.StackingTokenExpiresUtc is not DateTime when) return "";
                var left = when - DateTime.UtcNow;
                if (left <= TimeSpan.Zero) return "The sign-in has expired; sign in again.";
                if (left < TimeSpan.FromDays(1)) return "The sign-in expires today; sign in again soon.";
                var days = (int)Math.Ceiling(left.TotalDays);
                return days <= 14 ? $"The sign-in expires in {days} days; sign in again soon." : $"The sign-in is good for {days} days.";
            }
        }

        /// <summary>Within a fortnight of the token lapsing, or past it.</summary>
        public bool ExpiringSoon => SignedIn && settings.StackingTokenExpiresUtc is DateTime when && when - DateTime.UtcNow < TimeSpan.FromDays(14);

        /// <summary>Start a sign-in and wait for it to end: approved, denied, expired, failed or cancelled.</summary>
        public async Task SignInAsync() {
            CancellationTokenSource source;
            lock (gate) {
                if (Busy) return;
                source = cts = new CancellationTokenSource();
                Busy = true;
            }
            UserCode = "";
            VerificationUrl = "";
            Status = "Asking Rapid Stacker Collab for a sign-in code...";
            try {
                var client = api();
                var device = new DeviceSignIn(client, new SettingsTokenStore(settings), SystemClock.Instance);
                var start = await device.StartAsync(ClientName, ct: source.Token).ConfigureAwait(false);
                UserCode = start.UserCode;
                VerificationUrl = start.VerificationUriComplete.Length > 0 ? start.VerificationUriComplete : start.VerificationUri;
                Status = $"Approve this N.I.N.A. in the browser with the code {start.UserCode}.";
                OpenBrowser();

                var result = await device.WaitForApprovalAsync(start, source.Token).ConfigureAwait(false);
                switch (result.Outcome) {
                    case DeviceSignInOutcome.Approved:
                        settings.StackingUserName = result.User?.Name ?? "";
                        settings.StackingUserEmail = result.User?.Email ?? "";
                        settings.StackingTokenExpiresUtc = result.Info?.ExpiresAt.UtcDateTime;
                        AuthRequired = false;
                        Status = "";
                        Logger.Info($"Starfront stacking: signed in to Rapid Stacker Collab as {settings.StackingUserName}");
                        break;
                    case DeviceSignInOutcome.Denied:
                        Status = "The sign-in was denied in the browser.";
                        break;
                    case DeviceSignInOutcome.Expired:
                        Status = "The code expired before it was approved. Try again.";
                        break;
                    default:
                        Status = "The sign-in failed: " + (result.Message ?? "no reason given");
                        Logger.Warning("Starfront stacking: sign-in failed - " + result.Message);
                        break;
                }
            } catch (OperationCanceledException) {
                Status = "";
            } catch (Exception ex) when (ex is ApiException or HttpRequestException) {
                Status = "Rapid Stacker Collab could not be reached: " + ex.Message;
                Logger.Warning("Starfront stacking: sign-in failed - " + ex.Message);
            } catch (Exception ex) {
                Status = "The sign-in failed: " + ex.Message;
                Logger.Error("Starfront stacking: sign-in failed", ex);
            } finally {
                UserCode = "";
                lock (gate) {
                    if (ReferenceEquals(cts, source)) cts = null;
                    Busy = false;
                }
                source.Dispose();
                RaiseChanged();
            }
        }

        /// <summary>
        /// Open the approval page, with the code filled in, in the default
        /// browser: an https page, or an http one on this PC (BrowserUrls).
        /// Anything else is left for the person to open, and says where.
        /// </summary>
        public void OpenBrowser() {
            var url = VerificationUrl;
            if (!BrowserUrls.CanOpen(url)) {
                if (url.Length > 0) {
                    Status = $"Open {url} in a browser and approve the code {UserCode} there.";
                    Logger.Warning($"Starfront stacking: the approval page {url} was not opened, as it is neither https nor on this PC");
                }
                return;
            }
            try {
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            } catch (Exception ex) {
                Logger.Warning($"Starfront stacking: the browser could not be opened - {ex.Message}; open {url} by hand");
            }
        }

        /// <summary>Stop waiting for the approval.</summary>
        public void Cancel() {
            lock (gate) {
                try { cts?.Cancel(); } catch (ObjectDisposedException) { }
            }
        }

        /// <summary>
        /// Sign out: revoke the token on the server, then forget it here. Best
        /// effort: a server that cannot be reached does not keep a token on this
        /// PC, and a token the server already calls invalid counts as revoked.
        /// </summary>
        public async Task SignOutAsync() {
            Cancel();
            var revoked = false;
            if (SignedIn) {
                using var limit = new CancellationTokenSource(TimeSpan.FromSeconds(8));
                try {
                    await api().RevokeOwnTokenAsync(limit.Token).ConfigureAwait(false);
                    revoked = true;
                } catch (ApiException ex) when (ex.Code is ApiCodes.TokenInvalid or ApiCodes.NotAuthenticated or ApiCodes.SessionExpired) {
                    revoked = true;
                } catch (Exception ex) when (ex is ApiException or OperationCanceledException or HttpRequestException) {
                    Logger.Warning("Starfront stacking: the token could not be revoked on the server - " + ex.Message);
                }
            }
            settings.ForgetStackingCredentials();
            AuthRequired = false;
            Status = revoked ? "" : "Signed out here; the server could not be reached to revoke the token. Revoke it under Account on the Rapid Stacker Collab website.";
            Logger.Info("Starfront stacking: signed out of Rapid Stacker Collab");
            RaiseChanged();
        }

        /// <summary>The engine found the token rejected: nothing is uploaded until somebody signs in again.</summary>
        public void MarkAuthRequired() {
            if (Busy || AuthRequired) return;
            AuthRequired = true;
            Status = "Rapid Stacker Collab no longer accepts this sign-in. Sign in again to carry on uploading.";
            RaiseChanged();
        }

        private void RaiseChanged() {
            RaisePropertyChanged(nameof(SignedIn));
            RaisePropertyChanged(nameof(AccountText));
            RaisePropertyChanged(nameof(ExpiryText));
            RaisePropertyChanged(nameof(ExpiringSoon));
            try { Changed?.Invoke(this, EventArgs.Empty); } catch (Exception ex) { Logger.Error("Starfront stacking: a sign-in handler failed", ex); }
        }
    }
}

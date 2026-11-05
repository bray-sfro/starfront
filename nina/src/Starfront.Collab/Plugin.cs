using System.ComponentModel.Composition;
using System.Diagnostics;
using System.Windows.Input;
using CommunityToolkit.Mvvm.Input;
using NINA.Astrometry.Interfaces;
using NINA.Core.Utility;
using NINA.Equipment.Interfaces.Mediator;
using NINA.Plugin;
using NINA.Plugin.Interfaces;
using NINA.Profile.Interfaces;
using NINA.WPF.Base.Interfaces.Mediator;
using Starfront.Collab.Services;
using Starfront.Collab.Settings;
using Starfront.Collab.Ui;

namespace Starfront.Collab {

    /// <summary>
    /// The plugin as N.I.N.A. sees it: identity from the assembly attributes
    /// (generated from Plugin.props), and the data context of the options page.
    ///
    /// N.I.N.A. composes this manifest and the plugin's other parts (the
    /// sequence container, the dock panel) in separate MEF containers, so an
    /// <c>[Import]</c>ed service here would not be the instance those parts
    /// get. Everything shared therefore lives in <see cref="Runtime"/>, a
    /// process-wide holder that whichever part comes first initialises with
    /// the host services - which *are* the same instances in every container.
    /// </summary>
    [Export(typeof(IPluginManifest))]
    public class Plugin : PluginBase {
        internal static event Action ShuttingDown;

        /// <summary>The invitation to the Starfront Discord server, as the website gives it. Signing in needs membership.</summary>
        public const string DiscordInvite = "https://discord.com/invite/3ut6a7pfsY";

        /// <summary>Open the Discord invitation in the browser.</summary>
        internal static void OpenDiscord() {
            try {
                Process.Start(new ProcessStartInfo(DiscordInvite) { UseShellExecute = true });
            } catch (Exception ex) {
                Logger.Error(ex);
            }
        }

        private readonly ExposureTableVM exposures;

        [ImportingConstructor]
        public Plugin(IProfileService profileService,
                      ICameraMediator cameraMediator,
                      ITelescopeMediator telescopeMediator,
                      IFilterWheelMediator filterWheelMediator,
                      IRotatorMediator rotatorMediator,
                      IGuiderMediator guiderMediator,
                      IImageSaveMediator imageSaveMediator,
                      INighttimeCalculator nighttimeCalculator) {
            Runtime.Ensure(new HostServices(profileService, cameraMediator, telescopeMediator,
                                            filterWheelMediator, rotatorMediator, guiderMediator,
                                            imageSaveMediator, nighttimeCalculator));
            exposures = new ExposureTableVM(Runtime.Settings, profileService);
            Stacking = new StackingOptionsVM(Runtime.Settings, Runtime.Stacking);
            SignInCommand = new AsyncRelayCommand(SignInAsync, () => !Runtime.Session.Busy);
            SignOutCommand = new AsyncRelayCommand(SignOutAsync, () => !Runtime.Session.Busy);
            RefreshCommand = new AsyncRelayCommand(RefreshAsync, () => !Runtime.Session.Busy);
            JoinDiscordCommand = new CommunityToolkit.Mvvm.Input.RelayCommand(OpenDiscord);
            Runtime.Session.PropertyChanged += (_, e) => {
                if (e.PropertyName == nameof(Services.StarfrontSession.Busy)) {
                    Ui.Dispatch.OnUi(() => {
                        SignInCommand.NotifyCanExecuteChanged();
                        SignOutCommand.NotifyCanExecuteChanged();
                        RefreshCommand.NotifyCanExecuteChanged();
                    });
                }
            };
        }

        public override Task Initialize() {
            Runtime.Start();
            Logger.Info($"Starfront plugin {Version} initialised; server {Runtime.Settings.ServerUrl}");
            return Task.CompletedTask;
        }

        public override Task Teardown() {
            try { ShuttingDown?.Invoke(); } catch (Exception ex) { Logger.Error(ex); }
            exposures.Dispose();
            Runtime.Shutdown();
            return Task.CompletedTask;
        }

        // -- what the options page binds to ------------------------------------

        public StarfrontSettings Settings => Runtime.Settings;
        public Services.StarfrontSession Session => Runtime.Session;
        public ExposureTableVM Exposures => exposures;
        /// <summary>The Live stacking section.</summary>
        public StackingOptionsVM Stacking { get; }

        public AsyncRelayCommand SignInCommand { get; }
        public AsyncRelayCommand SignOutCommand { get; }
        public AsyncRelayCommand RefreshCommand { get; }
        public ICommand JoinDiscordCommand { get; }

        private async Task SignInAsync() {
            try {
                await Runtime.Session.SignInAsync(CancellationToken.None);
            } catch (Exception ex) {
                Logger.Error("Starfront sign-in failed", ex);
                NINA.Core.Utility.Notification.Notification.ShowError($"Starfront: {ex.Message}");
            }
        }

        private async Task SignOutAsync() {
            try {
                await Runtime.Session.SignOutAsync(CancellationToken.None);
            } catch (Exception ex) {
                Logger.Error("Starfront sign-out failed", ex);
            }
        }

        private async Task RefreshAsync() {
            try {
                await Runtime.Session.PollAsync(CancellationToken.None);
            } catch (Exception ex) {
                Logger.Error("Starfront refresh failed", ex);
            }
        }

    }
}

using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Windows.Input;
using CommunityToolkit.Mvvm.Input;
using RelayCommand = CommunityToolkit.Mvvm.Input.RelayCommand;
using NINA.Core.Utility;
using RapidStackerCollab.Core.Upload;
using Starfront.Collab.Services.Stacking;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Ui {

    /// <summary>What the Live stacking section of the options page binds to.</summary>
    public sealed class StackingOptionsVM : BaseINPC {
        /// <summary>The upload modes on offer, in the order the list shows them. The code is what the setting stores.</summary>
        public static readonly IReadOnlyList<ModeChoice> ModeChoices = new[] {
            new ModeChoice(UploadMode.WhenIdle.ToString(), "When the camera is idle"),
            new ModeChoice(UploadMode.Immediately.ToString(), "As soon as a frame is saved"),
            new ModeChoice(UploadMode.TimeWindow.ToString(), "Only inside a daily time window"),
        };

        public sealed record ModeChoice(string Code, string Label);

        private readonly StackingUploader stacking;

        public StackingOptionsVM(StarfrontSettings settings, StackingUploader stacking) {
            Settings = settings;
            this.stacking = stacking;
            Links = new ObservableCollection<StackingLinkRowVM>();
            SignInCommand = new AsyncRelayCommand(SignInAsync, () => Settings.StackingEnabled && !SignIn.Busy);
            SignOutCommand = new AsyncRelayCommand(SignOutAsync, () => !SignIn.Busy);
            CancelSignInCommand = new RelayCommand(SignIn.Cancel, () => SignIn.Busy);
            OpenBrowserCommand = new RelayCommand(SignIn.OpenBrowser, () => SignIn.Waiting);
            SignIn.PropertyChanged += (_, e) => {
                if (e.PropertyName is nameof(StackingSignIn.Busy) or nameof(StackingSignIn.Waiting) or nameof(StackingSignIn.SignedIn)) Dispatch.OnUi(Refresh);
            };
            settings.PropertyChanged += (_, e) => {
                if (e.PropertyName is nameof(StarfrontSettings.StackingEnabled) or nameof(StarfrontSettings.StackingUploadMode) || string.IsNullOrEmpty(e.PropertyName)) Dispatch.OnUi(Refresh);
            };
            stacking.Links.Changed += (_, _) => Dispatch.OnUi(RebuildLinks);
            stacking.Changed += (_, _) => Dispatch.OnUi(RefreshLibrary);
            BrowseLibraryCommand = new RelayCommand(BrowseLibrary);
            SyncLibraryNowCommand = new AsyncRelayCommand(SyncLibraryNowAsync, () => stacking.Library?.CanSync == true);
            RebuildLinks();
        }

        public StarfrontSettings Settings { get; }
        public StackingSignIn SignIn => stacking.SignIn;
        public IReadOnlyList<ModeChoice> Modes => ModeChoices;

        /// <summary>The window rows are shown only when the mode uses them.</summary>
        public bool UsesWindow => Settings.StackingMode == UploadMode.TimeWindow;

        /// <summary>Every collaboration that names a stacking project, and how this telescope stands with it.</summary>
        public ObservableCollection<StackingLinkRowVM> Links { get; }
        public bool HasLinks => Links.Count > 0;

        // -- the calibration library ------------------------------------------

        /// <summary>What the library sync is doing, or what it needs first.</summary>
        public string LibraryStatusText => stacking.Library?.StatusText
            ?? (Settings.StackingEnabled ? "Starting..." : "Turn live stacking on to use the calibration library.");

        /// <summary>The counts of the last pass, or empty.</summary>
        public string LibraryDetailText => stacking.Library?.DetailText ?? "";

        /// <summary>Frames the stacking service could not use, and files that could not be read, one per line.</summary>
        public string LibraryProblemsText => stacking.Library?.ProblemsText ?? "";

        public RelayCommand BrowseLibraryCommand { get; }
        public AsyncRelayCommand SyncLibraryNowCommand { get; }

        private void RefreshLibrary() {
            RaisePropertyChanged(nameof(LibraryStatusText));
            RaisePropertyChanged(nameof(LibraryDetailText));
            RaisePropertyChanged(nameof(LibraryProblemsText));
            SyncLibraryNowCommand.NotifyCanExecuteChanged();
        }

        private void BrowseLibrary() {
            try {
                var dialog = new Microsoft.Win32.OpenFolderDialog { Title = "Your folder of bias, dark and dark-flat frames" };
                var current = Settings.StackingLibraryFolder;
                if (current.Length > 0 && System.IO.Directory.Exists(current)) dialog.InitialDirectory = current;
                if (dialog.ShowDialog() == true) Settings.StackingLibraryFolder = dialog.FolderName;
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: the folder could not be chosen", ex);
            }
        }

        private async Task SyncLibraryNowAsync() {
            try {
                if (stacking.Library is StackingLibrary library) await library.SyncAsync();
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: the calibration library could not be synced", ex);
            } finally {
                Dispatch.OnUi(RefreshLibrary);
            }
        }

        public AsyncRelayCommand SignInCommand { get; }
        public AsyncRelayCommand SignOutCommand { get; }
        public RelayCommand CancelSignInCommand { get; }
        public RelayCommand OpenBrowserCommand { get; }

        private void Refresh() {
            SignInCommand.NotifyCanExecuteChanged();
            SignOutCommand.NotifyCanExecuteChanged();
            CancelSignInCommand.NotifyCanExecuteChanged();
            OpenBrowserCommand.NotifyCanExecuteChanged();
            foreach (var row in Links) row.Refresh();
            RaisePropertyChanged(nameof(UsesWindow));
            RefreshLibrary();
        }

        private void RebuildLinks() {
            Links.Clear();
            foreach (var link in stacking.Links.All()) Links.Add(new StackingLinkRowVM(link, stacking, Settings));
            RaisePropertyChanged(nameof(HasLinks));
        }

        private async Task SignInAsync() {
            try {
                await SignIn.SignInAsync();
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: sign-in failed", ex);
                NINA.Core.Utility.Notification.Notification.ShowError($"Starfront stacking: {ex.Message}");
            }
        }

        private async Task SignOutAsync() {
            try {
                await SignIn.SignOutAsync();
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: sign-out failed", ex);
            }
        }
    }

    /// <summary>One linked collaboration on the options page: where it goes, how it stands, and the buttons that move it on.</summary>
    public sealed class StackingLinkRowVM : BaseINPC {
        private readonly StackingLink link;
        private readonly StackingUploader stacking;
        private readonly StarfrontSettings settings;
        private bool busy;

        public StackingLinkRowVM(StackingLink link, StackingUploader stacking, StarfrontSettings settings) {
            this.link = link;
            this.stacking = stacking;
            this.settings = settings;
            AcceptTermsCommand = new AsyncRelayCommand(AcceptTermsAsync, () => !busy && settings.StackingEnabled && TermsOutdated);
            RetryCommand = new AsyncRelayCommand(RetryAsync, () => !busy && settings.StackingEnabled && CanRetry);
            ReadTermsCommand = new RelayCommand(ReadTerms, () => BrowserUrls.CanOpen(link.TermsUrl));
        }

        public string Name => link.StarfrontName;
        public string Project => link.CollabName.Length > 0 ? link.CollabName : "not reached yet";
        public string StatusText => link.Describe();
        /// <summary>Where the project is stacked: "Cloud" (Rapid Stacker Collab) or "Hub: name".</summary>
        public string BackendText => !link.IsHub ? "Cloud"
            : "Hub: " + (link.HubName.Length > 0 ? link.HubName : link.HubId.Length > 0 ? link.HubId : "unnamed");
        public string MappingText => link.Mapping.Count == 0 ? ""
            : "Filters: " + string.Join(", ", link.Mapping.Select(kv => kv.Value == StackingMapping.Skip ? $"{kv.Key} not sent" : $"{kv.Key} as {kv.Value}"))
              + (link.IsHub ? "." : ". Change them on the Rapid Stacker Collab website.");
        public string RetryText => link.NextAttemptUtc is DateTime when && when > DateTime.UtcNow
            ? $"Tried again at {when.ToLocalTime():HH:mm}, or now with Retry." : "";

        public bool TermsOutdated => link.Status == LinkStatus.TermsOutdated;
        public bool CanRetry => link.Status is LinkStatus.Failed or LinkStatus.NeedsRig or LinkStatus.NoFilters;

        public AsyncRelayCommand AcceptTermsCommand { get; }
        public AsyncRelayCommand RetryCommand { get; }
        public RelayCommand ReadTermsCommand { get; }

        public void Refresh() {
            AcceptTermsCommand.NotifyCanExecuteChanged();
            RetryCommand.NotifyCanExecuteChanged();
        }

        private async Task AcceptTermsAsync() => await Run(() => stacking.Joiner.AcceptTermsAsync(link.StarfrontProjectId, CancellationToken.None));

        private async Task RetryAsync() => await Run(() => stacking.Joiner.RetryAsync(link.StarfrontProjectId, CancellationToken.None));

        private async Task Run(Func<Task> action) {
            busy = true;
            Refresh();
            try {
                await action();
            } catch (Exception ex) {
                Logger.Error($"Starfront stacking: {link.StarfrontName} could not be joined", ex);
            } finally {
                busy = false;
                Dispatch.OnUi(Refresh);
            }
        }

        private void ReadTerms() {
            try {
                Process.Start(new ProcessStartInfo(link.TermsUrl) { UseShellExecute = true });
            } catch (Exception ex) {
                Logger.Warning($"Starfront stacking: the terms could not be opened - {ex.Message}; they are at {link.TermsUrl}");
            }
        }
    }
}

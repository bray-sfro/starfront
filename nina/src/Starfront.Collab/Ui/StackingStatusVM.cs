using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.Input;
using RelayCommand = CommunityToolkit.Mvvm.Input.RelayCommand;
using NINA.Core.Utility;
using RapidStackerCollab.Core.Upload;
using Starfront.Collab.Services.Stacking;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Ui {

    /// <summary>
    /// The Live stacking lines of the Starfront panel: what is waiting, what
    /// is being sent, why the uploads are holding when they are, and the last
    /// thing that happened. Read from the queue every two seconds, but only
    /// while live stacking is on and the panel is open; a closed panel or an
    /// idle plugin reads nothing.
    /// </summary>
    public sealed class StackingStatusVM : BaseINPC, IDisposable {
        private readonly StackingUploader stacking;
        private readonly StarfrontSettings settings;
        private readonly Timer timer;
        private bool panelVisible;
        private string headline = "";
        private string currentText = "";
        private string reasonText = "";
        private string lastText = "";
        private string hint = "";
        private string calibrationText = "";

        public StackingStatusVM(StackingUploader stacking, StarfrontSettings settings) {
            this.stacking = stacking;
            this.settings = settings;
            timer = new Timer(_ => Dispatch.OnUi(Update), null, Timeout.Infinite, Timeout.Infinite);
            TogglePauseCommand = new RelayCommand(() => { stacking.TogglePause(); Update(); }, () => stacking.Running);
            UploadNowCommand = new RelayCommand(() => { stacking.UploadNow(); Update(); }, () => stacking.Running && !settings.StackingPaused);
            ResumeProjectCommand = new RelayCommand<HeldProject>(row => { if (row != null) stacking.ResumeProject(row.ProjectRef); Update(); }, _ => stacking.Running);
            stacking.Changed += OnChanged;
            stacking.SignIn.PropertyChanged += OnSignInChanged;
            settings.PropertyChanged += OnSettingChanged;
            Schedule();
            Update();
        }

        /// <summary>Shown while live stacking is on.</summary>
        public bool Visible => settings.StackingEnabled;
        public string Headline { get => headline; private set { if (headline != value) { headline = value; RaisePropertyChanged(); } } }
        public string CurrentText { get => currentText; private set { if (currentText != value) { currentText = value; RaisePropertyChanged(); } } }
        public string ReasonText { get => reasonText; private set { if (reasonText != value) { reasonText = value; RaisePropertyChanged(); } } }
        public string LastText { get => lastText; private set { if (lastText != value) { lastText = value; RaisePropertyChanged(); } } }
        /// <summary>Something the person has to do: sign in, accept terms, link a collaboration.</summary>
        public string Hint { get => hint; private set { if (hint != value) { hint = value; RaisePropertyChanged(); } } }
        /// <summary>Flats waiting for their night's lights, and the calibration library; empty when there is nothing to say.</summary>
        public string CalibrationText { get => calibrationText; private set { if (calibrationText != value) { calibrationText = value; RaisePropertyChanged(); } } }
        public string PauseLabel => settings.StackingPaused ? "Resume uploads" : "Pause uploads";

        public RelayCommand TogglePauseCommand { get; }
        public RelayCommand UploadNowCommand { get; }
        public RelayCommand<HeldProject> ResumeProjectCommand { get; }

        /// <summary>The projects whose uploads are held, each with its own Resume button; empty when none is.</summary>
        public ObservableCollection<HeldProject> Held { get; } = new();

        /// <summary>The panel was shown or hidden; the queue is read only while it is shown.</summary>
        public void PanelVisible(bool visible) {
            panelVisible = visible;
            Schedule();
            if (visible) Dispatch.OnUi(Update);
        }

        private void Schedule() {
            var on = panelVisible && stacking.Running;
            try { timer.Change(on ? TimeSpan.FromSeconds(2) : Timeout.InfiniteTimeSpan, on ? TimeSpan.FromSeconds(2) : Timeout.InfiniteTimeSpan); } catch (ObjectDisposedException) { }
        }

        private void OnChanged(object sender, EventArgs e) {
            Schedule();
            Dispatch.OnUi(Update);
        }

        private void OnSignInChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) => Dispatch.OnUi(Update);

        private void OnSettingChanged(object sender, System.ComponentModel.PropertyChangedEventArgs e) {
            if (e.PropertyName is nameof(StarfrontSettings.StackingEnabled) or nameof(StarfrontSettings.StackingPaused) || string.IsNullOrEmpty(e.PropertyName)) {
                Dispatch.OnUi(() => {
                    RaisePropertyChanged(nameof(Visible));
                    RaisePropertyChanged(nameof(PauseLabel));
                    Update();
                });
            }
        }

        /// <summary>On the UI thread. Never throws: a status line is not worth a broken panel.</summary>
        private void Update() {
            try {
                TogglePauseCommand.NotifyCanExecuteChanged();
                UploadNowCommand.NotifyCanExecuteChanged();
                ResumeProjectCommand.NotifyCanExecuteChanged();
                if (!settings.StackingEnabled) {
                    Headline = CurrentText = ReasonText = LastText = Hint = CalibrationText = "";
                    ShowHeld(Array.Empty<HeldProject>());
                    return;
                }
                if (!stacking.Running) {
                    Headline = "Live stacking is starting, or could not start; the N.I.N.A. log says which.";
                    CurrentText = ReasonText = LastText = CalibrationText = "";
                    ShowHeld(Array.Empty<HeldProject>());
                    Hint = HintText();
                    return;
                }
                var counts = stacking.Counts();
                int Count(string state) => counts.TryGetValue(state, out var n) ? n : 0;
                var waiting = Count(QueueStates.Pending);
                var parts = new List<string>();
                if (waiting > 0) parts.Add($"{waiting} waiting");
                if (Count(QueueStates.Uploading) > 0) parts.Add($"{Count(QueueStates.Uploading)} sending");
                var sent = Count(QueueStates.Uploaded) + Count(QueueStates.Duplicate);
                if (sent > 0) parts.Add($"{sent} sent");
                if (Count(QueueStates.Failed) > 0) parts.Add($"{Count(QueueStates.Failed)} failed");
                Headline = parts.Count == 0 ? "Nothing sent yet." : string.Join(" · ", parts);

                if (stacking.Current is (QueueItem item, UploadProgress progress)) {
                    var percent = progress.TotalBytes > 0 ? $" {100.0 * progress.BytesSent / progress.TotalBytes:0}%" : "";
                    CurrentText = $"{item.Filename}: {Stage(progress.Stage)}{percent}";
                } else {
                    CurrentText = "";
                }
                var decision = stacking.Decision;
                ReasonText = waiting > 0 && decision is { Allow: false } ? decision.Reason : "";
                LastText = stacking.LastResult;
                CalibrationText = stacking.CalibrationText();
                ShowHeld(stacking.HeldProjects());
                Hint = HintText();
            } catch (Exception ex) {
                Logger.Debug($"Starfront stacking: the panel could not be updated - {ex.Message}");
            }
        }

        /// <summary>Replace the rows only when they changed: rebuilt every two seconds, a button would be swapped out from under the click.</summary>
        private void ShowHeld(IReadOnlyList<HeldProject> now) {
            if (Held.SequenceEqual(now)) return;
            Held.Clear();
            foreach (var row in now) Held.Add(row);
        }

        private string HintText() {
            var signIn = stacking.SignIn;
            var links = stacking.Links.All();
            // Hub projects need no Rapid Stacker Collab sign-in: it is asked for only when a cloud link needs it, or nothing is linked yet.
            var needsCloud = links.Any(l => !l.IsHub) || !links.Any(l => l.IsHub && l.Grant.Length > 0);
            if (!settings.StackingSignedIn && needsCloud) return "Sign in to Rapid Stacker Collab under Options > Plugins > Starfront to start sending.";
            if (settings.StackingSignedIn && signIn.AuthRequired) return "Rapid Stacker Collab no longer accepts this sign-in; sign in again under Options > Plugins > Starfront.";
            var terms = links.Where(l => l.Status == LinkStatus.TermsOutdated).Select(l => l.StarfrontName).ToList();
            if (terms.Count > 0) return $"The terms of {string.Join(", ", terms)} changed; accept them under Options > Plugins > Starfront.";
            if (settings.StackingSignedIn && signIn.ExpiringSoon) return signIn.ExpiryText;
            if (links.Count == 0) return "None of your collaborations is stacked yet. Stacking follows the collaboration: its coordinator turns it on, on a hub or Rapid Stacker Collab, in Starfront's Collab tab.";
            return "";
        }

        private static string Stage(UploadStage stage) => stage switch {
            UploadStage.Hashing => "checking",
            UploadStage.Initialising => "starting",
            UploadStage.Uploading => "sending",
            UploadStage.Completing => "finishing",
            _ => "done",
        };

        public void Dispose() {
            stacking.Changed -= OnChanged;
            stacking.SignIn.PropertyChanged -= OnSignInChanged;
            settings.PropertyChanged -= OnSettingChanged;
            timer.Dispose();
        }
    }
}

using System.Collections.Concurrent;
using NINA.Core.Utility;
using NINA.Image.ImageData;
using NINA.WPF.Base.Interfaces.Mediator;
using Starfront.Collab.Ledger;
using Starfront.Collab.Server;
using Starfront.Collab.Sky;

namespace Starfront.Collab.Services {

    /// <summary>Which collaboration panel a target name stands for while the container shoots it.</summary>
    public sealed class PanelContext {
        public string TargetName { get; init; } = "";
        public string ProjectId { get; init; } = "";
        public string ProjectName { get; init; } = "";
        public string TaskId { get; init; } = "";
        public int PanelNumber { get; init; }
        public string Filter { get; init; } = "";
        public Region Footprint { get; init; }
        public double? Scale { get; init; }
        public double? FocalLength { get; init; }
        public bool Colour { get; init; }
        public double? Bandpass { get; init; }
        /// <summary>The project's star-size limit in arcseconds, if it set one; a frame over it is not credited.</summary>
        public double? MaxHfr { get; init; }
    }

    /// <summary>
    /// Credits every light N.I.N.A. saves to the panel it was shot over.
    ///
    /// The frame is matched to its panel by the target name N.I.N.A. stamps
    /// into the image's metadata, not by whatever the container happens to
    /// be doing when the save completes: saving runs behind capture, and the
    /// last frame of one panel is often written while the slew to the next
    /// is already under way.
    /// </summary>
    public sealed class FrameReporter : IDisposable {
        private readonly IImageSaveMediator images;
        private readonly FrameLedger ledger;
        private readonly ConcurrentDictionary<string, PanelContext> byTarget = new(StringComparer.Ordinal);
        private bool initialized;

        public FrameReporter(IImageSaveMediator images, FrameLedger ledger) {
            this.images = images;
            this.ledger = ledger;
        }

        public void Initialize() {
            if (initialized) return;
            images.ImageSaved += OnImageSaved;
            images.BeforeImageSaved += OnBeforeImageSaved;
            initialized = true;
        }

        /// <summary>When the last light was saved, whatever it was of: how the
        /// others' charts know this telescope is shooting even when the
        /// Starfront container is not the thing running.</summary>
        public DateTime? LastLightUtc { get; private set; }
        /// <summary>The target name on the last light saved, as N.I.N.A. stamped it.</summary>
        public string LastTarget { get; private set; } = "";
        /// <summary>The collaboration the last light belonged to, when it was one of ours.</summary>
        public string LastProject { get; private set; } = "";

        /// <summary>Where each saved collaboration light is handed, the moment it is saved. Null sends nothing.</summary>
        public Upload.LightUploader Uploader { get; set; }

        public void Register(PanelContext context) => byTarget[context.TargetName] = context;

        public void Forget(string targetName) => byTarget.TryRemove(targetName, out _);

        public void ForgetAll() => byTarget.Clear();

        public PanelContext Lookup(string targetName) =>
            !string.IsNullOrEmpty(targetName) && byTarget.TryGetValue(targetName, out var context) ? context : null;

        private Task OnBeforeImageSaved(object sender, BeforeImageSavedEventArgs args) {
            try {
                var context = Lookup(args.Image?.MetaData?.Target?.Name);
                if (context != null && args.Image.MetaData.Image.ImageType == "LIGHT") {
                    args.Image.MetaData.GenericHeaders.Add(new StringMetaDataHeader("STARFRNT", context.ProjectId, "Starfront collaboration"));
                    args.Image.MetaData.GenericHeaders.Add(new IntMetaDataHeader("SFPANEL", context.PanelNumber, "Starfront panel number"));
                    // The key the server's upload check reads (Starfront writes the same).
                    args.Image.MetaData.GenericHeaders.Add(new StringMetaDataHeader("PROJID", context.ProjectId, "Starfront collaboration id"));
                }
            } catch (Exception ex) {
                Logger.Debug($"Starfront: could not stamp the frame - {ex.Message}");
            }
            return Task.CompletedTask;
        }

        private void OnImageSaved(object sender, ImageSavedEventArgs args) {
            try {
                var metadata = args.MetaData;
                if (metadata?.Image == null || metadata.Image.ImageType != "LIGHT") return;
                var context = Lookup(metadata.Target?.Name);
                LastLightUtc = DateTime.UtcNow;
                LastTarget = (metadata.Target?.Name ?? "").Trim();
                LastProject = context?.ProjectId ?? "";
                if (context == null) return;

                var filter = Filters.Canonical(string.IsNullOrWhiteSpace(args.Filter) ? context.Filter : args.Filter);
                if (string.IsNullOrEmpty(filter)) filter = context.Filter;
                var stamp = metadata.Image.ExposureStart;
                var started = stamp == DateTime.MinValue ? DateTime.Now
                    : stamp.Kind == DateTimeKind.Utc ? stamp.ToLocalTime() : stamp;
                var night = SkyMath.NightName(started);
                var seconds = args.Duration > 0 ? args.Duration : metadata.Image.ExposureTime;
                if (double.IsNaN(seconds) || seconds <= 0) return;

                double? hfr = null;
                var hfrPixels = args.StarDetectionAnalysis?.HFR ?? double.NaN;
                if (!double.IsNaN(hfrPixels) && hfrPixels > 0 && context.Scale is double scale && scale > 0) hfr = hfrPixels * scale;

                double? rms = null;
                var recorded = metadata.Image.RecordedRMS;
                if (recorded != null && recorded.DataPoints > 0 && recorded.Total > 0) rms = recorded.Total * recorded.Scale;

                // The project's star-size rule, frame by frame. The server judges
                // a night by its mean, and a night of good subs with a trailed
                // hour in the middle used to pass as a whole or fail as a whole;
                // a sub over the limit is simply not credited, so the night's
                // report carries only the frames that meet the rule.
                if (context.MaxHfr is double limit && limit > 0 && hfr is double measured && measured > limit) {
                    Logger.Warning($"Starfront: {context.TargetName} {filter} +{seconds:0}s not credited - HFR {measured:0.00}\" is over the "
                                   + $"project's {limit:0.0}\" limit");
                    return;
                }

                // Straight onto the upload queue: this only appends to a list and
                // wakes the uploader's own thread, so the save never waits on it.
                var saved = args.PathToImage;
                if (Uploader != null && saved != null && saved.IsFile) {
                    Uploader.Enqueue(saved.LocalPath, context.ProjectId, context.TaskId);
                }

                ledger.Credit(night, context.ProjectId, context.TaskId, context.PanelNumber.ToString(), filter, seconds,
                              hfr, rms, context.Footprint, context.Scale, context.FocalLength, context.Colour, context.Bandpass);
                var shown = string.IsNullOrWhiteSpace(args.Filter) ? filter : args.Filter.Trim();
                Logger.Info($"Starfront: {context.TargetName} {shown} +{seconds:0}s"
                            + (hfr.HasValue ? $", HFR {hfr:0.00}\"" : "") + (rms.HasValue ? $", guiding {rms:0.00}\"" : ""));
            } catch (Exception ex) {
                Logger.Error("Starfront: a saved frame could not be credited", ex);
            }
        }

        public void Dispose() {
            if (!initialized) return;
            images.ImageSaved -= OnImageSaved;
            images.BeforeImageSaved -= OnBeforeImageSaved;
            initialized = false;
            byTarget.Clear();
        }
    }
}

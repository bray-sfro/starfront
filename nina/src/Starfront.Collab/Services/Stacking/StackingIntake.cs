using System.IO;
using System.Text;
using NINA.Core.Utility;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Frames;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Upload;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>Where a saved file goes.</summary>
    public enum IntakeRoute {
        /// <summary>Nowhere; the reason says why.</summary>
        Skip,
        /// <summary>To the link's stacking project.</summary>
        Project,
        /// <summary>To the flat router, which sends it after the night's lights.</summary>
        Flat,
        /// <summary>To this telescope's calibration library, which serves every project.</summary>
        Library,
    }

    /// <summary>
    /// What becomes of a saved file. <see cref="OtherTarget"/> marks a light
    /// that is not sent because it is of another target than any linked
    /// collaboration's; its night's flats are then not sent either.
    /// <see cref="Quiet"/> marks a skip that is no news (a switch that is
    /// off, a light that was never for a collaboration), which the log keeps
    /// to its debug level rather than repeat for every frame of a night.
    /// </summary>
    public sealed record IntakeDecision(IntakeRoute Route, string Reason, FrameKind Kind, InitHeader Header, bool OtherTarget = false, bool Quiet = false) {
        public bool Enqueue => Route == IntakeRoute.Project;
    }

    /// <summary>The switches the intake follows. <see cref="LibraryLive"/> is false without a rig: the library is the rig's.</summary>
    public sealed record IntakeOptions(bool UploadFlats, bool LibraryLive, bool IncludeCalibration);

    /// <summary>
    /// From a file on disk to a row in the upload queue: read the header,
    /// tell a light from a calibration frame from a snapshot, and send each
    /// where it belongs. Starfront already knows which collaboration a light
    /// belongs to, so a light goes to its collaboration's stacking project,
    /// unless it was pointed well away from that project's field (it shows
    /// another target, and the stacking service could only reject it). Flats
    /// go to the flat router, which sends each to the projects that got the
    /// lights of its filter and night; darks, bias and dark flats go to the
    /// rig's calibration library, or with the old switch to the project of
    /// the last light sent. The file is opened read-only and never written;
    /// the only thing written is the plugin's own queue.
    /// </summary>
    public sealed class StackingIntake {
        private readonly UploadQueue queue;
        private readonly StarfrontSettings settings;
        private readonly Func<string> rigId;
        private readonly TimeZoneInfo timeZone;

        public StackingIntake(UploadQueue queue, StarfrontSettings settings, TimeZoneInfo timeZone, Func<string> rigId = null) {
            this.rigId = rigId ?? (() => settings.StackingRigId);
            this.queue = queue;
            this.settings = settings;
            this.timeZone = timeZone;
        }

        /// <summary>Raised when a file was queued, so the runner is woken and held flats find their lights.</summary>
        public event Action<QueueItem> Enqueued;

        /// <summary>
        /// Never throws. <paramref name="link"/> is the collaboration's link for
        /// a light (null when it was not shot for a linked collaboration), and
        /// the last light's link for a calibration frame.
        /// </summary>
        public IntakeDecision Evaluate(string path, string filterHint, StackingLink link, IntakeOptions options) {
            InitHeader header;
            try {
                if (!File.Exists(path)) return Skip("the file is not there");
                var format = Sniff(path);
                if (format == Format.Other) return Skip("not a FITS or XISF file");
                header = format == Format.Xisf ? XisfHeaderReader.Read(path) : FitsHeaderReader.Read(path);
            } catch (Exception ex) when (ex is FitsFormatException or IOException or UnauthorizedAccessException) {
                return Skip("the header could not be read: " + ex.Message);
            } catch (Exception ex) {
                return Skip("the file could not be read: " + ex.Message);
            }

            if (string.IsNullOrWhiteSpace(header.Filter) && !string.IsNullOrWhiteSpace(filterHint)) header.Filter = filterHint.Trim();

            var kind = FrameKinds.Classify(header.ImageTyp);
            if (kind == FrameKind.Other) return Skip($"not a light or a calibration frame (IMAGETYP '{header.ImageTyp ?? "missing"}')", kind, header) with { Quiet = true };

            if (kind == FrameKind.Light) {
                if (link == null || !link.Feeds) return new IntakeDecision(IntakeRoute.Skip, "it was not shot for a linked collaboration", kind, header, OtherTarget: true, Quiet: true);
                if (link.Skips(header.Filter)) return Skip($"filter '{header.Filter}' is one {link.CollabName} does not want", kind, header);
                if (link.OtherTargetReason(header) is string why) {
                    return new IntakeDecision(IntakeRoute.Skip, why + "; frames of other targets are not sent", kind, header, OtherTarget: true);
                }
                return new IntakeDecision(IntakeRoute.Project, link.Status == LinkStatus.Pending ? $"queued for {link.CollabName} (waiting for its owner's approval)" : $"queued for {link.CollabName}", kind, header);
            }

            if (FrameKinds.IsFlat(kind)) {
                return options.UploadFlats
                    ? new IntakeDecision(IntakeRoute.Flat, "goes to the projects that got the lights of its filter and night", kind, header)
                    : Skip("sending flats is switched off", kind, header) with { Quiet = true };
            }

            if (IsLibraryKind(kind) && options.LibraryLive) return new IntakeDecision(IntakeRoute.Library, "queued for this telescope's calibration library", kind, header);
            if (!options.IncludeCalibration) return Skip("calibration frames are not sent", kind, header) with { Quiet = true };
            if (link == null || !link.Feeds) return Skip("calibration frames go to the project of the last light sent, and none has been sent", kind, header);
            if (!link.TakesCalibration) return Skip($"{link.CollabName} takes no calibration frames", kind, header);
            return new IntakeDecision(IntakeRoute.Project, $"queued for {link.CollabName}", kind, header);
        }

        /// <summary>Queue a decision for its link's project. Idempotent per project and path.</summary>
        public (QueueItem Item, bool Added)? Enqueue(string path, IntakeDecision decision, StackingLink link) {
            if (!decision.Enqueue || decision.Header == null || link == null) return null;
            return Add(new NewQueueItem(link.CollabProjectId, path, Path.GetFileName(path), decision.Kind, decision.Header, Rig(),
                                        FlatRouting.NightOf(decision.Header, timeZone) ?? "", FlatRouting.FilterKey(decision.Header.Filter)));
        }

        /// <summary>Queue a decision for this telescope's calibration library (row <c>lib:{rig}</c>). Idempotent per path.</summary>
        public (QueueItem Item, bool Added)? EnqueueLibrary(string path, IntakeDecision decision) {
            if (decision.Route != IntakeRoute.Library || decision.Header == null || Rig() is not string rig) return null;
            return Add(new NewQueueItem(LibraryRef.For(rig), path, Path.GetFileName(path), decision.Kind, decision.Header, rig, "", ""));
        }

        /// <summary>
        /// Remember a light of another target by rig, filter and local night,
        /// so the flats of that night that no queued light claims are not sent
        /// either. Idempotent per path; nothing for any other decision.
        /// </summary>
        public void RecordOtherTarget(string path, IntakeDecision decision) {
            if (!decision.OtherTarget || decision.Header == null) return;
            if (FlatRouting.NightOf(decision.Header, timeZone) is not string night) return;
            queue.AddOtherTargetLight(path, Rig(), FlatRouting.FilterKey(decision.Header.Filter), night, decision.Header.Object);
        }

        private (QueueItem Item, bool Added) Add(NewQueueItem item) {
            var result = queue.Enqueue(item);
            if (result.Added) {
                try { Enqueued?.Invoke(result.Item); } catch (Exception ex) { Logger.Error("Starfront stacking: an enqueue handler failed", ex); }
            }
            return result;
        }

        private string Rig() {
            var rig = rigId() ?? "";
            return rig.Length > 0 ? rig : null;
        }

        private static bool IsLibraryKind(FrameKind k) =>
            k is FrameKind.Dark or FrameKind.Bias or FrameKind.DarkFlat or FrameKind.MasterDark or FrameKind.MasterBias or FrameKind.MasterDarkFlat;

        private static IntakeDecision Skip(string reason, FrameKind kind = FrameKind.Other, InitHeader header = null) => new(IntakeRoute.Skip, reason, kind, header);

        private enum Format { Fits, Xisf, Other }

        private static Format Sniff(string path) {
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var bytes = new byte[8];
            var n = 0;
            while (n < bytes.Length) {
                var got = stream.Read(bytes, n, bytes.Length - n);
                if (got <= 0) break;
                n += got;
            }
            var text = Encoding.ASCII.GetString(bytes, 0, n);
            if (text.StartsWith("XISF0100", StringComparison.Ordinal)) return Format.Xisf;
            if (text.StartsWith("SIMPLE", StringComparison.Ordinal)) return Format.Fits;
            return Format.Other;
        }
    }
}

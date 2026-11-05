using NINA.Core.Enum;
using NINA.Equipment.Equipment.MyCamera;
using NINA.Equipment.Interfaces.Mediator;
using RapidStackerCollab.Core.Upload;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// What the camera is doing, for the upload policy: exposing or not, when
    /// the last image landed, and whether somebody pressed Upload now. The
    /// sequencer's own state is not read, which is why the engine's
    /// "after the sequence ends" mode is not offered.
    /// </summary>
    public sealed class ImagingStateTracker {
        private readonly object gate = new();
        private bool exposing;
        private DateTimeOffset? lastImageSaved;
        private bool uploadNow;

        /// <summary>Raised after any change, on the thread that made it, so the runner can ask the policy again.</summary>
        public event Action Changed;

        public void CameraExposing(bool value) => Mutate(() => exposing = value);

        public void ImageSaved() => Mutate(() => lastImageSaved = DateTimeOffset.Now);

        /// <summary>Upload now: overrides the mode, never the pause, until cleared.</summary>
        public void RequestUploadNow() => Mutate(() => uploadNow = true);

        public void ClearUploadNow() => Mutate(() => uploadNow = false);

        public ImagingState Snapshot() {
            lock (gate) return new ImagingState(exposing, false, null, lastImageSaved, uploadNow);
        }

        private void Mutate(Action change) {
            bool changed;
            lock (gate) {
                var before = (exposing, lastImageSaved, uploadNow);
                change();
                changed = before != (exposing, lastImageSaved, uploadNow);
            }
            if (!changed) return;
            try { Changed?.Invoke(); } catch (Exception) { }
        }
    }

    /// <summary>
    /// "Is an exposure in progress" comes from the camera: N.I.N.A. hands every
    /// camera update to its consumers, and the camera is busy while it exposes,
    /// reads out or downloads. Never throws into the camera's update loop.
    /// </summary>
    internal sealed class CameraStateConsumer : ICameraConsumer {
        private readonly ImagingStateTracker tracker;

        public CameraStateConsumer(ImagingStateTracker tracker) => this.tracker = tracker;

        public void UpdateDeviceInfo(CameraInfo deviceInfo) {
            try { tracker.CameraExposing(IsBusy(deviceInfo)); } catch (Exception) { }
        }

        internal static bool IsBusy(CameraInfo info) =>
            info is { Connected: true } && (info.IsExposing || info.CameraState is CameraStates.Exposing or CameraStates.Reading or CameraStates.Download);

        public void Dispose() { }
    }
}

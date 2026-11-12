using System.IO;
using NINA.Astrometry.Interfaces;
using NINA.Core.Utility;
using NINA.Equipment.Interfaces.Mediator;
using NINA.Profile.Interfaces;
using NINA.WPF.Base.Interfaces.Mediator;
using Starfront.Collab.Ledger;
using Starfront.Collab.Server;
using Starfront.Collab.Settings;

namespace Starfront.Collab.Services {

    /// <summary>The host services every part of the plugin needs, as N.I.N.A. hands them out.</summary>
    public sealed record HostServices(IProfileService Profile,
                                      ICameraMediator Camera,
                                      ITelescopeMediator Telescope,
                                      IFilterWheelMediator FilterWheel,
                                      IRotatorMediator Rotator,
                                      IGuiderMediator Guider,
                                      IImageSaveMediator ImageSave,
                                      INighttimeCalculator Nighttime);

    /// <summary>
    /// The one set of plugin services, shared by the manifest, the sequence
    /// container and the dock panel whatever MEF container each was composed in.
    /// Initialised by the first part that arrives; torn down by the manifest.
    /// </summary>
    public static class Runtime {
        private static readonly object Gate = new();

        public static bool Ready { get; private set; }
        public static HostServices Host { get; private set; }
        public static StarfrontSettings Settings { get; private set; }
        public static StarfrontClient Client { get; private set; }
        public static FrameLedger Ledger { get; private set; }
        public static FrameReporter Reporter { get; private set; }
        public static StarfrontSession Session { get; private set; }
        public static Upload.LightUploader Uploader { get; private set; }

        public static void Ensure(HostServices host) {
            lock (Gate) {
                if (Ready) return;
                Host = host;
                Settings = new StarfrontSettings(host.Profile);
                Settings.Load();
                Client = new StarfrontClient(Settings);
                Ledger = new FrameLedger(Path.Combine(Settings.DataFolder, "ledger.json"));
                Ledger.Load();
                Reporter = new FrameReporter(host.ImageSave, Ledger);
                var settings = Settings;
                Uploader = new Upload.LightUploader(Path.Combine(Settings.DataFolder, "uploads"),
                    () => settings.AutoUpload && settings.Enrolled,
                    new Upload.HttpUploadWire(() => settings.ServerUrl, () => settings.AgentToken),
                    (level, text) => { if (level == "warn") Logger.Warning(text); else Logger.Info(text); });
                Reporter.Uploader = Uploader;
                Session = new StarfrontSession(host, Settings, Client, Ledger, Reporter);
                Ready = true;
            }
        }

        /// <summary>Hook the image pipeline and start checking in. Idempotent.</summary>
        public static void Start() {
            lock (Gate) {
                if (!Ready) return;
                Reporter.Initialize();
                Session.Start();
                Uploader.Start();
            }
        }

        public static void Shutdown() {
            lock (Gate) {
                if (!Ready) return;
                try { Session.Stop(); } catch (Exception ex) { Logger.Error(ex); }
                try { Uploader?.Dispose(); } catch (Exception ex) { Logger.Error(ex); }
                try { Reporter.Dispose(); } catch (Exception ex) { Logger.Error(ex); }
                try { Ledger.Save(); } catch (Exception ex) { Logger.Error(ex); }
                Ready = false;
            }
        }
    }
}

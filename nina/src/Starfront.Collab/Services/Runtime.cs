using System.IO;
using NINA.Astrometry.Interfaces;
using NINA.Core.Utility;
using NINA.Equipment.Interfaces.Mediator;
using NINA.Profile.Interfaces;
using NINA.WPF.Base.Interfaces.Mediator;
using Starfront.Collab.Ledger;
using Starfront.Collab.Server;
using Starfront.Collab.Services.Stacking;
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
        /// <summary>Live stacking; built always, started only while its setting is on.</summary>
        public static StackingUploader Stacking { get; private set; }

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
                Session = new StarfrontSession(host, Settings, Client, Ledger, Reporter);
                Stacking = new StackingUploader(host, Settings, Reporter, Session);
                Ready = true;
            }
        }

        /// <summary>Hook the image pipeline and start checking in. Idempotent.</summary>
        public static void Start() {
            lock (Gate) {
                if (!Ready) return;
                Reporter.Initialize();
                Session.Start();
                try { Stacking.Apply(); } catch (Exception ex) { Logger.Error("Starfront stacking could not start", ex); }
            }
        }

        public static void Shutdown() {
            lock (Gate) {
                if (!Ready) return;
                // Stacking first: it is fed by the image pipeline the reporter
                // unhooks below, and nothing may land in a queue being closed.
                try { Stacking.Dispose(); } catch (Exception ex) { Logger.Error(ex); }
                try { Session.Stop(); } catch (Exception ex) { Logger.Error(ex); }
                try { Reporter.Dispose(); } catch (Exception ex) { Logger.Error(ex); }
                try { Ledger.Save(); } catch (Exception ex) { Logger.Error(ex); }
                Ready = false;
            }
        }
    }
}

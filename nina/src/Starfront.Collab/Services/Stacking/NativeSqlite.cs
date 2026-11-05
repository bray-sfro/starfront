using System.IO;
using System.Runtime.InteropServices;
using NINA.Core.Utility;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// SQLite's native library is looked up by name the first time the queue
    /// opens. N.I.N.A. adds a plugin's sub-folders to the native search path
    /// only after the plugin has initialised, which is too late, so the
    /// packaged <c>runtimes\win-x64\native\e_sqlite3.dll</c> is loaded by full
    /// path first: once a module of that name is in the process, the later
    /// lookup by name binds to it.
    /// </summary>
    internal static class NativeSqlite {
        private static bool done;

        public static bool Preload() {
            if (done) return true;
            try {
                var pluginDirectory = Path.GetDirectoryName(typeof(NativeSqlite).Assembly.Location) ?? "";
                var arch = RuntimeInformation.ProcessArchitecture == Architecture.Arm64 ? "win-arm64" : "win-x64";
                var path = Path.Combine(pluginDirectory, "runtimes", arch, "native", "e_sqlite3.dll");
                if (!File.Exists(path)) {
                    Logger.Warning($"Starfront stacking: e_sqlite3.dll is not at {path}; relying on the default native library lookup");
                    return false;
                }
                NativeLibrary.Load(path);
                done = true;
                return true;
            } catch (Exception ex) {
                Logger.Error("Starfront stacking: the bundled SQLite library could not be loaded; relying on the default lookup", ex);
                return false;
            }
        }
    }
}

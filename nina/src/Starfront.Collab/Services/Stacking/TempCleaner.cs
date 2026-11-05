using System.IO;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// Deletes the plugin's own leftover temporary copies, and only those: the
    /// runner's naming, in the plugin's own tmp folder, never recursing, so a
    /// person's file can never be touched.
    /// </summary>
    internal static class TempCleaner {
        public const string Pattern = "rsc-upload-*.tmp";

        public static int Sweep(string directory, TimeSpan olderThan, DateTimeOffset now) {
            var removed = 0;
            try {
                if (!Directory.Exists(directory)) return 0;
                foreach (var file in Directory.EnumerateFiles(directory, Pattern, SearchOption.TopDirectoryOnly)) {
                    try {
                        if (now - new DateTimeOffset(File.GetLastWriteTimeUtc(file), TimeSpan.Zero) < olderThan) continue;
                        File.Delete(file);
                        removed++;
                    } catch (IOException) {
                    } catch (UnauthorizedAccessException) {
                    }
                }
            } catch (IOException) {
            } catch (UnauthorizedAccessException) {
            }
            return removed;
        }
    }
}

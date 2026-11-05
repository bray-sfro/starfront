// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Tests.Support;

/// <summary>Deletes a test's temporary directory.</summary>
internal static class TempDirs
{
    /// <summary>
    /// Deletes <paramref name="dir"/>, retrying for about two seconds: on Windows another process (the CI runner's
    /// virus scanner or indexer) can hold a file the test has just closed, typically <c>queue.db</c> right after
    /// <c>UploadQueue.Dispose</c>. A handle the code itself leaked does not go away by waiting, so it still fails.
    /// </summary>
    public static void Delete(string dir)
    {
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                if (Directory.Exists(dir)) Directory.Delete(dir, true);
                return;
            }
            catch (Exception e) when ((e is IOException || e is UnauthorizedAccessException) && attempt < 20)
            {
                Thread.Sleep(10 * attempt + 10); // about 2 s in all
            }
        }
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using RapidStackerCollab.Core.Fits;
using RapidStackerCollab.Core.Upload;

namespace RapidStackerCollab.Core.Library;

/// <summary>
/// The SHA-256 of the stripped copy a library file would be uploaded as (what <c>GET .../library</c> lists as <c>frames[].sha256</c>), computed
/// once per file: the result is remembered in the queue database against the file's size and modified time, so a second sync of an unchanged
/// folder strips and hashes nothing. The strip is the one the upload runner applies (same options, same output for the same input), and always
/// on: a library frame is never uploaded with its location (README open point 104). The user's file is only read.
/// </summary>
public sealed class LibraryHasher
{
    private readonly UploadQueue queue;
    private readonly string tempDirectory;
    private readonly StripOptions strip;

    public LibraryHasher(UploadQueue queue, string tempDirectory, StripOptions? strip = null)
    {
        this.queue = queue;
        this.tempDirectory = tempDirectory;
        this.strip = strip ?? StripOptions.Default;
        Directory.CreateDirectory(tempDirectory);
    }

    /// <summary>The stripped SHA-256, from the cache when the file is unchanged. Throws <see cref="FitsFormatException"/>, <see cref="IOException"/> or <see cref="UnauthorizedAccessException"/> for a file that cannot be stripped.</summary>
    public async Task<string> StrippedSha256Async(LibraryCandidate file, CancellationToken ct = default)
    {
        if (queue.GetLibraryFile(file.Path) is { } cached && cached.Size == file.Size && cached.ModifiedMs == file.ModifiedMs)
            return cached.StrippedSha256;

        var temp = System.IO.Path.Combine(tempDirectory, $"rsc-library-{Guid.NewGuid():N}.fits.tmp");
        try
        {
            if (XisfHeaderReader.IsXisf(file.Path))
                XisfHeaderStripper.StripToFile(file.Path, temp, strip);
            else if (LooksLikeFits(file.Path))
                FitsHeaderStripper.StripToFile(file.Path, temp, strip);
            else
                throw new FitsFormatException("Only FITS and XISF files can be uploaded.");
            var digest = await FileDigest.ComputeAsync(temp, ct: ct).ConfigureAwait(false);
            queue.PutLibraryFile(new LibraryFileEntry(file.Path, file.Size, file.ModifiedMs, digest.Sha256));
            return digest.Sha256;
        }
        finally
        {
            try { File.Delete(temp); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }

    private static bool LooksLikeFits(string path)
    {
        try
        {
            using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            var b = new byte[6];
            return s.Read(b, 0, 6) == 6 && Encoding.ASCII.GetString(b) == "SIMPLE";
        }
        catch (IOException)
        {
            return false;
        }
    }
}

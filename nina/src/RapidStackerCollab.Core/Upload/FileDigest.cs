// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Security.Cryptography;

namespace RapidStackerCollab.Core.Upload;

/// <summary>SHA-256 (lower-case hex, as the contract's <c>Sha256</c> type) and size of a file.</summary>
public readonly record struct FileDigest(string Sha256, long Size)
{
    public static async Task<FileDigest> ComputeAsync(string path, IProgress<long>? progress = null, CancellationToken ct = default)
    {
        await using var s = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            1 << 16, FileOptions.Asynchronous | FileOptions.SequentialScan);
        using var h = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[1 << 20];
        long total = 0;
        int n;
        while ((n = await s.ReadAsync(buffer, ct).ConfigureAwait(false)) > 0)
        {
            h.AppendData(buffer, 0, n);
            total += n;
            progress?.Report(total);
        }
        return new FileDigest(Convert.ToHexString(h.GetHashAndReset()).ToLowerInvariant(), total);
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Net;

namespace RapidStackerCollab.Core.Upload;

/// <summary>
/// The body of one part PUT: a byte range of the file, streamed in 64 KiB pieces through the shared
/// <see cref="BandwidthLimiter"/>, with a known Content-Length (S3 presigned uploads must not use chunked encoding).
/// A fresh instance is made for every attempt, so a retry re-reads the file from the part's start.
/// </summary>
internal sealed class FilePartContent : HttpContent
{
    private const int Chunk = 64 * 1024;

    private readonly string path;
    private readonly long offset;
    private readonly long length;
    private readonly BandwidthLimiter limiter;
    private readonly Action<long> onBytes;

    public FilePartContent(string path, long offset, long length, BandwidthLimiter limiter, Action<long> onBytes)
    {
        this.path = path;
        this.offset = offset;
        this.length = length;
        this.limiter = limiter;
        this.onBytes = onBytes;
        Headers.ContentLength = length;
    }

    protected override bool TryComputeLength(out long len)
    {
        len = length;
        return true;
    }

    protected override Task SerializeToStreamAsync(Stream stream, TransportContext? context) =>
        SerializeToStreamAsync(stream, context, CancellationToken.None);

    protected override async Task SerializeToStreamAsync(Stream stream, TransportContext? context, CancellationToken ct)
    {
        await using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete,
            1 << 16, FileOptions.Asynchronous | FileOptions.SequentialScan);
        file.Seek(offset, SeekOrigin.Begin);
        var buffer = new byte[Chunk];
        var remaining = length;
        while (remaining > 0)
        {
            var want = (int)Math.Min(buffer.Length, remaining);
            var n = await file.ReadAsync(buffer.AsMemory(0, want), ct).ConfigureAwait(false);
            if (n == 0) throw new IOException("The file became shorter while it was being uploaded.");
            await limiter.AcquireAsync(n, ct).ConfigureAwait(false);
            await stream.WriteAsync(buffer.AsMemory(0, n), ct).ConfigureAwait(false);
            remaining -= n;
            onBytes(n);
        }
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Library;

public sealed record LibraryScanFile(string Path, long Size, long ModifiedMs);

/// <param name="Files">Settled FITS and XISF files, in path order.</param>
/// <param name="Unsettled">Files left for the next pass because they were still being written.</param>
/// <param name="Truncated">The folder has more than <see cref="LibraryScanner.MaxFiles"/> files; the rest were not looked at.</param>
public sealed record LibraryScanResult(IReadOnlyList<LibraryScanFile> Files, int Unsettled, bool Truncated);

/// <summary>
/// Lists the FITS and XISF files of the library folder, recursively. It lists only; it never opens a file for writing.
/// <para>
/// <b>Settling.</b> A file counts only when its modified time is at least <see cref="SettleFor"/> in the past and its size and modified time
/// are the same as the last time this scanner saw it, so a master still being written by a stacker, or a copy in progress, is picked up by
/// a later pass, not half uploaded. The plugin's own temporary folder is never scanned.
/// </para>
/// </summary>
public sealed class LibraryScanner
{
    public const int MaxFiles = 50_000;
    public static readonly TimeSpan SettleFor = TimeSpan.FromSeconds(30);
    public static readonly IReadOnlyList<string> Extensions = new[] { ".fits", ".fit", ".fts", ".xisf" };

    private readonly IClock clock;
    private readonly string? tempDirectory;
    private readonly object gate = new();
    private readonly Dictionary<string, (long Size, long ModifiedMs)> seen = new(StringComparer.OrdinalIgnoreCase);

    public LibraryScanner(IClock clock, string? tempDirectory = null)
    {
        this.clock = clock;
        this.tempDirectory = tempDirectory is null ? null : System.IO.Path.GetFullPath(tempDirectory);
    }

    public LibraryScanResult Scan(string folder, CancellationToken ct = default)
    {
        var files = new List<LibraryScanFile>();
        var unsettled = 0;
        var truncated = false;
        if (!Directory.Exists(folder)) return new LibraryScanResult(files, 0, false);
        var options = new EnumerationOptions { RecurseSubdirectories = true, IgnoreInaccessible = true, AttributesToSkip = FileAttributes.System };
        var now = clock.Now;
        foreach (var file in Directory.EnumerateFiles(folder, "*", options).Order(StringComparer.OrdinalIgnoreCase))
        {
            ct.ThrowIfCancellationRequested();
            if (!Extensions.Contains(System.IO.Path.GetExtension(file), StringComparer.OrdinalIgnoreCase)) continue;
            var full = System.IO.Path.GetFullPath(file);
            if (tempDirectory is not null && full.StartsWith(tempDirectory, StringComparison.OrdinalIgnoreCase)) continue;
            if (files.Count + unsettled >= MaxFiles) { truncated = true; break; }
            FileInfo info;
            try
            {
                info = new FileInfo(full);
                if (!info.Exists) continue;
            }
            catch (IOException) { continue; }
            catch (UnauthorizedAccessException) { continue; }

            var mtime = new DateTimeOffset(info.LastWriteTimeUtc, TimeSpan.Zero);
            var modifiedMs = mtime.ToUnixTimeMilliseconds();
            bool unchanged;
            lock (gate)
            {
                unchanged = !seen.TryGetValue(full, out var before) || before == (info.Length, modifiedMs);
                seen[full] = (info.Length, modifiedMs);
            }
            if (!unchanged || now - mtime < SettleFor || info.Length == 0)
            {
                unsettled++;
                continue;
            }
            files.Add(new LibraryScanFile(full, info.Length, modifiedMs));
        }
        return new LibraryScanResult(files, unsettled, truncated);
    }
}

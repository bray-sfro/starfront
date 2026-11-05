// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using RapidStackerCollab.Core.Library;
using RapidStackerCollab.Core.Tests.Fakes;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

public class LibraryScannerTests
{
    private string dir = null!;
    private FakeClock clock = null!;

    [SetUp]
    public void SetUp()
    {
        dir = Directory.CreateTempSubdirectory("rsc-libscan-").FullName;
        clock = new FakeClock();
    }

    [TearDown]
    public void TearDown() => TempDirs.Delete(dir);

    private string Write(string relative, int seed, TimeSpan? age = null, string? content = null)
    {
        var path = Path.Combine(dir, relative.Replace('/', Path.DirectorySeparatorChar)); // the scanner returns native separators
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        if (content is not null) File.WriteAllText(path, content);
        else FitsBuilder.Calibration(seed: seed).WriteTo(Path.GetDirectoryName(path)!, Path.GetFileName(path));
        File.SetLastWriteTimeUtc(path, (clock.Now - (age ?? TimeSpan.FromMinutes(10))).UtcDateTime);
        return path;
    }

    [Test]
    public void It_finds_fits_and_xisf_files_recursively_and_nothing_else_in_path_order()
    {
        var b = Write("sub/deeper/b.fit", 2);
        var a = Write("a.fits", 1);
        Write("notes.txt", 3, content: "hello");
        var x = Write("z.XISF", 4, content: "XISF0100");
        var result = new LibraryScanner(clock).Scan(dir);
        Assert.That(result.Files.Select(f => f.Path), Is.EqualTo(new[] { a, b, x }.OrderBy(p => p, StringComparer.OrdinalIgnoreCase)));
        Assert.That(result.Unsettled, Is.Zero);
        Assert.That(result.Truncated, Is.False);
    }

    [Test]
    public void A_file_modified_within_thirty_seconds_waits_for_a_later_pass()
    {
        Write("old.fits", 1);
        Write("fresh.fits", 2, age: TimeSpan.FromSeconds(10));
        var scanner = new LibraryScanner(clock);
        var first = scanner.Scan(dir);
        Assert.That(first.Files.Select(f => Path.GetFileName(f.Path)), Is.EqualTo(new[] { "old.fits" }));
        Assert.That(first.Unsettled, Is.EqualTo(1));

        clock.Advance(TimeSpan.FromSeconds(25));
        Assert.That(scanner.Scan(dir).Files, Has.Count.EqualTo(2), "35 s after its last write it is settled, and unchanged since the last pass");
    }

    [Test]
    public void A_file_whose_size_or_time_changed_since_the_last_pass_is_not_settled_even_when_its_time_is_old()
    {
        var path = Write("growing.fits", 1);
        var scanner = new LibraryScanner(clock);
        Assert.That(scanner.Scan(dir).Files, Has.Count.EqualTo(1));

        // A copy that keeps the source's old modified time but is still growing.
        File.AppendAllText(path, "more bytes");
        File.SetLastWriteTimeUtc(path, (clock.Now - TimeSpan.FromMinutes(10)).UtcDateTime);
        var second = scanner.Scan(dir);
        Assert.That(second.Files, Is.Empty);
        Assert.That(second.Unsettled, Is.EqualTo(1));
        Assert.That(scanner.Scan(dir).Files, Has.Count.EqualTo(1), "unchanged on the next pass");
    }

    [Test]
    public void The_plugins_temp_folder_inside_the_library_folder_is_never_scanned_and_empty_files_wait()
    {
        Write("tmp/rsc-upload-1.fits.tmp", 1);
        Write("tmp/other.fits", 2);
        Write("real.fits", 3);
        File.WriteAllBytes(Path.Combine(dir, "empty.fits"), Array.Empty<byte>());
        File.SetLastWriteTimeUtc(Path.Combine(dir, "empty.fits"), (clock.Now - TimeSpan.FromHours(1)).UtcDateTime);
        var result = new LibraryScanner(clock, Path.Combine(dir, "tmp")).Scan(dir);
        Assert.That(result.Files.Select(f => Path.GetFileName(f.Path)), Is.EqualTo(new[] { "real.fits" }));
        Assert.That(result.Unsettled, Is.EqualTo(1));
    }

    [Test]
    public void A_missing_folder_is_an_empty_result_not_an_error()
    {
        var result = new LibraryScanner(clock).Scan(Path.Combine(dir, "nope"));
        Assert.That(result.Files, Is.Empty);
    }
}

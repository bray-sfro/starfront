// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using RapidStackerCollab.Core.Frames;

namespace RapidStackerCollab.Core.Tests;

public class SavedFrameDispatcherTests
{
    private static SavedFrame Frame(string path) => new(path, "Ha", 300, false, "FITS");

    [Test]
    public async Task Frames_are_handled_in_order_off_the_posting_thread()
    {
        using var d = new SavedFrameDispatcher();
        var seen = new List<(string Path, int Thread)>();
        d.SetHandler(f => { lock (seen) seen.Add((f.Path, Environment.CurrentManagedThreadId)); return Task.CompletedTask; });
        var poster = Environment.CurrentManagedThreadId;
        for (var i = 0; i < 20; i++) d.Post(Frame($"/f{i}.fits"));
        await d.DrainAsync();
        Assert.That(seen.Select(s => s.Path), Is.EqualTo(Enumerable.Range(0, 20).Select(i => $"/f{i}.fits")));
        Assert.That(seen.All(s => s.Thread != poster));
    }

    [Test]
    public async Task A_throwing_handler_is_counted_and_does_not_stop_later_frames()
    {
        using var d = new SavedFrameDispatcher();
        var ok = 0;
        d.SetHandler(f =>
        {
            if (f.Path == "/bad.fits") throw new InvalidOperationException("boom");
            Interlocked.Increment(ref ok);
            return Task.CompletedTask;
        });
        d.Post(Frame("/a.fits"));
        d.Post(Frame("/bad.fits"));
        d.Post(Frame("/b.fits"));
        await d.DrainAsync();
        Assert.That(ok, Is.EqualTo(2));
        Assert.That(d.HandlerErrors, Is.EqualTo(1));
        Assert.That(d.LastError, Is.InstanceOf<InvalidOperationException>());
    }

    [Test]
    public async Task Frames_posted_with_no_handler_are_dropped_without_error()
    {
        using var d = new SavedFrameDispatcher();
        d.Post(Frame("/a.fits"));
        await d.DrainAsync();
        Assert.That(d.HandlerErrors, Is.Zero);
    }
}

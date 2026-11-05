// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Threading.Channels;

namespace RapidStackerCollab.Core.Frames;

/// <summary>
/// Moves saved-frame notifications off N.I.N.A.'s image-save thread onto one background reader, in order, and
/// makes sure a faulty handler can never throw back into N.I.N.A.
/// </summary>
public sealed class SavedFrameDispatcher : IDisposable
{
    private readonly Channel<SavedFrame> channel = Channel.CreateUnbounded<SavedFrame>(
        new UnboundedChannelOptions { SingleReader = true, SingleWriter = false });
    private readonly CancellationTokenSource cts = new();
    private readonly Task pump;
    private volatile Func<SavedFrame, Task>? handler;
    private int errors;

    public SavedFrameDispatcher()
    {
        pump = Task.Run(PumpAsync);
    }

    /// <summary>Frames whose handler threw. They are dropped, not retried.</summary>
    public int HandlerErrors => Volatile.Read(ref errors);

    /// <summary>Last handler error, for the dock panel's diagnostics line.</summary>
    public Exception? LastError { get; private set; }

    /// <summary>Set (or clear with null) the handler. Frames posted while none is set are dropped.</summary>
    public void SetHandler(Func<SavedFrame, Task>? newHandler) => handler = newHandler;

    /// <summary>Queue a frame; never blocks and never throws.</summary>
    public void Post(SavedFrame frame) => channel.Writer.TryWrite(frame);

    /// <summary>Wait until everything posted so far has been handled (tests, shutdown).</summary>
    public async Task DrainAsync(CancellationToken ct = default)
    {
        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        // A sentinel frame with a null path marks the drain point.
        if (!channel.Writer.TryWrite(new SavedFrame(DrainMarker, null, 0, false, null)))
            return;
        lock (drains) drains.Enqueue(done);
        await done.Task.WaitAsync(ct).ConfigureAwait(false);
    }

    private const string DrainMarker = "\0drain";
    private readonly Queue<TaskCompletionSource> drains = new();

    private async Task PumpAsync()
    {
        try
        {
            await foreach (var frame in channel.Reader.ReadAllAsync(cts.Token).ConfigureAwait(false))
            {
                if (frame.Path == DrainMarker)
                {
                    TaskCompletionSource? t;
                    lock (drains) drains.TryDequeue(out t);
                    t?.TrySetResult();
                    continue;
                }
                var h = handler;
                if (h is null) continue;
                try
                {
                    await h(frame).ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    LastError = ex;
                    Interlocked.Increment(ref errors);
                }
            }
        }
        catch (OperationCanceledException)
        {
        }
    }

    public void Dispose()
    {
        channel.Writer.TryComplete();
        cts.Cancel();
        try { pump.Wait(TimeSpan.FromSeconds(2)); } catch { /* shutting down */ }
        cts.Dispose();
    }
}

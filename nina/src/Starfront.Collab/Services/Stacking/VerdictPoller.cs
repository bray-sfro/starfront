using NINA.Core.Utility;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Upload;

namespace Starfront.Collab.Services.Stacking {

    /// <summary>
    /// What the stacking service made of each frame sent: accepted, rejected
    /// and why, or quarantined, read from its frame list and written into the
    /// queue so the dock can say. A row is asked about until its verdict is
    /// final.
    /// </summary>
    internal sealed class VerdictPoller {
        private const int MaxSingleLookups = 20;

        private readonly Func<IApiClient> api;
        private readonly UploadQueue queue;

        public VerdictPoller(Func<IApiClient> api, UploadQueue queue) {
            this.api = api;
            this.queue = queue;
        }

        /// <summary>One pass over everything awaiting a verdict; how many rows learnt something.</summary>
        public async Task<int> PollAsync(CancellationToken ct) {
            var awaiting = queue.AwaitingVerdict(200);
            var updated = 0;
            var singles = 0;
            var client = api();
            foreach (var group in awaiting.GroupBy(a => a.ProjectRef)) {
                IReadOnlyDictionary<string, Frame> byId = new Dictionary<string, Frame>();
                try {
                    var list = await client.ListFramesAsync(group.Key, new FrameListQuery { Mine = true, Limit = 100 }, ct).ConfigureAwait(false);
                    byId = list.Frames.GroupBy(f => f.FrameId).ToDictionary(g => g.Key, g => g.First());
                } catch (ApiException ex) {
                    Logger.Debug($"Starfront stacking: verdicts for {group.Key} could not be read - {ex.Code}");
                    continue;
                }
                foreach (var item in group) {
                    ct.ThrowIfCancellationRequested();
                    if (item.FrameId == null) continue;
                    if (!byId.TryGetValue(item.FrameId, out var frame)) {
                        if (singles >= MaxSingleLookups) continue;
                        singles++;
                        try {
                            frame = await client.GetFrameAsync(group.Key, item.FrameId, ct).ConfigureAwait(false);
                        } catch (ApiException ex) {
                            if (ex.Code == ApiCodes.NotFound) queue.SetVerdict(item.Id, FrameStatuses.Withdrawn, "the server no longer has this frame");
                            continue;
                        }
                    }
                    var detail = Detail(frame);
                    if (frame.Status == item.Verdict && detail == item.VerdictDetail) continue;
                    queue.SetVerdict(item.Id, frame.Status, detail);
                    updated++;
                    if (FrameStatuses.IsFinalVerdict(frame.Status) && frame.Status is not (FrameStatuses.Accepted or FrameStatuses.Accumulated or FrameStatuses.InGold)) {
                        Logger.Info($"Starfront stacking: {item.Filename} {frame.Status}" + (detail == null ? "" : $" - {detail}"));
                    }
                }
            }
            return updated;
        }

        private static string Detail(Frame f) {
            var parts = new List<string>();
            if (!string.IsNullOrEmpty(f.RejectReason)) parts.Add(Humanise(f.RejectReason));
            if (!string.IsNullOrEmpty(f.RejectDetail)) parts.Add(f.RejectDetail);
            if (!string.IsNullOrEmpty(f.QuarantineReason)) parts.Add(Humanise(f.QuarantineReason));
            return parts.Count == 0 ? null : string.Join(" - ", parts);
        }

        /// <summary><c>no_usable_stars</c> reads "no usable stars".</summary>
        public static string Humanise(string code) => (code ?? "").Replace('_', ' ');
    }
}

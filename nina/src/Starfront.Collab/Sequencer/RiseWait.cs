namespace Starfront.Collab.Sequencer {

    /// <summary>What the collaboration container should do after the fields
    /// that are already up have been tried.</summary>
    public enum RiseWaitAction {
        /// <summary>One or more skipped fields are up and Moon-safe; shoot them.</summary>
        ShootReady,
        /// <summary>A skipped field will still clear the floor tonight; wait and check again.</summary>
        Wait,
        /// <summary>Nothing left that will rise before dawn, or waiting is off.</summary>
        Stop
    }

    /// <summary>One field of tonight's deal, as the wait looks at it right now.</summary>
    public sealed record RiseWaitField(
        string Name,
        double MinAltitude,
        double HorizonAltitude,
        bool SkippedForAltitude,
        bool WillClearFloorTonight,
        bool IsUpNow,
        bool IsMoonSafe,
        DateTime? RiseAt) {
        public double Floor => Math.Max(MinAltitude, HorizonAltitude);
    }

    /// <summary>The next step of the wait, and the words to show for it.</summary>
    public sealed record RiseWaitStatus(RiseWaitAction Action, string Message, RiseWaitField Field = null);

    /// <summary>
    /// Whether to wait for a skipped collaboration field to rise, and the
    /// status line to show while doing it. No N.I.N.A. types, so the cases
    /// can be checked without the host.
    /// </summary>
    public static class RiseWait {

        /// <summary>How often the container re-checks the sky while waiting.</summary>
        public static TimeSpan PollInterval { get; set; } = TimeSpan.FromMinutes(1);

        public static RiseWaitStatus Decide(IReadOnlyList<RiseWaitField> panels, DateTime now, DateTime dawn, bool waitEnabled) {
            if (!waitEnabled) return new RiseWaitStatus(RiseWaitAction.Stop, "");
            var skipped = (panels ?? Array.Empty<RiseWaitField>()).Where(p => p.SkippedForAltitude && p.WillClearFloorTonight).ToList();
            if (skipped.Count == 0) return new RiseWaitStatus(RiseWaitAction.Stop, "");
            var ready = skipped.Where(p => p.IsUpNow && p.IsMoonSafe).ToList();
            if (ready.Count > 0) {
                return new RiseWaitStatus(RiseWaitAction.ShootReady, $"starting {ready[0].Name}", ready[0]);
            }
            if (now >= dawn) return new RiseWaitStatus(RiseWaitAction.Stop, "dawn reached");
            var soonest = skipped
                .OrderBy(p => p.IsUpNow ? 0 : 1)
                .ThenBy(p => p.RiseAt ?? DateTime.MaxValue)
                .ThenBy(p => p.Name)
                .First();
            string message;
            if (!soonest.IsUpNow) {
                var when = soonest.RiseAt is DateTime rise ? $" (~{rise:HH:mm})" : "";
                message = $"Waiting for {soonest.Name} to rise above {soonest.Floor:0}°{when}";
            } else {
                message = $"Waiting for {soonest.Name} to clear the Moon";
            }
            return new RiseWaitStatus(RiseWaitAction.Wait, message, soonest);
        }
    }
}

using Starfront.Collab.Sequencer;
using Xunit;

namespace Starfront.Collab.Tests {

    public sealed class RiseWaitTests {

        private static readonly DateTime Night = new(2026, 11, 12, 20, 0, 0);
        private static readonly DateTime Dawn = new(2026, 11, 13, 5, 30, 0);
        private static readonly DateTime RiseAt = new(2026, 11, 12, 22, 10, 0);

        private static RiseWaitField Field(string name, bool skipped, bool willClear, bool up, bool moonSafe, DateTime? rise, double minAlt = 30, double horizon = 0)
            => new(name, minAlt, horizon, skipped, willClear, up, moonSafe, rise);

        [Fact]
        public void TargetThatRisesLater_WaitsThenIsReady() {
            var down = Field("M31", skipped: true, willClear: true, up: false, moonSafe: true, rise: RiseAt);
            var waiting = RiseWait.Decide(new[] { down }, Night, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.Wait, waiting.Action);
            Assert.Equal("Waiting for M31 to rise above 30° (~22:10)", waiting.Message);

            var up = down with { IsUpNow = true, RiseAt = RiseAt };
            var ready = RiseWait.Decide(new[] { up }, RiseAt, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.ShootReady, ready.Action);
        }

        [Fact]
        public void NeverRisesTonight_DoesNotWait() {
            var never = Field("M31", skipped: true, willClear: false, up: false, moonSafe: true, rise: null);
            var status = RiseWait.Decide(new[] { never }, Night, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.Stop, status.Action);
            Assert.Equal("", status.Message);
        }

        [Fact]
        public void DawnReached_Stops() {
            var down = Field("M31", skipped: true, willClear: true, up: false, moonSafe: true, rise: Dawn.AddHours(1));
            var status = RiseWait.Decide(new[] { down }, Dawn, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.Stop, status.Action);
            Assert.Equal("dawn reached", status.Message);
        }

        [Fact]
        public async Task Cancel_StopsAtOnce() {
            using var cts = new CancellationTokenSource();
            var polls = 0;
            var delaying = new TaskCompletionSource();
            async Task Loop() {
                var token = cts.Token;
                while (true) {
                    token.ThrowIfCancellationRequested();
                    polls++;
                    var down = Field("M31", skipped: true, willClear: true, up: false, moonSafe: true, rise: RiseAt);
                    var status = RiseWait.Decide(new[] { down }, Night, Dawn, waitEnabled: true);
                    if (status.Action != RiseWaitAction.Wait) return;
                    var wait = Task.Delay(Timeout.InfiniteTimeSpan, token);
                    delaying.TrySetResult();
                    await wait;
                }
            }
            var running = Loop();
            await delaying.Task;
            cts.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => running);
            Assert.Equal(1, polls);
        }

        [Fact]
        public void MosaicWithSomeFieldsUp_DoesNotWaitOnThose() {
            var up = Field("M31 panel 1", skipped: false, willClear: true, up: true, moonSafe: true, rise: Night, minAlt: 30);
            var later = Field("M31 panel 2", skipped: true, willClear: true, up: false, moonSafe: true, rise: RiseAt, minAlt: 30);
            // After the first pass, only the still-down panel is skipped. Decide
            // waits for that one; the one already up is not in the wait set.
            var status = RiseWait.Decide(new[] { up, later }, Night, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.Wait, status.Action);
            Assert.Equal("M31 panel 2", status.Field.Name);
            Assert.Contains("M31 panel 2", status.Message);
            // The already-up field is not skipped, so a first pass that only
            // contains it is told to stop waiting (it should already have been shot).
            var onlyUp = RiseWait.Decide(new[] { up }, Night, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.Stop, onlyUp.Action);
        }

        [Fact]
        public void WaitingOff_StopsEvenIfAFieldWillRise() {
            var down = Field("M31", skipped: true, willClear: true, up: false, moonSafe: true, rise: RiseAt);
            var status = RiseWait.Decide(new[] { down }, Night, Dawn, waitEnabled: false);
            Assert.Equal(RiseWaitAction.Stop, status.Action);
        }

        [Fact]
        public void UpButMoonUnsafe_WaitsToClearTheMoon() {
            var close = Field("M31", skipped: true, willClear: true, up: true, moonSafe: false, rise: Night);
            var status = RiseWait.Decide(new[] { close }, Night, Dawn, waitEnabled: true);
            Assert.Equal(RiseWaitAction.Wait, status.Action);
            Assert.Equal("Waiting for M31 to clear the Moon", status.Message);
        }
    }
}

using NUnit.Framework;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// The plugin's form refuses a join code exactly when the Starfront server
    /// would (server/app.py, _stacking_join_code; tools/check_server.py has the
    /// server's half), so a coordinator learns of a typo before saving.
    /// </summary>
    public class JoinCodesTests {
        [TestCase(" h4rt 92bk-wnpc ", "H4RT-92BK-WNPC")]
        [TestCase("H4RT92BKWNPC", "H4RT-92BK-WNPC")]
        [TestCase("wnpc-h4rt-92bk", "WNPC-H4RT-92BK")]
        public void A_code_is_read_whatever_its_case_and_spacing(string typed, string canonical) {
            Assert.That(JoinCodes.TryNormalize(typed, out var result), Is.True);
            Assert.That(result, Is.EqualTo(canonical));
        }

        [TestCase("")]
        [TestCase("   ")]
        [TestCase(null)]
        public void Blank_is_no_code_and_is_allowed(string typed) {
            Assert.That(JoinCodes.TryNormalize(typed, out var result), Is.True);
            Assert.That(result, Is.EqualTo(""));
        }

        [TestCase("H4RT-92BK-WNP1", Description = "1 is not in the alphabet")]
        [TestCase("H4RT-92BK-WNPO", Description = "nor is O")]
        [TestCase("H4RT-92BK-WNPI", Description = "nor I")]
        [TestCase("H4RT-92BK-WNPL", Description = "nor L")]
        [TestCase("H4RT-92BK-WNP0", Description = "nor 0")]
        [TestCase("H4RT-92BK", Description = "too short")]
        [TestCase("H4RT-92BK-WNPC-X", Description = "too long")]
        [TestCase("nope")]
        public void A_code_that_cannot_be_real_is_refused(string typed) {
            Assert.That(JoinCodes.TryNormalize(typed, out _), Is.False);
        }
    }
}

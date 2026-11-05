using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// A light shot for a linked collaboration but pointed somewhere else.
    /// The stacking service cannot place it and rejects it, after it has been
    /// uploaded and counted against the person's quota; on 2026-10-21 sixteen
    /// Bubble Nebula lights went to an M33 project that way. Not sending them
    /// is right; not sending a light of the project's own target would lose
    /// a night's data, so each case that must still go is here too.
    /// </summary>
    public class StackingTargetsTests {
        // M33 as the live project has it: 3840 x 2160 px at 1.1876"/px, half-diagonal 0.73 degrees.
        private static readonly Grid M33Grid = new() {
            CenterRaDeg = 23.47155, CenterDecDeg = 30.66404, RotationDeg = 31.42, ScaleArcsecPx = 1.1876, WidthPx = 3840, HeightPx = 2160,
        };

        private static string Reason(InitHeader header, bool pending = false, Grid grid = null) =>
            StackingTargets.OtherTargetReason(header, StackingTargets.Project("prj_m33", "M33 LRGB", "M33", grid ?? M33Grid, pending));

        [Test]
        public void A_light_of_the_Bubble_Nebula_is_not_sent_to_M33_and_the_reason_says_how_far_it_was() {
            var why = Reason(new InitHeader { Object = "Bubble Nebula", Ra = 350.204, Dec = 61.201, ObjctRa = "23 20 48", ObjctDec = "+61 12 06" });
            Assert.That(why, Does.Contain("another target ('Bubble Nebula')").And.Contain("from the field of M33 LRGB"));
        }

        [Test]
        public void A_light_of_the_target_goes_even_when_only_part_of_its_header_agrees() {
            Assert.That(Reason(new InitHeader { Object = "M33_FRA600", Ra = 23.414, Dec = 30.662 }), Is.Null, "on the field");
            Assert.That(Reason(new InitHeader { Object = "M33 wide", Ra = 25.0, Dec = 30.66 }), Is.Null, "off the frame but within its half-diagonal plus a degree");
            Assert.That(Reason(new InitHeader { Object = "M33_UC131", Ra = 27.0, Dec = 31.5, ObjctRa = "01 33 51", ObjctDec = "+30 39 36" }), Is.Null,
                "the mount lost its sync but the target's coordinates are M33's");
            Assert.That(Reason(new InitHeader { Object = "M33", Ra = 120.0, Dec = -10.0 }), Is.Null, "OBJECT names the project's target");
            Assert.That(Reason(new InitHeader { Object = "Bubble Nebula" }), Is.Null, "no position: nothing says it is elsewhere");
        }

        [Test]
        public void A_frame_waiting_for_its_first_light_allows_three_degrees_and_a_project_without_a_frame_judges_nothing() {
            var twoAndAHalfOut = new InitHeader { Object = "x", Ra = 23.47155, Dec = 30.66404 + 2.5 };
            Assert.That(Reason(twoAndAHalfOut), Is.Not.Null);
            Assert.That(Reason(twoAndAHalfOut, pending: true), Is.Null, "a placeholder centre is given more room");
            Assert.That(StackingTargets.Project("prj_m33", "M33", "M33", null, false), Is.Null);
            Assert.That(StackingTargets.OtherTargetReason(new InitHeader { Ra = 350.2, Dec = 61.2 }, null), Is.Null);
        }
    }
}

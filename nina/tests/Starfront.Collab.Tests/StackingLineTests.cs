using Newtonsoft.Json;
using NUnit.Framework;
using Starfront.Collab.Server;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// The card's live-stacking line. It used to hang on <c>stackingLinked</c>
    /// (a cloud join code), so a hub-stacked collaboration had no line, every line
    /// said Rapid Stacker Collab, and a cloud project still being made, or one
    /// Collab refused, showed nothing while the plugin skipped it in silence.
    /// </summary>
    public class StackingLineTests {
        private static ProjectListing Listing(string json) => JsonConvert.DeserializeObject<ProjectListing>(json);

        [Test]
        public void HubLineNamesTheHub() {
            var listing = Listing("""
                {"id":"p1","name":"M33","stackingLinked":false,"stackingJoinCode":"",
                 "stacking":{"backend":"hub","hubName":"Garden hub","collabProjectId":"c-9","grant":"g","state":"ready"}}
                """);
            Assert.That(StackingLine.Describe(listing, true, null, true),
                Is.EqualTo("Live-stacked on the hub Garden hub; joining at the next check-in."));
            Assert.That(StackingLine.Describe(listing, false, null, false),
                Is.EqualTo("Live-stacked on the hub Garden hub; turn on Live stacking in the options to send yours."));
        }

        [Test]
        public void HubThatIsGoneSaysSo() {
            var listing = Listing("""{"id":"p1","stacking":{"backend":"hub","state":"failed"}}""");
            Assert.That(StackingLine.Describe(listing, true, null, true), Does.Contain("no longer registered"));
        }

        [Test]
        public void CloudReadyWithALink() {
            var listing = Listing("""
                {"id":"p1","stackingLinked":true,"stackingJoinCode":"H4RT-92BK-WNPC",
                 "stacking":{"backend":"cloud","managed":true,"state":"ready"}}
                """);
            var link = new StackingLink { CollabName = "M33 wide", Status = LinkStatus.Active };
            Assert.That(StackingLine.Describe(listing, true, link, true),
                Is.EqualTo("Live-stacked on Rapid Stacker Collab: Joined M33 wide."));
        }

        [Test]
        public void CloudPendingSaysItIsBeingSetUp() {
            var listing = Listing("""
                {"id":"p1","stackingLinked":false,"stackingJoinCode":"",
                 "stacking":{"backend":"cloud","managed":true,"state":"pending"}}
                """);
            // Said whether or not this plugin has Live stacking on: it is about the collaboration.
            foreach (var enabled in new[] { true, false })
                Assert.That(StackingLine.Describe(listing, enabled, null, true),
                    Is.EqualTo("Live-stacked on Rapid Stacker Collab; the stacking project is being set up, and lights join once it is ready."));
        }

        [Test]
        public void CloudFailedGivesTheOwnerTheReason() {
            var owners = Listing("""
                {"id":"p1","stacking":{"backend":"cloud","managed":true,"state":"failed",
                 "problem":"the collaboration's owner has no working Rapid Stacker Collab link: link the account again in the Collab tab."}}
                """);
            Assert.That(StackingLine.Describe(owners, true, null, true), Is.EqualTo(
                "Live-stacked on Rapid Stacker Collab, but the stacking project could not be set up yet: " +
                "the collaboration's owner has no working Rapid Stacker Collab link: link the account again in the Collab tab."));
            var others = Listing("""{"id":"p1","stacking":{"backend":"cloud","managed":true,"state":"failed","problem":""}}""");
            Assert.That(StackingLine.Describe(others, true, null, true), Does.EndWith("the owner's Starfront Collab tab says why."));
        }

        [Test]
        public void OldServerStillReadsAsCloud() {
            // No stacking object: the owner's telescope has the code, a browsing one only the yes-or-no.
            Assert.That(StackingLine.Describe(Listing("""{"id":"p1","stackingLinked":true,"stackingJoinCode":"H4RT-92BK-WNPC"}"""), true, null, false),
                Is.EqualTo("Live-stacked on Rapid Stacker Collab."));
            Assert.That(StackingLine.Describe(Listing("""{"id":"p1","stackingLinked":true,"stackingJoinCode":""}"""), true, null, false),
                Is.EqualTo("Live-stacked on Rapid Stacker Collab."));
        }

        [Test]
        public void UnstackedIsBlank() {
            Assert.That(StackingLine.Describe(Listing("""{"id":"p1","stackingLinked":false}"""), true, null, true), Is.EqualTo(""));
            Assert.That(StackingLine.Describe(Listing("""{"id":"p1","stacking":{"backend":"","state":""}}"""), true, null, true), Is.EqualTo(""));
        }

        [Test]
        public void StateNeverChangesTheRoute() {
            var pending = Listing("""{"id":"p1","stacking":{"backend":"hub","state":"pending","grant":"g","collabProjectId":"c"}}""");
            Assert.That(JoinRoutes.Of(pending, false), Is.EqualTo(JoinRoute.Hub));
        }
    }
}

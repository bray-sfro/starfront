using Newtonsoft.Json;
using NUnit.Framework;
using Starfront.Collab.Server;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// Per-project backends: the listing carries a <c>stacking</c> binding, old
    /// servers only a join code, and links saved before hubs existed must load
    /// and read as cloud links.
    /// </summary>
    public class StackingBindingTests {
        [Test]
        public void ListingWithStackingObjectDeserialises() {
            var json = """
                {"id":"p1","name":"M33","stackingLinked":true,"stackingJoinCode":"",
                 "stacking":{"backend":"hub","hubId":"h-1","hubName":"Garden hub","hubLanUrl":"http://192.168.1.20:8080",
                             "apiBase":"https://example.test/stack/api/","collabProjectId":"c-9","grant":"g.tok"}}
                """;
            var listing = JsonConvert.DeserializeObject<ProjectListing>(json);
            var b = listing.EffectiveStacking();
            Assert.That(b.Backend, Is.EqualTo("hub"));
            Assert.That(b.HubId, Is.EqualTo("h-1"));
            Assert.That(b.HubName, Is.EqualTo("Garden hub"));
            Assert.That(b.HubLanUrl, Is.EqualTo("http://192.168.1.20:8080"));
            Assert.That(b.ApiBase, Is.EqualTo("https://example.test/stack/api/"));
            Assert.That(b.CollabProjectId, Is.EqualTo("c-9"));
            Assert.That(b.Grant, Is.EqualTo("g.tok"));
        }

        [Test]
        public void ListingWithOnlyAJoinCodeReadsAsCloud() {
            var listing = JsonConvert.DeserializeObject<ProjectListing>("""{"id":"p1","stackingLinked":true,"stackingJoinCode":"ABC-123"}""");
            Assert.That(listing.Stacking, Is.Null);
            var b = listing.EffectiveStacking();
            Assert.That(b.Backend, Is.EqualTo("cloud"));
            Assert.That(b.JoinCode, Is.EqualTo("ABC-123"));
        }

        [Test]
        public void ListingWithNothingIsNotStacked() {
            var b = JsonConvert.DeserializeObject<ProjectListing>("""{"id":"p1"}""").EffectiveStacking();
            Assert.That(b.Backend, Is.EqualTo(""));
        }

        [Test]
        public void OldLinksFileLoadsAsCloudLinks() {
            var json = """
                [{"StarfrontProjectId":"p1","StarfrontName":"M33","JoinCode":"ABC-123","CollabProjectId":"c1","CollabName":"M33 stack",
                  "Status":"active","Message":"","RigId":"r1","Mapping":{"L":"Lum"},"UpdatedUtc":"2026-10-01T00:00:00Z"}]
                """;
            var link = JsonConvert.DeserializeObject<List<StackingLink>>(json)[0];
            Assert.That(link.Backend, Is.EqualTo("cloud"));
            Assert.That(link.IsHub, Is.False);
            Assert.That(link.ApiBase, Is.EqualTo(""));
            Assert.That(link.LanApiBase, Is.EqualTo(""));
            Assert.That(link.Grant, Is.EqualTo(""));
            Assert.That(link.GrantExpiresUtc, Is.Null);
            Assert.That(link.HubId, Is.EqualTo(""));
            Assert.That(link.Active, Is.True);
        }

        [Test]
        public void HubLinkRoundTrips() {
            var link = new StackingLink { StarfrontProjectId = "p1", Backend = "hub", HubId = "h", Grant = "g", GrantExpiresUtc = new DateTime(2026, 11, 1, 0, 0, 0, DateTimeKind.Utc) };
            var back = JsonConvert.DeserializeObject<StackingLink>(JsonConvert.SerializeObject(link));
            Assert.That(back.IsHub, Is.True);
            Assert.That(back.Grant, Is.EqualTo("g"));
            Assert.That(back.GrantExpiresUtc, Is.EqualTo(link.GrantExpiresUtc));
        }
    }
}

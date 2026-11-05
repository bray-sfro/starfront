using System.Net;
using System.Net.Http;
using NUnit.Framework;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    internal sealed class FakeHandler : HttpMessageHandler {
        public Func<HttpRequestMessage, HttpResponseMessage> Respond = _ => new HttpResponseMessage(HttpStatusCode.NotFound);
        public List<string> Urls = new();
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct) {
            Urls.Add(request.RequestUri!.ToString());
            return Task.FromResult(Respond(request));
        }
    }

    [TestFixture]
    public class LanProbeTests {
        private const string Lan = "http://192.168.1.20:8080";
        private static StackingLink Hub() => new() {
            StarfrontProjectId = "s1", StarfrontName = "M31", Backend = "hub", HubId = "hub-1",
            ApiBase = "https://starfront.example/stack/api/", LanApiBase = Lan,
        };
        private static HttpResponseMessage Json(string body, HttpStatusCode code = HttpStatusCode.OK) =>
            new(code) { Content = new StringContent(body, System.Text.Encoding.UTF8, "application/json") };

        private static (LanProbe, FakeHandler, List<string>) Make(Func<HttpRequestMessage, HttpResponseMessage> respond, Func<bool> trust = null) {
            var handler = new FakeHandler { Respond = respond };
            var lines = new List<string>();
            return (new LanProbe(new HttpClient(handler), trust ?? (() => true), lines.Add), handler, lines);
        }

        [Test]
        public async Task UnreachableUsesTheServer() {
            var (probe, _, lines) = Make(_ => throw new HttpRequestException("no route"));
            var link = Hub();
            await probe.CheckAsync(new[] { link });
            Assert.That(probe.EffectiveBase(link), Is.EqualTo(link.ApiBase));
            Assert.That(lines, Is.Empty);
        }

        [Test]
        public async Task WrongHubIdUsesTheServer() {
            var (probe, _, _) = Make(_ => Json("{\"hub_id\":\"someone-else\"}"));
            var link = Hub();
            await probe.CheckAsync(new[] { link });
            Assert.That(probe.EffectiveBase(link), Is.EqualTo(link.ApiBase));
        }

        [Test]
        public async Task NonSuccessOrNonJsonUsesTheServer() {
            var (p1, _, _) = Make(_ => Json("{\"hub_id\":\"hub-1\"}", HttpStatusCode.ServiceUnavailable));
            var (p2, _, _) = Make(_ => Json("<html>hub-1</html>"));
            var link = Hub();
            await p1.CheckAsync(new[] { link });
            await p2.CheckAsync(new[] { link });
            Assert.That(p1.EffectiveBase(link), Is.EqualTo(link.ApiBase));
            Assert.That(p2.EffectiveBase(link), Is.EqualTo(link.ApiBase));
        }

        [Test]
        public async Task RightHubIdUsesTheLan() {
            var (probe, handler, lines) = Make(_ => Json("{\"hub_id\":\"hub-1\"}"));
            var link = Hub();
            await probe.CheckAsync(new[] { link });
            Assert.That(probe.EffectiveBase(link), Is.EqualTo(Lan + "/api/"));
            Assert.That(handler.Urls, Is.EqualTo(new[] { Lan + "/api/hub/v1/health" }));
            Assert.That(lines, Has.Count.EqualTo(1));
            Assert.That(lines[0], Does.StartWith("Starfront stacking: "));
        }

        [Test]
        public async Task TrustOffNeverProbesAndUsesTheServer() {
            var (probe, handler, _) = Make(_ => Json("{\"hub_id\":\"hub-1\"}"), () => false);
            var link = Hub();
            await probe.CheckAsync(new[] { link });
            Assert.That(probe.EffectiveBase(link), Is.EqualTo(link.ApiBase));
            Assert.That(handler.Urls, Is.Empty);
        }

        [Test]
        public async Task SwitchingBackLogsOnceAndNoChangeLogsNothing() {
            var up = true;
            var (probe, _, lines) = Make(_ => up ? Json("{\"hub_id\":\"hub-1\"}") : throw new HttpRequestException());
            var link = Hub();
            await probe.CheckAsync(new[] { link });
            await probe.CheckAsync(new[] { link });
            Assert.That(lines, Has.Count.EqualTo(1));
            up = false;
            await probe.CheckAsync(new[] { link });
            Assert.That(lines, Has.Count.EqualTo(2));
            Assert.That(probe.EffectiveBase(link), Is.EqualTo(link.ApiBase));
        }

        [Test]
        public async Task CloudLinksAndLinksWithoutLanAreNotProbed() {
            var (probe, handler, _) = Make(_ => Json("{\"hub_id\":\"hub-1\"}"));
            var cloud = new StackingLink { StarfrontProjectId = "c", Backend = "cloud", LanApiBase = Lan, HubId = "hub-1" };
            var noLan = Hub(); noLan.LanApiBase = "";
            await probe.CheckAsync(new[] { cloud, noLan });
            Assert.That(handler.Urls, Is.Empty);
        }

        [Test]
        public async Task TheCacheGivesANewClientAfterASwitchAndTheSameOneWhenNothingChanged() {
            var up = true;
            var (probe, _, _) = Make(_ => up ? Json("{\"hub_id\":\"hub-1\"}") : throw new HttpRequestException());
            var cache = new ClientCache<object>();
            var link = Hub();
            object Get() => cache.For(probe.EffectiveBase(link), "grant:s1", () => new object());
            await probe.CheckAsync(new[] { link });
            var onLan = Get();
            await probe.CheckAsync(new[] { link });
            Assert.That(Get(), Is.SameAs(onLan));
            up = false;
            await probe.CheckAsync(new[] { link });
            var onServer = Get();
            Assert.That(onServer, Is.Not.SameAs(onLan));
            up = true;
            await probe.CheckAsync(new[] { link });
            Assert.That(Get(), Is.SameAs(onLan));
        }

        [Test]
        public async Task AFrameFinishesOnTheClientItStartedOn() {
            var r = new FakeRoutes();
            r.Add("s1", "cp", "hub", "g");
            var first = r.PerLink["s1"];
            var router = new RoutingApiClient(r);
            var lan = FakeClient.Make("lan");
            r.PerLink["s1"] = lan;
            await router.InitFrameAsync("cp", null);
            r.PerLink["s1"] = first; // the base switched mid-frame
            await router.CompleteFrameAsync("cp", "f1", null);
            Assert.That(FakeClient.CallsOf(lan), Does.Contain("CompleteFrameAsync"));
            Assert.That(FakeClient.CallsOf(first), Does.Not.Contain("CompleteFrameAsync"));
        }
    }
}

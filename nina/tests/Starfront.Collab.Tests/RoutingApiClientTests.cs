using System.Reflection;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>A fake client that records which of its methods were called, built on a proxy so it needs no body per method.</summary>
    public class FakeClient : DispatchProxy {
        public string Name = "";
        public List<string> Calls = new();

        public static IApiClient Make(string name) {
            var proxy = Create<IApiClient, FakeClient>();
            ((FakeClient)(object)proxy).Name = name;
            return proxy;
        }

        public static List<string> CallsOf(IApiClient client) => ((FakeClient)(object)client).Calls;

        protected override object Invoke(MethodInfo targetMethod, object[] args) {
            Calls.Add(targetMethod.Name);
            var type = targetMethod.ReturnType;
            if (type == typeof(Task)) return Task.CompletedTask;
            var result = type.GetGenericArguments()[0];
            if (result == typeof(FrameInitResponse)) return Task.FromResult(new FrameInitResponse { FrameId = "f1" });
            return typeof(Task).GetMethod(nameof(Task.FromResult))!.MakeGenericMethod(result).Invoke(null, new object[] { result.IsValueType ? Activator.CreateInstance(result) : null });
        }
    }

    internal sealed class FakeRoutes : IStackingRoutes {
        public List<StackingLink> Links = new();
        public string DefaultBackend { get; set; } = "cloud";
        public IApiClient Cloud { get; set; } = FakeClient.Make("cloud");
        public string CloudRig = "";
        public List<string> Refused = new();
        public Dictionary<string, IApiClient> PerLink = new();

        public StackingLink LinkOfProject(string project) => Links.FirstOrDefault(l => l.CollabProjectId == project);
        public IApiClient ForLink(StackingLink link) => PerLink.TryGetValue(link.StarfrontProjectId, out var c) ? c : Cloud;
        public StackingLink DefaultHubLink() => Links.FirstOrDefault(l => l.IsHub && l.Grant.Length > 0);
        public string RigIdOf(StackingLink hubOrNull) => hubOrNull == null ? CloudRig : RigIds.OfHub(hubOrNull, Links);
        public void HubRefused(StackingLink link) {
            Refused.Add(link.StarfrontProjectId);
            new GrantTokenStore(link.StarfrontProjectId, id => Links.FirstOrDefault(l => l.StarfrontProjectId == id), _ => { }).Clear();
        }
        public StackingLink Add(string id, string collab, string backend, string grant = "") {
            var link = new StackingLink { StarfrontProjectId = id, CollabProjectId = collab, Backend = backend, Grant = grant, ApiBase = backend == "hub" ? "https://hub/" : "" };
            Links.Add(link);
            PerLink[id] = backend == "hub" ? FakeClient.Make("hub-" + id) : Cloud;
            return link;
        }
    }

    [TestFixture]
    public class RoutingApiClientTests {
        private static string Who(FakeRoutes r, IApiClient c) => c == r.Cloud ? "cloud" : ((FakeClient)(object)c).Name;

        [Test]
        public async Task ProjectCallsGoToTheLinksClient() {
            var r = new FakeRoutes();
            r.Add("s1", "cp-hub", "hub", "g");
            r.Add("s2", "cp-cloud", "cloud");
            var router = new RoutingApiClient(r);
            await router.InitFrameAsync("cp-hub", null);
            await router.GetProjectStatusAsync("cp-hub");
            await router.GetMembershipAsync("cp-cloud");
            Assert.That(FakeClient.CallsOf(r.PerLink["s1"]), Is.EqualTo(new[] { "InitFrameAsync", "GetProjectStatusAsync" }));
            Assert.That(FakeClient.CallsOf(r.Cloud), Is.EqualTo(new[] { "GetMembershipAsync" }));
        }

        [Test]
        public async Task AnUnknownProjectGoesToTheCloud() {
            var r = new FakeRoutes();
            r.Add("s1", "cp-hub", "hub", "g");
            await new RoutingApiClient(r).GetProjectAsync("somebody-elses");
            Assert.That(FakeClient.CallsOf(r.Cloud), Is.EqualTo(new[] { "GetProjectAsync" }));
            Assert.That(FakeClient.CallsOf(r.PerLink["s1"]), Is.Empty);
        }

        [Test]
        public async Task DeviceAndRevokeCallsGoToTheCloudWhateverTheDefault() {
            var r = new FakeRoutes { DefaultBackend = "hub" };
            r.Add("s1", "cp-hub", "hub", "g");
            var router = new RoutingApiClient(r);
            await router.DeviceStartAsync(null);
            await router.DevicePollAsync("x");
            await router.RevokeOwnTokenAsync();
            await router.ResolveJoinCodeAsync("c");
            Assert.That(FakeClient.CallsOf(r.Cloud), Has.Count.EqualTo(4));
            Assert.That(FakeClient.CallsOf(r.PerLink["s1"]), Is.Empty);
        }

        [Test]
        public async Task RigAndLibraryCallsGoToTheDefaultBackend() {
            var r = new FakeRoutes { DefaultBackend = "hub" };
            r.Add("s1", "cp-hub", "hub", "g");
            var router = new RoutingApiClient(r);
            await router.ListRigsAsync();
            await router.GetLibraryAsync("rig");
            await router.NormalizeFiltersAsync(new[] { "L" });
            Assert.That(FakeClient.CallsOf(r.PerLink["s1"]), Has.Count.EqualTo(3));
            Assert.That(FakeClient.CallsOf(r.Cloud), Is.Empty);

            r.DefaultBackend = "cloud";
            await router.ListRigsAsync();
            await router.InitLibraryFrameAsync("rig", null);
            Assert.That(FakeClient.CallsOf(r.Cloud), Has.Count.EqualTo(2));
        }

        [Test]
        public async Task ADefaultOfHubWithoutAHubLinkFallsBackToTheCloud() {
            var r = new FakeRoutes { DefaultBackend = "hub" };
            r.Add("s2", "cp-cloud", "cloud");
            await new RoutingApiClient(r).ListRigsAsync();
            Assert.That(FakeClient.CallsOf(r.Cloud), Is.EqualTo(new[] { "ListRigsAsync" }));
        }

        [Test]
        public async Task NormaliseWithAProjectFollowsTheProject() {
            var r = new FakeRoutes { DefaultBackend = "cloud" };
            r.Add("s1", "cp-hub", "hub", "g");
            await new RoutingApiClient(r).NormalizeFiltersAsync(new[] { "L" }, "cp-hub");
            Assert.That(FakeClient.CallsOf(r.PerLink["s1"]), Has.Count.EqualTo(1));
        }

        [Test]
        public void ACacheReturnsTheSameClientForTheSameKey() {
            var cache = new ClientCache<object>();
            var a = cache.For("https://hub/api/", "grant:s1", () => new object());
            var b = cache.For("https://HUB/api", "grant:s1", () => new object());
            var c = cache.For("https://hub/api/", "grant:s2", () => new object());
            var d = cache.For("https://other/api/", "grant:s1", () => new object());
            Assert.That(b, Is.SameAs(a));
            Assert.That(c, Is.Not.SameAs(a));
            Assert.That(d, Is.Not.SameAs(a));
            Assert.That(cache.Count, Is.EqualTo(3));
        }

        [Test]
        public void TheGrantStoreReturnsTheGrantAndClearMarksTheLink() {
            var link = new StackingLink { StarfrontProjectId = "s1", Backend = "hub", Grant = "g-1", Status = LinkStatus.Active, NextAttemptUtc = DateTime.UtcNow.AddHours(1) };
            var saved = new List<StackingLink>();
            var store = new GrantTokenStore("s1", id => id == "s1" ? link : null, saved.Add);
            Assert.That(store.Load(), Is.EqualTo("g-1"));
            store.Save("g-2");
            Assert.That(store.Load(), Is.EqualTo("g-2"));
            store.Clear();
            Assert.That(store.Load(), Is.Null);
            Assert.That(link.Status, Is.EqualTo(LinkStatus.GrantRefresh));
            Assert.That(link.NextAttemptUtc, Is.Null, "fetched at the next check-in, not waited for");
            Assert.That(saved, Has.Count.EqualTo(2));
            Assert.DoesNotThrow(() => new GrantTokenStore("gone", _ => null, _ => { }).Clear());
        }
    }
}

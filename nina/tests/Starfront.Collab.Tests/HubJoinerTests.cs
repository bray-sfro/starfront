using System.Reflection;
using System.Text;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Filters;
using Starfront.Collab.Server;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>A hub client whose answers the test scripts; anything unscripted returns null and is recorded.</summary>
    public class ScriptedClient : DispatchProxy {
        public List<string> Calls = new();
        public Dictionary<string, Func<object[], object>> Script = new();
        public List<JoinRequest> Joins = new();

        public static (IApiClient, ScriptedClient) Make() {
            var proxy = Create<IApiClient, ScriptedClient>();
            return (proxy, (ScriptedClient)(object)proxy);
        }

        protected override object Invoke(MethodInfo method, object[] args) {
            Calls.Add(method.Name);
            if (method.Name == nameof(IApiClient.JoinProjectAsync)) Joins.Add((JoinRequest)args[1]);
            var type = method.ReturnType;
            var result = type.GetGenericArguments()[0];
            try {
                var value = Script.TryGetValue(method.Name, out var f) ? f(args) : null;
                return typeof(Task).GetMethod(nameof(Task.FromResult))!.MakeGenericMethod(result).Invoke(null, new[] { value })!;
            } catch (ApiException ex) {
                return typeof(Task).GetMethod(nameof(Task.FromException), 1, new[] { typeof(Exception) })!.MakeGenericMethod(result).Invoke(null, new object[] { ex })!;
            }
        }
    }

    internal sealed class FakeContext : IHubJoinContext {
        public string Missing;
        public string WhatIsMissingForRig() => Missing;
        public RigUpsertRequest DescribeRig() => new() { Camera = "cam", Telescope = "scope" };
        public IReadOnlyList<string> WheelFilterNames { get; } = new[] { "L" };
        public bool Colour => false;
        public IReadOnlyDictionary<string, string> FilterMap { get; } = new Dictionary<string, string>();
        public string DisplayName => "Ada";
        public string TelescopeName => "Scope";
        public FilterNormalizer Normalizer { get; } = FilterNormalizer.LoadBundled();
    }

    internal sealed class MemoryLinks : IHubLinkStore {
        public Dictionary<string, StackingLink> Rows = new();
        public StackingLink Lookup(string id) => Rows.TryGetValue(id, out var l) ? l : null!;
        public void Upsert(StackingLink link) => Rows[link.StarfrontProjectId] = link;
        public IReadOnlyList<StackingLink> All() => Rows.Values.ToList();
    }

    [TestFixture]
    public class HubJoinerTests {
        private MemoryLinks links = null!;
        private FakeContext context = null!;
        private IApiClient api = null!;
        private ScriptedClient script = null!;
        private HubJoiner joiner = null!;
        private int linkFetches;

        [SetUp]
        public void SetUp() {
            links = new MemoryLinks();
            context = new FakeContext();
            (api, script) = ScriptedClient.Make();
            script.Script[nameof(IApiClient.GetProjectAsync)] = _ => new ProjectDetail { ProjectId = "p1", Name = "M31 on the hub", Filters = new[] { "L" }, TermsVersion = "t1" };
            script.Script[nameof(IApiClient.UpsertRigAsync)] = _ => new Rig { RigId = "rig1", Confirmed = true };
            script.Script[nameof(IApiClient.JoinProjectAsync)] = _ => new Membership { Status = MemberStatuses.Active, DisplayName = "Ada", AcceptedTermsVersion = "t1" };
            joiner = new HubJoiner(context, links, _ => { linkFetches++; return api; });
        }

        private static string Grant(long exp) {
            var payload = Convert.ToBase64String(Encoding.UTF8.GetBytes("{\"v\":1,\"hub\":\"hub-1\",\"exp\":" + exp + "}")).TrimEnd('=').Replace('+', '-').Replace('/', '_');
            return "sfg_" + payload + ".c2lnbmF0dXJl";
        }

        private static (ProjectListing, StackingBinding) Listing(string grant) {
            var binding = new StackingBinding {
                Backend = "hub", HubId = "hub-1", HubName = "Garden hub", HubLanUrl = "http://192.168.1.20:8080",
                ApiBase = "https://starfront.example/stack/api/", CollabProjectId = "p1", Grant = grant,
            };
            return (new ProjectListing { Id = "s1", Name = "M31", Stacking = binding }, binding);
        }

        [Test]
        public void GrantExpiryIsReadFromThePayload() {
            Assert.That(HubGrants.ExpiresUtc(Grant(1_900_000_000)), Is.EqualTo(DateTimeOffset.FromUnixTimeSeconds(1_900_000_000).UtcDateTime));
            Assert.That(HubGrants.ExpiresUtc("rscu_nope"), Is.Null);
            Assert.That(HubGrants.ExpiresUtc("sfg_!!!.x"), Is.Null);
            Assert.That(HubGrants.ExpiresUtc(""), Is.Null);
        }

        [Test]
        public async Task AHubListingJoinsWithNoCloudTokenAndTakesTheBinding() {
            var grant = Grant(1_900_000_000);
            var (listing, binding) = Listing(grant);
            await joiner.SyncAsync(listing, binding, CancellationToken.None);

            var link = links.Lookup("s1")!;
            Assert.That(link.IsHub, Is.True);
            Assert.That(link.Status, Is.EqualTo(LinkStatus.Active));
            Assert.That(link.ApiBase, Is.EqualTo("https://starfront.example/stack/api/"));
            Assert.That(link.LanApiBase, Is.EqualTo("http://192.168.1.20:8080"));
            Assert.That(link.HubId, Is.EqualTo("hub-1"));
            Assert.That(link.HubName, Is.EqualTo("Garden hub"));
            Assert.That(link.CollabProjectId, Is.EqualTo("p1"));
            Assert.That(link.Grant, Is.EqualTo(grant));
            Assert.That(link.GrantExpiresUtc, Is.EqualTo(DateTimeOffset.FromUnixTimeSeconds(1_900_000_000).UtcDateTime));
            Assert.That(script.Calls, Is.EqualTo(new[] { "GetProjectAsync", "UpsertRigAsync", "JoinProjectAsync" }));
            Assert.That(script.Joins[0].DisplayName, Is.EqualTo("Ada"));
            Assert.That(script.Joins[0].AcceptedTermsVersion, Is.EqualTo("t1"));
            Assert.That(links.Lookup("s1")!.RigId, Is.EqualTo("rig1"), "the hub rig lives on the hub link; the cloud setting is not in reach");
        }

        [Test]
        public async Task AnActiveMembershipIsReportedAndAPendingOneIsNot() {
            var confirmed = new List<(string, DateTimeOffset)>();
            joiner.MembershipConfirmed = (link, asked) => confirmed.Add((link.CollabProjectId, asked));
            var before = DateTimeOffset.Now;
            var (listing, binding) = Listing(Grant(1_900_000_000));
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            Assert.That(confirmed.Select(c => c.Item1), Is.EqualTo(new[] { "p1" }));
            Assert.That(confirmed[0].Item2, Is.InRange(before, DateTimeOffset.Now), "the time is when the read was sent");

            confirmed.Clear();
            links.Lookup("s1")!.Status = LinkStatus.Failed;
            script.Script[nameof(IApiClient.GetProjectAsync)] = _ => new ProjectDetail {
                ProjectId = "p1", Name = "M31", Filters = new[] { "L" }, TermsVersion = "t1",
                MyMembership = new Membership { Status = MemberStatuses.Pending },
            };
            await joiner.JoinAsync(links.Lookup("s1")!, CancellationToken.None);
            Assert.That(links.Lookup("s1")!.Status, Is.EqualTo(LinkStatus.Pending));
            Assert.That(confirmed, Is.Empty, "a pending membership answers nothing");
        }

        [Test]
        public async Task EveryCheckInRefreshesTheGrantWithoutJoiningAgain() {
            var (listing, binding) = Listing(Grant(1_900_000_000));
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            script.Calls.Clear();
            var newer = Grant(1_900_086_400);
            binding.Grant = newer;
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            Assert.That(links.Lookup("s1")!.Grant, Is.EqualTo(newer));
            Assert.That(links.Lookup("s1")!.GrantExpiresUtc, Is.EqualTo(DateTimeOffset.FromUnixTimeSeconds(1_900_086_400).UtcDateTime));
            Assert.That(script.Calls, Is.Empty);
        }

        [Test]
        public void OnlyCloudListingsNeedTheSignIn() {
            var (hubListing, _) = Listing(Grant(1_900_000_000));
            Assert.That(JoinRoutes.Of(hubListing, false), Is.EqualTo(JoinRoute.Hub));
            var cloud = new ProjectListing { Id = "s3", Name = "C", Stacking = new StackingBinding { Backend = "cloud" }, StackingJoinCode = "ABC" };
            Assert.That(JoinRoutes.Of(cloud, false), Is.EqualTo(JoinRoute.CloudNeedsSignIn));
            Assert.That(JoinRoutes.Of(cloud, true), Is.EqualTo(JoinRoute.Cloud));
            var old = new ProjectListing { Id = "s4", Name = "Old", StackingJoinCode = "ABC" };
            Assert.That(JoinRoutes.Of(old, false), Is.EqualTo(JoinRoute.CloudNeedsSignIn));
            hubListing.Stacking!.Grant = "";
            Assert.That(JoinRoutes.Of(hubListing, true), Is.EqualTo(JoinRoute.Skip));
        }

        [Test]
        public async Task AMissingCameraLeavesTheLinkWaitingForARig() {
            context.Missing = "connect your camera first";
            var (listing, binding) = Listing(Grant(1_900_000_000));
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            Assert.That(links.Lookup("s1")!.Status, Is.EqualTo(LinkStatus.NeedsRig));
            Assert.That(script.Calls, Is.EqualTo(new[] { "GetProjectAsync" }));
        }

        [Test]
        public async Task A401OnTheHubLinkClearsOnlyThatLinksGrant() {
            var other = new StackingLink { StarfrontProjectId = "s2", StarfrontName = "Other", Backend = "hub", CollabProjectId = "p2", Grant = "sfg_other.sig", Status = LinkStatus.Active };
            links.Upsert(other);
            script.Script[nameof(IApiClient.GetProjectAsync)] = _ => throw new ApiException(ApiCodes.TokenInvalid, "bad grant", 401);
            var (listing, binding) = Listing(Grant(1_900_000_000));
            await joiner.SyncAsync(listing, binding, CancellationToken.None);

            var link = links.Lookup("s1")!;
            Assert.That(link.Grant, Is.Empty);
            Assert.That(link.GrantExpiresUtc, Is.Null);
            Assert.That(link.Status, Is.EqualTo(LinkStatus.GrantRefresh));
            Assert.That(link.NextAttemptUtc, Is.Null, "the next check-in fetches a new grant at once");
            Assert.That(links.Lookup("s2")!.Grant, Is.EqualTo("sfg_other.sig"));
            Assert.That(links.Lookup("s2")!.Status, Is.EqualTo(LinkStatus.Active));
        }

        [Test]
        public async Task AClearedLinkJoinsAgainWithTheNextCheckInsGrant() {
            script.Script[nameof(IApiClient.GetProjectAsync)] = _ => throw new ApiException(ApiCodes.TokenInvalid, "bad grant", 401);
            var (listing, binding) = Listing(Grant(1_900_000_000));
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            script.Script[nameof(IApiClient.GetProjectAsync)] = _ => new ProjectDetail { ProjectId = "p1", Name = "M31", Filters = new[] { "L" }, TermsVersion = "t1" };
            binding.Grant = Grant(1_900_086_400);
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            Assert.That(links.Lookup("s1")!.Status, Is.EqualTo(LinkStatus.Active));
            Assert.That(links.Lookup("s1")!.Grant, Is.EqualTo(binding.Grant));
        }

        [Test]
        public async Task ATermsChangeHoldsTheLinkForSomebodyToAccept() {
            script.Script[nameof(IApiClient.JoinProjectAsync)] = _ => throw new ApiException(ApiCodes.TermsOutdated, "terms", 409);
            var (listing, binding) = Listing(Grant(1_900_000_000));
            await joiner.SyncAsync(listing, binding, CancellationToken.None);
            Assert.That(links.Lookup("s1")!.Status, Is.EqualTo(LinkStatus.TermsOutdated));
        }
    }
}

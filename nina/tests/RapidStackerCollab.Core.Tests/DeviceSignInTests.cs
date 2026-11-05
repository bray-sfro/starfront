// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using Microsoft.AspNetCore.Http;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Tests.Fakes;

namespace RapidStackerCollab.Core.Tests;

public class DeviceSignInTests
{
    private FakeCollabServer server = null!;
    private InMemoryTokenStore store = null!;
    private FakeClock clock = null!;
    private DeviceSignIn flow = null!;

    [SetUp]
    public async Task SetUp()
    {
        server = await FakeCollabServer.StartAsync();
        store = new InMemoryTokenStore();
        clock = new FakeClock();
        // Sign-in happens before there is a token, so the client starts with an empty store.
        flow = new DeviceSignIn(TestApi.Client(server, store), store, clock);
    }

    [TearDown]
    public async Task TearDown() => await server.DisposeAsync();

    private int Polls => server.RequestsTo("POST", "device/poll").Count;

    [Test]
    public async Task Approval_after_a_few_polls_stores_the_token_and_returns_the_user()
    {
        var start = await flow.StartAsync("N.I.N.A. plugin on TEST");
        Assert.That(start.UserCode, Is.EqualTo(FakeCollabServer.UserCode));
        clock.OnDelay = n => { if (n == 3) server.ApproveDevice(); };

        var result = await flow.WaitForApprovalAsync(start);

        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Approved));
        Assert.That(result.User!.Email, Is.EqualTo("ed@example.com"));
        Assert.That(store.Load(), Is.EqualTo(FakeCollabServer.Token));
        Assert.That(Polls, Is.EqualTo(3));
        Assert.That(clock.Delays, Is.All.EqualTo(TimeSpan.FromSeconds(5)), "polls at the server's interval");
    }

    [Test]
    public async Task Denial_stores_nothing()
    {
        var start = await flow.StartAsync("x");
        server.DenyDevice();
        var result = await flow.WaitForApprovalAsync(start);
        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Denied));
        Assert.That(store.Load(), Is.Null);
    }

    [Test]
    public async Task A_server_side_expiry_is_reported()
    {
        var start = await flow.StartAsync("x");
        server.ExpireDevice();
        Assert.That((await flow.WaitForApprovalAsync(start)).Outcome, Is.EqualTo(DeviceSignInOutcome.Expired));
    }

    [Test]
    public async Task Local_expiry_stops_polling_after_expires_in_seconds()
    {
        var start = await flow.StartAsync("x");
        var result = await flow.WaitForApprovalAsync(start);
        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Expired));
        // 600 s at a 5 s interval: the loop ends on the poll that would land at 600 s.
        Assert.That(Polls, Is.InRange(118, 120));
        Assert.That(store.Load(), Is.Null);
    }

    [Test]
    public async Task Slow_down_adds_five_seconds_to_every_later_interval()
    {
        var start = await flow.StartAsync("x");
        server.InterceptError(FakeCollabServer.Route("POST", "device/poll"), 429, "slow_down", "too fast", times: 1);
        clock.OnDelay = n => { if (n == 4) server.ApproveDevice(); };

        var result = await flow.WaitForApprovalAsync(start);

        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Approved));
        Assert.That(clock.Delays.Select(d => (int)d.TotalSeconds), Is.EqualTo(new[] { 5, 10, 10, 10 }));
    }

    [Test]
    public async Task Rate_limited_honours_retry_after_for_the_next_wait_only()
    {
        var start = await flow.StartAsync("x");
        server.InterceptError(FakeCollabServer.Route("POST", "device/poll"), 429, "rate_limited", "slow", retryAfterSeconds: 45);
        clock.OnDelay = n => { if (n == 3) server.ApproveDevice(); };

        var result = await flow.WaitForApprovalAsync(start);

        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Approved));
        Assert.That(clock.Delays.Select(d => (int)d.TotalSeconds), Is.EqualTo(new[] { 5, 45, 5 }));
    }

    [Test]
    public async Task Transient_poll_failures_are_ridden_out()
    {
        var start = await flow.StartAsync("x");
        server.Intercept(FakeCollabServer.Route("POST", "device/poll"), async ctx =>
        {
            ctx.Response.StatusCode = 502;
            ctx.Response.ContentType = "text/html";
            await ctx.Response.WriteAsync("<html>bad gateway</html>");
        }, times: 2);
        clock.OnDelay = n => { if (n == 3) server.ApproveDevice(); };
        Assert.That((await flow.WaitForApprovalAsync(start)).Outcome, Is.EqualTo(DeviceSignInOutcome.Approved));
        Assert.That(Polls, Is.EqualTo(3));
    }

    [Test]
    public async Task A_permanent_error_fails_the_flow()
    {
        var start = await flow.StartAsync("x");
        server.InterceptError(FakeCollabServer.Route("POST", "device/poll"), 400, "invalid_code", "unknown code");
        var result = await flow.WaitForApprovalAsync(start);
        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Failed));
        Assert.That(result.Message, Does.Contain("unknown"));
    }

    [Test]
    public async Task Approved_without_a_token_is_a_failure_because_the_token_cannot_be_replayed()
    {
        var start = await flow.StartAsync("x");
        server.Intercept(FakeCollabServer.Route("POST", "device/poll"),
            ctx => FakeCollabServer.WriteOkAsync(ctx, new System.Text.Json.Nodes.JsonObject { ["status"] = "approved" }));
        var result = await flow.WaitForApprovalAsync(start);
        Assert.That(result.Outcome, Is.EqualTo(DeviceSignInOutcome.Failed));
        Assert.That(store.Load(), Is.Null);
    }

    [Test]
    public async Task Cancelling_stops_the_wait()
    {
        var start = await flow.StartAsync("x");
        using var cts = new CancellationTokenSource();
        clock.OnDelay = n => { if (n == 2) cts.Cancel(); };
        Assert.CatchAsync<OperationCanceledException>(() => flow.WaitForApprovalAsync(start, cts.Token));
        Assert.That(store.Load(), Is.Null);
    }

    [Test]
    public async Task The_default_scopes_never_include_manage()
    {
        await flow.StartAsync("x");
        var body = server.RequestsTo("POST", "device/start").Single().Body!;
        Assert.That(body, Does.Contain("frames:write").And.Not.Contain("projects:manage"));
    }
}

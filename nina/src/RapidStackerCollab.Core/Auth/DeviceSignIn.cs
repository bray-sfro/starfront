// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Util;

namespace RapidStackerCollab.Core.Auth;

public enum DeviceSignInOutcome
{
    /// <summary>The user approved; the token is in the <see cref="ITokenStore"/>.</summary>
    Approved,
    /// <summary>The user denied the request on the website.</summary>
    Denied,
    /// <summary>The code lapsed (the server said so, or <c>expires_in</c> passed locally). Start again.</summary>
    Expired,
    /// <summary>Something that retrying will not fix; see <see cref="DeviceSignInResult.Message"/>.</summary>
    Failed,
}

public sealed record DeviceSignInResult(
    DeviceSignInOutcome Outcome,
    UserInfo? User = null,
    TokenInfo? Info = null,
    string? Message = null);

/// <summary>
/// The device-code sign-in of plan §6.3: <see cref="StartAsync"/> returns the <c>user_code</c> to show,
/// <see cref="WaitForApprovalAsync"/> polls until the user approves it on the website. No password is ever typed into
/// N.I.N.A. The token is shown by the server exactly once, so it is saved the moment it arrives.
/// </summary>
public sealed class DeviceSignIn
{
    private static readonly TimeSpan SlowDownPenalty = TimeSpan.FromSeconds(5);

    private readonly IApiClient api;
    private readonly ITokenStore store;
    private readonly IClock clock;

    public DeviceSignIn(IApiClient api, ITokenStore store, IClock clock)
    {
        this.api = api;
        this.store = store;
        this.clock = clock;
    }

    /// <summary>Ask for a code. <paramref name="scopes"/> defaults to the plugin set (never <c>projects:manage</c>).</summary>
    public Task<DeviceStartResponse> StartAsync(string clientName, IEnumerable<string>? scopes = null, CancellationToken ct = default) =>
        api.DeviceStartAsync(new DeviceStartRequest
        {
            ClientName = clientName,
            Scopes = (scopes ?? ApiScopes.PluginDefaults).ToArray(),
        }, ct);

    /// <summary>
    /// Poll every <c>interval</c> seconds until approved, denied, expired or cancelled. A <c>slow_down</c> adds 5 s to the
    /// interval for the rest of the flow, <c>Retry-After</c> is honoured, and a transient failure (network, garbled
    /// response, 5xx) just waits for the next poll. Throws <see cref="OperationCanceledException"/> when cancelled.
    /// </summary>
    public async Task<DeviceSignInResult> WaitForApprovalAsync(DeviceStartResponse start, CancellationToken ct = default)
    {
        var interval = TimeSpan.FromSeconds(Math.Max(1, start.Interval));
        var deadline = clock.Now + TimeSpan.FromSeconds(Math.Max(1, start.ExpiresIn));
        var extraWait = TimeSpan.Zero;

        while (true)
        {
            var wait = interval > extraWait ? interval : extraWait;
            extraWait = TimeSpan.Zero;
            await clock.DelayAsync(wait, ct).ConfigureAwait(false);
            if (clock.Now >= deadline)
                return new DeviceSignInResult(DeviceSignInOutcome.Expired, Message: "The code expired before it was approved.");

            DevicePollResponse poll;
            try
            {
                poll = await api.DevicePollAsync(start.DeviceCode, ct).ConfigureAwait(false);
            }
            catch (ApiException ex)
            {
                switch (ex.Code)
                {
                    case ApiCodes.DeviceDenied:
                        return new DeviceSignInResult(DeviceSignInOutcome.Denied, Message: ex.Message);
                    case ApiCodes.DeviceExpired:
                        return new DeviceSignInResult(DeviceSignInOutcome.Expired, Message: ex.Message);
                    case ApiCodes.SlowDown:
                        interval += SlowDownPenalty;
                        if (ex.RetryAfter is { } ra) extraWait = ra;
                        continue;
                    case ApiCodes.RateLimited:
                        extraWait = ex.RetryAfter ?? interval * 2;
                        continue;
                    default:
                        if (ex.IsTransient) continue;
                        return new DeviceSignInResult(DeviceSignInOutcome.Failed, Message: ex.Message);
                }
            }

            if (poll.Status == "approved")
            {
                if (string.IsNullOrEmpty(poll.Token))
                    return new DeviceSignInResult(DeviceSignInOutcome.Failed,
                        Message: "The server approved the code but sent no token (a lost response cannot be replayed). Start a new sign-in.");
                store.Save(poll.Token);
                return new DeviceSignInResult(DeviceSignInOutcome.Approved, poll.User, poll.Info);
            }
            // "pending", or a status a newer server invented: keep waiting.
        }
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Auth;

/// <summary>
/// Where the <c>rscu_</c> API token lives between runs. The plugin project supplies a DPAPI-backed
/// implementation (current-user scope, in the plugin settings); Core ships only the in-memory one.
/// Implementations must be safe to call from any thread.
/// </summary>
public interface ITokenStore
{
    /// <summary>The stored token, or null when nobody is signed in.</summary>
    string? Load();

    void Save(string token);

    /// <summary>Forget the token (sign out, or the server said it is invalid).</summary>
    void Clear();
}

/// <summary>A token store that keeps the token in memory only: tests, and the fallback when nothing else is wired.</summary>
public sealed class InMemoryTokenStore : ITokenStore
{
    private readonly object gate = new();
    private string? token;

    public InMemoryTokenStore(string? initial = null) => token = initial;

    public string? Load()
    {
        lock (gate) return token;
    }

    public void Save(string value)
    {
        ArgumentException.ThrowIfNullOrEmpty(value);
        lock (gate) token = value;
    }

    public void Clear()
    {
        lock (gate) token = null;
    }
}

// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Security.Cryptography;
using System.Text;

namespace RapidStackerCollab.Core.Auth;

/// <summary>
/// Encrypts a small blob for the current user. The Windows implementation (in the plugin project) is DPAPI,
/// <c>ProtectedData.Protect(..., DataProtectionScope.CurrentUser)</c>; Core only needs the seam, so the store below is
/// tested on any OS with a stand-in.
/// </summary>
public interface IDataProtector
{
    byte[] Protect(byte[] plain);

    /// <summary>Throws <see cref="CryptographicException"/> when the blob was not protected for this user or is damaged.</summary>
    byte[] Unprotect(byte[] protectedBytes);
}

/// <summary>
/// An <see cref="ITokenStore"/> that keeps the API token in a file of its own, encrypted through an
/// <see cref="IDataProtector"/> (DPAPI, current-user scope, in the plugin): the token never touches the disk in clear text and
/// another Windows user, or another machine, cannot read it. The account is plugin-wide, not per N.I.N.A. profile, so the
/// file lives next to the queue database. Anything unreadable counts as "signed out": <see cref="Load"/> never throws, so a
/// damaged file cannot disturb an imaging session. Safe to call from any thread.
/// </summary>
public sealed class ProtectedFileTokenStore : ITokenStore
{
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("RSC1");

    private readonly string path;
    private readonly IDataProtector protector;
    private readonly object gate = new();
    private string? cached;
    private bool loaded;

    public ProtectedFileTokenStore(string path, IDataProtector protector)
    {
        this.path = path;
        this.protector = protector;
    }

    public string? Load()
    {
        lock (gate)
        {
            if (loaded) return cached;
            loaded = true;
            cached = ReadFile();
            return cached;
        }
    }

    public void Save(string token)
    {
        ArgumentException.ThrowIfNullOrEmpty(token);
        var blob = protector.Protect(Encoding.UTF8.GetBytes(token));
        var data = new byte[Magic.Length + blob.Length];
        Magic.CopyTo(data, 0);
        blob.CopyTo(data, Magic.Length);
        lock (gate)
        {
            var dir = Path.GetDirectoryName(Path.GetFullPath(path));
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
            var tmp = path + ".tmp";
            File.WriteAllBytes(tmp, data);
            File.Move(tmp, path, overwrite: true);
            cached = token;
            loaded = true;
        }
    }

    public void Clear()
    {
        lock (gate)
        {
            cached = null;
            loaded = true;
            try { File.Delete(path); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }

    private string? ReadFile()
    {
        try
        {
            if (!File.Exists(path)) return null;
            var data = File.ReadAllBytes(path);
            if (data.Length <= Magic.Length || !data.AsSpan(0, Magic.Length).SequenceEqual(Magic)) return null;
            var plain = protector.Unprotect(data.AsSpan(Magic.Length).ToArray());
            var token = Encoding.UTF8.GetString(plain);
            return token.Length == 0 ? null : token;
        }
        catch (Exception ex) when (ex is CryptographicException or IOException or UnauthorizedAccessException or ArgumentException)
        {
            return null;
        }
    }
}

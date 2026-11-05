// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using NUnit.Framework;
using System.Security.Cryptography;
using System.Text;
using RapidStackerCollab.Core.Auth;

namespace RapidStackerCollab.Core.Tests;

[TestFixture]
public class ProtectedFileTokenStoreTests
{
    private string dir = "";

    [SetUp]
    public void SetUp() => dir = Directory.CreateTempSubdirectory("rsc-token-").FullName;

    [TearDown]
    public void TearDown()
    {
        try { Directory.Delete(dir, true); } catch (IOException) { }
    }

    /// <summary>Stand-in for DPAPI: XOR with a per-user key, refuses a blob made under another key.</summary>
    private sealed class FakeProtector : IDataProtector
    {
        private readonly byte key;

        public FakeProtector(byte key) => this.key = key;

        public byte[] Protect(byte[] plain) => new[] { key }.Concat(plain.Select(b => (byte)(b ^ key))).ToArray();

        public byte[] Unprotect(byte[] protectedBytes)
        {
            if (protectedBytes.Length < 1 || protectedBytes[0] != key) throw new CryptographicException("not protected for this user");
            return protectedBytes.Skip(1).Select(b => (byte)(b ^ key)).ToArray();
        }
    }

    private string TokenPath => Path.Combine(dir, "sub", "token.bin");

    [Test]
    public void A_saved_token_survives_a_new_store_and_is_not_on_disk_in_clear_text()
    {
        new ProtectedFileTokenStore(TokenPath, new FakeProtector(0x5A)).Save("rscu_secret_token_value");

        Assert.That(new ProtectedFileTokenStore(TokenPath, new FakeProtector(0x5A)).Load(), Is.EqualTo("rscu_secret_token_value"));
        var raw = File.ReadAllBytes(TokenPath);
        Assert.That(Encoding.UTF8.GetString(raw), Does.Not.Contain("rscu_secret"));
    }

    [Test]
    public void A_file_protected_for_another_user_reads_as_signed_out_and_does_not_throw()
    {
        new ProtectedFileTokenStore(TokenPath, new FakeProtector(0x11)).Save("rscu_abc");
        Assert.That(new ProtectedFileTokenStore(TokenPath, new FakeProtector(0x22)).Load(), Is.Null);
    }

    [TestCase(new byte[0])]
    [TestCase(new byte[] { 1, 2, 3 })]
    [TestCase(new byte[] { 0x52, 0x53, 0x43, 0x31 })]
    public void Garbage_reads_as_signed_out(byte[] bytes)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(TokenPath)!);
        File.WriteAllBytes(TokenPath, bytes);
        Assert.That(new ProtectedFileTokenStore(TokenPath, new FakeProtector(1)).Load(), Is.Null);
    }

    [Test]
    public void Clear_forgets_the_token_and_deletes_the_file()
    {
        var store = new ProtectedFileTokenStore(TokenPath, new FakeProtector(7));
        store.Save("rscu_x");
        store.Clear();
        Assert.That(store.Load(), Is.Null);
        Assert.That(File.Exists(TokenPath), Is.False);
        Assert.DoesNotThrow(store.Clear);
    }

    [Test]
    public void Concurrent_saves_and_loads_never_fail_or_tear()
    {
        var store = new ProtectedFileTokenStore(TokenPath, new FakeProtector(9));
        store.Save("rscu_0");
        Parallel.For(0, 200, i =>
        {
            if (i % 2 == 0) store.Save("rscu_" + i);
            else Assert.That(store.Load(), Does.StartWith("rscu_"));
        });
        Assert.That(new ProtectedFileTokenStore(TokenPath, new FakeProtector(9)).Load(), Does.StartWith("rscu_"));
    }
}

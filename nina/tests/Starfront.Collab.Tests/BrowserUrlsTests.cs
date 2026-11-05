using NUnit.Framework;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// The approval and terms pages come from Rapid Stacker Collab's answers
    /// and are handed to the shell. Only web pages may get through: any https
    /// page, and http only on this PC's loopback address, which is where the
    /// local stack serves (it used to be https only, so a local sign-in meant
    /// typing the address by hand).
    /// </summary>
    public class BrowserUrlsTests {
        [TestCase("https://collab.astro.ekeservices.com/device?code=K7QM-3XWD")]
        [TestCase("HTTPS://collab.astro.ekeservices.com/terms")]
        [TestCase("http://127.0.0.1:8787/device?code=K7QM-3XWD")]
        [TestCase("http://localhost:8787/device")]
        [TestCase("http://LOCALHOST:8787/device")]
        [TestCase("http://127.0.0.2:8787/device", Description = "all of 127/8 is loopback")]
        [TestCase("http://[::1]:8787/device")]
        [TestCase(" http://127.0.0.1:8787/device ", Description = "stray spaces")]
        public void Web_pages_the_browser_may_open(string url) {
            Assert.That(BrowserUrls.CanOpen(url), Is.True);
        }

        [TestCase("http://collab.astro.ekeservices.com/device", Description = "plain http across the network")]
        [TestCase("http://192.168.1.20:8787/device", Description = "another machine on the LAN")]
        [TestCase("http://localhost.example.com/device")]
        [TestCase("http://127.0.0.1.example.com/device")]
        [TestCase("http://127.0.0.1@example.com/device", Description = "user info, the host is example.com")]
        [TestCase("file:///C:/Windows/System32/calc.exe")]
        [TestCase("C:\\Windows\\System32\\calc.exe")]
        [TestCase("ms-settings:privacy")]
        [TestCase("javascript:alert(1)")]
        [TestCase("/device")]
        [TestCase("")]
        [TestCase("   ")]
        [TestCase(null)]
        public void Anything_else_is_not_handed_to_the_shell(string url) {
            Assert.That(BrowserUrls.CanOpen(url), Is.False);
        }
    }
}

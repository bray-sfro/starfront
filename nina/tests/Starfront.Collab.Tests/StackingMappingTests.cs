using NUnit.Framework;
using RapidStackerCollab.Core.Filters;
using Starfront.Collab.Services.Stacking;

namespace Starfront.Collab.Tests {

    /// <summary>
    /// The filter mapping a telescope joins a stacking project with, made with
    /// nobody at the keyboard. A wrong "yes" sends frames the project does not
    /// want (and they count against the person's quota); a wrong "no" silently
    /// keeps back frames it does want. Each case is a wheel somebody has.
    /// </summary>
    public class StackingMappingTests {
        private static readonly FilterNormalizer Normalizer = FilterNormalizer.LoadBundled();
        private static readonly Dictionary<string, string> NoDeliveries = new();

        private static Dictionary<string, string> Map(IEnumerable<string> wheel, string[] declared, bool bayer = false,
                                                      Dictionary<string, string> deliveries = null) {
            var result = StackingMapping.Build(wheel, declared, bayer, deliveries ?? NoDeliveries, Normalizer);
            return result.Entries.ToDictionary(e => e.Raw, e => e.Canonical);
        }

        [Test]
        public void A_narrowband_project_takes_the_narrowband_filters_and_skips_the_broadband_ones() {
            var map = Map(new[] { "L", "R", "G", "B", "Ha", "OIII", "SII" }, new[] { "HA", "OIII", "SII" });
            Assert.That(map["Ha"], Is.EqualTo("HA"));
            Assert.That(map["OIII"], Is.EqualTo("OIII"));
            Assert.That(map["SII"], Is.EqualTo("SII"));
            foreach (var letter in new[] { "L", "R", "G", "B" }) Assert.That(map[letter], Is.EqualTo(StackingMapping.Skip), letter);
        }

        [Test]
        public void A_broadband_project_takes_the_letters_and_skips_the_narrowband() {
            var map = Map(new[] { "Lum", "Red", "Green", "Blue", "Ha" }, new[] { "L", "R", "G", "B" });
            Assert.That(map["Lum"], Is.EqualTo("L"));
            Assert.That(map["Red"], Is.EqualTo("R"));
            Assert.That(map["Green"], Is.EqualTo("G"));
            Assert.That(map["Blue"], Is.EqualTo("B"));
            Assert.That(map["Ha"], Is.EqualTo(StackingMapping.Skip));
        }

        [Test]
        public void Vendor_names_with_bandpasses_are_read() {
            var map = Map(new[] { "Antlia Ha 3nm", "Astrodon OIII 3nm", "Chroma SII 3nm" }, new[] { "HA", "OIII", "SII" });
            Assert.That(map.Values, Is.EqualTo(new[] { "HA", "OIII", "SII" }));
        }

        [Test]
        public void A_name_nobody_can_read_is_never_sent() {
            var map = Map(new[] { "Slot 7", "Ha" }, new[] { "HA" });
            Assert.That(map["Slot 7"], Is.EqualTo(StackingMapping.Skip));
            Assert.That(map["Ha"], Is.EqualTo("HA"));
        }

        [Test]
        public void The_persons_own_delivery_table_wins_over_the_name() {
            // A filter called "Filter 2" that the person said delivers Ha.
            var map = Map(new[] { "Filter 2" }, new[] { "HA" }, deliveries: new Dictionary<string, string> { ["Filter 2"] = "H" });
            Assert.That(map["Filter 2"], Is.EqualTo("HA"));
        }

        [Test]
        public void A_filter_marked_as_giving_nothing_is_never_sent_whatever_its_name() {
            var map = Map(new[] { "Ha" }, new[] { "HA" }, deliveries: new Dictionary<string, string> { ["Ha"] = "-" });
            Assert.That(map["Ha"], Is.EqualTo(StackingMapping.Skip));
        }

        [Test]
        public void A_delivery_the_project_does_not_want_does_not_force_the_filter_through() {
            // Marked as delivering L, but the project takes narrowband only: still skipped.
            var map = Map(new[] { "Clear" }, new[] { "HA" }, deliveries: new Dictionary<string, string> { ["Clear"] = "L" });
            Assert.That(map["Clear"], Is.EqualTo(StackingMapping.Skip));
        }

        [Test]
        public void A_colour_camera_behind_a_dual_band_filter_is_mapped_when_the_project_takes_it() {
            var map = Map(new[] { "L-eXtreme" }, new[] { "DUO" }, bayer: true, deliveries: new Dictionary<string, string> { ["L-eXtreme"] = "HO" });
            Assert.That(map["L-eXtreme"], Is.EqualTo("DUO"));
        }

        [Test]
        public void A_wheel_with_nothing_the_project_wants_is_said_to_be_all_skipped() {
            var result = StackingMapping.Build(new[] { "L", "R", "G", "B" }, new[] { "HA", "OIII" }, false, NoDeliveries, Normalizer);
            Assert.That(result.AllSkipped, Is.True);
            var some = StackingMapping.Build(new[] { "L", "Ha" }, new[] { "HA", "OIII" }, false, NoDeliveries, Normalizer);
            Assert.That(some.AllSkipped, Is.False);
        }

        [Test]
        public void An_empty_wheel_is_all_skipped_and_blank_or_repeated_names_count_once() {
            Assert.That(StackingMapping.Build(Array.Empty<string>(), new[] { "HA" }, false, NoDeliveries, Normalizer).AllSkipped, Is.True);
            var result = StackingMapping.Build(new[] { "Ha", " Ha ", "", "  " }, new[] { "HA" }, false, NoDeliveries, Normalizer);
            Assert.That(result.Entries.Select(e => e.Raw), Is.EqualTo(new[] { "Ha" }));
        }

        [Test]
        public void The_summary_names_every_filter() {
            var result = StackingMapping.Build(new[] { "Ha", "L" }, new[] { "HA" }, false, NoDeliveries, Normalizer);
            Assert.That(result.Summary, Is.EqualTo("Ha = HA, L skipped"));
        }
    }
}

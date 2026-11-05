// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text;
using NUnit.Framework;
using RapidStackerCollab.Core.Filters;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

/// <summary>
/// The conformance test of docs/filters.md section 7.2: every row of the language-neutral corpus exported by the Rust
/// crate (<c>corpus.tsv</c>, 609 rows) is replayed through the C# port and columns 7-12 must match.
/// </summary>
public class FilterNormalizerTests
{
    private sealed record Row(int Line, string Raw, bool Bayer, string[] Declared, string? Filename, string? Override, string? Contributor,
        string Canonical, string Step, string Confidence, double? Bandwidth, string? Vendor, bool Ambiguous, string Source);

    private static string AssetPath(string name) => Path.Combine(AppContext.BaseDirectory, "Assets", name);

    private static List<Row> LoadCorpus()
    {
        var rows = new List<Row>();
        var lines = File.ReadAllLines(AssetPath("corpus.tsv"), Encoding.UTF8);
        var seenHeader = false;
        for (var i = 0; i < lines.Length; i++)
        {
            var line = lines[i];
            if (line.StartsWith('#') || line.Length == 0) continue;
            if (!seenHeader)
            {
                seenHeader = true;
                Assert.That(line.Split('\t'), Has.Length.EqualTo(13), "header has the 13 documented columns");
                continue;
            }
            var c = line.Split('\t');
            Assert.That(c, Has.Length.EqualTo(13), $"corpus line {i + 1}");
            static string? Dash(string s) => s == "-" ? null : s;
            rows.Add(new Row(i + 1, c[0], c[1] == "1", c[2] == "-" ? Array.Empty<string>() : c[2].Split(','), Dash(c[3]), Dash(c[4]), Dash(c[5]),
                c[6], c[7], c[8], c[9] == "-" ? null : double.Parse(c[9], System.Globalization.CultureInfo.InvariantCulture), Dash(c[10]), c[11] == "1", c[12]));
        }
        return rows;
    }

    private static FilterContext ContextOf(Row r) => new(
        r.Declared,
        r.Bayer,
        r.Filename,
        r.Override is null ? null : new Dictionary<string, string> { [r.Raw] = r.Override },
        r.Contributor is null ? null : new Dictionary<string, string> { [r.Raw] = r.Contributor });

    [Test]
    public void The_bundled_table_reproduces_every_row_of_the_conformance_corpus()
    {
        var normalizer = FilterNormalizer.LoadBundled();
        var rows = LoadCorpus();
        Assert.That(rows, Has.Count.GreaterThanOrEqualTo(300), "plan §5.4 asks for at least 300 rows");

        var failures = new List<string>();
        foreach (var r in rows)
        {
            var got = normalizer.Normalize(r.Raw == "@absent" ? null : r.Raw, ContextOf(r));
            var problems = new List<string>();
            if (got.Canonical != r.Canonical) problems.Add($"canonical {got.Canonical} != {r.Canonical}");
            if (got.Step != r.Step) problems.Add($"step {got.Step} != {r.Step}");
            if (got.Confidence != r.Confidence) problems.Add($"confidence {got.Confidence} != {r.Confidence}");
            if ((got.BandwidthNm is null) != (r.Bandwidth is null) || (got.BandwidthNm is { } g && Math.Abs(g - r.Bandwidth!.Value) > 0.005))
                problems.Add($"bandwidth {got.BandwidthNm?.ToString() ?? "-"} != {r.Bandwidth?.ToString() ?? "-"}");
            if (got.Vendor != r.Vendor) problems.Add($"vendor {got.Vendor ?? "-"} != {r.Vendor ?? "-"}");
            if (got.Ambiguous != r.Ambiguous) problems.Add($"ambiguous {got.Ambiguous} != {r.Ambiguous}");
            if (problems.Count > 0) failures.Add($"line {r.Line} [{r.Raw}] bayer={r.Bayer} declared=[{string.Join(',', r.Declared)}]: {string.Join("; ", problems)}");
        }
        Assert.That(failures, Is.Empty, $"{failures.Count} of {rows.Count} rows differ:{Environment.NewLine}{string.Join(Environment.NewLine, failures.Take(40))}");
    }

    [Test]
    public void The_corpus_covers_every_canonical_step_and_confidence_the_port_can_produce()
    {
        var rows = LoadCorpus();
        var canonicals = new[] { "L", "R", "G", "B", "HA", "OIII", "SII", "HB", "NII", "CLEAR", "DUO", "OSC", "IR", "UV", "U", "V", "I", "Z", "Y", "UNKNOWN" };
        foreach (var c in canonicals)
            Assert.That(rows.Count(r => r.Canonical == c), Is.GreaterThanOrEqualTo(3), $"canonical {c} appears at least three times (plan §5.4)");
        Assert.That(rows.Select(r => r.Step).Distinct(), Is.SupersetOf(new[]
        {
            FilterSteps.Override, FilterSteps.Contributor, FilterSteps.Alias, FilterSteps.Empty, FilterSteps.Pattern,
            FilterSteps.SingleLetter, FilterSteps.Sensor, FilterSteps.Filename, FilterSteps.Quarantine,
        }));
        Assert.That(rows.Select(r => r.Confidence).Distinct(), Is.SupersetOf(new[] { "exact", "pattern", "inferred", "user" }));
    }

    [Test]
    public void The_bundled_table_is_the_one_the_rust_crate_currently_exports()
    {
        var crate = RepoPaths.Find(Path.Combine("worker", "crates", "filters", "aliases.json"));
        if (crate is null) Assert.Ignore("worker/crates/filters/aliases.json is not in this checkout (the m1-filters branch has not been merged here).");
        var bundled = new StreamReader(typeof(FilterNormalizer).Assembly.GetManifestResourceStream("RapidStackerCollab.Core.Filters.aliases.json")!, Encoding.UTF8).ReadToEnd();
        Assert.That(bundled.ReplaceLineEndings("\n"), Is.EqualTo(File.ReadAllText(crate!, Encoding.UTF8).ReplaceLineEndings("\n")),
            "copy worker/crates/filters/aliases.json over src/RapidStackerCollab.Core/Filters/aliases.json (and refresh corpus.tsv)");
        var corpus = RepoPaths.Find(Path.Combine("worker", "crates", "filters", "tests", "corpus.tsv"));
        if (corpus is not null)
            Assert.That(File.ReadAllText(AssetPath("corpus.tsv"), Encoding.UTF8).ReplaceLineEndings("\n"),
                Is.EqualTo(File.ReadAllText(corpus, Encoding.UTF8).ReplaceLineEndings("\n")), "corpus.tsv has drifted from the crate's copy");
    }

    [Test]
    public void The_published_file_has_the_members_the_contract_says_it_has()
    {
        var yaml = File.ReadAllText(RepoPaths.Contract("openapi.yaml"));
        var root = (Dictionary<object, object>)new YamlDotNet.Serialization.DeserializerBuilder().Build().Deserialize<object>(yaml);
        var alias = (Dictionary<object, object>)((Dictionary<object, object>)((Dictionary<object, object>)root["components"])["schemas"])["AliasTable"];
        var required = ((List<object>)alias["required"]).Cast<string>().ToList();
        var properties = ((Dictionary<object, object>)alias["properties"]).Keys.Cast<string>().ToHashSet();
        using var doc = System.Text.Json.JsonDocument.Parse(new StreamReader(
            typeof(FilterNormalizer).Assembly.GetManifestResourceStream("RapidStackerCollab.Core.Filters.aliases.json")!, Encoding.UTF8).ReadToEnd());
        var members = doc.RootElement.EnumerateObject().Select(p => p.Name).ToList();
        Assert.That(members, Is.SupersetOf(required), "every member the contract requires is in the file M1 exports");
        Assert.That(properties, Is.SupersetOf(members), "the contract describes every member of the file");
    }

    // ---- behaviour the corpus does not spell out, for the plugin's own use ---------------------------------------------

    private static readonly FilterNormalizer N = FilterNormalizer.LoadBundled();

    [Test]
    public void Common_wheel_names_resolve_the_way_a_contributor_expects()
    {
        var declared = new[] { "L", "R", "G", "B", "HA", "OIII", "SII" };
        var ctx = new FilterContext(declared);
        Assert.Multiple(() =>
        {
            Assert.That(N.Normalize("Ha 3nm", ctx).Canonical, Is.EqualTo("HA"));
            Assert.That(N.Normalize("Hα", ctx).Canonical, Is.EqualTo("HA"));
            Assert.That(N.Normalize("Oiii", ctx).Canonical, Is.EqualTo("OIII"));
            Assert.That(N.Normalize("S2", ctx).Canonical, Is.EqualTo("SII"));
            Assert.That(N.Normalize("Luminance", ctx).Canonical, Is.EqualTo("L"));
            Assert.That(N.Normalize("Red", ctx).Canonical, Is.EqualTo("R"));
            Assert.That(N.Normalize("Baader 6.5nm H-alpha", ctx).Vendor, Is.EqualTo("baader"));
            Assert.That(N.Normalize("Baader 6.5nm H-alpha", ctx).BandwidthNm, Is.EqualTo(6.5));
            Assert.That(N.Normalize("Slot 4", ctx).Canonical, Is.EqualTo("UNKNOWN"));
            Assert.That(N.Normalize("Slot 4", ctx).IsKnown, Is.False);
        });
    }

    [Test]
    public void A_contributor_skip_is_passed_through_and_is_not_a_filter()
    {
        var r = N.Normalize("Dark", new FilterContext(Contributor: new Dictionary<string, string> { ["Dark"] = "SKIP" }));
        Assert.That(r.Canonical, Is.EqualTo("SKIP"));
        Assert.That(r.Step, Is.EqualTo(FilterSteps.Contributor));
        Assert.That(r.IsKnown, Is.False);
    }

    [Test]
    public void Suggest_prefills_the_mapping_grid_and_keeps_what_the_user_already_chose()
    {
        var s = N.Suggest(new[] { "Ha", "Oiii", "Weird", "Sii" }, new[] { "HA", "OIII", "SII" }, bayer: false,
            existing: new Dictionary<string, string> { ["Weird"] = "L" });
        Assert.That(s.Select(x => (x.Name, x.Result.Canonical)), Is.EqualTo(new[] { ("Ha", "HA"), ("Oiii", "OIII"), ("Weird", "L"), ("Sii", "SII") }));
        Assert.That(s[2].Result.Confidence, Is.EqualTo(FilterConfidences.User));
    }

    [Test]
    public void Mono_h_needs_the_project_to_declare_ha_but_a_one_shot_colour_camera_with_a_clear_wheel_is_osc()
    {
        Assert.That(N.Normalize("H", new FilterContext()).Canonical, Is.EqualTo("UNKNOWN"));
        Assert.That(N.Normalize("H", new FilterContext(new[] { "HA" })).Canonical, Is.EqualTo("HA"));
        Assert.That(N.Normalize("", new FilterContext(Bayer: true)).Canonical, Is.EqualTo("OSC"));
        Assert.That(N.Normalize(null, new FilterContext(Bayer: false, Filename: "2025-01-01_19-27-10_Si_-9.90_300.00s_0000.fits")).Canonical, Is.EqualTo("SII"));
    }

    [Test]
    public void An_unknown_schema_version_is_refused_not_misread()
    {
        var json = new StreamReader(typeof(FilterNormalizer).Assembly.GetManifestResourceStream("RapidStackerCollab.Core.Filters.aliases.json")!, Encoding.UTF8).ReadToEnd();
        Assert.Throws<FormatException>(() => FilterNormalizer.FromJson(json.Replace("\"schema_version\": 1", "\"schema_version\": 2")));
        Assert.Throws<FormatException>(() => FilterNormalizer.FromJson(json.Replace("collab-filters/aliases", "something/else")));
    }

    [Test]
    public void The_result_is_case_and_whitespace_insensitive_over_the_corpus_strings()
    {
        // Plan §5.4 property test (case and whitespace), over every corpus string with no project-specific maps.
        var rows = LoadCorpus().Where(r => r.Raw != "@absent" && r.Override is null && r.Contributor is null && r.Raw.Trim().Length > 0).ToList();
        foreach (var r in rows)
        {
            var ctx = new FilterContext(r.Declared, r.Bayer);
            var a = N.Normalize(r.Raw, ctx);
            var b = N.Normalize("  " + r.Raw.ToUpperInvariant().Replace(" ", "   ") + "\t", ctx);
            if (r.Raw.Any(ch => ch > 127)) continue; // upper-casing some non-ASCII letters is not a pure case change
            Assert.That(b.Canonical, Is.EqualTo(a.Canonical), $"[{r.Raw}]");
        }
    }
}

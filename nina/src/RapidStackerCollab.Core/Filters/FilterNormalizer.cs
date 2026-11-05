// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace RapidStackerCollab.Core.Filters;

/// <summary>Step names of docs/filters.md section 3, as the corpus spells them.</summary>
public static class FilterSteps
{
    public const string Override = "override";
    public const string Contributor = "contributor";
    public const string Alias = "alias";
    public const string Empty = "empty";
    public const string Pattern = "pattern";
    public const string SingleLetter = "single-letter";
    public const string Sensor = "sensor";
    public const string Filename = "filename";
    public const string Quarantine = "quarantine";
}

public static class FilterConfidences
{
    public const string Exact = "exact";
    public const string Pattern = "pattern";
    public const string Inferred = "inferred";
    public const string User = "user";
}

/// <summary>What the normaliser needs to know besides the string.</summary>
/// <param name="Declared">Canonical identifiers the project declares. Single letters resolve only to a declared canonical; declaring any of U V I Z Y makes the project photometric.</param>
/// <param name="Bayer">The frame has a <c>BAYERPAT</c> header.</param>
/// <param name="Filename">The frame's file name, for the fallback when the header is absent or blank.</param>
/// <param name="Overrides">The project's owner-maintained map: exact raw string to canonical.</param>
/// <param name="Contributor">The contributor's own map: exact raw string to canonical (or <c>SKIP</c>).</param>
public sealed record FilterContext(
    IReadOnlyCollection<string>? Declared = null,
    bool Bayer = false,
    string? Filename = null,
    IReadOnlyDictionary<string, string>? Overrides = null,
    IReadOnlyDictionary<string, string>? Contributor = null);

/// <summary>The result of normalising one <c>FILTER</c> value.</summary>
/// <param name="Canonical">A canonical identifier, <c>UNKNOWN</c> for quarantine, or <c>SKIP</c> when the contributor said never to upload this wheel name.</param>
/// <param name="Step">Which step decided (<see cref="FilterSteps"/>).</param>
/// <param name="Confidence"><see cref="FilterConfidences"/>.</param>
/// <param name="Ambiguous">A bare <c>G</c> in a project that also declares Sloan filters.</param>
/// <param name="Text">The key the tables were matched against, for debugging.</param>
public sealed record NormalizedFilter(
    string Canonical,
    string Step,
    string Confidence,
    double? BandwidthNm,
    string? Vendor,
    bool Ambiguous,
    string Text)
{
    public bool IsKnown => Canonical is not ("UNKNOWN" or "SKIP");
}

/// <summary>
/// The C# port of <c>collab-filters</c> (plan §5.2, docs/filters.md section 7): it evaluates the exported alias table
/// (<c>aliases.json</c>, schema <c>collab-filters/aliases</c> version 1) and holds no table of its own. Its correctness
/// is defined by <c>tests/corpus.tsv</c>, which the unit tests replay row by row.
/// <para>
/// The plugin uses it to pre-fill the filter-mapping grid from the filter wheel's names (so contributors confirm rather
/// than type); the server runs the authoritative resolution at <c>frames/init</c>.
/// </para>
/// </summary>
public sealed class FilterNormalizer
{
    private const int SupportedSchemaVersion = 1;
    private static readonly char[] SloanLetters = { 'u', 'g', 'r', 'i', 'z' };

    private readonly Table table;

    private FilterNormalizer(Table table) => this.table = table;

    public int TableVersion => table.TableVersion;

    /// <summary>The table embedded in this assembly (a copy of the one M1 exported; see <c>Filters/ALIASES.SOURCE.md</c>).</summary>
    public static FilterNormalizer LoadBundled()
    {
        using var s = typeof(FilterNormalizer).Assembly.GetManifestResourceStream("RapidStackerCollab.Core.Filters.aliases.json")
            ?? throw new InvalidOperationException("The embedded alias table is missing from the assembly.");
        using var r = new StreamReader(s, Encoding.UTF8);
        return FromJson(r.ReadToEnd());
    }

    public static FilterNormalizer FromFile(string path) => FromJson(File.ReadAllText(path, Encoding.UTF8));

    /// <summary>Load an exported table. Throws <see cref="FormatException"/> for a schema this port does not know.</summary>
    public static FilterNormalizer FromJson(string json)
    {
        using var doc = JsonDocument.Parse(json);
        return new FilterNormalizer(Table.Parse(doc.RootElement));
    }

    // ---- the normaliser (docs/filters.md section 3) ------------------------------------------------------------------

    /// <param name="raw">The <c>FILTER</c> value; null when the header card is absent, empty when it is present but blank.</param>
    public NormalizedFilter Normalize(string? raw, FilterContext? context = null)
    {
        var ctx = context ?? new FilterContext();
        var declared = ctx.Declared is null ? new HashSet<string>() : new HashSet<string>(ctx.Declared, StringComparer.Ordinal);
        var photometric = declared.Overlaps(new[] { "U", "V", "I", "Z", "Y" }); // docs/filters.md 3.6: a project that declares any of these

        // Step 1.
        var p = raw is null ? null : Prepare(raw);
        var bandwidth = p?.Bandwidth;
        var vendor = p?.Vendor;
        var key = p?.Key ?? "";
        var sloan = p?.Sloan ?? false;

        NormalizedFilter Result(string canonical, string? v, string confidence, string step, bool ambiguous) =>
            new(canonical, step, confidence, bandwidth, v, ambiguous, key);

        // Steps 2 and 3: exact lookups on the raw string; they win outright, even over the sensor.
        if (raw is not null)
        {
            if (ctx.Overrides is not null && ctx.Overrides.TryGetValue(raw, out var o))
                return Result(o, vendor, FilterConfidences.User, FilterSteps.Override, false);
            if (ctx.Contributor is not null && ctx.Contributor.TryGetValue(raw, out var c))
                return Result(c, vendor, FilterConfidences.User, FilterSteps.Contributor, false);
        }

        var empty = p is null || p.Blank;

        // Steps 4 to 6.
        var ambiguous = false;
        Hit? hit = null;
        if (p is { Blank: false })
        {
            switch (ResolveText(p, declared, photometric))
            {
                case Resolved.HitOf(var h): hit = h; break;
                case Resolved.AmbiguousResult: ambiguous = true; break;
            }
        }

        // Step 7: sensor context.
        if (ctx.Bayer)
        {
            var lOrClear = hit is { Canonical: "L" or "CLEAR" };
            var bareLOrC = hit is null && !sloan && (key == "l" || key == "c");
            if (empty || lOrClear || bareLOrC)
            {
                var v = hit is null ? vendor : hit.Vendor ?? vendor;
                return Result("OSC", v, FilterConfidences.Inferred, FilterSteps.Sensor, false);
            }
        }
        if (hit is not null)
            return Result(hit.Canonical, hit.Vendor ?? vendor, hit.Confidence, hit.Step, false);

        // Step 8: filename fallback, when the FILTER header is absent or blank.
        if (empty && ctx.Filename is { } name && FromFilename(name, declared, photometric) is { } f)
        {
            var fromName = Prepare(Basename(name)).Bandwidth;
            return new NormalizedFilter(f.Canonical, FilterSteps.Filename, FilterConfidences.Inferred, bandwidth ?? fromName, f.Vendor ?? vendor, false, key);
        }

        // Step 9.
        if (p is { Blank: true })
            return Result("CLEAR", null, FilterConfidences.Inferred, FilterSteps.Empty, false);
        return Result("UNKNOWN", vendor, FilterConfidences.Inferred, FilterSteps.Quarantine, ambiguous);
    }

    /// <summary>
    /// Pre-fill for the mapping grid: the normaliser's reading of each filter-wheel name, keyed by the name as typed.
    /// Names the user already mapped (<paramref name="existing"/>) are returned as-is with confidence <c>user</c>.
    /// </summary>
    public IReadOnlyList<(string Name, NormalizedFilter Result)> Suggest(IEnumerable<string> wheelNames, IReadOnlyCollection<string>? declared,
        bool bayer, IReadOnlyDictionary<string, string>? existing = null)
    {
        var ctx = new FilterContext(declared, bayer, Contributor: existing);
        return wheelNames.Select(n => (n, Normalize(n, ctx))).ToList();
    }

    // ---- text pipeline (section 3.1) --------------------------------------------------------------------------------

    private sealed record Prepared(string Folded, string Key, bool Blank, double? Bandwidth, string? Vendor, bool Sloan);

    private Prepared Prepare(string raw)
    {
        var t = table;
        var folded = Fold(raw);
        var blank = Strip(folded).Length == 0;
        var sloan = t.SloanPrime.IsMatch(folded);

        var work = t.Range.Replace(folded, " ");
        double? bandwidth = null;
        foreach (Match m in t.Bandwidth.Matches(work))
        {
            if (double.TryParse(m.Groups[t.BandwidthGroup].Value.Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out var v)
                && v > t.BandwidthMinExclusive && v <= t.BandwidthMax)
            {
                bandwidth = v;
                break;
            }
        }
        work = t.Bandwidth.Replace(work, " ");

        var key = t.Residual.Replace(Strip(work), "");

        string? vendor = null;
        foreach (var (id, re) in t.Vendors)
        {
            if (re.IsMatch(key))
            {
                vendor ??= id;
                key = re.Replace(key, "");
            }
        }
        sloan |= t.SloanWord.IsMatch(key);
        foreach (var re in t.Noise) key = re.Replace(key, "");

        return new Prepared(folded, key, blank, bandwidth, vendor, sloan);
    }

    // NFKC, lower-case, the table's replacements (alpha, beta), runs of whitespace to one space, trimmed.
    private string Fold(string raw)
    {
        var lowered = raw.Normalize(NormalizationForm.FormKC).ToLowerInvariant();
        var sb = new StringBuilder(lowered.Length);
        var pendingSpace = false;
        foreach (var ch in lowered)
        {
            if (char.IsWhiteSpace(ch))
            {
                pendingSpace = sb.Length > 0;
                continue;
            }
            if (pendingSpace)
            {
                sb.Append(' ');
                pendingSpace = false;
            }
            sb.Append(table.FoldReplace.TryGetValue(ch, out var to) ? to : ch);
        }
        return sb.ToString();
    }

    private string Strip(string s)
    {
        var sb = new StringBuilder(s.Length);
        foreach (var ch in s)
            if (!char.IsWhiteSpace(ch) && table.StripChars.IndexOf(ch) < 0) sb.Append(ch);
        return sb.ToString();
    }

    // ---- steps 4 to 6 ---------------------------------------------------------------------------------------------------

    private sealed record Hit(string Canonical, string? Vendor, string Confidence, string Step);

    private abstract record Resolved
    {
        public sealed record HitOf(Hit Hit) : Resolved;
        public sealed record AmbiguousResult : Resolved;
        public sealed record Miss : Resolved;
    }

    private Resolved ResolveText(Prepared p, HashSet<string> declared, bool photometric)
    {
        if (p.Key.Length == 0) return new Resolved.Miss();
        bool Gate(string c) => !table.PhotometricCanonicals.Contains(c) || declared.Contains(c);

        // Step 4: exact alias.
        if (table.Aliases.TryGetValue(p.Key, out var alias))
        {
            if (!Gate(alias.Canonical)) return new Resolved.Miss();
            return new Resolved.HitOf(new Hit(alias.Canonical, alias.Vendor ?? p.Vendor, FilterConfidences.Exact, FilterSteps.Alias));
        }

        // Step 5: pattern rules, in order.
        foreach (var rule in table.Patterns)
        {
            if (rule.RequiresBandwidth && p.Bandwidth is null) continue;
            if (rule.Regex.IsMatch(p.Key))
            {
                if (!Gate(rule.Canonical)) return new Resolved.Miss();
                return new Resolved.HitOf(new Hit(rule.Canonical, p.Vendor, FilterConfidences.Pattern, FilterSteps.Pattern));
            }
        }

        // Step 6: single letters.
        if (p.Key.Length != 1 || !table.SingleLetters.TryGetValue(p.Key[0], out var candidate)) return new Resolved.Miss();
        Resolved Take(string canonical) => new Resolved.HitOf(new Hit(canonical, p.Vendor, FilterConfidences.Inferred, FilterSteps.SingleLetter));
        if (p.Sloan && Array.IndexOf(SloanLetters, p.Key[0]) >= 0)
        {
            if (!photometric) return new Resolved.Miss();
            return candidate is "G" or "R" ? Take(candidate) : declared.Contains(candidate) ? Take(candidate) : new Resolved.Miss();
        }
        if (candidate == "G" && photometric) return new Resolved.AmbiguousResult();
        if (declared.Contains(candidate)) return Take(candidate);
        return new Resolved.Miss();
    }

    // ---- step 8 -----------------------------------------------------------------------------------------------------------

    private static string Basename(string path)
    {
        var i = path.LastIndexOfAny(new[] { '/', '\\' });
        return i < 0 ? path : path[(i + 1)..];
    }

    private Hit? FromFilename(string name, HashSet<string> declared, bool photometric)
    {
        var folded = Fold(Basename(name));
        var tokens = table.FilenameSplit.Split(folded).Where(t => t.Length > 0).ToArray();
        for (var i = 0; i < tokens.Length; i++)
        {
            var candidates = new List<string>(2);
            if (i + 1 < tokens.Length) candidates.Add(tokens[i] + tokens[i + 1]);
            candidates.Add(tokens[i]);
            foreach (var c in candidates)
                if (ResolveText(Prepare(c), declared, photometric) is Resolved.HitOf(var h)) return h;
        }
        return null;
    }

    // ---- the exported table -----------------------------------------------------------------------------------------------

    private sealed record Alias(string Canonical, string? Vendor);

    private sealed record PatternRule(string Id, Regex Regex, string Canonical, bool RequiresBandwidth);

    private sealed class Table
    {
        public int TableVersion { get; private init; }
        public HashSet<string> PhotometricCanonicals { get; private init; } = new();
        public Dictionary<char, char> FoldReplace { get; private init; } = new();
        public string StripChars { get; private init; } = "";
        public Regex Range { get; private init; } = null!;
        public Regex Bandwidth { get; private init; } = null!;
        public int BandwidthGroup { get; private init; }
        public double BandwidthMinExclusive { get; private init; }
        public double BandwidthMax { get; private init; }
        public Regex Residual { get; private init; } = null!;
        public Regex SloanPrime { get; private init; } = null!;
        public Regex SloanWord { get; private init; } = null!;
        public List<(string Id, Regex Re)> Vendors { get; private init; } = new();
        public List<Regex> Noise { get; private init; } = new();
        public Dictionary<char, string> SingleLetters { get; private init; } = new();
        public Dictionary<string, Alias> Aliases { get; private init; } = new(StringComparer.Ordinal);
        public List<PatternRule> Patterns { get; private init; } = new();
        public Regex FilenameSplit { get; private init; } = null!;

        // No flags: the input is already lower-case, and the sources are in the common subset of Rust, JavaScript and .NET.
        private static Regex Rx(string source) => new(source, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(2));

        public static Table Parse(JsonElement root)
        {
            if (root.GetProperty("schema").GetString() != "collab-filters/aliases")
                throw new FormatException("Not a collab-filters alias table.");
            var schema = root.GetProperty("schema_version").GetInt32();
            if (schema != SupportedSchemaVersion)
                throw new FormatException($"Alias table schema {schema} is not supported by this plugin (it knows {SupportedSchemaVersion}); update the plugin.");

            var text = root.GetProperty("text");
            var fold = text.GetProperty("fold");
            var bw = text.GetProperty("bandwidth");
            var resolution = root.GetProperty("resolution");

            var t = new Table
            {
                TableVersion = root.GetProperty("table_version").GetInt32(),
                StripChars = text.GetProperty("strip_chars").GetString()!,
                Range = Rx(text.GetProperty("range_regex").GetString()!),
                Bandwidth = Rx(bw.GetProperty("regex").GetString()!),
                BandwidthGroup = bw.GetProperty("group").GetInt32(),
                BandwidthMinExclusive = bw.GetProperty("min_exclusive_nm").GetDouble(),
                BandwidthMax = bw.GetProperty("max_nm").GetDouble(),
                Residual = Rx(text.GetProperty("residual_nm_regex").GetString()!),
                SloanPrime = Rx(text.GetProperty("sloan_prime_regex").GetString()!),
                SloanWord = Rx(text.GetProperty("sloan_word_regex").GetString()!),
                FilenameSplit = Rx(resolution.GetProperty("filename").GetProperty("split_regex").GetString()!),
            };
            if (fold.GetProperty("unicode_normalization").GetString() != "NFKC" || !fold.GetProperty("lowercase").GetBoolean())
                throw new FormatException("The alias table asks for text folding this port does not implement.");
            foreach (var pair in fold.GetProperty("replace").EnumerateArray())
            {
                var from = pair.GetProperty("from").GetString()!;
                var to = pair.GetProperty("to").GetString()!;
                if (from.Length != 1 || to.Length != 1) throw new FormatException("Only single-character fold replacements are supported.");
                t.FoldReplace[from[0]] = to[0];
            }
            foreach (var v in text.GetProperty("vendors").EnumerateArray())
                t.Vendors.Add((v.GetProperty("id").GetString()!, Rx(v.GetProperty("regex").GetString()!)));
            foreach (var n in text.GetProperty("noise").EnumerateArray())
                t.Noise.Add(Rx(n.GetString()!));
            foreach (var c in root.GetProperty("canonical").EnumerateArray())
                if (c.GetProperty("photometric").GetBoolean()) t.PhotometricCanonicals.Add(c.GetProperty("id").GetString()!);
            foreach (var s in root.GetProperty("single_letters").EnumerateArray())
                t.SingleLetters[s.GetProperty("letter").GetString()![0]] = s.GetProperty("canonical").GetString()!;
            foreach (var a in root.GetProperty("aliases").EnumerateArray())
                t.Aliases[a.GetProperty("key").GetString()!] = new Alias(
                    a.GetProperty("canonical").GetString()!,
                    a.GetProperty("vendor").ValueKind == JsonValueKind.String ? a.GetProperty("vendor").GetString() : null);
            foreach (var p in root.GetProperty("patterns").EnumerateArray())
                t.Patterns.Add(new PatternRule(p.GetProperty("id").GetString()!, Rx(p.GetProperty("regex").GetString()!),
                    p.GetProperty("canonical").GetString()!, p.GetProperty("requires_bandwidth").GetBoolean()));
            return t;
        }
    }
}

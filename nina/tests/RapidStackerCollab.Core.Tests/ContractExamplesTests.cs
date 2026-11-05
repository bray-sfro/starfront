// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text.Json;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Projects;
using RapidStackerCollab.Core.Tests.Contract;
using RapidStackerCollab.Core.Tests.Support;

namespace RapidStackerCollab.Core.Tests;

/// <summary>
/// The plugin's share of <c>scripts/contract-examples.json</c>: the same examples the backend and the website are held to.
/// Each test skips, with a reason, when the file or the contract is not in the checkout.
/// </summary>
public class ContractExamplesTests
{
    [Test]
    public void Every_example_identifier_is_judged_by_the_contract_as_the_examples_say()
    {
        // Proves the examples themselves against the contract's patterns from this side too (check-contract.mjs does it in Node).
        using var ex = RepoPaths.Examples();
        var contract = ContractDocument.Load();
        var problems = new List<string>();
        foreach (var kind in ex.RootElement.GetProperty("identifiers").EnumerateObject().Where(p => !p.Name.StartsWith('$')))
        {
            var schema = System.Text.Json.Nodes.JsonNode.Parse($"{{\"$ref\":\"#/components/schemas/{kind.Name}\"}}")!;
            foreach (var v in kind.Value.GetProperty("valid").EnumerateArray())
                foreach (var e in contract.Validate(schema, System.Text.Json.Nodes.JsonValue.Create(v.GetString())))
                    problems.Add($"{kind.Name}: '{v}' is listed valid but: {e}");
            foreach (var v in kind.Value.GetProperty("invalid").EnumerateArray())
                if (contract.Validate(schema, System.Text.Json.Nodes.JsonValue.Create(v.GetString())).Count == 0)
                    problems.Add($"{kind.Name}: '{v}' is listed invalid but the contract accepts it");
        }
        Assert.That(problems, Is.Empty, string.Join(Environment.NewLine, problems));
    }

    private static readonly (double Ra, double Dec)[] None = Array.Empty<(double, double)>();

    [Test]
    public void The_grid_footprint_convention_reproduces_the_shared_corner_examples()
    {
        // rotation_deg is the position angle of image-up, east of north; at 0 east is LEFT. SkyMath.TryGridOffset turns a
        // sky position into (right, up) pixel offsets from the centre, so each expected corner must come back as
        // (+-w/2, +-h/2), bottom-left first. This is the plugin's half of the three-way agreement with the website's
        // grid-math.js and the engine's ProjectGrid (the engine's test is added when m3-worker is merged).
        using var ex = RepoPaths.Examples();
        var grid = ex.RootElement.GetProperty("grid");
        var tolPx = 0.02; // 1e-5 degrees is 0.036 arcsec: a few hundredths of a pixel at 1.5-2 arcsec/px
        var problems = new List<string>();
        foreach (var c in grid.GetProperty("cases").EnumerateArray())
        {
            var g = new Grid
            {
                CenterRaDeg = c.GetProperty("center_ra_deg").GetDouble(),
                CenterDecDeg = c.GetProperty("center_dec_deg").GetDouble(),
                RotationDeg = c.GetProperty("rotation_deg").GetDouble(),
                ScaleArcsecPx = c.GetProperty("scale_arcsec_px").GetDouble(),
                WidthPx = c.GetProperty("width_px").GetInt32(),
                HeightPx = c.GetProperty("height_px").GetInt32(),
            };
            var signs = new[] { (-1, -1), (1, -1), (1, 1), (-1, 1) }; // BL, BR, TR, TL as (right, up)
            var i = 0;
            foreach (var corner in c.GetProperty("corners").EnumerateArray())
            {
                var ok = SkyMath.TryGridOffset(g, corner.GetProperty("ra_deg").GetDouble(), corner.GetProperty("dec_deg").GetDouble(), out var right, out var up);
                var (sx, sy) = signs[i++];
                var wantRight = sx * g.WidthPx / 2.0;
                var wantUp = sy * g.HeightPx / 2.0;
                if (!ok || Math.Abs(right - wantRight) > tolPx || Math.Abs(up - wantUp) > tolPx)
                    problems.Add($"{c.GetProperty("name").GetString()} corner {i - 1}: offset ({right:F3}, {up:F3}) px, expected ({wantRight}, {wantUp})");
            }
            // Inside/outside agree with the footprint rectangle.
            Assert.That(SkyMath.InsideGrid(g, g.CenterRaDeg, g.CenterDecDeg), Is.True, c.GetProperty("name").GetString() + " centre");
        }
        Assert.That(problems, Is.Empty, string.Join(Environment.NewLine, problems));
    }

    [Test]
    public void Status_constants_equal_the_contract_enums()
    {
        var schemas = ContractDocument.Load().Schemas;
        static HashSet<string> Enum(System.Text.Json.Nodes.JsonObject schemas, string name) =>
            schemas[name]!["enum"]!.AsArray().Select(n => n!.GetValue<string>()).ToHashSet();
        static HashSet<string> Consts(Type t) => t.GetFields().Where(f => f.IsLiteral).Select(f => (string)f.GetRawConstantValue()!).ToHashSet();
        Assert.That(Consts(typeof(ProjectStatuses)), Is.EquivalentTo(Enum(schemas, "ProjectStatus")));
        Assert.That(Consts(typeof(MemberStatuses)), Is.EquivalentTo(Enum(schemas, "MemberStatus")));
        Assert.That(Consts(typeof(ApiScopes)).Except(new[] { "PluginDefaults" }), Is.SubsetOf(Enum(schemas, "Scope")));
    }
}

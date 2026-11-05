// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text.Json.Nodes;

namespace RapidStackerCollab.Core.Tests.Fakes;

/// <summary>Contract-shaped mosaic JSON for tests (openapi.yaml MosaicState, panels.schema.json). Centres are a flat approximation: the tests check the plumbing, not the projection.</summary>
internal static class MosaicSamples
{
    /// <summary>A 3 x 4 mosaic (rows x cols, 12 panels), 10 % overlap, 1.5 x 1.0 degree panels, Orion-ish centre.</summary>
    public static JsonObject ThreeByFour(string[]? myPanels = null, string[]? myClaims = null, JsonObject? targets = null)
    {
        const int rows = 3, cols = 4;
        const double stepW = 1.5 * 0.9, stepH = 1.0 * 0.9, ra0 = 83.82, dec0 = -5.39;
        var panels = new JsonArray();
        for (var r = 1; r <= rows; r++)
            for (var c = 1; c <= cols; c++)
            {
                var id = $"r{r}c{c}";
                panels.Add(new JsonObject
                {
                    ["panel_id"] = id, ["row"] = r, ["col"] = c,
                    ["ra_deg"] = Math.Round(ra0 - (c - (cols + 1) / 2.0) * stepW, 6),
                    ["dec_deg"] = Math.Round(dec0 + ((rows + 1) / 2.0 - r) * stepH, 6),
                    ["pa_deg"] = 0.25,
                    ["assigned_to"] = id == "r1c1" ? "Ed" : id == "r1c2" ? "Mira" : null,
                    ["claimed_by"] = id == "r2c3" ? new JsonArray("Ed", "Mira") : new JsonArray(),
                });
            }
        return new JsonObject
        {
            ["layout"] = new JsonObject
            {
                ["rows"] = rows, ["cols"] = cols, ["overlap_pct"] = 10, ["panel_w_deg"] = 1.5, ["panel_h_deg"] = 1.0,
                ["rotation_deg"] = 0, ["scale_arcsec_px"] = 2.0, ["centre"] = new JsonObject { ["ra_deg"] = ra0, ["dec_deg"] = dec0 },
            },
            ["grid"] = new JsonObject
            {
                ["center_ra_deg"] = ra0, ["center_dec_deg"] = dec0, ["rotation_deg"] = 0, ["scale_arcsec_px"] = 2.0,
                ["width_px"] = 14000, ["height_px"] = 6200, // above the 12 000 an owner can set: a derived grid may be larger
            },
            ["locked"] = false, ["revision"] = 3, ["panels"] = panels,
            ["targets"] = targets ?? new JsonObject
            {
                ["default"] = new JsonObject { ["HA"] = 6, ["OIII"] = 6 },
                ["panels"] = new JsonObject { ["r1c1"] = new JsonObject { ["HA"] = 10, ["OIII"] = 0 } },
            },
            ["my_panels"] = new JsonArray((myPanels ?? new[] { "r1c1" }).Select(x => (JsonNode)x).ToArray()),
            ["my_claims"] = new JsonArray((myClaims ?? new[] { "r2c3" }).Select(x => (JsonNode)x).ToArray()),
        };
    }

    /// <summary>A panels.json where r1c1 has 3.2 h of HA (in two streams of the filter) and r2c3 has 1 h of OIII.</summary>
    public static JsonObject Progress() => new()
    {
        ["schema_version"] = 1, ["generated_at"] = "2026-10-13T12:00:00.000Z", ["access"] = "public", ["project_id"] = "prj_7k3m9q2x5v8d1hn4t6wz",
        ["attribution"] = "nearest_centre", ["layout"] = new JsonObject { ["rows"] = 3, ["cols"] = 4 },
        ["targets"] = new JsonObject { ["default"] = new JsonObject { ["HA"] = 6, ["OIII"] = 6 }, ["panels"] = new JsonObject() },
        ["totals"] = new JsonObject { ["panels_with_data"] = 2, ["frames_accepted"] = 52, ["integration_s"] = 15120 },
        ["panels"] = new JsonArray(
            Panel("r1c1", ("HA", 7200, 24), ("HA@3nm", 4320, 14)),
            Panel("r2c3", ("OIII", 3600, 12))),
        ["a_future_member"] = true, // unknown members are ignored
    };

    private static JsonObject Panel(string id, params (string Key, double S, int N)[] streams)
    {
        var s = new JsonObject();
        foreach (var (key, secs, n) in streams)
            s[key] = new JsonObject
            {
                ["frames_accepted"] = n, ["integration_s"] = secs, ["median_fwhm_arcsec"] = 2.4, ["contributors"] = new JsonArray("Ed"),
                ["coverage"] = null, ["target_s"] = null,
            };
        return new JsonObject
        {
            ["panel_id"] = id, ["frames_accepted"] = streams.Sum(x => x.N), ["integration_s"] = streams.Sum(x => x.S),
            ["contributors"] = new JsonArray("Ed"), ["streams"] = s,
        };
    }
}

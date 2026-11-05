// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Net;
using System.Text.Json.Nodes;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Panels;
using RapidStackerCollab.Core.Tests.Fakes;

namespace RapidStackerCollab.Core.Tests;

public class SexagesimalTests
{
    [TestCase(0.0, "00h 00m 00.0s")]
    [TestCase(83.8220833, "05h 35m 17.3s")]
    [TestCase(10.6847, "00h 42m 44.3s")]
    [TestCase(359.99999, "00h 00m 00.0s")] // rounds up into the next day, never "24h"
    [TestCase(-15.0, "23h 00m 00.0s")]
    public void Right_ascension_is_hours_minutes_seconds(double deg, string expected) =>
        Assert.That(Sexagesimal.Ra(deg), Is.EqualTo(expected));

    [TestCase(-5.3911, "-05° 23' 28\"")]
    [TestCase(41.2692, "+41° 16' 09\"")]
    [TestCase(0.0, "+00° 00' 00\"")]
    [TestCase(-0.00001, "+00° 00' 00\"")]
    [TestCase(89.99999, "+90° 00' 00\"")]
    public void Declination_is_signed_degrees_minutes_seconds(double deg, string expected) =>
        Assert.That(Sexagesimal.Dec(deg), Is.EqualTo(expected));

    [Test]
    public void The_colon_form_is_what_the_framing_assistant_takes()
    {
        Assert.That(Sexagesimal.Ra(83.8220833, 1, colon: true), Is.EqualTo("05:35:17.3"));
        Assert.That(Sexagesimal.Dec(-5.3911, 0, colon: true), Is.EqualTo("-05:23:28"));
    }
}

public class YourPanelsTests
{
    private static MosaicState State(JsonObject? json = null) => ApiJson.Deserialize<MosaicState>((json ?? MosaicSamples.ThreeByFour()).ToJsonString())!;

    private static PanelsDocument Progress() => ApiJson.Deserialize<PanelsDocument>(MosaicSamples.Progress().ToJsonString())!;

    [Test]
    public void It_lists_assigned_then_claimed_panels_in_grid_order_with_where_to_point()
    {
        var view = YourPanels.Build(State(), progress: null, myDisplayName: "Ed");

        Assert.That(view.Header, Is.EqualTo("Mosaic 3 x 4 panels, 10 % overlap, panel 1.5 x 1 deg"));
        Assert.That(view.Rows.Select(r => r.PanelId), Is.EqualTo(new[] { "r1c1", "r2c3" }));
        var a = view.Rows[0];
        Assert.That(a.Role, Is.EqualTo("assigned"));
        Assert.That(a.RoleText, Is.EqualTo("Assigned to you"));
        Assert.That(a.PaText, Is.EqualTo("PA 0.3°").Or.EqualTo("PA 0.2°"));
        Assert.That(a.PositionText, Is.EqualTo("RA 05h 43m 22.8s (85.8450\u00B0), Dec -04\u00B0 29' 24\" (-4.4900\u00B0)"));
        Assert.That(a.OverlapText, Is.EqualTo("Overlap with neighbours 10 %"));
        Assert.That(a.SharedWithText, Is.Empty, "the user's own name is not listed");
        var c = view.Rows[1];
        Assert.That(c.Role, Is.EqualTo("claimed"));
        Assert.That(c.SharedWithText, Is.EqualTo("Also: Mira"), "display names only, never ourselves");
    }

    [Test]
    public void A_panel_both_assigned_and_claimed_is_one_row()
    {
        var view = YourPanels.Build(State(MosaicSamples.ThreeByFour(myPanels: new[] { "r1c1" }, myClaims: new[] { "r1c1", "r2c3" })), null);
        Assert.That(view.Rows.Select(r => (r.PanelId, r.Role)), Is.EqualTo(new[] { ("r1c1", "both"), ("r2c3", "claimed") }));
    }

    [Test]
    public void An_anonymous_reader_has_no_rows_and_a_hint()
    {
        var view = YourPanels.Build(State(MosaicSamples.ThreeByFour(myPanels: Array.Empty<string>(), myClaims: Array.Empty<string>())), null);
        Assert.That(view.HasRows, Is.False);
        Assert.That(view.EmptyText, Does.Contain("no panels"));
    }

    [Test]
    public void Progress_is_hours_against_the_panels_target_with_streams_of_one_filter_added_up()
    {
        // r1c1 has an override: HA 10 h, OIII 0 (= no target). 7200 s + 4320 s of HA / HA@3nm = 3.2 h.
        var row = YourPanels.Build(State(), Progress(), "Ed").Rows[0];
        Assert.That(row.Progress.Select(p => p.Filter), Is.EqualTo(new[] { "HA" }), "OIII 0 override: no target and no data, so no line");
        Assert.That(row.Progress[0].HoursDone, Is.EqualTo(3.2).Within(1e-9));
        Assert.That(row.Progress[0].TargetHours, Is.EqualTo(10));
        Assert.That(row.Progress[0].Fraction, Is.EqualTo(0.32).Within(1e-9));
        Assert.That(row.ProgressText, Is.EqualTo("HA 3.2 h of 10 h (32 %)"));
    }

    [Test]
    public void A_panel_without_data_shows_its_targets_at_zero_and_one_with_data_but_no_target_shows_hours()
    {
        var view = YourPanels.Build(State(), Progress(), "Ed");
        var r2c3 = view.Rows[1];
        Assert.That(r2c3.ProgressText, Is.EqualTo("HA 0 h of 6 h (0 %); OIII 1 h of 6 h (17 %)").Or.EqualTo("OIII 1 h of 6 h (17 %); HA 0 h of 6 h (0 %)"));

        var targets = new JsonObject { ["default"] = new JsonObject(), ["panels"] = new JsonObject() };
        var noTargets = YourPanels.Build(State(MosaicSamples.ThreeByFour(targets: targets)), Progress(), "Ed").Rows[0];
        Assert.That(noTargets.ProgressText, Is.EqualTo("HA 3.2 h"));
        Assert.That(noTargets.Progress[0].Fraction, Is.Null);
    }

    [Test]
    public void Without_panels_json_the_targets_still_show_and_say_progress_is_not_known()
    {
        var row = YourPanels.Build(State(), progress: null, "Ed").Rows[0];
        Assert.That(row.ProgressText, Is.EqualTo("HA 0 h of 10 h (0 %) (progress not available yet)"));
    }

    [TestCase("HA", "HA")]
    [TestCase("HA@3nm", "HA")]
    [TestCase("R@osc", "R")]
    [TestCase("HA@osc@7nm", "HA")]
    public void A_stream_key_belongs_to_its_canonical_filter(string key, string filter) => Assert.That(YourPanels.FilterOf(key), Is.EqualTo(filter));

    [Test]
    public void Target_resolution_is_override_then_default_and_zero_means_none()
    {
        var t = State().Targets;
        Assert.That(YourPanels.TargetHours(t, "r1c1", "HA"), Is.EqualTo(10));
        Assert.That(YourPanels.TargetHours(t, "r1c1", "OIII"), Is.Null, "override 0 beats the default 6");
        Assert.That(YourPanels.TargetHours(t, "r3c4", "OIII"), Is.EqualTo(6));
        Assert.That(YourPanels.TargetHours(t, "r3c4", "SII"), Is.Null);
    }

    [Test]
    public void Panel_ids_sort_row_major_not_alphabetically()
    {
        var ids = new[] { "r2c10", "r10c1", "r2c9", "r1c25" };
        Assert.That(ids.OrderBy(i => i, Comparer<string>.Create(YourPanels.ComparePanelIds)), Is.EqualTo(new[] { "r1c25", "r2c9", "r2c10", "r10c1" }));
    }

    [Test]
    public void A_grid_wider_than_12000_px_is_read_without_complaint()
    {
        Assert.That(State().Grid.WidthPx, Is.EqualTo(14000));
        Assert.That(State().Panels, Has.Count.EqualTo(12));
    }
}

public class PanelPointingsTests
{
    [Test]
    public void The_csv_has_a_header_one_row_per_panel_in_grid_order_and_crlf()
    {
        var state = ApiJson.Deserialize<MosaicState>(MosaicSamples.ThreeByFour().ToJsonString())!;
        var csv = PanelPointings.Csv(state.Panels.Where(p => p.PanelId is "r2c3" or "r1c1"), "Orion, the \"Sword\"");
        var lines = csv.Split("\r\n");
        Assert.That(lines[0], Is.EqualTo("id,name,ra_deg,dec_deg,pa_deg,ra,dec"));
        Assert.That(lines, Has.Length.EqualTo(4), "header, two rows, trailing newline");
        Assert.That(lines[3], Is.Empty);
        Assert.That(lines[1], Does.StartWith("r1c1,\"Orion, the \"\"Sword\"\" r1c1\",85.845000,-4.490000,0.25,05:43:22.8,-04:29:24"));
        Assert.That(lines[2], Does.StartWith("r2c3,"));
    }

    [Test]
    public void Without_a_target_name_the_name_is_the_panel_id()
    {
        var state = ApiJson.Deserialize<MosaicState>(MosaicSamples.ThreeByFour().ToJsonString())!;
        Assert.That(PanelPointings.Csv(state.Panels.Take(1), null).Split("\r\n")[1], Does.StartWith("r1c1,r1c1,"));
    }
}

public class MosaicApiClientTests
{
    private FakeCollabServer server = null!;
    private ApiClient api = null!;

    [SetUp]
    public async Task SetUp()
    {
        server = await FakeCollabServer.StartAsync();
        api = TestApi.Client(server);
    }

    [TearDown]
    public async Task TearDown() => await server.DisposeAsync();

    [Test]
    public async Task A_single_field_project_has_a_null_mosaic()
    {
        Assert.That(await api.GetMosaicAsync(FakeCollabServer.ProjectId), Is.Null);
        var sent = server.RequestsTo("GET", "/mosaic").Single();
        Assert.That(sent.Authorization, Is.EqualTo("Bearer " + FakeCollabServer.Token));
    }

    [Test]
    public async Task A_three_by_four_mosaic_is_read_in_full()
    {
        server.Mosaic = MosaicSamples.ThreeByFour();
        var m = (await api.GetMosaicAsync(FakeCollabServer.Slug))!;
        Assert.That(m.Layout.Rows * m.Layout.Cols, Is.EqualTo(12));
        Assert.That(m.Panels, Has.Count.EqualTo(12));
        Assert.That(m.Panels[0].PanelId, Is.EqualTo("r1c1"));
        Assert.That(m.Panels[0].AssignedTo, Is.EqualTo("Ed"));
        Assert.That(m.Panels.Single(p => p.PanelId == "r2c3").ClaimedBy, Is.EqualTo(new[] { "Ed", "Mira" }));
        Assert.That(m.Targets.Default["HA"], Is.EqualTo(6));
        Assert.That(m.Targets.Panels["r1c1"]["OIII"], Is.EqualTo(0));
        Assert.That(m.MyPanels, Is.EqualTo(new[] { "r1c1" }));
        Assert.That(m.Grid.WidthPx, Is.EqualTo(14000));
        Assert.That(m.Revision, Is.EqualTo(3));
    }

    [Test]
    public async Task An_anonymous_reader_gets_empty_my_panels()
    {
        server.Mosaic = MosaicSamples.ThreeByFour(myPanels: Array.Empty<string>(), myClaims: Array.Empty<string>());
        var m = (await api.GetMosaicAsync(FakeCollabServer.ProjectId))!;
        Assert.That(m.MyPanels, Is.Empty);
        Assert.That(m.MyClaims, Is.Empty);
    }

    [Test]
    public async Task Claims_are_sent_as_the_whole_list_and_the_new_state_comes_back()
    {
        server.Mosaic = MosaicSamples.ThreeByFour();
        var m = (await api.PutClaimsAsync(FakeCollabServer.ProjectId, new[] { "r3c4" }))!;
        Assert.That(m.MyClaims, Is.EqualTo(new[] { "r3c4" }));
        var body = JsonNode.Parse(server.RequestsTo("PUT", "/mosaic/claims").Single().Body!)!;
        Assert.That(body["panels"]!.AsArray().Select(n => n!.GetValue<string>()), Is.EqualTo(new[] { "r3c4" }));
    }

    [Test]
    public void Claims_on_a_single_field_project_are_mosaic_required()
    {
        var ex = Assert.ThrowsAsync<ApiException>(async () => await api.PutClaimsAsync(FakeCollabServer.ProjectId, new[] { "r1c1" }))!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.MosaicRequired));
    }

    [Test]
    public async Task The_panels_document_is_read_tolerantly_without_the_bearer_token()
    {
        server.PanelsDoc = MosaicSamples.Progress();
        var doc = await api.GetPanelsDocumentAsync(new Uri(server.BaseAddress, "public/panels.json").AbsoluteUri);
        Assert.That(doc.Panels.Select(p => p.PanelId), Is.EqualTo(new[] { "r1c1", "r2c3" }));
        Assert.That(doc.Panels[0].Streams["HA@3nm"].IntegrationS, Is.EqualTo(4320));
        Assert.That(doc.Panels[0].Streams["HA"].TargetS, Is.Null);
        Assert.That(doc.Layout.Rows, Is.EqualTo(3));
        Assert.That(server.Requests.Single(r => r.Path == "/public/panels.json").Authorization, Is.Empty);
    }

    [Test]
    public void A_missing_panels_document_is_a_bad_response_not_a_crash()
    {
        var ex = Assert.ThrowsAsync<ApiException>(async () => await api.GetPanelsDocumentAsync(new Uri(server.BaseAddress, "public/panels.json").AbsoluteUri))!;
        Assert.That(ex.Code, Is.EqualTo(ApiCodes.BadResponse));
        Assert.That(ex.HttpStatus, Is.EqualTo((int)HttpStatusCode.NotFound));
    }
}

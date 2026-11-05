// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Tests.Support;
using YamlDotNet.Serialization;

namespace RapidStackerCollab.Core.Tests;

/// <summary>The DTOs are held against openapi.yaml itself, so a contract change that is not mirrored fails here.</summary>
public class ApiDtoTests
{
    private static Dictionary<object, object> LoadContract()
    {
        var text = File.ReadAllText(RepoPaths.Contract("openapi.yaml"));
        return (Dictionary<object, object>)new DeserializerBuilder().Build().Deserialize<object>(text);
    }

    private static Dictionary<object, object> Dict(object o) => (Dictionary<object, object>)o;

    private static IEnumerable<string> SchemaProps(Dictionary<object, object> schemas, string name)
    {
        var s = Dict(schemas[name]);
        return Dict(s["properties"]).Keys.Select(k => (string)k);
    }

    private static HashSet<string> ClrNames(Type t) =>
        ApiJson.Options.GetTypeInfo(t).Properties.Select(p => p.Name).ToHashSet();

    // schema name -> (CLR type, schema properties deliberately not modelled)
    private static readonly (string Schema, Type Clr, string[] Omitted)[] Map =
    {
        ("DeviceStartRequest", typeof(DeviceStartRequest), Array.Empty<string>()),
        ("DeviceStartResponse", typeof(DeviceStartResponse), Array.Empty<string>()),
        ("DevicePollRequest", typeof(DevicePollRequest), Array.Empty<string>()),
        ("DevicePollResponse", typeof(DevicePollResponse), Array.Empty<string>()),
        ("TokenInfo", typeof(TokenInfo), Array.Empty<string>()),
        ("User", typeof(UserInfo), Array.Empty<string>()),
        ("Rig", typeof(Rig), Array.Empty<string>()),
        ("RigOrientation", typeof(RigOrientation), Array.Empty<string>()),
        ("RigUpsertRequest", typeof(RigUpsertRequest), Array.Empty<string>()),
        ("RigPatchRequest", typeof(RigPatchRequest), Array.Empty<string>()),
        ("RigResponse", typeof(RigResponse), Array.Empty<string>()),
        ("RigListResponse", typeof(RigListResponse), Array.Empty<string>()),
        ("Grid", typeof(Grid), Array.Empty<string>()),
        ("ContributorMappingEntry", typeof(ContributorMappingEntry), Array.Empty<string>()),
        // The plugin reads policy to show it and to honour require_coordinate_strip; the tuning knobs are the
        // worker's business.
        ("Policy", typeof(ProjectPolicy), new[]
        {
            "live_clip_k", "live_n_min", "gold_nightly", "gold_nightly_hour_utc", "gold_threshold_frames",
            "gold_threshold_growth_pct", "gold_exclude_classes",
        }),
        ("ProjectCounts", typeof(ProjectCounts), Array.Empty<string>()),
        ("ProjectSummary", typeof(ProjectSummary), Array.Empty<string>()),
        ("Project", typeof(ProjectDetail), new[] { "notify", "quotas" }),
        ("ProjectResponse", typeof(ProjectResponse), Array.Empty<string>()),
        ("ProjectListResponse", typeof(ProjectListResponse), Array.Empty<string>()),
        ("ResolveJoinCodeRequest", typeof(ResolveJoinCodeRequest), Array.Empty<string>()),
        ("ResolveJoinCodeResponse", typeof(ResolveJoinCodeResponse), Array.Empty<string>()),
        ("JoinRequest", typeof(JoinRequest), Array.Empty<string>()),
        ("JoinResponse", typeof(JoinResponse), Array.Empty<string>()),
        ("Membership", typeof(Membership), Array.Empty<string>()),
        ("MemberStats", typeof(MemberStats), Array.Empty<string>()),
        ("MembershipResponse", typeof(MembershipResponse), Array.Empty<string>()),
        ("UpdateMembershipRequest", typeof(UpdateMembershipRequest), Array.Empty<string>()),
        ("FilterResolution", typeof(FilterResolution), Array.Empty<string>()),
        ("NormalizeResponse", typeof(NormalizeResponse), Array.Empty<string>()),
        ("InitHeader", typeof(InitHeader), Array.Empty<string>()),
        ("FrameInitRequest", typeof(FrameInitRequest), Array.Empty<string>()),
        ("UploadPart", typeof(UploadPart), Array.Empty<string>()),
        ("UploadInstructions", typeof(UploadInstructions), Array.Empty<string>()),
        ("FrameInitResponse", typeof(FrameInitResponse), Array.Empty<string>()),
        ("FrameCompleteRequest", typeof(FrameCompleteRequest), Array.Empty<string>()),
        ("FrameCompleteResponse", typeof(FrameCompleteResponse), Array.Empty<string>()),
        ("FrameMetrics", typeof(FrameMetrics), Array.Empty<string>()),
        ("FrameSummary", typeof(FrameSummary), Array.Empty<string>()),
        // session_id and contributor are maintainer-view fields the plugin never shows.
        ("Frame", typeof(Frame), new[] { "session_id", "contributor" }),
        ("FrameResponse", typeof(FrameResponse), Array.Empty<string>()),
        ("FrameListResponse", typeof(FrameListResponse), Array.Empty<string>()),
        ("WithdrawResponse", typeof(WithdrawResponse), Array.Empty<string>()),
        // Calibration library (contract 1.23.0).
        ("LibraryFrameInitRequest", typeof(LibraryFrameInitRequest), Array.Empty<string>()),
        ("LibraryFrameInitResponse", typeof(LibraryFrameInitResponse), Array.Empty<string>()),
        ("LibraryFrameCompleteResponse", typeof(LibraryFrameCompleteResponse), Array.Empty<string>()),
        ("LibraryFrame", typeof(LibraryFrame), Array.Empty<string>()),
        ("LibraryMaster", typeof(LibraryMaster), Array.Empty<string>()),
        ("LibraryResponse", typeof(LibraryResponse), Array.Empty<string>()),
        ("AppliedMaster", typeof(AppliedMaster), Array.Empty<string>()),
        // Mosaic (contract 1.17.0). UpdateMosaicRequest, MosaicOperation and PanelSelector are the owner's: the plugin never sends them.
        ("Mosaic", typeof(Mosaic), Array.Empty<string>()),
        ("MosaicPanel", typeof(MosaicPanel), Array.Empty<string>()),
        ("MosaicTargets", typeof(MosaicTargets), Array.Empty<string>()),
        ("MosaicState", typeof(MosaicState), Array.Empty<string>()),
        ("MosaicResponse", typeof(MosaicResponse), Array.Empty<string>()),
        ("ClaimsRequest", typeof(ClaimsRequest), Array.Empty<string>()),
    };

    [Test]
    public void Every_modelled_schema_matches_the_contract_field_for_field()
    {
        var schemas = Dict(Dict(LoadContract()["components"])["schemas"]);
        var problems = new List<string>();
        foreach (var (schema, clr, omitted) in Map)
        {
            var wanted = SchemaProps(schemas, schema).Where(p => p is not ("ok" or "message")).ToHashSet();
            var have = ClrNames(clr);
            foreach (var missing in wanted.Except(have).Except(omitted))
                problems.Add($"{schema}: contract field '{missing}' is not in {clr.Name}");
            foreach (var extra in have.Except(wanted))
                problems.Add($"{schema}: {clr.Name} has '{extra}', which the contract does not");
        }
        Assert.That(problems, Is.Empty, string.Join(Environment.NewLine, problems));
    }

    [Test]
    public void Status_document_types_only_use_fields_the_schema_defines()
    {
        var text = File.ReadAllText(RepoPaths.Contract("status.schema.json"));
        using var doc = JsonDocument.Parse(text);
        var defs = doc.RootElement.GetProperty("$defs");
        var root = doc.RootElement.GetProperty("properties");
        static HashSet<string> Names(JsonElement obj) => obj.GetProperty("properties").EnumerateObject().Select(p => p.Name).ToHashSet();

        Assert.That(ClrNames(typeof(ProjectStatusDoc)).Except(root.EnumerateObject().Select(p => p.Name)), Is.Empty);
        Assert.That(ClrNames(typeof(StatusTotals)).Except(Names(root.GetProperty("totals"))), Is.Empty);
        Assert.That(ClrNames(typeof(StatusStream)).Except(Names(defs.GetProperty("stream"))), Is.Empty);
        Assert.That(ClrNames(typeof(PreviewSet)).Except(Names(defs.GetProperty("preview_set"))), Is.Empty);
        Assert.That(ClrNames(typeof(StatusCombined)).Except(Names(root.GetProperty("combined"))), Is.Empty);
        Assert.That(ClrNames(typeof(StatusContributor)).Except(
            root.GetProperty("contributors").GetProperty("items").GetProperty("properties").EnumerateObject().Select(p => p.Name)), Is.Empty);
        Assert.That(ClrNames(typeof(StatusPanels)).SetEquals(Names(root.GetProperty("panels"))), Is.True, "status.panels, field for field");
    }

    [Test]
    public void The_panels_document_types_match_panels_schema_json_field_for_field()
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(RepoPaths.Contract("panels.schema.json")));
        var root = doc.RootElement.GetProperty("properties");
        static HashSet<string> Names(JsonElement obj) => obj.GetProperty("properties").EnumerateObject().Select(p => p.Name).ToHashSet();
        var item = root.GetProperty("panels").GetProperty("items");
        var stream = item.GetProperty("properties").GetProperty("streams").GetProperty("additionalProperties");

        Assert.That(ClrNames(typeof(PanelsDocument)).SetEquals(root.EnumerateObject().Select(p => p.Name)), Is.True, "document");
        Assert.That(ClrNames(typeof(PanelsLayout)).SetEquals(Names(root.GetProperty("layout"))), Is.True, "layout");
        Assert.That(ClrNames(typeof(PanelsTotals)).SetEquals(Names(root.GetProperty("totals"))), Is.True, "totals");
        Assert.That(ClrNames(typeof(PanelProgress)).SetEquals(Names(item)), Is.True, "panel");
        // The previews of a panel stream are not modelled: the dock shows hours, not images, per panel.
        Assert.That(ClrNames(typeof(PanelStreamProgress)).SetEquals(Names(stream).Where(n => n != "previews")), Is.True, "stream");
        Assert.That(ClrNames(typeof(MosaicTargets)).SetEquals(Names(root.GetProperty("targets"))), Is.True, "targets");
    }

    [Test]
    public void ApiCodes_covers_exactly_the_contract_ErrorCode_enum()
    {
        var schemas = Dict(Dict(LoadContract()["components"])["schemas"]);
        var enumValues = ((List<object>)Dict(schemas["ErrorCode"])["enum"]).Cast<string>().ToHashSet();
        Assert.That(ApiCodes.Contract, Is.EquivalentTo(enumValues));
        var consts = typeof(ApiCodes).GetFields(BindingFlags.Public | BindingFlags.Static)
            .Where(f => f.IsLiteral).Select(f => (string)f.GetRawConstantValue()!).ToList();
        Assert.That(consts, Is.Unique);
    }

    [Test]
    public void Scopes_and_frame_statuses_match_the_contract()
    {
        var schemas = Dict(Dict(LoadContract()["components"])["schemas"]);
        var scopes = ((List<object>)Dict(schemas["Scope"])["enum"]).Cast<string>().ToHashSet();
        Assert.That(scopes, Does.Contain(ApiScopes.ProjectsManage));
        Assert.That(ApiScopes.PluginDefaults, Is.SubsetOf(scopes));
        Assert.That(ApiScopes.PluginDefaults, Does.Not.Contain(ApiScopes.ProjectsManage));

        var statuses = ((List<object>)Dict(schemas["FrameStatus"])["enum"]).Cast<string>().ToHashSet();
        var ours = typeof(FrameStatuses).GetFields(BindingFlags.Public | BindingFlags.Static)
            .Where(f => f.IsLiteral).Select(f => (string)f.GetRawConstantValue()!).ToHashSet();
        Assert.That(ours, Is.EquivalentTo(statuses));
    }

    [Test]
    public void InitHeader_can_emit_exactly_the_allow_list_and_nothing_else()
    {
        var allowed = ((List<object>)Dict(Dict(Dict(Dict(LoadContract()["components"])["schemas"])["InitHeader"])["properties"]).Keys.ToList<object>())
            .Cast<string>().ToList();
        Assert.That(InitHeader.AllowedKeys, Is.EquivalentTo(allowed));

        // Populate every settable property, then check the emitted JSON keys.
        var h = new InitHeader();
        foreach (var p in typeof(InitHeader).GetProperties().Where(p => p.CanWrite))
        {
            var t = Nullable.GetUnderlyingType(p.PropertyType) ?? p.PropertyType;
            object v = t == typeof(string) ? "x" : t == typeof(int) ? 1 : t == typeof(double) ? 1.5 : "x";
            p.SetValue(h, v);
        }
        using var json = JsonDocument.Parse(ApiJson.Serialize(h));
        Assert.That(json.RootElement.EnumerateObject().Select(p => p.Name), Is.EquivalentTo(InitHeader.AllowedKeys));
        foreach (var banned in new[] { "SITELAT", "SITELONG", "SITEELEV", "OBSERVER" })
            Assert.That(InitHeader.AllowedKeys, Does.Not.Contain(banned));
    }

    [Test]
    public void Snake_case_names_come_out_as_the_contract_spells_them()
    {
        var json = ApiJson.Serialize(new FrameInitRequest
        {
            Sha256 = new string('a', 64),
            Size = 123,
            Filename = "x.fits",
            Header = new InitHeader { Filter = "Ha", ExpTime = 300, XBinning = 1, Ra = 83.8, Dec = "-05 23 28" },
            RigHint = new RigHint { RigId = "rig_9c1d4e7a02bb" },
            Stripped = true,
        });
        using var d = JsonDocument.Parse(json);
        var root = d.RootElement;
        Assert.That(root.GetProperty("sha256").GetString(), Has.Length.EqualTo(64));
        Assert.That(root.GetProperty("stripped").GetBoolean(), Is.True);
        Assert.That(root.GetProperty("rig_hint").GetProperty("rig_id").GetString(), Is.EqualTo("rig_9c1d4e7a02bb"));
        var h = root.GetProperty("header");
        Assert.That(h.GetProperty("FILTER").GetString(), Is.EqualTo("Ha"));
        Assert.That(h.GetProperty("RA").GetDouble(), Is.EqualTo(83.8));
        Assert.That(h.GetProperty("DEC").GetString(), Is.EqualTo("-05 23 28"));
        Assert.That(h.TryGetProperty("GAIN", out _), Is.False, "nulls are omitted");
    }

    [Test]
    public void A_resume_response_deserialises_with_outstanding_parts_and_completed_etags()
    {
        const string body = """
        {"ok":true,"frame_id":"frm_01hzx8k3m9q2x5v8d1hn4t6wzc","status":"uploading","resumed":true,
         "frame_type":"light","filter":"HA","rig_id":"rig_9c1d4e7a02bb",
         "resolution":{"raw":"Ha 3nm","canonical":"HA","confidence":"pattern","source":"pattern","bandwidth_nm":3,"vendor":null},
         "upload":{"type":"multipart","upload_id":"U1","part_size":16777216,"part_count":3,
           "part_urls":[{"part_number":3,"url":"https://s3.example/p3","size":1000}],
           "completed_parts":[{"part_number":1,"etag":"\"e1\""},{"part_number":2,"etag":"\"e2\""}],
           "expires_at":"2026-10-13T12:00:00.000Z","future_field":42},
         "future_top_level":true}
        """;
        var r = ApiJson.Deserialize<FrameInitResponse>(body)!;
        Assert.That(r.Resumed, Is.True);
        Assert.That(r.Upload.PartUrls.Select(p => p.PartNumber), Is.EqualTo(new[] { 3 }));
        Assert.That(r.Upload.CompletedParts.Select(p => p.Etag), Is.EqualTo(new[] { "\"e1\"", "\"e2\"" }));
        Assert.That(r.Resolution!.BandwidthNm, Is.EqualTo(3));
        Assert.That(r.Upload.ExpiresAt.Year, Is.EqualTo(2026));
    }
}

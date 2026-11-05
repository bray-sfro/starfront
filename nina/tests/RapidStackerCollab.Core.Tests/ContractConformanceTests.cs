// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text.Json.Nodes;
using NUnit.Framework;
using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;
using RapidStackerCollab.Core.Tests.Contract;

namespace RapidStackerCollab.Core.Tests;

/// <summary>
/// The real <see cref="ApiClient"/>, driven through every method of <see cref="IApiClient"/>, held against
/// docs/api/openapi.yaml: each request it sends is a route the contract has, with only declared query parameters, and a
/// body that validates against the operation's request schema (so required fields are sent, no undeclared field is, and
/// formats and ranges hold). Companion to scripts/check-contract.mjs, which reads the same files from the other side.
/// </summary>
public class ContractConformanceTests
{
    private sealed record Sent(string Method, string Path, string[] Query, string? Body);

    private sealed class Recorder : HttpMessageHandler
    {
        public List<Sent> Requests { get; } = new();

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var uri = request.RequestUri!;
            var path = uri.AbsolutePath;
            var i = path.IndexOf("/api/", StringComparison.Ordinal);
            path = "/" + path[(i + 5)..];
            var query = uri.Query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries).Select(q => Uri.UnescapeDataString(q.Split('=')[0])).ToArray();
            var body = request.Content is null ? null : request.Content.ReadAsStringAsync(cancellationToken).GetAwaiter().GetResult();
            Requests.Add(new Sent(request.Method.Method, path, query, body));
            return Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.BadRequest)
            {
                Content = new StringContent("{\"ok\":false,\"code\":\"invalid_request\",\"message\":\"recorded\"}", System.Text.Encoding.UTF8, "application/json"),
            });
        }
    }

    private static async Task<List<Sent>> DriveEveryMethod()
    {
        var recorder = new Recorder();
        IApiClient c = new ApiClient(new HttpClient(recorder), new InMemoryTokenStore("rscu_" + new string('0', 64)),
            new ApiClientOptions { BaseUri = new Uri("https://collab.astro.ekeservices.com/api/") });
        var mapping = new[] { new ContributorMappingEntry { Raw = "Slot 4", Canonical = "HA" } };
        var header = new InitHeader { Filter = "Ha", ImageTyp = "Light Frame", ExpTime = 300, XBinning = 1, YBinning = 1, Ra = 85.25, Dec = -2.45, Naxis1 = 6248, Naxis2 = 4176, BitPix = 16 };
        var calls = new Func<Task>[]
        {
            () => c.DeviceStartAsync(new DeviceStartRequest { ClientName = "N.I.N.A. on OBSERVATORY-PC", Scopes = ApiScopes.PluginDefaults }),
            () => c.DevicePollAsync("rscd_" + new string('a', 64)),
            () => c.ListRigsAsync(),
            () => c.UpsertRigAsync(new RigUpsertRequest { Camera = "ASI2600MM Pro", Telescope = "Redcat 51", FocalLengthMm = 250, PixelSizeUm = 3.76, Binning = 1, WidthPx = 6248, HeightPx = 4176, Name = "Backyard" }),
            () => c.PatchRigAsync("rig_9c1d4e7a02bb", new RigPatchRequest { Name = "Backyard", Confirmed = true }),
            () => c.NormalizeFiltersAsync(new[] { "Ha", "Slot 4" }, "m31-lrgb", true),
            () => c.NormalizeFiltersAsync(new[] { "Ha" }),
            () => c.ListProjectsAsync("mine", 10, "cursor"),
            () => c.ResolveJoinCodeAsync("H4RT-92BK-WNPC"),
            () => c.GetProjectAsync("m31-lrgb"),
            () => c.JoinProjectAsync("prj_7k3m9q2x5v8d1hn4t6wz", new JoinRequest { Code = "H4RT-92BK-WNPC", DisplayName = "Demo Imager", Country = "DE", AcceptedTermsVersion = "2026-10-13-draft", AllowOwnerRawDownload = false, FilterMapping = mapping }),
            () => c.GetMembershipAsync("m31-lrgb"),
            () => c.UpdateMembershipAsync("m31-lrgb", new UpdateMembershipRequest { DisplayName = "Demo", AllowOwnerRawDownload = true, FilterMapping = mapping }),
            () => c.GetProjectStatusAsync("m31-lrgb"),
            () => c.InitFrameAsync("m31-lrgb", new FrameInitRequest { Sha256 = new string('a', 64), Size = 40_000_000, Filename = "Light_Ha_300s.fits", Header = header, RigHint = new RigHint { RigId = "rig_9c1d4e7a02bb" }, Stripped = true }),
            () => c.CompleteFrameAsync("m31-lrgb", "frm_01hzx8k3m9q2x5v8d1hn4t6wzc", new[] { new CompletedPart { PartNumber = 1, Etag = "\"abc\"" } }),
            () => c.ListFramesAsync("m31-lrgb", new FrameListQuery { Mine = true, Status = "accepted", Filter = "HA", Since = DateTimeOffset.Parse("2026-10-01T00:00:00Z"), Limit = 20, Cursor = "c" }),
            () => c.GetFrameAsync("m31-lrgb", "frm_01hzx8k3m9q2x5v8d1hn4t6wzc"),
            () => c.WithdrawFrameAsync("m31-lrgb", "frm_01hzx8k3m9q2x5v8d1hn4t6wzc"),
            () => c.RevokeOwnTokenAsync(),
            () => c.GetMosaicAsync("m31-lrgb"),
            () => c.PutClaimsAsync("m31-lrgb", new[] { "r1c1", "r2c3" }),
            () => c.GetLibraryAsync("rig_9c1d4e7a02bb", 50, "cursor"),
            () => c.InitLibraryFrameAsync("rig_9c1d4e7a02bb", new LibraryFrameInitRequest
            {
                Sha256 = new string('b', 64), Size = 40_000_000, Filename = "Dark_120s.fits", Stripped = true,
                Header = new InitHeader { ImageTyp = "Dark Frame", ExpTime = 120, Gain = 100, Offset = 50, XBinning = 1, YBinning = 1, Instrume = "ZWO ASI2600MM Pro", SetTemp = -10, Naxis1 = 6248, Naxis2 = 4176, BitPix = 16 },
            }),
            () => c.CompleteLibraryFrameAsync("rig_9c1d4e7a02bb", "frm_01hzx8k3m9q2x5v8d1hn4t6wzc", new[] { new CompletedPart { PartNumber = 1, Etag = "\"abc\"" } }),
            () => c.WithdrawLibraryFrameAsync("rig_9c1d4e7a02bb", "frm_01hzx8k3m9q2x5v8d1hn4t6wzc"),
        };
        foreach (var call in calls)
        {
            try { await call(); } catch (ApiException) { /* the recorder answers 400 on purpose */ }
        }
        return recorder.Requests;
    }

    [Test]
    public async Task Every_request_the_client_can_send_is_a_contract_route_with_a_valid_body()
    {
        var contract = ContractDocument.Load();
        var sent = await DriveEveryMethod();
        var problems = new List<string>();
        var seen = new HashSet<string>();
        foreach (var r in sent)
        {
            var match = contract.Match(r.Method, r.Path);
            if (match is null) { problems.Add($"{r.Method} {r.Path} is not a route of the contract"); continue; }
            var (path, op) = match.Value;
            seen.Add(r.Method + " " + path);
            var declared = contract.Parameters(op).Where(p => p.In == "query").ToList();
            foreach (var q in r.Query.Where(q => declared.All(d => d.Name != q))) problems.Add($"{r.Method} {path}: query '{q}' is not declared");
            foreach (var d in declared.Where(d => d.Required && !r.Query.Contains(d.Name))) problems.Add($"{r.Method} {path}: required query '{d.Name}' is missing");
            var schema = contract.RequestSchema(op);
            if (schema is not null)
            {
                var body = r.Body is null ? null : JsonNode.Parse(r.Body);
                foreach (var e in contract.Validate(schema, body)) problems.Add($"{r.Method} {path} body: {e}");
            }
            else if (!string.IsNullOrEmpty(r.Body))
            {
                problems.Add($"{r.Method} {path} sends a body ({r.Body}), but the operation declares none; the server ignores it and a client sends none");
            }
        }
        Assert.That(problems, Is.Empty, string.Join(Environment.NewLine, problems));
        Assert.That(seen.Count, Is.EqualTo(25), "every method of IApiClient reached a distinct operation (NormalizeFilters is called twice, with and without a project; GetPanelsDocumentAsync reads a static file and is no operation)");
    }
}

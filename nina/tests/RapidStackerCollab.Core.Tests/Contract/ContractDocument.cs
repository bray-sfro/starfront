// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using RapidStackerCollab.Core.Tests.Support;
using YamlDotNet.Serialization;

namespace RapidStackerCollab.Core.Tests.Contract;

/// <summary>
/// docs/api/openapi.yaml as a JSON tree, with a small validator for exactly the JSON-Schema keywords the contract uses
/// (the same subset the backend's validator implements). The tests skip, with a reason, when the file is not reachable:
/// the plugin is meant to be mirrored to a repository of its own, where the contract is not.
/// </summary>
internal sealed class ContractDocument
{
    public JsonObject Root { get; }

    private ContractDocument(JsonObject root) => Root = root;

    private static ContractDocument? cached;

    /// <summary>The parsed contract, or an <c>Assert.Ignore</c> when the file is not in the checkout.</summary>
    public static ContractDocument Load()
    {
        if (cached is not null) return cached;
        var text = File.ReadAllText(RepoPaths.Contract("openapi.yaml"));
        var obj = new DeserializerBuilder().WithAttemptingUnquotedStringTypeDeserialization().Build().Deserialize<object>(text);
        var json = new SerializerBuilder().JsonCompatible().Build().Serialize(obj);
        return cached = new ContractDocument(JsonNode.Parse(json)!.AsObject());
    }

    public JsonObject Schemas => Root["components"]!["schemas"]!.AsObject();

    /// <summary>All (method, path) operations, with the node of each.</summary>
    public IEnumerable<(string Method, string Path, JsonObject Op)> Operations()
    {
        foreach (var (path, item) in Root["paths"]!.AsObject())
            foreach (var m in new[] { "get", "post", "put", "patch", "delete" })
                if (item!.AsObject()[m] is JsonObject op && !path.StartsWith("/public/", StringComparison.Ordinal))
                    yield return (m.ToUpperInvariant(), path, op);
    }

    /// <summary>The operation a concrete request path ("/projects/m31/frames", relative to /api) belongs to.</summary>
    public (string Path, JsonObject Op)? Match(string method, string concretePath)
    {
        var parts = concretePath.Split('/');
        foreach (var (m, path, op) in Operations())
        {
            if (m != method.ToUpperInvariant()) continue;
            var t = path.Split('/');
            if (t.Length != parts.Length) continue;
            var ok = true;
            for (var i = 0; i < t.Length && ok; i++) ok = (t[i].StartsWith('{') && t[i].EndsWith('}')) || t[i] == parts[i];
            if (ok) return (path, op);
        }
        return null;
    }

    public JsonNode Deref(JsonNode node)
    {
        for (var guard = 0; node is JsonObject o && o["$ref"] is JsonNode r; guard++)
        {
            var m = Regex.Match(r.GetValue<string>(), "^#/components/(schemas|parameters)/(.+)$");
            if (!m.Success || guard > 20) throw new InvalidOperationException("unresolvable $ref " + r);
            node = Root["components"]![m.Groups[1].Value]![m.Groups[2].Value]!;
        }
        return node;
    }

    /// <summary>Declared parameters of an operation, dereferenced: (name, in).</summary>
    public IEnumerable<(string Name, string In, bool Required)> Parameters(JsonObject op)
    {
        if (op["parameters"] is not JsonArray arr) yield break;
        foreach (var p in arr)
        {
            var d = Deref(p!).AsObject();
            yield return (d["name"]!.GetValue<string>(), d["in"]!.GetValue<string>(), d["required"]?.GetValue<bool>() ?? false);
        }
    }

    public JsonNode? RequestSchema(JsonObject op) => op["requestBody"]?["content"]?["application/json"]?["schema"];

    // ---- validation --------------------------------------------------------------------------------------------

    public List<string> Validate(JsonNode schema, JsonNode? value)
    {
        var errors = new List<string>();
        Check(schema, value, "$", errors);
        return errors;
    }

    private static string KindOf(JsonNode? v) => v switch
    {
        null => "null",
        JsonObject => "object",
        JsonArray => "array",
        JsonValue jv => jv.GetValueKind() switch
        {
            System.Text.Json.JsonValueKind.String => "string",
            System.Text.Json.JsonValueKind.True or System.Text.Json.JsonValueKind.False => "boolean",
            System.Text.Json.JsonValueKind.Number => Math.Abs(jv.GetValue<double>() % 1) < double.Epsilon ? "integer" : "number",
            _ => "null",
        },
        _ => "null",
    };

    private void Check(JsonNode schemaIn, JsonNode? value, string path, List<string> errors)
    {
        var schema = Deref(schemaIn).AsObject();
        if (schema["oneOf"] is JsonArray alts)
        {
            var passing = alts.Count(a => Validate(a!, value).Count == 0);
            if (passing != 1) errors.Add($"{path}: matches {passing} of {alts.Count} oneOf alternatives");
            return;
        }
        if (schema["type"] is JsonNode t)
        {
            var wanted = t is JsonArray ta ? ta.Select(x => x!.GetValue<string>()).ToArray() : new[] { t.GetValue<string>() };
            var got = KindOf(value);
            if (!wanted.Any(w => w == got || (w == "number" && got == "integer"))) { errors.Add($"{path}: expected {string.Join("|", wanted)}, got {got}"); return; }
        }
        if (schema["const"] is JsonNode c && value?.ToJsonString() != c.ToJsonString()) errors.Add($"{path}: must be {c.ToJsonString()}");
        if (schema["enum"] is JsonArray en && !en.Any(e => e?.ToJsonString() == value?.ToJsonString())) errors.Add($"{path}: {value?.ToJsonString()} is not one of {en.ToJsonString()}");
        if (value is JsonValue jv)
        {
            var kind = KindOf(value);
            if (kind == "string")
            {
                var s = jv.GetValue<string>();
                if (schema["pattern"] is JsonNode pat && !Regex.IsMatch(s, pat.GetValue<string>())) errors.Add($"{path}: {s} does not match {pat.GetValue<string>()}");
                if (schema["minLength"] is JsonNode min && s.Length < min.GetValue<int>()) errors.Add($"{path}: shorter than {min}");
                if (schema["maxLength"] is JsonNode max && s.Length > max.GetValue<int>()) errors.Add($"{path}: longer than {max}");
                if (schema["format"] is JsonNode fmt)
                {
                    var okFormat = fmt.GetValue<string>() switch
                    {
                        "date" => System.DateTime.TryParseExact(s, "yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.None, out _),
                        "date-time" => System.DateTimeOffset.TryParse(s, System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.AssumeUniversal, out _),
                        "uuid" => Guid.TryParse(s, out _),
                        "uri" => Uri.TryCreate(s, UriKind.Absolute, out _),
                        _ => true,
                    };
                    if (!okFormat) errors.Add($"{path}: '{s}' is not a valid {fmt}");
                }
            }
            else if (kind is "number" or "integer")
            {
                var n = jv.GetValue<double>();
                if (schema["minimum"] is JsonNode min && n < min.GetValue<double>()) errors.Add($"{path}: below {min}");
                if (schema["maximum"] is JsonNode max && n > max.GetValue<double>()) errors.Add($"{path}: above {max}");
                if (schema["exclusiveMinimum"] is JsonNode emin && n <= emin.GetValue<double>()) errors.Add($"{path}: not above {emin}");
                if (schema["exclusiveMaximum"] is JsonNode emax && n >= emax.GetValue<double>()) errors.Add($"{path}: not below {emax}");
            }
        }
        if (value is JsonObject obj)
        {
            var props = schema["properties"] as JsonObject;
            if (schema["required"] is JsonArray req)
                foreach (var r in req) if (!obj.ContainsKey(r!.GetValue<string>())) errors.Add($"{path}: required field '{r}' is missing");
            foreach (var (k, v) in obj)
            {
                if (props is not null && props.TryGetPropertyValue(k, out var sub)) Check(sub!, v, path + "." + k, errors);
                else if (schema["additionalProperties"] is JsonValue ap && !ap.GetValue<bool>()) errors.Add($"{path}: '{k}' is not a field of this object");
            }
        }
        if (value is JsonArray arr)
        {
            if (schema["items"] is JsonNode items) for (var i = 0; i < arr.Count; i++) Check(items, arr[i], $"{path}[{i}]", errors);
            if (schema["minItems"] is JsonNode mi && arr.Count < mi.GetValue<int>()) errors.Add($"{path}: fewer than {mi} items");
            if (schema["maxItems"] is JsonNode ma && arr.Count > ma.GetValue<int>()) errors.Add($"{path}: more than {ma} items");
            if (schema["uniqueItems"] is JsonValue u && u.GetValue<bool>() && arr.Select(a => a?.ToJsonString()).Distinct().Count() != arr.Count) errors.Add($"{path}: items are not unique");
        }
    }
}

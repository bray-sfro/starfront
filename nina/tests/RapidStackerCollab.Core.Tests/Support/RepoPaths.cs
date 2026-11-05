// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Tests.Support;

/// <summary>Finds files of the surrounding repository (the API contract) from the test's bin folder.</summary>
internal static class RepoPaths
{
    /// <summary>Walks upwards from the test assembly looking for <paramref name="relative"/>; null when absent.</summary>
    public static string? Find(string relative)
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, relative);
            if (File.Exists(candidate)) return candidate;
            dir = dir.Parent;
        }
        return null;
    }

    /// <summary>The contract file, or an <c>Assert.Ignore</c> when the plugin is built outside the repository.</summary>
    public static string Contract(string name)
    {
        var path = Find(Path.Combine("docs", "api", name));
        if (path is null) NUnit.Framework.Assert.Ignore($"docs/api/{name} is not reachable from {AppContext.BaseDirectory}");
        return path!;
    }

    /// <summary>
    /// <c>scripts/contract-examples.json</c> (the positive and negative identifier examples and the grid corners shared by
    /// every component's tests), or an <c>Assert.Ignore</c> when the plugin is built outside the repository.
    /// </summary>
    public static System.Text.Json.JsonDocument Examples()
    {
        var path = Find(Path.Combine("scripts", "contract-examples.json"));
        if (path is null) NUnit.Framework.Assert.Ignore($"scripts/contract-examples.json is not reachable from {AppContext.BaseDirectory}");
        return System.Text.Json.JsonDocument.Parse(File.ReadAllText(path!));
    }
}

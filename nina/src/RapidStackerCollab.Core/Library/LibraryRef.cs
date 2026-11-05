// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Library;

/// <summary>
/// How the upload queue addresses a rig's calibration library. A queue row targets a project by its <c>project_ref</c>; a library
/// row uses <c>lib:{rig_id}</c> instead, so the <c>UNIQUE (project_ref, path)</c> rule, per-ref pausing (a library quota refusal
/// pauses only the library) and the "which target" decision in the upload engine all keep working without a second table.
/// </summary>
public static class LibraryRef
{
    public const string Prefix = "lib:";

    public static string For(string rigId) => Prefix + rigId;

    public static bool IsLibrary(string? projectRef) => projectRef is not null && projectRef.StartsWith(Prefix, StringComparison.Ordinal);

    /// <summary>The rig id of a library ref, or null for a project.</summary>
    public static string? RigOf(string? projectRef) => IsLibrary(projectRef) && projectRef!.Length > Prefix.Length ? projectRef[Prefix.Length..] : null;
}

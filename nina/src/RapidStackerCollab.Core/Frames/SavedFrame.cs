// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

namespace RapidStackerCollab.Core.Frames;

/// <summary>
/// What the N.I.N.A. shell knows about a frame the moment it is on disk. Header details (target, pointing,
/// IMAGETYP and the rest) are read from the file itself by <c>FitsHeaderReader</c>, so this carries only what
/// <c>ImageSavedEventArgs</c> offers cheaply.
/// </summary>
/// <param name="Path">Absolute path of the saved file at its final location.</param>
/// <param name="Filter">Filter wheel name N.I.N.A. recorded, or null/empty with no wheel.</param>
/// <param name="DurationSeconds">Exposure in seconds.</param>
/// <param name="IsBayered">True for a one-shot-colour sensor.</param>
/// <param name="FileType">N.I.N.A.'s file type name, for example <c>FITS</c> or <c>XISF</c>.</param>
public sealed record SavedFrame(
    string Path,
    string? Filter,
    double DurationSeconds,
    bool IsBayered,
    string? FileType);

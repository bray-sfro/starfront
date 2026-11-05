// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using System.Globalization;
using System.Text;
using RapidStackerCollab.Core.Api;

namespace RapidStackerCollab.Core.Panels;

/// <summary>
/// The pointings of mosaic panels as CSV, to paste into N.I.N.A.'s Framing Assistant / target import.
/// Columns (header row first, comma separated, CRLF line ends, fields quoted only when they need it):
/// <list type="bullet">
/// <item><c>id</c>: the panel id, <c>r{row}c{col}</c> (1-based, row 1 at the top).</item>
/// <item><c>name</c>: "&lt;target name&gt; &lt;panel id&gt;", the target name a sequence would use.</item>
/// <item><c>ra_deg</c>, <c>dec_deg</c>: the panel centre in decimal degrees (J2000), six decimals.</item>
/// <item><c>pa_deg</c>: the position angle (rotation) for the camera, degrees east of north, two decimals.</item>
/// <item><c>ra</c>, <c>dec</c>: the same centre as <c>hh:mm:ss.s</c> and <c>+dd:mm:ss</c>, the form N.I.N.A.'s coordinate fields take.</item>
/// </list>
/// </summary>
public static class PanelPointings
{
    public const string Header = "id,name,ra_deg,dec_deg,pa_deg,ra,dec";

    public static string Csv(IEnumerable<MosaicPanel> panels, string? targetName)
    {
        var sb = new StringBuilder(Header).Append("\r\n");
        foreach (var p in panels.OrderBy(p => p.PanelId, Comparer<string>.Create(YourPanels.ComparePanelIds)))
        {
            var name = string.IsNullOrWhiteSpace(targetName) ? p.PanelId : targetName.Trim() + " " + p.PanelId;
            sb.Append(Field(p.PanelId)).Append(',')
              .Append(Field(name)).Append(',')
              .Append(p.RaDeg.ToString("0.000000", CultureInfo.InvariantCulture)).Append(',')
              .Append(p.DecDeg.ToString("0.000000", CultureInfo.InvariantCulture)).Append(',')
              .Append(p.PaDeg.ToString("0.00", CultureInfo.InvariantCulture)).Append(',')
              .Append(Sexagesimal.Ra(p.RaDeg, 1, colon: true)).Append(',')
              .Append(Sexagesimal.Dec(p.DecDeg, 0, colon: true)).Append("\r\n");
        }
        return sb.ToString();
    }

    private static string Field(string v) => v.IndexOfAny(new[] { ',', '"', '\r', '\n' }) < 0 ? v : "\"" + v.Replace("\"", "\"\"") + "\"";
}

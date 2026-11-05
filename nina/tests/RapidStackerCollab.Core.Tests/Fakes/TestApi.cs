// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

using RapidStackerCollab.Core.Api;
using RapidStackerCollab.Core.Auth;

namespace RapidStackerCollab.Core.Tests.Fakes;

internal static class TestApi
{
    public static ApiClient Client(FakeCollabServer server, ITokenStore? tokens = null, HttpClient? http = null) =>
        new(http ?? new HttpClient(), tokens ?? new InMemoryTokenStore(FakeCollabServer.Token), new ApiClientOptions { BaseUri = server.ApiBase });

    public static FrameInitRequest InitRequest(string sha, long size, string filename = "light.fits") => new()
    {
        Sha256 = sha,
        Size = size,
        Filename = filename,
        Header = new InitHeader { Filter = "Ha", ImageTyp = "LIGHT", ExpTime = 300 },
    };
}

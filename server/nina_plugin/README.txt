The N.I.N.A. plugin releases this server offers.

The server serves every *.manifest.json here as its plugin feed at
/nina/plugins/manifests and the zips at /nina/download/<name>. N.I.N.A. users
add  https://<host>/nina  under Options > General > Plugin Repositories and
the plugin appears in their plugin manager.

Build a release with  nina/build/Build-Release.ps1  (or the PackagePlugin
target), then copy the zip and the manifest from nina/artifacts/release here
and deploy. The manifest's checksum is of the zip's bytes: never replace one
without the other.

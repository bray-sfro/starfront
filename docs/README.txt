Documents, each kept as the HTML it is written in and the PDF printed from it.

To print one again after editing the HTML (headless Edge, no header or footer):
  & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --no-pdf-header-footer "--print-to-pdf=<name>.pdf" "file:///<full path>/<name>.html"

Headless Chrome on macOS prints the same PDF, with the same switches (run it from docs/; spaces in the file URL as %20):
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --no-pdf-header-footer "--print-to-pdf=$PWD/<name>.pdf" "file://$PWD/<name>.html"

Starfront Collaboration for NINA   the plugin guide; a copy sits in server\nina_plugin and is served at /nina/guide
How Starfront deals exposures      how the collaboration server deals each night
Starfront lab on another local network   setting up the home lab (server, hub, workers) on another network, Terraform included

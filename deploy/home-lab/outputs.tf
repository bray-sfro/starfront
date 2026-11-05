output "machines" {
  description = "What discover.py found (each name's address now; informational only)."
  value       = data.external.discover.result
}

output "server_url" {
  description = "The Starfront server: N.I.N.A.'s ServerUrl and the desktop's collab.serverUrl."
  value       = local.server_url
}

output "hub_url" {
  value = local.hub_url
}

output "server_ssh" {
  description = "How this Mac reaches the server host (scripts/lab.sh uses it)."
  value       = local.server_ssh
}

output "pages" {
  description = "The stacking pages (the project page is printed by `scripts/lab.sh night`)."
  value       = "${local.server_url}/stack/dashboard"
}

output "next" {
  description = "What to run next."
  value = join("\n", [
    "scripts/lab.sh status        # the hub: workers, catalogue, Starfront link",
    "scripts/lab.sh night         # the 'Lab night' collaboration, its project page and grant",
    var.catalogue_mode == "synthetic"
    ? "scripts/lab.sh smoke relay   # synthetic frames through the server, then: scripts/lab.sh smoke hub"
    : "real catalogue mode: frames from N.I.N.A. on the Windows PC, or scripts/lab.sh upload <folder>",
  ])
}

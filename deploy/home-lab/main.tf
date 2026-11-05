locals {
  starfront = abspath("${path.module}/../..")
  scripts   = abspath("${path.module}/scripts")
  bin       = abspath("${path.module}/bin")
  key       = pathexpand(var.ssh_key)

  # Every connection and trigger uses the names, never the addresses discover
  # resolves: a machine on both wired and Wi-Fi answers with both, in either
  # order, and a trigger keyed on one would replace the server on every apply.
  server_ssh = "${var.server_user}@${var.server_host}"

  server_url = "http://${var.server_host}:${var.server_port}"
  hub_url    = "http://${var.server_host}:${var.hub_port}"

  # Each OS reaches the one catalogue folder its own way; enrolment hands every
  # worker the entry for its OS (RSC hub routes/admin.rs). macOS mounts the
  # share in Finder (Go > Connect to Server), Linux by bootstrap-linux.sh.
  xpsd = {
    linux   = "/mnt/xpsd"
    macos   = "/Volumes/${var.nas_share}"
    windows = "\\\\${split(".", var.nas_host)[0]}\\${var.nas_share}"
  }

  binary_os = { macos = "macos-arm64", linux = "linux-x64", windows = "windows-x64" }
  worker_bin = {
    for id, w in var.workers :
    id => "${local.bin}/${local.binary_os[w.os]}/${w.os == "windows" ? "collab-worker.exe" : "collab-worker"}"
  }
  hub_bin = "${local.bin}/linux-x64/collab-hub"

  wants_windows = anytrue([for w in values(var.workers) : w.os == "windows"])
}

# What is on the network: each name resolved through the router's DNS, and SSH
# open where Terraform will need it. Fails, with what is missing, before
# anything is touched.
data "external" "discover" {
  program = ["python3", "${local.scripts}/discover.py"]
  query = {
    server  = var.server_host
    windows = local.wants_windows ? var.windows_host : ""
    nas     = var.catalogue_mode == "real" ? var.nas_host : ""
    ssh     = join(",", compact(["server", local.wants_windows ? "windows" : ""]))
  }
}

# The server's sources, hashed, so a change on this Mac redeploys them.
data "external" "source" {
  program = ["bash", "${local.scripts}/source-hash.sh", local.starfront]
}

# ------------------------------------------------------------------ server
resource "terraform_data" "server" {
  triggers_replace = {
    source = data.external.source.result.hash
    host   = var.server_host
    port   = var.server_port
    public = var.server_host
  }

  input = {
    ssh     = local.server_ssh
    key     = local.key
    wipe    = var.wipe_data
    scripts = local.scripts
  }

  provisioner "local-exec" {
    command = "bash '${local.scripts}/server-up.sh'"
    environment = {
      LAB_SSH         = local.server_ssh
      LAB_KEY         = local.key
      LAB_SRC         = local.starfront
      LAB_PUBLIC_HOST = var.server_host
      LAB_PORT        = var.server_port
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash '${self.input.scripts}/server-down.sh'"
    environment = {
      LAB_SSH  = self.input.ssh
      LAB_KEY  = self.input.key
      LAB_WIPE = self.input.wipe ? "1" : "0"
    }
  }
}

# --------------------------------------------------------------------- hub
resource "terraform_data" "hub" {
  # After the server (it registers with it), but not replaced when only the
  # server's code changes: a restart of the server is enough for the hub.
  depends_on = [terraform_data.server]

  triggers_replace = {
    host    = var.server_host
    binary  = fileexists(local.hub_bin) ? filesha256(local.hub_bin) : "missing"
    mode    = var.catalogue_mode
    xpsd    = jsonencode(local.xpsd)
    hub_url = local.hub_url
    region  = var.lab_region
  }

  input = {
    ssh     = local.server_ssh
    key     = local.key
    wipe    = var.wipe_data
    scripts = local.scripts
  }

  lifecycle {
    precondition {
      condition     = fileexists(local.hub_bin)
      error_message = "No ${local.hub_bin}: run scripts/fetch-binaries.sh (--from-run RUN_ID or --build-on user@host) first."
    }
  }

  provisioner "local-exec" {
    command = "bash '${local.scripts}/hub-up.sh'"
    environment = {
      LAB_SSH          = local.server_ssh
      LAB_KEY          = local.key
      LAB_BIN          = local.hub_bin
      LAB_PORT         = var.server_port
      LAB_HUB_PORT     = var.hub_port
      LAB_HUB_URL      = local.hub_url
      LAB_MODE         = var.catalogue_mode
      LAB_XPSD_LINUX   = local.xpsd.linux
      LAB_XPSD_MACOS   = local.xpsd.macos
      LAB_XPSD_WINDOWS = local.xpsd.windows
      LAB_REGION       = var.lab_region
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash '${self.input.scripts}/hub-down.sh'"
    environment = {
      LAB_SSH  = self.input.ssh
      LAB_KEY  = self.input.key
      LAB_WIPE = self.input.wipe ? "1" : "0"
    }
  }
}

# ----------------------------------------------------------------- workers
resource "terraform_data" "worker" {
  for_each   = var.workers
  depends_on = [terraform_data.hub]

  # A worker keeps its token across re-installs, so only what it runs with
  # matters here; a new hub registration does not need a new enrolment.
  triggers_replace = {
    binary = fileexists(local.worker_bin[each.key]) ? filesha256(local.worker_bin[each.key]) : "missing"
    mode   = var.catalogue_mode
    worker = jsonencode(each.value)
    xpsd   = local.xpsd[each.value.os]
    hub    = local.hub_url
  }

  input = {
    id      = each.key
    os      = each.value.os
    target  = each.value.os == "linux" ? local.server_ssh : each.value.os == "windows" ? "${var.windows_user}@${var.windows_host}" : ""
    key     = local.key
    scripts = local.scripts
  }

  lifecycle {
    precondition {
      condition     = fileexists(local.worker_bin[each.key])
      error_message = "No ${local.worker_bin[each.key]}: run scripts/fetch-binaries.sh first (--mac for this Mac, --from-run RUN_ID for Windows and Linux)."
    }
    precondition {
      condition     = each.value.os != "windows" || var.windows_user != ""
      error_message = "A Windows worker needs windows_user in terraform.tfvars."
    }
  }

  provisioner "local-exec" {
    command = "bash '${local.scripts}/worker-up.sh'"
    environment = {
      LAB_ID         = each.key
      LAB_OS         = each.value.os
      LAB_TARGET     = self.input.target
      LAB_KEY        = local.key
      LAB_SERVER_SSH = local.server_ssh
      LAB_HUB_URL    = local.hub_url
      LAB_BIN        = local.worker_bin[each.key]
      LAB_SCRATCH_GB = each.value.scratch_gb
      LAB_RAM_GB     = each.value.ram_gb
      LAB_MODE       = var.catalogue_mode
      LAB_XPSD       = local.xpsd[each.value.os]
    }
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash '${self.input.scripts}/worker-down.sh'"
    environment = {
      LAB_ID     = self.input.id
      LAB_OS     = self.input.os
      LAB_TARGET = self.input.target
      LAB_KEY    = self.input.key
    }
  }
}

variable "server_host" {
  description = "The Linux machine that runs the Starfront server and the hub (a name the router's DNS answers)."
  type        = string
  default     = "omarchy.lan"
}

variable "server_user" {
  description = "The SSH user on the server host."
  type        = string
}

variable "windows_host" {
  description = "The Windows PC that runs N.I.N.A. and a worker; empty for no Windows worker."
  type        = string
  default     = "Eds9950X.lan"
}

variable "windows_user" {
  description = "The SSH user on the Windows PC (the account N.I.N.A. runs under)."
  type        = string
  default     = ""
}

variable "nas_host" {
  description = "The NAS that shares the XPSD catalogue folder over SMB."
  type        = string
  default     = "DXP8800PLUS-76B2.lan"
}

variable "nas_share" {
  description = "The SMB share holding the 16 gdr3-*.xpsd files."
  type        = string
  default     = "XPSD"
}

variable "ssh_key" {
  description = "The private key this Mac uses for the server host and the Windows PC."
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "catalogue_mode" {
  description = "\"synthetic\": workers use the synthetic sky (for lab_setup.py smoke; no NAS needed). \"real\": the hub reads the XPSD share and serves real catalogues."
  type        = string
  default     = "synthetic"

  validation {
    condition     = contains(["synthetic", "real"], var.catalogue_mode)
    error_message = "catalogue_mode is \"synthetic\" or \"real\"."
  }
}

variable "server_port" {
  type    = number
  default = 8800
}

variable "hub_port" {
  type    = number
  default = 8790
}

variable "lab_region" {
  description = "The \"Lab night\" collaboration's sky region, RA,DEC[,WIDTH,HEIGHT] in degrees; set it to the night's target, since real frames solve only against that cone."
  type        = string
  default     = "84,0,1,1"
}

variable "workers" {
  description = "One worker per entry. os is macos (this Mac, run locally), linux (the server host) or windows (the Windows PC)."
  type = map(object({
    os         = string
    scratch_gb = number
    ram_gb     = optional(number, 0)
  }))
  default = {
    mac-m3max = { os = "macos", scratch_gb = 40 }
    omarchy   = { os = "linux", scratch_gb = 100 }
    eds9950x  = { os = "windows", scratch_gb = 150 }
  }

  validation {
    condition     = alltrue([for w in values(var.workers) : contains(["macos", "linux", "windows"], w.os)])
    error_message = "A worker's os is macos, linux or windows."
  }
}

variable "wipe_data" {
  description = "On destroy, back up the hub then delete the lab's data on the server host (the database, frames and stacks). Off by default: destroy only stops and removes the services."
  type        = bool
  default     = false
}

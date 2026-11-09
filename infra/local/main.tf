# LOCAL infrastructure (validated; safe to apply: it only creates Docker objects on this Mac, $0).
# Mirrors the gateway service from docker-compose.yml so the same environment is reproducible from IaC.
#   cd infra/local && tofu init && tofu apply      # requires the image: make docker-build
#   tofu destroy                                   # removes the container, network and volume
terraform {
  required_version = ">= 1.6.0"
  required_providers {
    docker = { source = "kreuzwerker/docker", version = "~> 3.0" }
  }
}

provider "docker" {
  host = var.docker_host
}

variable "docker_host" {
  type        = string
  default     = "unix:///var/run/docker.sock"
  description = "Docker daemon socket (OrbStack/Docker Desktop)."
}

variable "image" {
  type    = string
  default = "spectral-canary-works:local"
}

variable "api_port" {
  type    = number
  default = 5140 # distinct from compose (5120) so both can coexist
}

resource "docker_network" "scw" {
  name = "scw-iac-local"
}

resource "docker_volume" "runs" {
  name = "scw-iac-runs"
}

resource "docker_container" "api" {
  name          = "scw-iac-api"
  image         = var.image
  restart       = "unless-stopped"
  read_only     = true
  user          = "10001:10001"
  must_run      = true
  security_opts = ["no-new-privileges:true"]
  capabilities { drop = ["ALL"] }
  tmpfs  = { "/tmp" = "rw,size=64m" }
  memory = 1024
  env    = ["SCW_ENABLE_CHAOS=0"]
  ports {
    internal = 5120
    external = var.api_port
    ip       = "127.0.0.1"
  }
  volumes {
    volume_name    = docker_volume.runs.name
    container_path = "/app/var/runs"
  }
  networks_advanced { name = docker_network.scw.name }
  healthcheck {
    test     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5120/readyz', timeout=2).status == 200 else 1)"]
    interval = "10s"
    timeout  = "3s"
    retries  = 3
  }
}

output "gateway_url" {
  value = "http://127.0.0.1:${var.api_port}"
}

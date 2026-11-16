#!/usr/bin/env bash
# install.sh — Prometheus + Grafana + GPU exporter on SPARK as user systemd units.
# Idempotent, no sudo. Run on SPARK from ~/deploy/AiStack/spark/monitoring:
#
#   ./install.sh            # download pinned versions, render config, (re)start units
#
# Binaries and data live in run/ (gitignored). Details: README.md.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
run="$here/run"
units="$HOME/.config/systemd/user"
# shellcheck source=versions.env
. "$here/versions.env"
[ -f "$here/.env" ] || { echo "missing .env (see .env.example)" >&2; exit 1; }
# shellcheck disable=SC1091
. "$here/.env"
: "${JODA_HOST:?set JODA_HOST in .env}" "${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD in .env}"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
mkdir -p "$run/dl" "$run/data/prometheus" "$run/data/grafana" "$run/logs" "$units"

# fetch <name> <url> <expected-sha256>: download once into run/dl, verify, extract into run/<name>
fetch() {
  local name="$1" url="$2" sha="$3" file="$run/dl/${2##*/}"
  if [ ! -f "$file" ]; then curl -fsSL -o "$file.part" "$url"; mv "$file.part" "$file"; fi
  echo "$sha  $file" | sha256sum -c --quiet - || { rm -f "$file"; echo "checksum mismatch: $url" >&2; exit 1; }
  local stamp="$run/$name/.version"
  if [ "$(cat "$stamp" 2>/dev/null)" != "${file##*/}" ]; then
    rm -rf "${run:?}/$name" && mkdir -p "$run/$name"
    tar -xzf "$file" -C "$run/$name" --strip-components="${4:-1}"
    echo "${file##*/}" >"$stamp"
    echo "   installed $name from ${file##*/}"
  fi
}

# sha_from <checksums-url> <filename>
sha_from() { curl -fsSL "$1" | awk -v f="$2" '$2==f || $2=="*"f {print $1}'; }

log "prometheus $PROMETHEUS_VERSION"
f="prometheus-$PROMETHEUS_VERSION.linux-arm64.tar.gz"
base="https://github.com/prometheus/prometheus/releases/download/v$PROMETHEUS_VERSION"
fetch prometheus "$base/$f" "$(sha_from "$base/sha256sums.txt" "$f")"

log "grafana $GRAFANA_VERSION"
url="https://dl.grafana.com/oss/release/grafana-$GRAFANA_VERSION.linux-arm64.tar.gz"
fetch grafana "$url" "$(curl -fsSL "$url.sha256" | awk '{print $1}')"

log "nvidia_gpu_exporter $GPU_EXPORTER_VERSION"
f="nvidia_gpu_exporter_${GPU_EXPORTER_VERSION}_linux_arm64.tar.gz"
base="https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v$GPU_EXPORTER_VERSION"
fetch gpu-exporter "$base/$f" "$(sha_from "$base/checksums.txt" "$f")" 0

log "config"
sed "s|@JODA_HOST@|$JODA_HOST|g" "$here/prometheus.yml.tmpl" >"$run/prometheus.yml.new"
"$run/prometheus/promtool" check config "$run/prometheus.yml.new" >/dev/null
mv "$run/prometheus.yml.new" "$run/prometheus.yml"
chmod 600 "$here/.env"

log "user units"
changed=0
for u in "$here"/systemd/*.service; do
  dst="$units/${u##*/}"
  sed "s|@MON@|$here|g" "$u" >"$dst.new"
  if cmp -s "$dst.new" "$dst"; then rm "$dst.new"; else mv "$dst.new" "$dst"; changed=1; echo "   $dst"; fi
done
systemctl --user daemon-reload
for s in mon-gpu-exporter mon-prometheus mon-grafana; do
  systemctl --user enable --quiet "$s"
  if [ "$changed" = 1 ]; then systemctl --user restart "$s"; else systemctl --user start "$s"; fi
done
# Prometheus picks up config changes without restart.
curl -fsS -X POST http://127.0.0.1:9090/-/reload >/dev/null 2>&1 || true

log "status"
systemctl --user --no-pager --lines=0 status mon-gpu-exporter mon-prometheus mon-grafana | grep -E '●|Active:'

#!/usr/bin/env bash
# mem-admit.sh <kontejner> <util> [rezerva_gib] — pustí start vLLM kontejneru,
# jen když se vejde i s rezervou pro zbytek stroje.
#
# vLLM si při startu předalokuje util × celková paměť (na GB10 unifikovaná, takže
# i paměť OS) a sám odmítne start jen pod tou hranicí — rezervu nehlídá nikdo.
# 25. 9. 2026 ruční `make up-director-night` s běžícím flux-schnell naběhl na
# nulovou rezervu a při zachytávání CUDA grafů zamrazil celý SPARK
# (rag-schedule.sh má vlastní memory_check, ruční start ho obešel).
#
# Rezerva default 12 GiB: 25. 9. běžel director s util 0.75 (91 GiB) při
# MemAvailable 96 GiB, tedy s rezervou ~5 GiB, a to nestačilo (flux-schnell
# nahoře, špička při capture CUDA grafů). V řádném nočním okně je dole i flux
# (~17 GiB), rezerva vychází ~20 GiB — 12 tedy pustí noc a zastaví 25. 9.
# Obejít: MEM_ADMIT_FORCE=1.
set -euo pipefail
name="$1" util="$2" headroom="${3:-${MEM_ADMIT_HEADROOM_GIB:-12}}"

if [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
  exit 0   # už běží — up -d nic nealokuje
fi
read -r total avail < <(awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {printf "%d %d\n", t/1048576, a/1048576}' /proc/meminfo)
need=$(awk -v u="$util" -v t="$total" -v h="$headroom" 'BEGIN {printf "%d\n", u * t + h + 0.999}')

if [ "$avail" -ge "$need" ]; then
  echo "mem-admit: $name — k dispozici ${avail} GiB, potřeba ${need} GiB (util ${util} × ${total} + ${headroom}) — ok"
  exit 0
fi
holders="$(docker ps --format '{{.Names}}' | grep -E 'director|flux|translate|qwen|agent|audio-|^tune-|swarm-|lab|^dev$|fallback|ocr' | paste -sd ' ' - || true)"
comfy="$(systemctl --user is-active comfyui 2>/dev/null || true)"
echo "mem-admit: $name se NEVEJDE — k dispozici ${avail} GiB, potřeba ${need} GiB (util ${util} × ${total} + rezerva ${headroom})" >&2
echo "mem-admit: paměť drží: ${holders:-žádné GPU kontejnery}; comfyui (user) ${comfy:-?}" >&2
if [ "${MEM_ADMIT_FORCE:-0}" = "1" ]; then
  echo "mem-admit: MEM_ADMIT_FORCE=1 — pouštím i tak" >&2
  exit 0
fi
exit 1

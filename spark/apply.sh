#!/usr/bin/env bash
# apply.sh — nasadí OOM ochranu SPARKu (spark/README.md). Idempotentní.
#
#   cd ~/deploy/AiStack/spark && sudo ./apply.sh            # všechno
#   sudo ./apply.sh sysctl watchdog protect swap guard      # jen vybrané kroky
#   sudo GUARD_DRY_RUN=1 ./apply.sh guard                   # guard jen hlásí, nezabíjí
#
# Nic nerestartuje: docker ani sshd zůstávají běžet, OOMScoreAdjust se běžícím
# procesům nastaví rovnou přes /proc. Zálohy přepsaných souborů jdou do
# /var/backups/spark-oom-guard/<čas>/.
set -euo pipefail

export SYSTEMD_PAGER=cat   # přes ssh -t by systemctl čekal na RETURN
[ "$(id -u)" -eq 0 ] || { echo "spusť přes sudo" >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
backup="/var/backups/spark-oom-guard/$(date +%Y%m%d-%H%M%S)"
SWAP_FILE="${SWAP_FILE:-/swap.img}"
SWAP_SIZE_G="${SWAP_SIZE_G:-64}"
PROTECT_UNITS="ssh.service docker.service containerd.service NetworkManager.service systemd-networkd.service tailscaled.service"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# install_file <zdroj> <cíl> [mode] — záloha jen když se cíl opravdu mění
install_file() {
  local src="$1" dst="$2" mode="${3:-0644}"
  if [ -e "$dst" ] && cmp -s "$src" "$dst"; then return 0; fi
  if [ -e "$dst" ]; then mkdir -p "$backup$(dirname "$dst")"; cp -a "$dst" "$backup$dst"; fi
  install -D -m "$mode" "$src" "$dst"
  echo "   nainstalováno $dst"
}

step_sysctl() {
  log "sysctl: rezervy, sysrq, swap tuning"
  install_file "$here/etc/sysctl.d/90-oom-guard.conf" /etc/sysctl.d/90-oom-guard.conf
  sysctl --system >/dev/null
  sysctl kernel.sysrq vm.min_free_kbytes vm.swappiness vm.watermark_scale_factor
}

step_watchdog() {
  log "watchdog: systemd RuntimeWatchdogSec=60 na /dev/watchdog0"
  [ -e /dev/watchdog0 ] || { echo "   /dev/watchdog0 chybí — přeskakuji"; return 0; }
  install_file "$here/etc/systemd/system.conf.d/90-watchdog.conf" /etc/systemd/system.conf.d/90-watchdog.conf
  systemctl daemon-reexec
  systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec
}

step_protect() {
  log "protect: OOMScoreAdjust=-1000 pro kritické služby"
  local u pid
  for u in $PROTECT_UNITS; do
    systemctl cat "$u" >/dev/null 2>&1 || { echo "   $u neexistuje — přeskakuji"; continue; }
    install_file "$here/etc/systemd/protect/oom-protect.conf" "/etc/systemd/system/$u.d/90-oom-protect.conf"
  done
  systemctl daemon-reload
  # Běžícím procesům rovnou — restart docker/sshd kvůli tomu nechceme.
  for u in $PROTECT_UNITS; do
    pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null || echo 0)"
    if [ "${pid:-0}" -gt 0 ]; then
      echo -1000 > "/proc/$pid/oom_score_adj" && echo "   $u (pid $pid) → -1000"
    fi
  done
}

step_swap() {
  log "swap: zram (pri 100) + $SWAP_FILE ${SWAP_SIZE_G}G (pri 10)"
  if [ "$(cat /sys/module/zswap/parameters/enabled 2>/dev/null)" = "Y" ]; then
    echo "   zswap je zapnutý — plán říká nekombinovat se zram; vypni ho (zswap.enabled=0) a pusť znovu" >&2
    return 1
  fi
  dpkg -s systemd-zram-generator >/dev/null 2>&1 || apt-get install -y systemd-zram-generator
  install_file "$here/etc/systemd/zram-generator.conf" /etc/systemd/zram-generator.conf
  systemctl daemon-reload
  systemctl restart systemd-zram-setup@zram0.service

  local want=$((SWAP_SIZE_G * 1024 * 1024 * 1024)) have=0
  [ -f "$SWAP_FILE" ] && have=$(stat -c %s "$SWAP_FILE")
  if [ "$have" -ne "$want" ]; then
    if swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
      echo "   swapoff $SWAP_FILE (obsazeno: $(swapon --show=NAME,USED --noheadings | awk -v f="$SWAP_FILE" '$1==f{print $2}'))"
      swapoff "$SWAP_FILE"
    fi
    fallocate -l "${SWAP_SIZE_G}G" "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
  fi
  mkdir -p "$backup/etc"; cp -a /etc/fstab "$backup/etc/fstab"
  if grep -qE "^$SWAP_FILE[[:space:]]" /etc/fstab; then
    sed -i -E "s|^($SWAP_FILE[[:space:]]+none[[:space:]]+swap[[:space:]]+)[^[:space:]]+|\1sw,pri=10|" /etc/fstab
  else
    echo "$SWAP_FILE none swap sw,pri=10 0 0" >> /etc/fstab
  fi
  cmp -s /etc/fstab "$backup/etc/fstab" && rm -f "$backup/etc/fstab"
  if swapon --show=NAME,PRIO --noheadings | awk -v f="$SWAP_FILE" '$1==f && $2!=10{found=1} END{exit !found}'; then
    swapoff "$SWAP_FILE"   # běží se starou prioritou
  fi
  swapon -a
  swapon --show
}

step_guard() {
  log "guard: spark-oom-guard"
  [ -x "$here/oom-guard/spark-oom-guard" ] || { echo "   chybí binárka — make oom-guard-build na Macu a nasadit" >&2; return 1; }
  install_file "$here/oom-guard/spark-oom-guard" /usr/local/sbin/spark-oom-guard 0755
  install_file "$here/oom-guard/spark-oom-guard.env" /etc/default/spark-oom-guard
  if [ -n "${GUARD_DRY_RUN:-}" ]; then
    sed -i -E "s/^GUARD_DRY_RUN=.*/GUARD_DRY_RUN=$GUARD_DRY_RUN/" /etc/default/spark-oom-guard
  fi
  install_file "$here/oom-guard/spark-oom-guard.service" /etc/systemd/system/spark-oom-guard.service
  systemctl daemon-reload
  systemctl enable spark-oom-guard.service >/dev/null
  systemctl restart spark-oom-guard.service
  sleep 2
  systemctl --no-pager --lines=5 status spark-oom-guard.service | sed 's/^/   /'
}

steps=("$@")
[ ${#steps[@]} -gt 0 ] || steps=(sysctl watchdog protect swap guard)
for s in "${steps[@]}"; do
  declare -F "step_$s" >/dev/null || { echo "neznámý krok: $s" >&2; exit 1; }
  "step_$s"
done
[ -d "$backup" ] && echo "zálohy: $backup"
log "hotovo"

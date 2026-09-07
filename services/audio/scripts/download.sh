#!/usr/bin/env bash
# Stáhne audio modely a jejich licence.
#
# Cíl je ~/dev/audio/models, ne /opt/audio jak říkal plán: na SPARKu není
# passwordless sudo, takže do /opt se zapsat nedá a čekat na heslo v tomhle
# skriptu nemá smysl. AUDIO_ROOT to přebije, když bude potřeba.
#
#   scripts/download.sh          # vše
#   scripts/download.sh music    # jen ACE-Step
#   scripts/download.sh sfx      # jen MOSS + Stable Audio
set -euo pipefail

AUDIO_ROOT="${AUDIO_ROOT:-$HOME/dev/audio}"
MODELS="$AUDIO_ROOT/models"
export HF_HOME="${HF_HOME:-$MODELS/hf}"
export HF_TOKEN="${HF_TOKEN:-$(cat "$HOME/.cache/huggingface/token" 2>/dev/null || true)}"

mkdir -p "$MODELS" "$AUDIO_ROOT/envs" "$AUDIO_ROOT/bench"

HF_BIN="$AUDIO_ROOT/envs/tools/bin/hf"
if [[ ! -x "$HF_BIN" ]]; then
  echo "== zakládám venv s hf CLI"
  python3 -m venv "$AUDIO_ROOT/envs/tools"
  "$AUDIO_ROOT/envs/tools/bin/pip" -q install --upgrade pip huggingface_hub
fi

failed=()

dl() {  # dl <repo_id> <local_dir_name>
  local repo="$1" name="$2"
  echo "== $repo → models/$name"
  if "$HF_BIN" download "$repo" --local-dir "$MODELS/$name"; then
    printf '%s\n' "$repo" > "$MODELS/$name/.hf-repo"
  else
    echo "!! $repo se nestáhl — gated repo? Přijmi licenci na https://huggingface.co/$repo" >&2
    failed+=("$repo")
  fi
}

case "${1:-all}" in
  music)
    dl ACE-Step/Ace-Step1.5      ace-step-1.5
    dl ACE-Step/ACE-Step-v1-3.5B ace-step-v1-3.5b
    ;;
  sfx)
    dl OpenMOSS-Team/MOSS-SoundEffect-v2.0 moss-soundeffect-v2
    # Stability modely jsou gated: bez odsouhlasené licence na HF vrátí 403.
    dl stabilityai/stable-audio-open-1.0   stable-audio-open-1.0
    dl stabilityai/stable-audio-open-small stable-audio-open-small
    ;;
  all)
    "$0" music
    "$0" sfx
    exit $?
    ;;
  *)
    echo "použití: $0 [all|music|sfx]" >&2
    exit 2
    ;;
esac

if (( ${#failed[@]} )); then
  echo
  echo "Nestaženo: ${failed[*]}" >&2
  exit 1
fi
echo "Hotovo. Licence modelů shrnuje services/audio/LICENSES.md"

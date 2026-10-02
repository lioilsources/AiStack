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
#   scripts/download.sh tts      # Kokoro, Piper (cs), XTTS-v2, Chatterbox (~12 GB)
#   scripts/download.sh tts-cpu  # jen Kokoro + Piper (~0,5 GB)
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

dl() {  # dl <repo_id> <local_dir_name> [soubory… | --include vzor…]
  # Pozor: `hf download` s vyjmenovanými soubory --include tiše ignoruje —
  # obojí najednou nejde, proto Kokoro volá dl dvakrát.
  local repo="$1" name="$2"
  shift 2
  echo "== $repo → models/$name $*"
  if "$HF_BIN" download "$repo" "$@" --local-dir "$MODELS/$name"; then
    printf '%s\n' "$repo" > "$MODELS/$name/.hf-repo"
  else
    echo "!! $repo se nestáhl — gated repo? Přijmi licenci na https://huggingface.co/$repo" >&2
    if [[ -n "${DL_OPTIONAL:-}" ]]; then
      echo "   (volitelný model — pokračuji)" >&2
    else
      failed+=("$repo")
    fi
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
  tts-cpu|tts)
    # Kokoro-82M jako ONNX (onnx-community, Apache-2.0): fp32 model + 54 hlasů.
    # model_quantized.onnx (int8) je ~2× rychlejší, ale chrčí na sykavkách.
    dl onnx-community/Kokoro-82M-v1.0-ONNX kokoro-82m onnx/model.onnx config.json README.md
    dl onnx-community/Kokoro-82M-v1.0-ONNX kokoro-82m --include "voices/*"
    # Licence a známky hlasů jsou v původním repu.
    dl hexgrad/Kokoro-82M kokoro-82m/upstream README.md VOICES.md
    # Piper: jen české hlasy. Licence se liší hlas od hlasu (MODEL_CARD vedle
    # .onnx) — kasandra CC BY 4.0, jirka dotrénovaný z lessac (viz LICENSES.md).
    dl rhasspy/piper-voices piper-voices --include "cs/cs_CZ/*"
    if [[ "${1:-}" == "tts" ]]; then
      # XTTS-v2: Coqui Public Model License — NEKOMERČNÍ. LICENSE.txt jde s váhami.
      dl coqui/XTTS-v2 xtts-v2 --exclude "samples/*"
      # Chatterbox Multilingual (MIT): v3 je výchozí, v2 záloha (upstream default).
      dl ResembleAI/chatterbox chatterbox \
        ve.pt s3gen.pt t3_mtl23ls_v3.safetensors t3_mtl23ls_v2.safetensors \
        grapheme_mtl_merged_expanded_v1.json conds.pt Cangjie5_TC.json \
        tokenizer.json mtl_tokenizer.json README.md
      # Český T3 — gated (auto-schválení po kliknutí na HF). Bez něj chatterbox-cs
      # hlásí available=false, nic dalšího se nerozbije.
      DL_OPTIONAL=1 dl Thomcles/Chatterbox-TTS-Czech chatterbox-cs
    fi
    ;;
  all)
    "$0" music
    "$0" sfx
    "$0" tts
    exit $?
    ;;
  *)
    echo "použití: $0 [all|music|sfx|tts|tts-cpu]" >&2
    exit 2
    ;;
esac

if (( ${#failed[@]} )); then
  echo
  echo "Nestaženo: ${failed[*]}" >&2
  exit 1
fi
echo "Hotovo. Licence modelů shrnuje services/audio/LICENSES.md"

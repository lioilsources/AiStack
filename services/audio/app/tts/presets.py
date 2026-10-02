"""Hotové hlasy enginů (presety) a jejich licence.

Kokoro: hlasy jsou součást modelu (Apache-2.0), takže licenci dědí. Známka
kvality je z hexgrad/Kokoro-82M VOICES.md — u hlasů pod C je slyšet, že
měly málo trénovacích dat.

Piper: hlas = samostatný model s vlastní licencí, proto je v katalogu modelů
(`piper-cs-*`) a tady se jen zrcadlí.

XTTS-v2 vestavěné hlasy (speakers_xtts.pth) a Chatterbox `default` hlásí až
běžící runtime — viz `main.list_voices`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Prefix jména Kokoro hlasu → (jazyk API, jazyk pro espeak-ng v kokoro-onnx).
# Druhé písmeno je pohlaví (f/m).
KOKORO_PREFIX = {
    "a": ("en", "en-us"),
    "b": ("en", "en-gb"),
    "e": ("es", "es"),
    "f": ("fr", "fr-fr"),
    "h": ("hi", "hi"),
    "i": ("it", "it"),
    "j": ("ja", "ja"),
    "p": ("pt", "pt-br"),
    "z": ("zh", "cmn"),
}

# hexgrad/Kokoro-82M VOICES.md, sloupec Overall Grade ("" = neuvedeno).
KOKORO_VOICES: dict[str, str] = {
    "af_heart": "A", "af_alloy": "C", "af_aoede": "C+", "af_bella": "A-", "af_jessica": "D",
    "af_kore": "C+", "af_nicole": "B-", "af_nova": "C", "af_river": "D", "af_sarah": "C+",
    "af_sky": "C-", "am_adam": "F+", "am_echo": "D", "am_eric": "D", "am_fenrir": "C+",
    "am_liam": "D", "am_michael": "C+", "am_onyx": "D", "am_puck": "C+", "am_santa": "D-",
    "bf_alice": "D", "bf_emma": "B-", "bf_isabella": "C", "bf_lily": "D", "bm_daniel": "D",
    "bm_fable": "C", "bm_george": "C", "bm_lewis": "D+",
    "ef_dora": "", "em_alex": "", "em_santa": "",
    "ff_siwis": "B-",
    "hf_alpha": "C", "hf_beta": "C", "hm_omega": "C", "hm_psi": "C",
    "if_sara": "C", "im_nicola": "C",
    "jf_alpha": "C+", "jf_gongitsune": "C", "jf_nezumi": "C-", "jf_tebukuro": "C", "jm_kumo": "C-",
    "pf_dora": "", "pm_alex": "", "pm_santa": "",
    "zf_xiaobei": "D", "zf_xiaoni": "D", "zf_xiaoxiao": "D", "zf_xiaoyi": "D",
    "zm_yunjian": "D", "zm_yunxi": "D", "zm_yunxia": "D", "zm_yunyang": "D",
}

# Piper: jméno hlasu (soubor .onnx) → model v katalogu.
PIPER_VOICES: dict[str, str] = {
    "cs_CZ-kasandra-medium": "piper-cs-kasandra-medium",
    "cs_CZ-jirka-medium": "piper-cs-jirka-medium",
    "cs_CZ-jirka-low": "piper-cs-jirka-low",
}
PIPER_GENDER = {"cs_CZ-kasandra-medium": "f", "cs_CZ-jirka-medium": "m", "cs_CZ-jirka-low": "m"}

# Výchozí hlas, když klient žádný nepošle.
DEFAULT_VOICE = {
    "kokoro": {"en": "af_heart", "es": "ef_dora", "fr": "ff_siwis", "hi": "hf_alpha",
               "it": "if_sara", "ja": "jf_alpha", "pt": "pf_dora", "zh": "zf_xiaoxiao"},
    "piper": {"cs": "cs_CZ-kasandra-medium"},
    # GPU enginy bez reference: vestavěný hlas runtime.
    "xtts": {"*": "Ana Florence"},
    "chatterbox": {"*": "default"},
}


@dataclass(frozen=True)
class Preset:
    voice: str
    engine: str
    model: str
    language: str
    gender: str
    grade: str = ""


def kokoro_preset(voice: str) -> Preset | None:
    if voice not in KOKORO_VOICES:
        return None
    lang, _ = KOKORO_PREFIX[voice[0]]
    return Preset(voice, "kokoro", "kokoro-82m", lang, voice[1], KOKORO_VOICES[voice])


def kokoro_espeak_lang(voice: str) -> str:
    return KOKORO_PREFIX.get(voice[:1], ("en", "en-us"))[1]


def piper_preset(voice: str) -> Preset | None:
    model = PIPER_VOICES.get(voice)
    if model is None:
        return None
    return Preset(voice, "piper", model, "cs", PIPER_GENDER.get(voice, ""))


def presets() -> list[Preset]:
    out = [p for v in KOKORO_VOICES if (p := kokoro_preset(v))]
    out += [p for v in PIPER_VOICES if (p := piper_preset(v))]
    return out

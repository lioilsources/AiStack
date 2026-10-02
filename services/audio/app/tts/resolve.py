"""Výběr enginu, modelu a hlasu pro TTS request.

Čistá funkce bez HTTP — aby šla pravidla otestovat bez kontejnerů. Pravidla:

1. `model` v requestu vyhrává (musí být TTS model z katalogu).
2. Hlas s prefixem (`kokoro:af_heart`, `piper:cs_CZ-…`, `xtts:Ana Florence`,
   `chatterbox:default`, `custom:smug-cat`) určí engine; bez prefixu se hledá
   nejdřív uložený hlas, pak preset Kokoro a Piperu.
3. Uložený hlas = klonování: Chatterbox (pokud jazyk umí), pro češtinu
   chatterbox-cs, nakonec XTTS-v2.
4. Bez hlasu podle jazyka: Kokoro → Piper (čeština) → Chatterbox → XTTS.
5. `commercial_only` (výchozí true) vyřadí všechno s `commercial=False` —
   při automatickém výběru se model přeskočí, při explicitním přijde 403.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..catalog import CATALOG, ModelSpec
from . import presets as P
from .voicestore import StoredVoice, VoiceStore

ENGINES = ("kokoro", "piper", "xtts", "chatterbox")
CPU_ENGINES = ("kokoro", "piper")
_DEFAULT_MODEL = {"kokoro": "kokoro-82m", "xtts": "xtts-v2", "chatterbox": "chatterbox-multilingual"}


class ResolveError(ValueError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class TtsPlan:
    engine: str
    model: ModelSpec
    voice: str  # jméno presetu / vestavěného hlasu / id uloženého hlasu
    voice_kind: str  # "preset" | "builtin" | "custom"
    language: str
    # Jazyk pro espeak-ng (Kokoro); u ostatních enginů = language.
    engine_language: str
    ref_path: Path | None = None
    stored: StoredVoice | None = None

    @property
    def lane(self) -> str:
        return "tts-cpu" if self.engine in CPU_ENGINES else "tts-gpu"


def normalize_language(lang: str) -> str:
    lang = (lang or "en").strip().lower().replace("_", "-")
    base = lang.split("-")[0]
    return {"cz": "cs", "jp": "ja", "cn": "zh"}.get(base, base)


def _tts_model(name: str) -> ModelSpec:
    spec = CATALOG.get(name)
    if spec is None or spec.kind != "tts":
        raise ResolveError(404, f"neznámý TTS model {name!r} — seznam: GET /v1/audio/models?kind=tts")
    return spec


def _check_license(spec: ModelSpec, commercial_only: bool) -> None:
    if commercial_only and not spec.commercial:
        raise ResolveError(
            403,
            f"{spec.name}: {spec.license} — {spec.license_status}, pro komerční výstup nepoužitelný. "
            "Na pokusy pošli commercial_only=false.",
        )


def _check_language(spec: ModelSpec, language: str) -> None:
    if spec.languages and language not in spec.languages:
        hint = ""
        if language == "cs":
            hint = " Česky umí: piper-cs-kasandra-medium (komerčně), xtts-v2 a chatterbox-cs (jen nekomerčně)."
        raise ResolveError(422, f"{spec.name} neumí jazyk {language!r} (umí: {', '.join(spec.languages)}).{hint}")


def _split_voice(voice: str) -> tuple[str, str]:
    voice = (voice or "").strip()
    if ":" in voice:
        prefix, name = voice.split(":", 1)
        if prefix in ENGINES or prefix == "custom":
            return prefix, name.strip()
    return "", voice


def _cloning_candidates(language: str) -> list[str]:
    out = []
    if language in CATALOG["chatterbox-multilingual"].languages:
        out.append("chatterbox-multilingual")
    if language == "cs":
        out.append("chatterbox-cs")
    out.append("xtts-v2")
    return out


def _auto_models(language: str) -> list[str]:
    out = []
    if language in CATALOG["kokoro-82m"].languages:
        out.append("kokoro-82m")
    if language == "cs":
        out += ["piper-cs-kasandra-medium", "chatterbox-cs", "xtts-v2"]
    if language in CATALOG["chatterbox-multilingual"].languages:
        out.append("chatterbox-multilingual")
    if language in CATALOG["xtts-v2"].languages and "xtts-v2" not in out:
        out.append("xtts-v2")
    return out


def _pick(candidates: list[str], commercial_only: bool, language: str, why: str) -> ModelSpec:
    allowed = [CATALOG[n] for n in candidates if CATALOG[n].commercial or not commercial_only]
    if not allowed:
        names = ", ".join(candidates) or "žádný"
        raise ResolveError(
            422 if not candidates else 403,
            f"pro jazyk {language!r} ({why}) není komerčně použitelný TTS model (kandidáti: {names}). "
            "Na pokusy pošli commercial_only=false.",
        )
    return allowed[0]


def resolve(
    *,
    language: str,
    voice: str = "",
    engine: str = "",
    model: str = "",
    commercial_only: bool = True,
    voices: VoiceStore | None = None,
) -> TtsPlan:
    language = normalize_language(language)
    prefix, name = _split_voice(voice)
    if engine and engine not in ENGINES:
        raise ResolveError(422, f"neznámý engine {engine!r} (umím: {', '.join(ENGINES)})")
    if prefix and prefix != "custom" and engine and prefix != engine:
        raise ResolveError(422, f"hlas {voice!r} patří enginu {prefix}, ne {engine}")
    engine = engine or (prefix if prefix != "custom" else "")

    spec: ModelSpec | None = None
    if model:
        spec = _tts_model(model)
        if engine and spec.backend != engine:
            raise ResolveError(422, f"model {model} jede na enginu {spec.backend}, ne {engine}")
        engine = spec.backend

    # --- uložený hlas (klonování) ---
    stored = None
    if voices is not None and name and prefix in ("", "custom", "xtts", "chatterbox"):
        stored = voices.get(name)
        if prefix == "custom" and stored is None:
            raise ResolveError(404, f"uložený hlas {name!r} neexistuje — nahraj ho přes POST /v1/audio/voices")
    if stored is not None:
        if engine in CPU_ENGINES:
            raise ResolveError(422, f"{engine} neumí klonovat hlas z reference — použij chatterbox nebo xtts")
        if spec is None:
            candidates = _cloning_candidates(language)
            if engine:
                candidates = [c for c in candidates if CATALOG[c].backend == engine]
            spec = _pick(candidates, commercial_only, language, "klonovaný hlas")
        _check_license(spec, commercial_only)
        _check_language(spec, language)
        return TtsPlan(
            engine=spec.backend, model=spec, voice=stored.voice_id, voice_kind="custom",
            language=language, engine_language=language,
            ref_path=voices.ref_path(stored.voice_id), stored=stored,
        )

    # --- preset podle jména ---
    preset = None
    if name and engine in ("", "kokoro"):
        preset = P.kokoro_preset(name)
    if preset is None and name and engine in ("", "piper"):
        preset = P.piper_preset(name)
    if preset is not None:
        if spec is not None and spec.name != preset.model:
            raise ResolveError(422, f"hlas {name!r} patří modelu {preset.model}, ne {spec.name}")
        spec = CATALOG[preset.model]
        _check_license(spec, commercial_only)
        _check_language(spec, language)
        if preset.language != language:
            raise ResolveError(
                422, f"hlas {name!r} je {preset.language}, request chce {language!r} — vyber hlas toho jazyka",
            )
        engine_lang = P.kokoro_espeak_lang(name) if preset.engine == "kokoro" else language
        return TtsPlan(preset.engine, spec, name, "preset", language, engine_lang)

    if name and engine in CPU_ENGINES:
        raise ResolveError(404, f"{engine} nemá hlas {name!r} — seznam: GET /v1/audio/voices?engine={engine}")
    if name and not engine:
        raise ResolveError(
            404, f"hlas {name!r} neznám — není uložený ani preset; vestavěné hlasy GPU enginů piš s prefixem (xtts:…)",
        )

    # --- model podle enginu nebo jazyka ---
    if spec is None:
        if engine == "piper":
            # Piper má v katalogu jen české hlasy; jiný jazyk odmítne _check_language.
            spec = CATALOG[P.PIPER_VOICES[P.DEFAULT_VOICE["piper"]["cs"]]]
        elif engine == "chatterbox":
            spec = CATALOG["chatterbox-cs" if language == "cs" else "chatterbox-multilingual"]
        elif engine:
            spec = CATALOG[_DEFAULT_MODEL[engine]]
        else:
            spec = _pick(_auto_models(language), commercial_only, language, "výchozí hlas")
    _check_license(spec, commercial_only)
    _check_language(spec, language)

    engine = spec.backend
    if engine == "kokoro":
        voice_name = P.DEFAULT_VOICE["kokoro"].get(language, "af_heart")
        return TtsPlan(engine, spec, voice_name, "preset", language, P.kokoro_espeak_lang(voice_name))
    if engine == "piper":
        voice_name = next(v for v, m in P.PIPER_VOICES.items() if m == spec.name)
        return TtsPlan(engine, spec, voice_name, "preset", language, language)
    voice_name = name or P.DEFAULT_VOICE[engine]["*"]
    return TtsPlan(engine, spec, voice_name, "builtin", language, language)

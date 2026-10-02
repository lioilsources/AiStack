"""Katalog modelů a jejich licencí.

Zdroj pravdy pro `GET /v1/audio/models` i pro `LICENSES.md`. Kirian jde na
Steam, takže `commercial=False` model se do produkce nesmí dostat — proto
je licence součástí API odpovědi, ne jen dokumentace.

TTS (MemeShorts, monetizované shorty) v katalogu vede i nekomerční XTTS-v2 —
uživatel ho chce na pokusy. Proto u TTS platí: `commercial=False` znamená
„nekomerční nebo neověřené" a `/v1/audio/tts` takový model bez výslovného
`commercial_only=false` odmítne.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str  # "music" | "sfx" | "tts"
    backend: str  # "acestep" | "moss" | "stableaudio" | "kokoro" | "piper" | "xtts" | "chatterbox"
    repo: str
    license: str
    license_url: str
    commercial: bool
    note: str = ""
    # False = model je stažený a licenčně čistý, ale žádný běžící runtime ho
    # neumí obsloužit. Bez tohohle by ho /v1/audio/models hlásil jako dostupný
    # jen proto, že běží kontejner jeho kategorie, a job by spadl až v generaci.
    served: bool = True
    # --- jen TTS ---
    # Jazyky ve tvaru ISO 639-1 (en, cs, …) — co model opravdu umí, ne co
    # se dá „nějak" přečíst cizí výslovností.
    languages: tuple[str, ...] = ()
    # Umí klonovat hlas z referenčního vzorku (uložený hlas postavy).
    cloning: bool = False
    # Text, který musí jít k výstupu (CC BY apod.). Prázdné = nic.
    attribution: str = ""
    # "ok" | "attribution" | "noncommercial" | "unclear" — strojově
    # čitelný důvod, proč je `commercial` takový, jaký je.
    license_status: str = "ok"
    # Výstup nese neslyšitelný vodoznak (Chatterbox → Resemble Perth).
    watermark: bool = False
    # Kterým kontejnerem jde (tts-cpu | tts-xtts | tts-chatterbox).
    runtime: str = ""


CATALOG: dict[str, ModelSpec] = {
    m.name: m
    for m in [
        ModelSpec(
            name="acestep-v15-turbo",
            kind="music",
            backend="acestep",
            repo="ACE-Step/Ace-Step1.5",
            license="MIT",
            license_url="https://huggingface.co/ACE-Step/Ace-Step1.5",
            commercial=True,
            note="LM 1.7B + turbo DiT + VAE. Model card výslovně povoluje komerční užití výstupů.",
        ),
        ModelSpec(
            name="ace-step-v1-3.5b",
            kind="music",
            backend="acestep",
            repo="ACE-Step/ACE-Step-v1-3.5B",
            license="Apache-2.0",
            license_url="https://huggingface.co/ACE-Step/ACE-Step-v1-3.5B",
            commercial=True,
            served=False,
            note="Záloha pro případ, že v1.5 nepojede na sm_121. Je to jiná "
            "architektura než v1.5 — server audio-music ji neumí načíst, "
            "chtěla by vlastní runtime image.",
        ),
        ModelSpec(
            name="moss-soundeffect-v2",
            kind="sfx",
            backend="moss",
            repo="OpenMOSS-Team/MOSS-SoundEffect-v2.0",
            license="Apache-2.0",
            license_url="https://huggingface.co/OpenMOSS-Team/MOSS-SoundEffect-v2.0",
            commercial=True,
            note="48 kHz, DiT + flow matching. Apache-2.0 = bez stropu na obrat.",
        ),
        ModelSpec(
            name="stable-audio-open-1.0",
            kind="sfx",
            backend="stableaudio",
            repo="stabilityai/stable-audio-open-1.0",
            license="Stability AI Community License",
            license_url="https://huggingface.co/stabilityai/stable-audio-open-1.0/blob/main/LICENSE.md",
            commercial=True,
            note="Komerčně jen do 1 M USD ročního obratu; nad to je potřeba enterprise licence. "
            "Gated repo — vyžaduje odsouhlasení licence na HF.",
        ),
        ModelSpec(
            name="stable-audio-open-small",
            kind="sfx",
            backend="stableaudio",
            repo="stabilityai/stable-audio-open-small",
            license="Stability AI Community License",
            license_url="https://huggingface.co/stabilityai/stable-audio-open-small/blob/main/LICENSE.md",
            commercial=True,
            note="ARM-optimalizovaná varianta, stejný strop 1 M USD. Gated repo. "
            "Formát stable-audio-tools (jen .ckpt), ne diffusers — wrapper ji "
            "zatím neumí načíst.",
            served=False,
        ),
    ]
}

# --- TTS ---------------------------------------------------------------------
# Licence ověřené 2026-10-02 přímo z HF (model card, LICENSE, MODEL_CARD
# hlasů); shrnutí a odkazy v LICENSES.md.

KOKORO_LANGS = ("en", "es", "fr", "hi", "it", "ja", "pt", "zh")
XTTS_LANGS = (
    "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh", "ja", "hu", "ko", "hi",
)
CHATTERBOX_LANGS = (
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja", "ko", "ms", "nl", "no",
    "pl", "pt", "ru", "sv", "sw", "tr", "zh",
)

_PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/blob/main/cs/cs_CZ"

TTS_MODELS: list[ModelSpec] = [
    ModelSpec(
        name="kokoro-82m",
        kind="tts",
        backend="kokoro",
        runtime="tts-cpu",
        repo="hexgrad/Kokoro-82M",
        license="Apache-2.0",
        license_url="https://huggingface.co/hexgrad/Kokoro-82M",
        commercial=True,
        languages=KOKORO_LANGS,
        note="ONNX export onnx-community/Kokoro-82M-v1.0-ONNX, běží na CPU. Češtinu NEUMÍ. "
        "Trénováno na public domain, Apache/MIT/CC BY datech a syntetickém audiu z "
        "komerčních TTS (model card). Fonémy přes espeak-ng (GPL-3.0, jen běh služby).",
    ),
    ModelSpec(
        name="piper-cs-kasandra-medium",
        kind="tts",
        backend="piper",
        runtime="tts-cpu",
        repo="rhasspy/piper-voices",
        license="CC-BY-4.0",
        license_url=f"{_PIPER_URL}/kasandra/medium/MODEL_CARD",
        commercial=True,
        license_status="attribution",
        attribution="Hlas Kasandra © Ondřej Šimek, CC BY 4.0 (rhasspy/piper-voices)",
        languages=("cs",),
        note="Jediný český hlas s komerčně čistou licencí: autor nahrál vlastní dataset a "
        "vydal ho pod CC BY 4.0 — výstup musí nést uvedení autora. Ženský hlas, 22,05 kHz. "
        "Kód Piperu (piper1-gpl) je GPL-3.0, na výstup se nevztahuje.",
    ),
    ModelSpec(
        name="piper-cs-jirka-medium",
        kind="tts",
        backend="piper",
        runtime="tts-cpu",
        repo="rhasspy/piper-voices",
        license="MIT (repo) + dataset CC0, ale dotrénováno z en_US-lessac",
        license_url=f"{_PIPER_URL}/jirka/medium/MODEL_CARD",
        commercial=False,
        license_status="unclear",
        languages=("cs",),
        note="Mužský hlas. Dataset je CC0, jenže model je dotrénovaný z en_US-lessac a ten stojí "
        "na Blizzard 2013 (Lessac) — licence datasetu výslovně zakazuje komerční užití "
        "pro „voice synthesis products or services“. Dokud se to nevyjasní, jen na pokusy.",
    ),
    ModelSpec(
        name="piper-cs-jirka-low",
        kind="tts",
        backend="piper",
        runtime="tts-cpu",
        repo="rhasspy/piper-voices",
        license="MIT (repo) + dataset CC0, ale dotrénováno z en_US-lessac",
        license_url=f"{_PIPER_URL}/jirka/low/MODEL_CARD",
        commercial=False,
        license_status="unclear",
        languages=("cs",),
        note="Totéž co jirka-medium, 16 kHz. Původ z Lessac (Blizzard 2013, jen výzkum).",
    ),
    ModelSpec(
        name="xtts-v2",
        kind="tts",
        backend="xtts",
        runtime="tts-xtts",
        repo="coqui/XTTS-v2",
        license="Coqui Public Model License 1.0.0",
        license_url="https://coqui.ai/cpml",
        commercial=False,
        license_status="noncommercial",
        cloning=True,
        languages=XTTS_LANGS,
        note="NEKOMERČNÍ: CPML povoluje model i jeho výstup jen pro nekomerční účely; "
        "monetizovaný short je komerční. Coqui v lednu 2024 skončil, komerční licenci už "
        "není od koho koupit. Umí češtinu a klonování hlasu — jen na pokusy a porovnání. "
        "Ke každé kopii výstupu musí jít text licence nebo její URL.",
    ),
    ModelSpec(
        name="chatterbox-multilingual",
        kind="tts",
        backend="chatterbox",
        runtime="tts-chatterbox",
        repo="ResembleAI/chatterbox",
        license="MIT",
        license_url="https://huggingface.co/ResembleAI/chatterbox",
        commercial=True,
        cloning=True,
        watermark=True,
        languages=CHATTERBOX_LANGS,
        note="Multilingual T3 (23 jazyků) + klonování hlasu. Češtinu NEUMÍ (nejbližší je "
        "polština — česky to zní cize). Každý výstup nese neslyšitelný vodoznak Resemble "
        "Perth (přežije MP3 i střih).",
    ),
    ModelSpec(
        name="chatterbox-cs",
        kind="tts",
        backend="chatterbox",
        runtime="tts-chatterbox",
        repo="Thomcles/Chatterbox-TTS-Czech",
        license="CC0-1.0 (váhy), dataset CC BY 4.0 s podmínkou jen pro trénink a experimenty",
        license_url="https://huggingface.co/Thomcles/Chatterbox-TTS-Czech",
        commercial=False,
        license_status="unclear",
        cloning=True,
        watermark=True,
        languages=("cs",),
        note="Komunitní český T3 nad Chatterbox Multilingual. Gated repo (nutné přijmout "
        "podmínky na HF). Váhy CC0, ale dataset YodaLingua-Czech je za bránou s podmínkou "
        "„use it solely for training and experimentation“ a původ nahrávek není popsaný — "
        "do vyjasnění nekomerční. Neověřený: neví se, s jakým jazykovým tagem se trénoval.",
    ),
]

CATALOG.update({m.name: m for m in TTS_MODELS})

# Modely, které se do produkce nesmí dostat kvůli licenci vah. Drženy tady,
# aby byl důvod dohledatelný, až se někdo příště zeptá „a co MusicGen?".
EXCLUDED: dict[str, str] = {
    "facebook/musicgen-*": "váhy CC-BY-NC-4.0 — nekomerční",
    "facebook/audiogen-*": "váhy CC-BY-NC-4.0 — nekomerční",
    "cvssp/audioldm2": "nekomerční licence",
    "declare-lab/TangoFlux": "nekomerční licence",
    "hkchengrex/MMAudio": "kód MIT, ale váhy CC-BY-NC-4.0 — nekomerční",
    # TTS — zvažované pro MemeShorts a vyřazené.
    "rhasspy/piper-voices en_US-lessac-*": "dataset Blizzard 2013 jen pro výzkum, komerční "
    "užití pro syntézu řeči výslovně zakázané",
    "myshell-ai/OpenVoice v1": "CC-BY-NC-4.0 — nekomerční (v2 je MIT, ale bez češtiny)",
}


def by_kind(kind: str) -> list[ModelSpec]:
    return [m for m in CATALOG.values() if m.kind == kind]

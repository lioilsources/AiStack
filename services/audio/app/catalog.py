"""Katalog modelů a jejich licencí.

Zdroj pravdy pro `GET /v1/audio/models` i pro `LICENSES.md`. Kirian jde na
Steam, takže `commercial=False` model se do produkce nesmí dostat — proto
je licence součástí API odpovědi, ne jen dokumentace.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    kind: str  # "music" | "sfx"
    backend: str  # "acestep" | "moss" | "stableaudio"
    repo: str
    license: str
    license_url: str
    commercial: bool
    note: str = ""
    # False = model je stažený a licenčně čistý, ale žádný běžící runtime ho
    # neumí obsloužit. Bez tohohle by ho /v1/audio/models hlásil jako dostupný
    # jen proto, že běží kontejner jeho kategorie, a job by spadl až v generaci.
    served: bool = True


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

# Modely, které se do produkce nesmí dostat kvůli licenci vah. Drženy tady,
# aby byl důvod dohledatelný, až se někdo příště zeptá „a co MusicGen?".
EXCLUDED: dict[str, str] = {
    "facebook/musicgen-*": "váhy CC-BY-NC-4.0 — nekomerční",
    "facebook/audiogen-*": "váhy CC-BY-NC-4.0 — nekomerční",
    "cvssp/audioldm2": "nekomerční licence",
    "declare-lab/TangoFlux": "nekomerční licence",
    "hkchengrex/MMAudio": "kód MIT, ale váhy CC-BY-NC-4.0 — nekomerční",
}


def by_kind(kind: str) -> list[ModelSpec]:
    return [m for m in CATALOG.values() if m.kind == kind]

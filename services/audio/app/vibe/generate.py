"""Parametry generování ze předlohy — dva režimy záměrně.

- **vibe** (výchozí): nová skladba se stejným zvukem a náladou. text2music
  s `reference_audio`; melodie se nekopíruje. Turbo DiT referenci umí
  (README ACE-Step 1.5, sloupec „Refer audio"), takže fallback na cover
  z plánu §5 není potřeba. LM plán struktury (`lm_plan`) je volitelný
  a ve výchozím stavu vypnutý: stejný seed s ním dá pokaždé jinou skladbu
  (korelace 0,03 proti 0,84 bez něj), varianta trvá 18–24 s místo 4–6 s
  a konec má 2–8 s ticha. Tempo drží oba stejně (NOTES.md).
- **groove**: drží rytmus, formu a konturu melodie předlohy, mění kabát.
  Cover nad `src_audio`, výchozí síla 0.5.

Čistá funkce bez I/O, aby šla testovat bez modelu.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from ..backends.base import GenSpec
from .prompt import build_caption
from .sample import SampleInfo, SampleStore

Mode = Literal["vibe", "groove"]

MIN_DURATION_S = 10.0  # pod 10 s ACE-Step generovat odmítá
MAX_DURATION_S = 240.0
# Plán říkal 0.2–0.4; na turbu cover při 0.3 rytmus předlohy drží jen
# někdy (korelace onsetů 0.05–0.51), od 0.5 spolehlivě (0.28–0.69) a výš
# už nepřibývá. Měření v NOTES.md.
DEFAULT_COVER_STRENGTH = 0.5


@dataclass(frozen=True)
class VibeParams:
    """Všechno, co o generaci rozhoduje — jde celé do manifestu."""

    mode: Mode
    caption: str
    bpm: Optional[int]
    keyscale: str
    timesignature: str
    duration_s: float
    instrumental: bool
    vocal_language: str
    lyrics: str
    cover_strength: Optional[float]
    # Struktura přes 5Hz LM (jen vibe; cover LM nepoužívá).
    lm_plan: bool = False


def resolve(req: dict[str, Any], analysis: dict[str, Any] | None, sample: SampleInfo) -> VibeParams:
    """Sloučí požadavek s analýzou: co uživatel poslal, vyhrává.

    Aplikace posílá hodnoty, které uživatel viděl a případně upravil, takže
    analýza je tu jen záloha pro volání bez nich (CLI, curl).
    """
    a = analysis or {}
    mode: Mode = "groove" if req.get("mode") == "groove" else "vibe"

    caption = build_caption(req.get("caption") or a.get("caption") or "", req.get("user_hint") or "")
    if not caption:
        raise ValueError("chybí caption — spusť analýzu nebo ho pošli v požadavku")

    instrumental = req.get("instrumental")
    if instrumental is None:
        instrumental = bool(a.get("instrumental", True))
    lyrics = "" if instrumental else (req.get("lyrics") or a.get("lyrics") or "")
    language = (req.get("vocal_language") or a.get("vocal_language") or "unknown") if not instrumental else "unknown"

    if mode == "groove":
        # Cover má délku předlohy; delší by jen dosmyčkoval konec.
        duration = sample.duration_s
    else:
        duration = float(req.get("duration_s") or sample.duration_s)
    duration = max(MIN_DURATION_S, min(MAX_DURATION_S, duration))

    return VibeParams(
        mode=mode,
        caption=caption,
        bpm=req.get("bpm") or a.get("bpm"),
        keyscale=req.get("keyscale") or a.get("keyscale") or "",
        timesignature=str(req.get("timesignature") or a.get("timesignature") or ""),
        duration_s=round(duration, 2),
        instrumental=bool(instrumental),
        vocal_language=language,
        lyrics=lyrics,
        cover_strength=float(req.get("cover_strength", DEFAULT_COVER_STRENGTH)) if mode == "groove" else None,
        lm_plan=bool(req.get("lm_plan")) and mode == "vibe",
    )


def build_spec(
    p: VibeParams, samples: SampleStore, sample_id: str, seed: int | None, model: str = ""
) -> GenSpec:
    common = dict(
        prompt=p.caption,
        duration_s=p.duration_s,
        seed=seed,
        lyrics=p.lyrics,
        instrumental=p.instrumental,
        bpm=p.bpm,
        key=p.keyscale,
        time_signature=p.timesignature,
        vocal_language="" if p.instrumental else p.vocal_language,
        model=model,
        # Caption uživatel viděl a schválil; LM ji má nechat být.
        rewrite_caption=False,
        thinking=p.lm_plan,
    )
    if p.mode == "groove":
        return GenSpec(
            task_type="cover",
            src_audio=samples.src(sample_id),
            cover_strength=p.cover_strength if p.cover_strength is not None else DEFAULT_COVER_STRENGTH,
            **common,
        )
    return GenSpec(task_type="text2music", reference_audio=samples.ref(sample_id), **common)

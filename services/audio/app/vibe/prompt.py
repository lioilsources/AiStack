"""Caption pro DiT: úklid LM popisu a připojení přání uživatele.

LM píše caption jako souvislou prózu („A classic lo-fi hip-hop instrumental
built on…") — to je tvar, na kterém se DiT učil (sám ji tak generuje v CoT),
takže se nepřestavuje do seznamu tagů. Uklízí se jen vata na začátku vět,
která nic nepopisuje. Tempo, tónina a takt do captionu nepatří: jdou
do metadat, kde je DiT drží doslova (plán §3 krok 2).
"""

from __future__ import annotations

import re

MAX_CAPTION_CHARS = 1500

# Úvody vět, které nic nepopisují. Jen na začátku věty, ať se nerozbije
# „…a track that is…" uprostřed.
_FILLER = re.compile(
    r"(^|(?<=[.!?]\s))"
    r"(?:this|the)\s+(?:audio|clip|recording|excerpt|sample)\s+"
    r"(?:is|contains|features|presents|consists\s+of)\s+",
    re.IGNORECASE,
)
_SONG_IS = re.compile(
    r"(^|(?<=[.!?]\s))(?:the|this)\s+(?:song|track|piece)\s+is\s+(?=(?:a|an)\s)",
    re.IGNORECASE,
)


def clean_caption(text: str) -> str:
    text = re.sub(r"[*_`#>]+", "", text or "")  # markdown z LM
    text = re.sub(r"\s+", " ", text).strip().strip('"').strip()
    text = _FILLER.sub(r"\1", text)
    text = _SONG_IS.sub(r"\1", text)
    text = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text[:MAX_CAPTION_CHARS].strip()


def build_caption(caption: str, hint: str = "") -> str:
    """Caption z analýzy (nebo upravená uživatelem) + volitelné přání.

    Přání jde na konec jako samostatná věta: popis předlohy nese zvuk,
    přání jen posouvá směr („more energetic", „add strings").
    """
    base = clean_caption(caption)
    extra = re.sub(r"\s+", " ", hint or "").strip()
    if not extra:
        return base
    if not base:
        return extra[:MAX_CAPTION_CHARS]
    if base[-1] not in ".!?":
        base += "."
    return f"{base} {extra[0].upper()}{extra[1:]}"[:MAX_CAPTION_CHARS]

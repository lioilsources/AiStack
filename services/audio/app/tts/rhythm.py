"""Řeč na mřížce — text rozdělený na fráze a každá fráze zarovnaná na doby.

Žádný TTS engine tu neumí tempo ani přízvuk (Chatterbox ignoruje i `speed`)
a tvar textu to nezmění: tentýž odstavec jako próza a rozsekaný čárkami po
osmi slabikách vyšel 12,0 a 12,5 s, `exaggeration` 0,3–1,2 a `cfg_weight`
0,3–0,7 v rozmezí 11,3–13,1 s (chatterbox-cs, 2026-10-10). Rytmus proto
vzniká až tady: text se rozdělí na fráze po celých slovech, každá se
namluví zvlášť, ořízne od ticha, natáhne nebo zkrátí na svůj počet dob
a doplní pauzou do konce frázového slotu. Výstup je tak dlouhý přesně
`doby × 60/bpm` a dá se pod něj položit klik nebo beat.

Co to není: flow. Přízvuky uvnitř fráze padají tam, kam je položil engine,
ne na doby — zarovnává se začátek a délka fráze, ne slabiky.

Kolik slabik se do fráze vejde, se neodvozuje z BPM napevno, ale z tempa
přirozené řeči: fráze dostane tolik textu, aby ho engine namluvil zhruba
v čase, který ve slotu má. Natažení pak jen dorovnává rozdíl a zůstává
blízko 1,0 při jakémkoli BPM.
"""

from __future__ import annotations

import dataclasses
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from ..backends.base import BackendError, RawAudio
from ..postproc import FFMPEG, FFmpegError, duration_s

# Jak dlouho engine mluví: `OVERHEAD_S + slabiky / rychlost`. Ta konstanta
# navíc není ticho (to se ořezává), ale náběh a dotažení poslední slabiky —
# u krátké fráze dělá polovinu délky. Změřeno na chatterbox-cs s klonem,
# 2026-10-10: 5 slabik 1,8 s, 6 slabik 2,3 s, 9 slabik 2,8 s, 11 slabik 3,1 s,
# 71 slabik 12,0 s. Počítat jen slabiky/s (bez konstanty) vyšlo u krátkých
# frází na 2,3 slabiky/s, dělení textu podle toho fráze dál zkracovalo a čtení
# se rozpadlo na 73 útržků místo 15. Hodnoty jsou jen start — skutečné čtení
# si obojí dopočítá z prvních frází (`fit_speech`).
OVERHEAD_S = 0.8
NATURAL_SYLLABLES_PER_S = {"cs": 5.3, "sk": 5.3, "en": 4.5}
DEFAULT_SYLLABLES_PER_S = 5.0
_RATE_LIMITS = (2.5, 9.0)
_OVERHEAD_LIMITS = (0.0, 1.5)

# Pod tolik slabik se fráze nedělí: engine z jednoho dvou slov dělá delší
# a horší zvuk, než kolik by ušetřila přesnost mřížky.
MIN_PHRASE_SYLLABLES = 5.0
# Okno pro řeč kratší než tohle (rychlé BPM) se zdvojuje na víc dob, aby se
# do něj vešla fráze, která má smysl namluvit.
MIN_WINDOW_S = 2.0

# Meze natažení (tempo > 1 = rychleji). Srozumitelnost drží v celém rozsahu
# 0,7–1,6 (přepis Whisperem, WER ≤ 0,07 pro atempo i rubberband); meze jsou
# užší kvůli přirozenosti a ta ověřená poslechem není.
MIN_TEMPO = 0.75
MAX_TEMPO = 1.5

# Víc frází = job na desítky minut; článek o 3 000 znacích jich má kolem 90.
MAX_PHRASES = 240
# Kolikrát se fráze namluví znovu s jiným seedem, když ji engine přetáhne.
HALLUCINATION_RETRIES = 2

SAMPLE_RATE = 48000
# Klonovaný hlas nese šum reference, takže „ticho" kolem řeči není −50 dB.
_TRIM_DB = -40.0


@dataclass(frozen=True)
class Style:
    """Preset frázování.

    `phrase_beats` je délka slotu, `rest_beats` pauza na jeho konci, `pace`
    hustota textu proti přirozené řeči (1,0 = jak engine mluví sám).
    """

    phrase_beats: float
    rest_beats: float
    pace: float
    exaggeration: float
    cfg_weight: float


# Hodnoty jsou výchozí návrh, ne výsledek poslechu — kalibrovat po prvních
# skutečných čteních. `exaggeration` a `cfg_weight` délku neměnily (viz výš),
# mění jen projev.
STYLES: dict[str, Style] = {
    "spoken": Style(phrase_beats=8, rest_beats=1, pace=1.0, exaggeration=0.5, cfg_weight=0.5),
    "news": Style(phrase_beats=8, rest_beats=0.5, pace=1.1, exaggeration=0.3, cfg_weight=0.6),
    "slam": Style(phrase_beats=8, rest_beats=3, pace=0.85, exaggeration=0.9, cfg_weight=0.4),
    "rap": Style(phrase_beats=4, rest_beats=0.5, pace=1.25, exaggeration=1.0, cfg_weight=0.3),
    "preacher": Style(phrase_beats=8, rest_beats=2, pace=0.8, exaggeration=1.2, cfg_weight=0.3),
}


@dataclass(frozen=True)
class Rhythm:
    bpm: int
    style: str = "spoken"
    beats_per_bar: int = 4
    # Klik pod hlasem (přízvuk na první době taktu).
    beat: bool = False

    @property
    def beat_s(self) -> float:
        return 60.0 / self.bpm

    @property
    def preset(self) -> Style:
        return STYLES[self.style]

    @property
    def _span(self) -> int:
        """Kolikrát se slot stylu zdvojí, aby okno pro řeč mělo MIN_WINDOW_S."""
        style = self.preset
        window = (style.phrase_beats - style.rest_beats) * self.beat_s
        span = 1
        while window * span < MIN_WINDOW_S:
            span *= 2
        return span

    @property
    def phrase_beats(self) -> float:
        return self.preset.phrase_beats * self._span

    @property
    def rest_beats(self) -> float:
        return self.preset.rest_beats * self._span

    @property
    def window_s(self) -> float:
        """Kolik času má ve slotu řeč."""
        return (self.phrase_beats - self.rest_beats) * self.beat_s


class RhythmError(ValueError):
    pass


# --- slabiky ---

_CS_VOWELS = "aáäeéěiíoóöuúůüyý"
_CS_DIPHTHONGS = ("ou", "au", "eu")
_EN_VOWELS = "aeiouy"
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# Konec věty i s uvozovkou nebo závorkou za tečkou.
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'“”„»)\]]*$")


def _czech_word(word: str) -> int:
    w = word.lower()
    count = 0
    i = 0
    while i < len(w):
        ch = w[i]
        if ch in _CS_VOWELS:
            count += 1
            # Dvojhláska je jedna slabika.
            i += 2 if w[i : i + 2] in _CS_DIPHTHONGS else 1
            continue
        # Slabikotvorné r/l: mezi souhláskami nebo po souhlásce na konci slova
        # (vlk, krk, vítr, nesl).
        if ch in "rl" and i > 0 and w[i - 1] not in _CS_VOWELS:
            if i + 1 == len(w) or w[i + 1] not in _CS_VOWELS:
                count += 1
        i += 1
    return max(count, 1)


def _english_word(word: str) -> int:
    w = word.lower()
    groups = len(re.findall(f"[{_EN_VOWELS}]+", w))
    # Němé e na konci (make, time), ne však -le po souhlásce (table).
    if w.endswith("e") and groups > 1 and not (w.endswith("le") and len(w) > 2 and w[-3] not in _EN_VOWELS):
        groups -= 1
    return max(groups, 1)


def syllables(text: str, language: str) -> int:
    """Odhad počtu slabik. Čísla se čtou jako jedno „slovo" o třech slabikách —
    engine je rozepíše po svém a přesně to tu vědět nejde."""
    count_word = _czech_word if language in ("cs", "sk") else _english_word
    total = sum(count_word(w) for w in _WORD_RE.findall(text))
    total += 3 * len(re.findall(r"\d+", text))
    return total


# --- rozdělení na fráze ---


def fit_speech(samples: list[tuple[float, float]], language: str) -> tuple[float, float]:
    """(náběh v s, slabik/s) z naměřených frází `(slabiky, délka)`.

    Přímka nejmenšími čtverci, jakmile je z čeho; do té doby a při nesmyslném
    výsledku (stejně dlouhé fráze, sklon mimo meze) výchozí náběh a rychlost
    dopočítaná z něj. Meze drží odhad u země, i když engine jednu frázi
    protáhne do šumu.
    """
    prior = NATURAL_SYLLABLES_PER_S.get(language, DEFAULT_SYLLABLES_PER_S)
    if not samples:
        return OVERHEAD_S, prior

    def clamp(value: float, limits: tuple[float, float]) -> float:
        return min(max(value, limits[0]), limits[1])

    n = len(samples)
    mean_x = sum(x for x, _ in samples) / n
    mean_y = sum(y for _, y in samples) / n
    spread = sum((x - mean_x) ** 2 for x, _ in samples)
    if n >= 4 and spread >= 8.0:
        slope = sum((x - mean_x) * (y - mean_y) for x, y in samples) / spread
        if slope > 0:
            rate = clamp(1.0 / slope, _RATE_LIMITS)
            return clamp(mean_y - mean_x / rate, _OVERHEAD_LIMITS), rate
    speech = sum(max(y - OVERHEAD_S, 0.2 * y) for _, y in samples)
    return OVERHEAD_S, clamp(sum(x for x, _ in samples) / speech, _RATE_LIMITS)


def phrase_syllables(rhythm: Rhythm, language: str, speech: tuple[float, float] | None = None) -> float:
    """Kolik slabik se má vejít do jedné fráze; `speech` = (náběh, slabik/s)."""
    overhead, rate = speech or fit_speech([], language)
    return max(rate * (rhythm.preset.pace * rhythm.window_s - overhead), MIN_PHRASE_SYLLABLES)


def _tail_syllables(words: list[str], start: int, language: str) -> int:
    """Slabiky od `start` do konce věty (včetně), nebo do konce textu."""
    total = 0
    for word in words[start:]:
        total += syllables(word, language)
        if total >= MIN_PHRASE_SYLLABLES or _SENTENCE_END_RE.search(word):
            break
    return total


def take_phrase(words: list[str], start: int, target: float, language: str) -> tuple[str, int]:
    """Jedna fráze od slova `start`: (text, index dalšího slova).

    Řez na interpunkci má přednost: konec věty frázi ukončí, čárka tehdy,
    když padne do 60–115 % cíle. Jinak se řeže za slovem, kterým se cíl
    naplní. Slovo se nedělí nikdy.

    Útržek pod MIN_PHRASE_SYLLABLES nevznikne, dokud je čím ho doplnit: zbytek
    věty o dvou slabikách zůstane u své fráze a věta o jednom slově se spojí
    s následující. Chatterbox z takového útržku udělal 27,7 s zvuku ze dvou
    slabik a 6 s z jedné (2026-10-10) — krátký vstup ho nechává halucinovat.
    """
    taken: list[str] = []
    count = 0
    i = start
    while i < len(words):
        word = words[i]
        n = syllables(word, language)
        # Slovo, které by frázi přetáhlo přes mez, patří už do další — pokud
        # s ním nezbude jen útržek.
        if (taken and count + n > target * 1.15 and count >= MIN_PHRASE_SYLLABLES
                and _tail_syllables(words, i, language) >= MIN_PHRASE_SYLLABLES):
            break
        taken.append(word)
        count += n
        i += 1
        if count < MIN_PHRASE_SYLLABLES:
            continue
        if _SENTENCE_END_RE.search(word):
            break
        soft_break = word[-1] in ",;:—–"
        if (count >= target or (soft_break and count >= target * 0.6)) and (
            i >= len(words) or _tail_syllables(words, i, language) >= MIN_PHRASE_SYLLABLES
        ):
            break
    return " ".join(taken), i


def plan_phrases(text: str, rhythm: Rhythm, language: str) -> list[str]:
    """Text → fráze při výchozím tempu řeči. Skutečné čtení si tempo měří
    za běhu (`generate_on_grid`), takže se počet frází může o něco lišit —
    tohle je odhad pro kontrolu vstupu a pro testy."""
    target = phrase_syllables(rhythm, language)
    words = text.split()
    phrases: list[str] = []
    i = 0
    while i < len(words):
        phrase, i = take_phrase(words, i, target, language)
        # Fráze bez jediného písmene nebo číslice (osamocená pomlčka) by
        # engine buď odmítl, nebo namluvil šum.
        if re.search(r"\w", phrase):
            phrases.append(phrase)
    return phrases


# --- zarovnání ---


@lru_cache(maxsize=1)
def _has_rubberband() -> bool:
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return " rubberband " in out


def _ffmpeg(args: list[str]) -> None:
    proc = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-v", "error", "-y", *args],
                          capture_output=True, text=True, timeout=300, check=False)
    if proc.returncode != 0:
        raise FFmpegError("ffmpeg selhal: " + (proc.stderr or "").strip()[-300:])


def fit_tempo(natural_s: float, rhythm: Rhythm) -> tuple[float, float]:
    """(tempo, počet dob slotu) pro frázi dlouhou `natural_s`.

    Fráze se natáhne na svůj díl slotu, nejvýš v mezích; co se nevejde ani
    při nejvyšším tempu, přeteče do pauzy, a když nestačí ani ta, slot se
    prodlouží o celé doby — mřížka se neposune nikdy.
    """
    beat = rhythm.beat_s
    tempo = min(max(natural_s / rhythm.window_s, MIN_TEMPO), MAX_TEMPO)
    fitted = natural_s / tempo
    beats = rhythm.phrase_beats
    # 10 ms tolerance: zaokrouhlení délky nesmí stát celou dobu navíc.
    if fitted > beats * beat + 0.01:
        beats = float(math.ceil((fitted - 0.01) / beat))
    return tempo, beats


def _trim(raw: Path, dst: Path) -> float:
    """Ořízne ticho na krajích, sjednotí formát; vrátí délku řeči."""
    flt = (
        f"silenceremove=start_periods=1:start_silence=0:start_threshold={_TRIM_DB}dB:detection=peak,"
        "areverse,"
        f"silenceremove=start_periods=1:start_silence=0:start_threshold={_TRIM_DB}dB:detection=peak,"
        "areverse"
    )
    _ffmpeg(["-i", str(raw), "-af", flt, "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst)])
    natural = duration_s(dst)
    if natural < 0.05:
        raise BackendError("engine vrátil pro frázi ticho")
    return natural


def _align(trimmed: Path, dst: Path, rhythm: Rhythm, max_s: float | None = None) -> dict[str, float]:
    """Natáhne oříznutou frázi na okno a doplní tichem přesně na slot.

    `max_s` frázi předtím usekne (s krátkým doznění) — jen pro řeč, kterou
    engine přetáhl do šumu.
    """
    natural = duration_s(trimmed)
    cut = []
    if max_s is not None and natural > max_s:
        cut = [f"atrim=end={max_s:.3f}", f"afade=t=out:st={max(max_s - 0.08, 0):.3f}:d=0.08"]
        natural = max_s
    tempo, beats = fit_tempo(natural, rhythm)
    slot = beats * rhythm.beat_s
    samples = round(slot * SAMPLE_RATE)
    stretch = []
    if abs(tempo - 1.0) > 0.01:
        stretch = [f"rubberband=tempo={tempo:.4f}" if _has_rubberband() else f"atempo={tempo:.4f}"]
    # Délka na vzorek přesně: chyba by se přes desítky frází nasčítala
    # a konec by z mřížky ujel.
    chain = ",".join([*cut, *stretch, f"apad=whole_len={samples}", f"atrim=end_sample={samples}"])
    _ffmpeg(["-i", str(trimmed), "-af", chain, "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)])
    return {"natural_s": round(natural, 3), "tempo": round(tempo, 3), "beats": beats}


def _click_filter(rhythm: Rhythm, total_s: float) -> str:
    """Krátké pípnutí na každé době, vyšší a hlasitější na první v taktu."""
    beat = rhythm.beat_s
    bar = beat * rhythm.beats_per_bar
    # Výraz aevalsrc: 25ms obálka na době; první doba taktu 1500 Hz, ostatní 1000 Hz.
    accent = f"lt(mod(t\\,{bar:.6f})\\,0.025)"
    expr = (
        f"(0.35+0.4*{accent})"
        f"*sin(2*PI*(1000+500*{accent})*t)"
        f"*lt(mod(t\\,{beat:.6f})\\,0.025)"
        f"*exp(-mod(t\\,{beat:.6f})*90)"
    )
    return f"aevalsrc={expr}:s={SAMPLE_RATE}:d={total_s:.6f}"


def generate_on_grid(
    generate: Callable[[Any, Path], RawAudio],
    spec: Any,
    rhythm: Rhythm,
    workdir: Path,
) -> RawAudio:
    """Namluví `spec.text` frázi po frázi a složí je na mřížku `rhythm`.

    `generate` je `backend.generate`; `spec` je TtsSpec (tady jen jako
    dataclass s `text`, `language`, `seed`, `params`).
    """
    words = spec.text.split()
    style = rhythm.preset
    # Projev stylu; co klient poslal výslovně, platí.
    params = {"exaggeration": style.exaggeration, "cfg_weight": style.cfg_weight, **spec.params}

    parts: list[Path] = []
    stats: list[dict[str, float]] = []
    last: RawAudio | None = None
    # Tempo řeči se měří za běhu: Piper mluví skoro o polovinu rychleji než
    # klon z Chatterboxu a každý hlas jinak, takže jedna konstanta by jedněm
    # dávala fráze poloprázdné (natažení na doraz) a druhým přeplněné. První
    # fráze jede s výchozím odhadem, další s tím, co engine opravdu předvedl.
    measured: list[tuple[float, float]] = []
    counts: list[int] = []
    i = 0
    while i < len(words):
        speech = fit_speech(measured, spec.language)
        phrase, i = take_phrase(words, i, phrase_syllables(rhythm, spec.language, speech), spec.language)
        if not re.search(r"\w", phrase):
            continue
        idx = len(parts)
        if idx >= MAX_PHRASES * 2:
            raise BackendError("text je na rytmické čtení moc dlouhý")
        count = float(syllables(phrase, spec.language))
        # Nejdelší délka, která je ještě řeč: přes ni engine halucinuje
        # (ticho, šum, opakování). Zkusí se jiný seed, a když nepomůže ani
        # ten, fráze se usekne — lepší useknutý konec než půl minuty šumu.
        limit = 2.0 * (speech[0] + count / speech[1]) + 1.0
        part = workdir / f"p{idx:03d}.wav"
        retries = 0
        while True:
            piece_dir = workdir / f"p{idx:03d}-{retries}"
            piece_dir.mkdir()
            seed = None if spec.seed is None else spec.seed + retries
            last = generate(dataclasses.replace(spec, text=phrase, params=params, seed=seed), piece_dir)
            natural = _trim(last.path, workdir / f"p{idx:03d}.trim.wav")
            shutil.rmtree(piece_dir, ignore_errors=True)
            if natural <= limit or retries >= HALLUCINATION_RETRIES:
                break
            retries += 1
        runaway = natural > limit
        stat = _align(workdir / f"p{idx:03d}.trim.wav", part, rhythm, max_s=limit if runaway else None)
        stat["retries"] = retries
        stats.append(stat)
        parts.append(part)
        if not runaway:
            measured.append((count, stat["natural_s"]))
        counts.append(int(count))
    if last is None:
        raise BackendError("v textu není co číst")

    listing = workdir / "parts.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    voice = workdir / "grid.wav"
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(voice)])

    beats = sum(s["beats"] for s in stats)
    out = voice
    if rhythm.beat:
        out = workdir / "grid-beat.wav"
        _ffmpeg([
            "-i", str(voice), "-f", "lavfi", "-i", _click_filter(rhythm, beats * rhythm.beat_s),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(out),
        ])

    tempos = [s["tempo"] for s in stats]
    info = dict(last.info or {})
    info["rhythm"] = {
        "bpm": rhythm.bpm,
        "style": rhythm.style,
        "beats_per_bar": rhythm.beats_per_bar,
        "beat": rhythm.beat,
        "phrases": len(parts),
        "beats": beats,
        "bars": round(beats / rhythm.beats_per_bar, 2),
        "duration_s": round(beats * rhythm.beat_s, 3),
        "tempo_mean": round(sum(tempos) / len(tempos), 3),
        "tempo_min": min(tempos),
        "tempo_max": max(tempos),
        # Fráze, které se nevešly a dostaly doby navíc.
        "overflowed": sum(1 for s in stats if s["beats"] > rhythm.phrase_beats),
        "phrase_beats": rhythm.phrase_beats,
        "stretch": "rubberband" if _has_rubberband() else "atempo",
        # Naměřené tempo řeči enginu v tomhle čtení.
        # (slabiky, délka před natažením, tempo, doby) každé fráze.
        "pieces": [[n, s["natural_s"], s["tempo"], s["beats"]] for n, s in zip(counts, stats)],
        # Kolikrát engine frázi přetáhl do šumu a musela se namluvit znovu.
        "retries": sum(int(s["retries"]) for s in stats),
        "overhead_s": round(fit_speech(measured, spec.language)[0], 2),
        "syllables_per_s": round(fit_speech(measured, spec.language)[1], 2),
    }
    return RawAudio(path=out, seed=last.seed, model=last.model, info=info)

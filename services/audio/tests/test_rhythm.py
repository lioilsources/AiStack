"""Řeč na mřížce — slabiky, dělení na fráze, zarovnání. Bez modelů a GPU."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.backends.base import RawAudio
from app.backends.ttshttp import TtsSpec
from app.postproc import duration_s
from app.tts.rhythm import (
    MAX_TEMPO,
    MIN_PHRASE_SYLLABLES,
    MIN_TEMPO,
    MIN_WINDOW_S,
    fit_speech,
    STYLES,
    Rhythm,
    fit_tempo,
    generate_on_grid,
    phrase_syllables,
    plan_phrases,
    syllables,
)
from tests.conftest import make_tone
from tests.test_tts import _await, client  # noqa: F401  (fixture)

ARTICLE = (
    "Vědci z brněnské univerzity oznámili, že se jim podařilo vyvinout nový materiál, "
    "který dokáže ukládat sluneční energii po dobu několika měsíců a uvolnit ji přesně "
    "ve chvíli, kdy je potřeba. Výzkum trval sedm let. Teď hledají partnera pro výrobu!"
)


# --- slabiky ---


@pytest.mark.parametrize("word,count", [
    ("vědci", 2), ("univerzity", 5), ("oznámili", 4), ("materiál", 4), ("sluneční", 3),
    ("vlk", 1), ("krk", 1), ("vítr", 2), ("nesl", 2), ("prst", 1), ("smrt", 1),
    ("louka", 2), ("auto", 2), ("neutron", 2), ("pneumatika", 4),
    ("měsíců", 3), ("potřeba", 3), ("s", 1), ("k", 1), ("a", 1),
    ("kteří", 2), ("čtvrtek", 2), ("zmrzlina", 3),
])
def test_czech_syllables(word, count):
    assert syllables(word, "cs") == count


@pytest.mark.parametrize("word,count", [
    ("scientists", 2), ("material", 4), ("energy", 3), ("make", 1), ("time", 1),
    ("table", 2), ("the", 1), ("months", 1), ("announced", 2), ("university", 5),
])
def test_english_syllables_are_close(word, count):
    # Heuristika: o slabiku vedle být smí (announced čte jako tři).
    assert abs(syllables(word, "en") - count) <= 1


def test_numbers_count_as_a_word():
    assert syllables("v roce 2026", "cs") == 1 + 2 + 3


# --- dělení na fráze ---


@pytest.mark.parametrize("style", sorted(STYLES))
@pytest.mark.parametrize("bpm", [60, 90, 140, 200])
def test_phrases_keep_every_word_in_order(style, bpm):
    rhythm = Rhythm(bpm=bpm, style=style)
    phrases = plan_phrases(ARTICLE, rhythm, "cs")
    assert " ".join(phrases).split() == ARTICLE.split()
    target = phrase_syllables(rhythm, "cs")
    longest_word = max(syllables(w, "cs") for w in ARTICLE.split())
    for phrase in phrases:
        # Fráze smí cíl přetáhnout o to, co se nedá rozdělit (slovo) a o
        # útržek, který by jinak zbyl sám.
        assert syllables(phrase, "cs") <= target * 1.15 + longest_word + MIN_PHRASE_SYLLABLES


def test_sentence_end_ends_a_phrase():
    text = 'Dneska ráno pršelo. Odpoledne vyšlo slunce! Půjdeme tedy ven? „Jistě, že půjdeme."'
    phrases = plan_phrases(text, Rhythm(bpm=60, style="preacher"), "cs")
    assert phrases == [
        "Dneska ráno pršelo.", "Odpoledne vyšlo slunce!", "Půjdeme tedy ven?", '„Jistě, že půjdeme."',
    ]


def test_no_scraps():
    # Jednoslovné věty se spojí s další, zbytek věty zůstane u své fráze —
    # z útržku pod pět slabik dělá Chatterbox desítky sekund šumu.
    rhythm = Rhythm(bpm=90, style="rap")
    phrases = plan_phrases("Ano. Ne! Opravdu? Tak jo, půjdeme tam hned zítra ráno.", rhythm, "cs")
    assert phrases[0].startswith("Ano. Ne! Opravdu?")
    for style in STYLES:
        for bpm in (60, 90, 140, 200):
            phrases = plan_phrases(ARTICLE, Rhythm(bpm=bpm, style=style), "cs")
            assert all(syllables(p, "cs") >= MIN_PHRASE_SYLLABLES for p in phrases), (style, bpm, phrases)
    # Text, který je celý útržek, se namluví, jak je.
    assert plan_phrases("Ahoj.", rhythm, "cs") == ["Ahoj."]


def test_faster_pace_puts_more_text_in_the_same_time():
    def density(style: str) -> float:
        rhythm = Rhythm(bpm=90, style=style)
        return phrase_syllables(rhythm, "cs") / rhythm.window_s

    assert density("rap") > density("spoken") > density("slam")


@pytest.mark.parametrize("style", sorted(STYLES))
@pytest.mark.parametrize("bpm", [40, 75, 120, 180, 220])
def test_fast_tempo_spans_more_beats_instead_of_shredding_text(style, bpm):
    rhythm = Rhythm(bpm=bpm, style=style)
    assert rhythm.window_s >= MIN_WINDOW_S
    # Slot zůstává celý násobek slotu stylu, takže fráze začíná na těžké době.
    assert rhythm.phrase_beats % STYLES[style].phrase_beats == 0
    assert phrase_syllables(rhythm, "cs") >= MIN_PHRASE_SYLLABLES


def test_fit_speech_separates_onset_from_rate():
    # Délky frází klonu ze SPARKu (2026-10-10): krátká fráze je „pomalá" jen
    # kvůli náběhu, ne proto, že by engine mluvil pomaleji.
    measured = [(5, 1.81), (6, 2.28), (9, 2.81), (8, 2.28), (11, 3.08)]
    overhead, rate = fit_speech(measured, "cs")
    assert 0.5 <= overhead <= 1.3
    assert 4.0 <= rate <= 7.5
    naive = sum(n for n, _ in measured) / sum(d for _, d in measured)
    assert rate > naive * 1.3


def test_fit_speech_survives_garbage():
    # Engine jednu frázi protáhl do šumu, všechny jsou stejně dlouhé, …
    assert fit_speech([], "cs")[1] > 0
    for samples in ([(6, 40.0)], [(6, 2.0)] * 6, [(4, 9.0), (12, 1.0), (5, 8.0), (11, 0.5)]):
        overhead, rate = fit_speech(samples, "cs")
        assert 0.0 <= overhead <= 1.5 and 2.5 <= rate <= 9.0


def test_nothing_to_read():
    assert plan_phrases("— … !", Rhythm(bpm=90), "cs") == []
    assert plan_phrases("", Rhythm(bpm=90), "cs") == []


# --- tempo ---


def test_fit_tempo_stretches_to_the_window():
    rhythm = Rhythm(bpm=80, style="rap")  # doba 0,75 s; okno 3,5 doby = 2,625 s
    assert rhythm.phrase_beats == 4
    tempo, beats = fit_tempo(2.625, rhythm)
    assert tempo == pytest.approx(1.0) and beats == 4
    tempo, beats = fit_tempo(3.15, rhythm)
    assert tempo == pytest.approx(1.2) and beats == 4


def test_fit_tempo_never_leaves_the_limits():
    rhythm = Rhythm(bpm=80, style="rap")  # slot 4 doby = 3,0 s
    # Krátká fráze se nenatahuje do nesrozumitelna: zůstane kratší než okno.
    tempo, beats = fit_tempo(0.5, rhythm)
    assert tempo == MIN_TEMPO and beats == 4
    # Dlouhá: nejvyšší tempo, přeteče do pauzy (4,4/1,5 = 2,93 s < 3 s slotu).
    tempo, beats = fit_tempo(4.4, rhythm)
    assert tempo == MAX_TEMPO and beats == 4
    # Nevejde se ani tak: slot se prodlouží o celé doby, mřížka drží.
    tempo, beats = fit_tempo(6.0, rhythm)
    assert tempo == MAX_TEMPO and beats == 6


# --- skládání ---


class _Speaker:
    """Engine, který „mluví" 0,5 s náběhu + 0,2 s na slabiku, s tichem na krajích."""

    def __init__(self) -> None:
        self.calls: list[TtsSpec] = []

    def generate(self, spec: TtsSpec, workdir: Path) -> RawAudio:
        self.calls.append(spec)
        stereo = make_tone(workdir / "s.wav", seconds=0.5 + 0.2 * syllables(spec.text, "cs"), silence_pad=0.3)
        path = workdir / "raw.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(stereo), "-ac", "1", str(path)], check=True)
        return RawAudio(path=path, seed=7, model=spec.model, info={"engine": spec.engine})


def _spec(text: str, **params) -> TtsSpec:
    return TtsSpec(text=text, language="cs", engine="chatterbox", model="chatterbox-cs",
                   voice="x", seed=7, params=params)


@pytest.mark.parametrize("bpm,style,beat", [(90, "rap", False), (120, "spoken", True), (64, "slam", False)])
def test_output_lands_on_the_grid(tmp_path, bpm, style, beat):
    speaker = _Speaker()
    rhythm = Rhythm(bpm=bpm, style=style, beat=beat)
    raw = generate_on_grid(speaker.generate, _spec(ARTICLE), rhythm, tmp_path)
    info = raw.info["rhythm"]
    assert info["phrases"] == len(speaker.calls)
    # Nic se neztratilo ani nepřehodilo.
    assert " ".join(c.text for c in speaker.calls).split() == ARTICLE.split()
    # Engine „mluví" 0,5 s + 5 slabik/s; z prvních frází se to dopočítá.
    assert info["syllables_per_s"] == pytest.approx(5.0, rel=0.1)
    assert info["overhead_s"] == pytest.approx(0.5, abs=0.15)
    # Na vzorek přesně: celé doby, žádný drift přes desítky frází.
    assert duration_s(raw.path) == pytest.approx(info["beats"] * 60 / bpm, abs=0.002)
    assert info["overflowed"] == 0
    assert info["beats"] == int(info["beats"])
    assert MIN_TEMPO <= info["tempo_min"] <= info["tempo_max"] <= MAX_TEMPO
    # Fráze nesedí na dorazu natažení. Průměr vychází pod hustotou stylu,
    # protože konec věty frázi ukončí dřív, než se naplní.
    assert MIN_TEMPO < info["tempo_mean"] <= STYLES[style].pace + 0.2
    assert raw.seed == 7 and raw.info["engine"] == "chatterbox"


def test_runaway_phrase_is_retried_with_another_seed(tmp_path):
    class Babbler(_Speaker):
        """První pokus o třetí frázi přetáhne do půl minuty „šumu"."""

        def generate(self, spec, workdir):
            if len({c.text for c in self.calls}) == 2 and spec.seed == 7:
                self.calls.append(spec)
                path = workdir / "raw.wav"
                stereo = make_tone(workdir / "s.wav", seconds=28.0)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(stereo), "-ac", "1", str(path)], check=True)
                return RawAudio(path=path, seed=7, model=spec.model, info={})
            return super().generate(spec, workdir)

    speaker = Babbler()
    rhythm = Rhythm(bpm=90, style="spoken")
    raw = generate_on_grid(speaker.generate, _spec(ARTICLE), rhythm, tmp_path)
    info = raw.info["rhythm"]
    assert info["retries"] == 1 and info["overflowed"] == 0
    assert [c.seed for c in speaker.calls][2:4] == [7, 8]
    # Text se neopakuje ani neztrácí, i když se jedna fráze mluvila dvakrát.
    said = [c.text for i, c in enumerate(speaker.calls) if i != 2]
    assert " ".join(said).split() == ARTICLE.split()
    assert duration_s(raw.path) == pytest.approx(info["beats"] * 60 / 90, abs=0.002)


def test_runaway_that_never_stops_is_cut_not_kept(tmp_path):
    def babble(spec, workdir):
        path = workdir / "raw.wav"
        stereo = make_tone(workdir / "s.wav", seconds=30.0)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(stereo), "-ac", "1", str(path)], check=True)
        return RawAudio(path=path, seed=spec.seed, model=spec.model, info={})

    raw = generate_on_grid(babble, _spec("Ahoj světe, jak se máš."), Rhythm(bpm=90, style="spoken"), tmp_path)
    info = raw.info["rhythm"]
    assert info["retries"] == 2
    assert duration_s(raw.path) < 12.0


def test_style_sets_delivery_unless_the_caller_did(tmp_path):
    speaker = _Speaker()
    generate_on_grid(speaker.generate, _spec("Ahoj světe.", cfg_weight=0.9), Rhythm(bpm=90, style="rap"), tmp_path)
    params = speaker.calls[0].params
    assert params["exaggeration"] == STYLES["rap"].exaggeration
    assert params["cfg_weight"] == 0.9
    # Každá fráze stejným seedem — hlas se mezi nimi nemá měnit.
    assert {c.seed for c in speaker.calls} == {7}


# --- API ---


def test_tts_with_rhythm_end_to_end(client):  # noqa: F811
    body = {"text": ARTICLE, "language": "cs", "rhythm": {"bpm": 100, "style": "rap", "beat": True}}
    resp = client.post("/v1/audio/tts", json=body)
    assert resp.status_code == 202, resp.text
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done", job["error"]
    info = job["result"]["variations"][0]["runtime"]["rhythm"]
    assert info["bpm"] == 100 and info["beat"] is True and info["phrases"] > 3
    # Konec se neořezává: výstup má celé doby i s pauzou poslední fráze.
    assert job["outputs"][0]["duration"] == pytest.approx(info["beats"] * 0.6, abs=0.02)
    assert job["result"]["request"]["rhythm"]["style"] == "rap"


def test_tts_without_rhythm_is_unchanged(client):  # noqa: F811
    resp = client.post("/v1/audio/tts", json={"text": "Ahoj.", "language": "cs"})
    job = _await(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert len(client.tts.calls) == 1
    assert "rhythm" not in job["result"]["variations"][0]["runtime"]


def test_rhythm_rejects_bad_input_before_queueing(client):  # noqa: F811
    bad_bpm = client.post("/v1/audio/tts", json={"text": "Ahoj.", "language": "cs", "rhythm": {"bpm": 10}})
    assert bad_bpm.status_code == 422
    empty = client.post("/v1/audio/tts", json={"text": "— …", "language": "cs", "rhythm": {"bpm": 90}})
    assert empty.status_code == 422 and "není co číst" in empty.text
    long_text = " ".join(["Venku dneska prší."] * 260)
    too_long = client.post("/v1/audio/tts", json={"text": long_text, "language": "cs", "rhythm": {"bpm": 90}})
    assert too_long.status_code == 422 and "po částech" in too_long.text
    assert client.tts.calls == []

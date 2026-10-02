"""Uložené hlasy postav — referenční vzorky pro klonování (XTTS, Chatterbox).

Hlas se nahraje jednou (`POST /v1/audio/voices`) a pak se na něj odkazuje
jménem, např. `{"voice": "smug-cat"}` — MemeShorts tak má jeden hlas na
postavu, aniž by vzorek posílal s každou replikou.

Vzorek se hned převede na mono WAV 24 kHz a ořízne na `MAX_REF_S`:
Chatterbox z reference bere jen prvních 10 s (s3gen) a 6 s (T3), XTTS
nejvýš 30 s — delší upload by jen zabíral místo a zpomaloval upload do
modelového kontejneru.

Klonovat cizí hlas bez svolení nejde — proto je u každého vzorku povinné
prohlášení o právech (`rights`) a zdroj (`source`). Služba to neověří, ale
zůstane to zapsané u hlasu a jde to do manifestu každého jobu.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..postproc import FFMPEG, duration_s

MAX_UPLOAD_BYTES = 30 * 1024 * 1024
MIN_REF_S = 3.0
MAX_REF_S = 30.0
REF_SAMPLE_RATE = 24000

VOICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
RIGHTS = ("own", "consented", "licensed", "synthetic")


class VoiceError(ValueError):
    pass


@dataclass(frozen=True)
class StoredVoice:
    voice_id: str
    name: str
    language: str
    gender: str
    rights: str
    source: str
    note: str
    duration_s: float
    sha256: str
    created_at: float

    def public(self) -> dict[str, Any]:
        return asdict(self)


class VoiceStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _dir(self, voice_id: str) -> Path:
        if not VOICE_ID_RE.match(voice_id):
            raise VoiceError(f"neplatné id hlasu {voice_id!r} (malá písmena, číslice, - a _)")
        return self.root / voice_id

    def ref_path(self, voice_id: str) -> Path | None:
        try:
            path = self._dir(voice_id) / "ref.wav"
        except VoiceError:
            return None
        return path if path.is_file() else None

    def get(self, voice_id: str) -> StoredVoice | None:
        try:
            meta = self._dir(voice_id) / "meta.json"
        except VoiceError:
            return None
        if not meta.is_file():
            return None
        return StoredVoice(**json.loads(meta.read_text()))

    def list(self) -> list[StoredVoice]:
        out = []
        for meta in sorted(self.root.glob("*/meta.json")):
            try:
                out.append(StoredVoice(**json.loads(meta.read_text())))
            except (ValueError, TypeError):
                continue
        return out

    def delete(self, voice_id: str) -> bool:
        path = self._dir(voice_id)
        if not path.is_dir():
            return False
        with self._lock:
            shutil.rmtree(path)
        return True

    def ingest(
        self,
        voice_id: str,
        data: bytes,
        *,
        name: str = "",
        language: str = "",
        gender: str = "",
        rights: str,
        source: str,
        note: str = "",
        replace: bool = False,
    ) -> StoredVoice:
        target = self._dir(voice_id)
        if rights not in RIGHTS:
            raise VoiceError(f"rights musí být jedno z {', '.join(RIGHTS)} — bez svolení se hlas neklonuje")
        if len(source.strip()) < 3:
            raise VoiceError("chybí source — odkud vzorek je (kdo mluví, kdy a s jakým svolením)")
        if not data:
            raise VoiceError("prázdný vzorek")
        if len(data) > MAX_UPLOAD_BYTES:
            raise VoiceError(f"vzorek je větší než {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        if target.exists() and not replace:
            raise VoiceError(f"hlas {voice_id!r} už existuje (replace=true ho přepíše)")

        with tempfile.TemporaryDirectory(prefix="tts-voice-") as tmpdir:
            tmp = Path(tmpdir)
            src = tmp / "upload"
            src.write_bytes(data)
            ref = tmp / "ref.wav"
            # Ořez ticha na začátku, mono, 24 kHz, nejvýš MAX_REF_S. Normalizace
            # na −20 LUFS: XTTS i Chatterbox jsou citlivé na hlasitost reference
            # a uživatel nahraje cokoli od šepotu po klipující mikrofon.
            proc = subprocess.run(
                [
                    FFMPEG, "-hide_banner", "-nostats", "-v", "error", "-y", "-i", str(src),
                    "-af",
                    "silenceremove=start_periods=1:start_threshold=-45dB:detection=peak,"
                    "loudnorm=I=-20:TP=-2:LRA=11",
                    "-ac", "1", "-ar", str(REF_SAMPLE_RATE), "-t", f"{MAX_REF_S:.1f}",
                    "-c:a", "pcm_s16le", str(ref),
                ],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0 or not ref.is_file():
                raise VoiceError(f"vzorek se nepodařilo dekódovat: {proc.stderr.strip()[-300:]}")
            seconds = duration_s(ref)
            if seconds < MIN_REF_S:
                raise VoiceError(f"vzorek má po ořezu ticha jen {seconds:.1f} s, potřeba je aspoň {MIN_REF_S:.0f} s řeči")

            ref_bytes = ref.read_bytes()
            voice = StoredVoice(
                voice_id=voice_id,
                name=name.strip() or voice_id,
                language=language.strip().lower(),
                gender=gender.strip().lower()[:1],
                rights=rights,
                source=source.strip(),
                note=note.strip(),
                duration_s=round(seconds, 2),
                sha256=hashlib.sha256(ref_bytes).hexdigest(),
                created_at=time.time(),
            )
            with self._lock:
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(parents=True)
                (target / "ref.wav").write_bytes(ref_bytes)
                (target / "meta.json").write_text(json.dumps(voice.public(), ensure_ascii=False, indent=1))
        return voice

"""Adaptér na ACE-Step 1.5 REST API (kontejner audio-music).

ACE-Step má vlastní frontu: POST /release_task vrátí task_id, POST /query_result
se ptá na stav a v hotovém výsledku jsou cesty k audiu, které se stáhnou přes
GET /v1/audio?path=…. Tenhle modul to celé překlopí na jedno synchronní
`generate()`, protože frontu už drží services/audio a dvě fronty nad sebou by
jen znásobily místa, kde se job může ztratit.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from .base import Backend, BackendError, BackendUnavailable, GenSpec, RawAudio

log = logging.getLogger(__name__)

# Turbo DiT jede na 8 krocích; víc jich kvalitu nezvedne, jen prodlouží job.
_TURBO_STEPS = 8
_POLL_INTERVAL_S = 2.0


class AceStepBackend(Backend):
    name = "acestep"
    kind = "music"

    def __init__(self, base_url: str, default_model: str, timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout_s = timeout_s
        self._client = httpx.Client(base_url=self.base_url, timeout=60.0)

    def close(self) -> None:
        self._client.close()

    # --- Backend protocol ---

    def health(self) -> tuple[bool, str]:
        try:
            resp = self._client.get("/health", timeout=5.0)
        except httpx.HTTPError as exc:
            return False, f"nedostupný: {exc}"
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        return True, resp.text[:200]

    def loaded_models(self) -> list[str]:
        try:
            resp = self._client.get("/v1/models", timeout=5.0)
            resp.raise_for_status()
        except httpx.HTTPError:
            return []
        return _model_names(resp.json())

    def generate(self, spec: GenSpec, workdir: Path) -> RawAudio:
        model = spec.model or self.default_model
        payload: dict[str, Any] = {
            "prompt": spec.prompt,
            "lyrics": "" if spec.instrumental else spec.lyrics,
            "audio_duration": round(spec.duration_s, 2),
            "inference_steps": _TURBO_STEPS,
            "model": model,
            # thinking=True nechá 5Hz LM naplánovat strukturu skladby. U 40s
            # herní smyčky to je rozdíl mezi „nekonečné intro" a stopou, která
            # má vlastní tvar. U coveru ho ACE-Step přeskočí sám (forma je
            # daná předlohou).
            "thinking": spec.thinking,
            "audio_format": "wav",
            "batch_size": 1,
        }
        if spec.bpm:
            payload["bpm"] = spec.bpm
        if spec.key:
            payload["key_scale"] = spec.key
        if spec.time_signature:
            payload["time_signature"] = spec.time_signature
        if spec.vocal_language:
            payload["vocal_language"] = spec.vocal_language
        if not spec.rewrite_caption:
            payload["use_cot_caption"] = False
        if spec.task_type != "text2music":
            payload["task_type"] = spec.task_type
        if spec.task_type == "cover":
            payload["audio_cover_strength"] = spec.cover_strength
        if spec.seed is None:
            payload["use_random_seed"] = True
        else:
            payload["use_random_seed"] = False
            payload["seed"] = spec.seed

        files: dict[str, Path] = {}
        if spec.reference_audio is not None:
            files["reference_audio"] = spec.reference_audio
        if spec.src_audio is not None:
            files["src_audio"] = spec.src_audio

        task_id = self._submit(payload, files)
        result = self._await_result(task_id)
        files_out = _files(result["entries"])
        if not files_out:
            raise BackendError(
                f"ACE-Step vrátil hotový job bez audia: {json.dumps(result['entries'])[:400]}"
            )

        dst = workdir / "acestep.wav"
        self._download(files_out[0], dst)
        # /query_result seed nevrací — proto se posílá vždy explicitní
        # (jobs.Runner ho dopočítá, i když ho volající nezadal), jinak by
        # stopa nešla zreprodukovat.
        seed = _first_seed(_seed_from(result), spec.seed)
        return RawAudio(path=dst, seed=seed, model=model, info=_generation_info(result))

    def analyze(self, audio: Path) -> dict[str, Any]:
        """Poslech předlohy: 5Hz LM z ní vyčte caption, tempo, tóninu a text.

        ACE-Step to umí jako `full_analysis_only` job — předlohu zakóduje do
        audio kódů a LM je „přečte" zpátky do metadat. Běží ve stejné frontě
        jako generování, takže se čeká stejně.
        """
        task_id = self._submit({"full_analysis_only": True}, {"src_audio": audio})
        result = self._await_result(task_id)
        for entry in result["entries"]:
            if entry.get("status_message") == _ANALYSIS_OK or "metas" in entry:
                out = dict(entry)
                # Kódy jsou tisíce znaků tokenů; nikdo mimo runtime je nečte.
                out.pop("audio_codes", None)
                return out
        raise BackendError(f"ACE-Step analýza nevrátila metadata: {json.dumps(result['entries'])[:400]}")

    # --- interní ---

    def _submit(self, payload: dict[str, Any], files: dict[str, Path] | None = None) -> str:
        try:
            if files:
                # Soubor se dá předat jen uploadem: modelový kontejner nevidí
                # na disk orchestrátoru a cestu na serveru by stejně odmítl.
                form = {k: _form_value(v) for k, v in payload.items()}
                handles = {name: path.open("rb") for name, path in files.items()}
                try:
                    resp = self._client.post(
                        "/release_task",
                        data=form,
                        files={name: (files[name].name, fh) for name, fh in handles.items()},
                        timeout=120.0,
                    )
                finally:
                    for fh in handles.values():
                        fh.close()
            else:
                resp = self._client.post("/release_task", json=payload, timeout=60.0)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Kontejner neběží (DNS jméno `audio-music` se v síti ai bez něj
            # nepřeloží) nebo nepřijímá spojení.
            raise BackendUnavailable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"ACE-Step /release_task nedostupný: {exc}") from exc
        if resp.status_code == 429:
            raise BackendError("ACE-Step fronta je plná")
        if resp.status_code != 200:
            raise BackendError(f"ACE-Step /release_task → HTTP {resp.status_code}: {resp.text[:300]}")
        data = _unwrap(resp.json())
        task_id = data.get("task_id")
        if not task_id:
            raise BackendError(f"ACE-Step nevrátil task_id: {resp.text[:300]}")
        return str(task_id)

    def _await_result(self, task_id: str) -> dict[str, Any]:
        """Poll /query_result, dokud job neskončí.

        Odpověď je `{code, data:[{task_id, result, status, progress_text}]}`,
        kde `result` je JSON **řetězec** se seznamem položek a `status` je
        číslo (0 = ve frontě/běží, 1 = hotovo, 2 = chyba). Cesta k audiu je
        v položce pod `file` jako relativní URL `/v1/audio?path=…`.
        """
        deadline = time.monotonic() + self.timeout_s
        last = "bez odpovědi"
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_S)
            try:
                resp = self._client.post(
                    "/query_result",
                    json={"task_id_list": json.dumps([task_id])},
                    timeout=30.0,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                last = str(exc)
                continue

            item = _first_item(_unwrap(resp.json()))
            if item is None:
                last = "prázdná odpověď query_result"
                continue

            status = _status_code(item)
            entries = _result_entries(item)
            if status == _STATUS_FAILED:
                raise BackendError(f"ACE-Step job selhal: {_error_text(item, entries)}")
            if status == _STATUS_DONE or _files(entries):
                return {"files": _files(entries), "entries": entries, "item": item}
            last = item.get("progress_text") or f"status={status}"
        raise BackendError(
            f"ACE-Step job {task_id} nedoběhl do {self.timeout_s:.0f}s (poslední stav: {last})"
        )

    def _download(self, url: str, dst: Path) -> None:
        try:
            with self._client.stream("GET", url, timeout=300.0) as resp:
                if resp.status_code != 200:
                    raise BackendError(f"stažení audia: {url} → HTTP {resp.status_code}")
                with dst.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise BackendError(f"stažení audia z ACE-Step selhalo: {exc}") from exc
        if dst.stat().st_size == 0:
            raise BackendError(f"stažení audia: {url} vrátilo prázdný soubor")


_STATUS_DONE = 1
_STATUS_FAILED = 2
# status_message, kterým ACE-Step značí hotovou analýzu (job_analysis_runtime).
_ANALYSIS_OK = "Full Hardware Analysis Success"


def _form_value(value: Any) -> str:
    """Multipart pole jsou text; ACE-Step bool čte z "true"/"false"."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _generation_info(result: dict[str, Any]) -> dict[str, Any]:
    """Co z výsledku patří do manifestu: modely a metadata, ne cesty."""
    for entry in result.get("entries") or []:
        info = {k: entry[k] for k in ("dit_model", "lm_model", "metas") if entry.get(k)}
        if info:
            return info
    return {}


def _status_code(item: dict[str, Any]) -> int:
    """Číselný stav; textový (starší tvar) se přemapuje."""
    raw = item.get("status")
    if isinstance(raw, int):
        return raw
    return {"succeeded": _STATUS_DONE, "failed": _STATUS_FAILED, "error": _STATUS_FAILED}.get(
        str(raw).lower(), 0
    )


def _result_entries(item: dict[str, Any]) -> list[dict[str, Any]]:
    """`result` je JSON řetězec se seznamem položek; snese i hotový seznam."""
    raw = item.get("result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        return [raw]
    return [e for e in raw or [] if isinstance(e, dict)]


def _files(entries: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for entry in entries:
        if entry.get("file"):
            files.append(str(entry["file"]))
        for path in entry.get("audio_paths") or []:
            if path:
                files.append(str(path))
    return files


def _error_text(item: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("error"):
            return str(entry["error"])
    return str(item.get("progress_text") or item.get("status_message") or item)[:400]


# Klíče, podle kterých se pozná, že dict je už výsledek a ne obálka.
_RESULT_KEYS = frozenset({"task_id", "result", "status", "progress_text", "status_message"})


def _unwrap(payload: Any) -> Any:
    """ACE-Step balí odpovědi do {code, data, …}; jinde vrací holý objekt.

    Rozbaluje se podle toho, co v dictu je, ne podle přítomnosti `code`:
    obálka se napříč verzemi liší (někdy `code`, někdy `message`, někdy nic),
    kdežto jméno pole s výsledkem je stabilní.
    """
    if isinstance(payload, dict) and "data" in payload and not (_RESULT_KEYS & payload.keys()):
        return payload["data"]
    return payload


def _first_item(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else None
    if isinstance(data, dict):
        return data
    return None


def _seed_from(result: dict[str, Any]) -> Any:
    for entry in result.get("entries") or []:
        if entry.get("seed_value") is not None:
            return entry["seed_value"]
    return result.get("item", {}).get("seed_value")


def _first_seed(seed_value: Any, fallback: int | None) -> int | None:
    if seed_value is None:
        return fallback
    first = str(seed_value).split(",")[0].strip()
    try:
        return int(first)
    except ValueError:
        return fallback


def _model_names(payload: Any) -> list[str]:
    data = _unwrap(payload)
    if isinstance(data, dict):
        data = data.get("data", data.get("models", []))
    if not isinstance(data, list):
        return []
    names = []
    for entry in data:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("id") or entry.get("name") or entry.get("model")
            if name:
                names.append(str(name))
    return names

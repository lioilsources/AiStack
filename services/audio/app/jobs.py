"""Fronta jobů a workery.

Jedna fronta na kategorii (hudba / SFX): modely sedí každý ve svém kontejneru
a jeden job saturuje GPU, takže víc paralelních jobů na stejný model by se jen
pralo o paměť. Vlákna, ne asyncio — práce je čekání na HTTP a volání ffmpeg,
obojí blokující, a workerů jsou jednotky.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import random
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backends.base import Backend, BackendError, GenSpec
from .config import Config
from .postproc import FFmpegError, process
from .store import Store

if TYPE_CHECKING:
    from .vibe.sample import SampleStore

log = logging.getLogger(__name__)

_SEED_MAX = 2**31 - 1


@dataclass
class Job:
    job_id: str
    kind: str
    spec: GenSpec
    variations: int
    fmt: str
    mono: bool
    loop: bool
    # "generate" = /v1/audio/music|sfx; "vibe" = skladba z předlohy;
    # "analyze" = poslech předlohy (žádné audio, jen výsledek).
    task: str = "generate"
    # None = cíl kategorie z konfigurace.
    target_lufs: float | None = None
    # Další formáty vedle `fmt` ze stejného zpracování (vibe drží i WAV).
    extra_formats: tuple[str, ...] = ()
    # Základ manifestu; runner doplní varianty a časy a uloží ho k jobu.
    manifest: dict[str, Any] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class Runner:
    """Drží fronty, workery a mapování kind → backend."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        backends: dict[str, Backend],
        samples: SampleStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.backends = backends
        self.samples = samples
        self.queues: dict[str, queue.Queue[Job]] = {
            "music": queue.Queue(),
            "sfx": queue.Queue(),
        }
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._last_activity: dict[str, float] = {"music": 0.0, "sfx": 0.0}

    def start(self) -> None:
        counts = {"music": self.cfg.music_workers, "sfx": self.cfg.sfx_workers}
        for kind, n in counts.items():
            for i in range(max(1, n)):
                t = threading.Thread(
                    target=self._worker, args=(kind,), name=f"{kind}-worker-{i}", daemon=True
                )
                t.start()
                self._threads.append(t)
        log.info("workery nastartovány: %s", counts)

    def stop(self) -> None:
        self._stop.set()

    def submit(self, job: Job) -> int:
        q = self.queues[job.kind]
        q.put(job)
        return q.qsize()

    def queue_position(self, job_id: str, kind: str) -> int | None:
        with self.queues[kind].mutex:
            for idx, job in enumerate(list(self.queues[kind].queue)):
                if job.job_id == job_id:
                    return idx + 1
        return None

    def idle_seconds(self, kind: str) -> float | None:
        """Sekundy od posledního dokončeného jobu; None, když ještě žádný nebyl.

        Ne nekonečno: prošlo by to do JSON odpovědi jako `Infinity`, což není
        platný JSON a přísnější klient by na tom spadl.
        """
        last = self._last_activity.get(kind, 0.0)
        return time.time() - last if last else None

    # --- worker ---

    def _worker(self, kind: str) -> None:
        q = self.queues[kind]
        while not self._stop.is_set():
            try:
                job = q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_job(job)
            except Exception as exc:  # worker nesmí umřít na jednom jobu
                log.exception("job %s selhal", job.job_id)
                self.store.mark_error(job.job_id, str(exc))
            finally:
                self._last_activity[kind] = time.time()
                q.task_done()

    def _run_job(self, job: Job) -> None:
        backend = self.backends.get(job.kind)
        if backend is None:
            raise BackendError(f"backend pro {job.kind} není nakonfigurován")

        self.store.mark_running(job.job_id)
        if job.task == "analyze":
            from .vibe.jobs import run_analyze

            self.store.mark_done(job.job_id, [], result=run_analyze(backend, self.samples, job.payload))
            return

        started = time.monotonic()
        out_dir = Path(self.cfg.data_dir) / job.job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        target_lufs = job.target_lufs
        if target_lufs is None:
            target_lufs = self.cfg.music_lufs if job.kind == "music" else self.cfg.sfx_lufs
        outputs: list[dict[str, Any]] = []
        failures: list[str] = []
        generated: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix=f"audio-{job.kind}-") as tmpdir:
            tmp = Path(tmpdir)
            for idx in range(job.variations):
                # Seed se dopočítá tady, ne v backendu: do manifestu musí jít
                # číslo, kterým se dá stopa přesně zopakovat, i když si model
                # náhodný seed generuje sám a nevrátí ho.
                seed = job.spec.seed if job.spec.seed is not None else random.randint(0, _SEED_MAX)
                if job.variations > 1 and job.spec.seed is not None:
                    seed = (job.spec.seed + idx) % (_SEED_MAX + 1)
                spec = GenSpec(**{**job.spec.__dict__, "seed": seed})

                var_dir = tmp / f"v{idx}"
                var_dir.mkdir()
                try:
                    gen_started = time.monotonic()
                    raw = backend.generate(spec, var_dir)
                    gen_s = time.monotonic() - gen_started
                    name = f"{idx:02d}.{job.fmt}"
                    extra = [(out_dir / f"{idx:02d}.{fmt}", fmt) for fmt in job.extra_formats]
                    result = process(
                        raw.path,
                        out_dir / name,
                        fmt=job.fmt,
                        target_lufs=target_lufs,
                        true_peak_db=self.cfg.true_peak_db,
                        mono=job.mono,
                        # Ořez i u hudby, a hlavně před zacyklením: ACE-Step
                        # končí stopu doběhem do ticha, a to by prolnutí
                        # přeložilo na začátek smyčky — měřeno, šev pak skáče
                        # o 0,75 z rozsahu 0–1.
                        trim=True,
                        loop=job.loop,
                        crossfade_s=self.cfg.loop_crossfade_s,
                        # Jen u SFX: hudba smí vyjít kratší i delší, ale
                        # efekt delší, než se žádalo, se při rychlé palbě
                        # překrývá sám se sebou.
                        max_duration_s=spec.duration_s if job.kind == "sfx" else None,
                        extra=extra,
                    )
                except (BackendError, FFmpegError) as exc:
                    failures.append(f"varianta {idx}: {exc}")
                    log.warning("job %s varianta %d selhala: %s", job.job_id, idx, exc)
                    continue

                data = result.path.read_bytes()
                output = {
                    "url": f"/v1/audio/jobs/{job.job_id}/outputs/{name}",
                    "filename": name,
                    "duration": round(result.duration, 3),
                    "loudness_lufs": round(result.loudness_lufs, 2),
                    "true_peak_db": round(result.true_peak_db, 2),
                    "seed": raw.seed if raw.seed is not None else seed,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
                if extra:
                    output["alt"] = {fmt: _file_entry(job.job_id, path) for path, fmt in extra}
                outputs.append(output)
                generated.append({
                    "filename": name,
                    "seed": output["seed"],
                    "sha256": output["sha256"],
                    "generate_s": round(gen_s, 2),
                    "runtime": raw.info or {},
                })

        if not outputs:
            raise BackendError("; ".join(failures) or "žádný výstup")
        if failures:
            log.warning("job %s: %d/%d variant selhalo", job.job_id, len(failures), job.variations)

        result_doc = None
        if job.manifest is not None:
            result_doc = {
                **job.manifest,
                "variations": generated,
                "failures": failures,
                "elapsed_s": round(time.monotonic() - started, 2),
                "finished_at": time.time(),
            }
            (out_dir / "manifest.json").write_text(
                json.dumps(result_doc, ensure_ascii=False, indent=1)
            )
        self.store.mark_done(job.job_id, outputs, result=result_doc)


def _file_entry(job_id: str, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "url": f"/v1/audio/jobs/{job_id}/outputs/{path.name}",
        "filename": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def make_spec_from_request(kind: str, payload: dict[str, Any]) -> GenSpec:
    return GenSpec(
        prompt=payload["prompt"],
        duration_s=float(payload.get("duration_s", 40.0 if kind == "music" else 2.0)),
        seed=payload.get("seed"),
        lyrics=payload.get("lyrics", ""),
        instrumental=bool(payload.get("instrumental", True)),
        bpm=payload.get("bpm"),
        key=payload.get("key", ""),
        model=payload.get("model", ""),
    )

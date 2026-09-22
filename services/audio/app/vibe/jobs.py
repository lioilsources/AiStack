"""Běh vibe úloh ve frontě hudby.

Generování jede běžnou cestou `Runner._run_job` (varianty, seedy,
post-processing); tady je jen analýza, která audio nevyrábí.
"""

from __future__ import annotations

import time
from typing import Any

from ..backends.base import Backend, BackendError
from .analyze import analyze
from .sample import SampleStore


def run_analyze(backend: Backend, samples: SampleStore | None, payload: dict[str, Any]) -> dict[str, Any]:
    if samples is None:
        raise BackendError("úložiště předloh není nakonfigurováno")
    sample_id = payload["sample_id"]
    info = samples.get(sample_id)
    if info is None:
        raise BackendError("předloha na serveru už není — nahraj ji znovu")
    started = time.monotonic()
    result = analyze(samples.src(sample_id), info.duration_s, getattr(backend, "analyze", None))
    doc = {**result.to_dict(), "sample_id": sample_id, "elapsed_s": round(time.monotonic() - started, 2)}
    samples.save_analysis(sample_id, doc)
    return doc

"""Process-level CPU thread tuning.

IMPORT THIS FIRST — before numpy, before torch, before vox_core.

Note: the public repository is a desensitized mirror of this working tree, kept in sync
by tools/sync_public.ps1. Edit here, then run the sync script to publish changes.


Why this module exists
----------------------
`OMP_NUM_THREADS` / `MKL_NUM_THREADS` are read by the BLAS/OpenMP runtime when it
INITIALISES, which happens the first time numpy or torch is imported. Setting them
later has no effect, so the only place they can be set is at the very top of an entry
point — hence a module and not a function call buried in vox_core.

What it buys (measured on this project's own workload, SenseVoice-Small on CPU):

    torch default (one worker per core)   model load 22.58 s   latency 0.272/0.246/0.271 s
    capped to 8 threads                   model load  8.92 s   latency 0.233/0.233/0.240 s
    capped to 4 threads                   model load  9.08 s   latency 0.282/0.269/0.258 s

So a modest cap makes the local model load ~2.5x faster AND leaves inference latency
equal or slightly better. A small model on a many-core machine spends more time in
thread synchronisation than in arithmetic; the extra workers are pure overhead. It also
collapses the process's native thread count, which matters because the daemon keeps the
engine warmly loaded for streaming preview.

An explicitly set environment variable (or `VOX_CPU_THREADS`) always wins; this only
fills in a default.
"""

from __future__ import annotations

import os

DEFAULT_THREADS = 8

# Vars honoured by the common BLAS/OpenMP/threading runtimes shipped with numpy+torch.
_THREAD_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


def cpu_thread_budget() -> int:
    """How many compute threads this process should use (never raises)."""
    override = (os.environ.get("VOX_CPU_THREADS") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    try:
        cores = os.cpu_count() or 4
    except Exception:
        cores = 4
    return max(2, min(DEFAULT_THREADS, int(cores)))


def apply_thread_limits() -> int:
    """Fill in thread-count defaults for any var the user did not set."""
    n = cpu_thread_budget()
    for var in _THREAD_VARS:
        if not (os.environ.get(var) or "").strip():
            try:
                os.environ[var] = str(n)
            except Exception:
                pass
    return n


THREADS = apply_thread_limits()


def pin_torch_threads():
    """Also cap torch's own pools at runtime (covers an already-imported torch).

    The env vars above only apply if torch has not initialised yet. Calling this right
    before a model load makes the cap effective regardless of import order.
    """
    try:
        import torch
        torch.set_num_threads(THREADS)
        try:
            torch.set_num_interop_threads(max(1, THREADS // 2))
        except Exception:
            # set_num_interop_threads raises once the pool has been used; harmless.
            pass
        return True
    except Exception:
        return False

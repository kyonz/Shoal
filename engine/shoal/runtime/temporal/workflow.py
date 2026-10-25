"""The Temporal workflow that drives one Shoal run.

It is deterministic by construction because it holds no Shoal logic: every pass
over the run (steps, model and tool calls, sandboxes, store writes) is the
``shoal.advance`` activity. The workflow only decides when the next pass happens,
after a durable timer for the run's next wake-up or as soon as a ``wake`` signal
arrives (a Shoal signal, an approval, an integration event, a cancel, a child run
finishing). The run itself, including the workflow version it is pinned to, lives
in Shoal's store, so this code does not change when a Shoal workflow does.

This module is loaded in Temporal's workflow sandbox: import only the standard
library and ``temporalio`` here. Any change to the order of commands below needs
``workflow.patched`` so histories of runs already in flight still replay.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

WORKFLOW = "shoal.run"
ADVANCE = "shoal.advance"
CANCEL = "shoal.cancel"
WAKE = "wake"

# Start a fresh history (continue-as-new) after this many passes, or earlier when Temporal suggests it.
PASSES_PER_HISTORY = 500

# A pass that cannot get the run's lease, or whose worker died, is retried; Shoal's own step retries
# (retry: in the workflow) happen inside passes and are not Temporal retries.
PASS_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30))


def workflow_id(run_id: str) -> str:
    return f"shoal-run-{run_id}"


@workflow.defn(name=WORKFLOW)
class RunWorkflow:
    def __init__(self) -> None:
        self._wakes = 0

    @workflow.signal(name=WAKE)
    def wake(self) -> None:
        self._wakes += 1

    @workflow.query(name="wakes")
    def wakes(self) -> int:
        return self._wakes

    @workflow.run
    async def run(self, run_id: str, options: dict[str, Any]) -> str:
        """``options``: ``pass_timeout`` and ``heartbeat_timeout`` in seconds."""
        pass_options: dict[str, Any] = {
            "start_to_close_timeout": timedelta(seconds=options["pass_timeout"]),
            "heartbeat_timeout": timedelta(seconds=options["heartbeat_timeout"]),
            "retry_policy": PASS_RETRY,
        }
        try:
            for _ in range(PASSES_PER_HISTORY):
                seen = self._wakes
                result = await workflow.execute_activity(
                    ADVANCE, run_id, summary="advance run", **pass_options
                )
                if result["done"]:
                    return str(result["status"])
                wake_in = result["wake_in"]
                if self._wakes == seen:
                    try:
                        await workflow.wait_condition(
                            lambda seen=seen: self._wakes != seen,
                            timeout=timedelta(seconds=max(wake_in, 0.001)) if wake_in is not None else None,
                            timeout_summary="next wake-up",
                        )
                    except TimeoutError:
                        pass
                if workflow.info().is_continue_as_new_suggested():
                    break
        except asyncio.CancelledError:
            # Cancelled in Temporal: cancel the Shoal run the way the API does, then report cancelled.
            await workflow.execute_activity(CANCEL, run_id, summary="cancel run", **pass_options)
            raise
        workflow.continue_as_new(args=[run_id, options])

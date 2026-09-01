#!/usr/bin/env python3
"""Structured progress facade for the 5g-Ansible machine interface.

The deployment engine remains in ``fiveg_machine.py``.  This facade adds an
optional event channel without parsing Ansible output: ``--events`` emits
``fiveg/event/v1`` JSONL on stderr while stdout remains the final machine JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
MACHINE_PATH = ROOT / "tools" / "fiveg_machine.py"
EVENT_SCHEMA = "fiveg/event/v1"

_SPEC = importlib.util.spec_from_file_location("fiveg_machine_core", MACHINE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load fiveg machine implementation")
core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(core)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventEmitter:
    """Emit semantic machine progress as JSONL, never raw Ansible text."""

    def __init__(self, *, enabled: bool, stream=None) -> None:
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr

    def emit(
        self,
        deployment_id: str,
        phase: str,
        event: str,
        *,
        component: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        payload: dict[str, Any] = {
            "schema": EVENT_SCHEMA,
            "time": _utc_now(),
            "deployment_id": deployment_id,
            "phase": phase,
            "event": event,
        }
        if component:
            payload["component"] = component
        if detail:
            payload["detail"] = dict(detail)
        print(json.dumps(payload, sort_keys=True), file=self.stream, flush=True)


def _failure_detail(exc: BaseException) -> dict[str, str]:
    return {"message": str(exc)[-1000:]}


def _log_operation(log: Path | None) -> tuple[str, str] | None:
    """Classify machine-owned operations by their explicit evidence log."""

    if log is None:
        return None
    name = log.name
    if name == "collections.log":
        return "collections", "ansible-galaxy"
    if name == "r2lab.log":
        return "r2lab-deployment", "physical-resources"
    if name == "deploy.log":
        return "deployment", "5g-stack"
    if name == "down.log":
        return "cleanup", "5g-stack"
    if name.startswith("scenario-"):
        component = name.removesuffix(".log")
        return "scenario", component
    return None


def install_event_wrappers(emitter: EventEmitter) -> None:
    """Wrap semantic machine operations; deployment mechanics stay untouched."""

    original_run = core.run
    original_provider_context = core.provider_context
    original_reserve = core.reserve
    original_r2lab_authority = core.r2lab_authority

    def run(command, log=None, check=True):
        operation = _log_operation(log)
        deployment_id = log.parent.name if operation is not None and log is not None else ""
        if operation is not None:
            phase, component = operation
            emitter.emit(deployment_id, phase, "started", component=component)
        try:
            result = original_run(command, log=log, check=check)
        except Exception as exc:
            if operation is not None:
                phase, component = operation
                emitter.emit(
                    deployment_id,
                    phase,
                    "failed",
                    component=component,
                    detail=_failure_detail(exc),
                )
            raise
        if operation is not None:
            phase, component = operation
            event = "completed" if result.returncode == 0 else "failed"
            detail = None if result.returncode == 0 else {"returncode": result.returncode}
            emitter.emit(
                deployment_id,
                phase,
                event,
                component=component,
                detail=detail,
            )
        return result

    def provider_context(spec, state, *, create_missing):
        deployment_id = str(spec["id"])
        provider = spec["provider"]
        detail = {
            "project": str(provider.get("project", "")),
            "experiment": str(provider.get("experiment", deployment_id)),
        }
        emitter.emit(deployment_id, "provider", "started", component="slices", detail=detail)
        try:
            result = original_provider_context(spec, state, create_missing=create_missing)
        except Exception as exc:
            emitter.emit(
                deployment_id,
                "provider",
                "failed",
                component="slices",
                detail=_failure_detail(exc),
            )
            raise
        completed = dict(detail)
        current = state.get("provider")
        if isinstance(current, Mapping):
            completed["experiment_created"] = bool(current.get("experiment_created"))
        emitter.emit(
            deployment_id,
            "provider",
            "completed",
            component="slices",
            detail=completed,
        )
        return result

    def reserve(spec, state):
        deployment_id = str(spec["id"])
        emitter.emit(deployment_id, "reservation", "started", component="slices-calendar")
        try:
            result = original_reserve(spec, state)
        except Exception as exc:
            emitter.emit(
                deployment_id,
                "reservation",
                "failed",
                component="slices-calendar",
                detail=_failure_detail(exc),
            )
            raise
        reservation = state.get("slices_reservation")
        detail: dict[str, Any] = {}
        if isinstance(reservation, Mapping):
            nodes = reservation.get("nodes")
            if isinstance(nodes, list):
                detail["nodes"] = [str(value) for value in nodes]
        emitter.emit(
            deployment_id,
            "reservation",
            "completed",
            component="slices-calendar",
            detail=detail or None,
        )
        return result

    def r2lab_authority(spec, state):
        deployment_id = str(spec["id"])
        emitter.emit(deployment_id, "r2lab-authority", "started", component="r2lab")
        try:
            result = original_r2lab_authority(spec, state)
        except Exception as exc:
            emitter.emit(
                deployment_id,
                "r2lab-authority",
                "failed",
                component="r2lab",
                detail=_failure_detail(exc),
            )
            raise
        emitter.emit(deployment_id, "r2lab-authority", "completed", component="r2lab")
        return result

    core.run = run
    core.provider_context = provider_context
    core.reserve = reserve
    core.r2lab_authority = r2lab_authority


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    events = "--events" in arguments
    if events:
        arguments = [argument for argument in arguments if argument != "--events"]
        install_event_wrappers(EventEmitter(enabled=True))
    return int(core.main(arguments))


if __name__ == "__main__":
    raise SystemExit(main())

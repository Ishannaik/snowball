"""Idle-safe owner for already-started research goals; never grants execution."""
from __future__ import annotations

import threading
from pathlib import Path
from tools.generic_runtime import Service


def supervise(db_path: Path, goal_id: str | None = None, *, max_waves: int = 5,
              poll_sec: float = 60, stop_event: threading.Event | None = None,
              max_cycles: int | None = None) -> dict:
    if isinstance(poll_sec, bool) or not isinstance(poll_sec, (int, float)) or not 0 < poll_sec <= 1800:
        raise ValueError("poll_sec must be greater than zero and at most 1800")
    if max_cycles is not None and (isinstance(max_cycles, bool) or not isinstance(max_cycles, int) or max_cycles < 1):
        raise ValueError("max_cycles must be a positive integer")
    service = Service(db_path)
    stop = stop_event or threading.Event()
    cycles = 0
    result = {"outcome": "no_runnable_goal", "goals": []}
    try:
        while not stop.is_set():
            if (Path(__file__).resolve().parent.parent / "STOP").exists():
                return {"outcome": "user_stopped", "goals": result["goals"]}
            goals = [g for g in service.list() if g["state"] == "running" and (goal_id is None or g["id"] == goal_id)]
            results = []
            for goal in goals:
                if stop.is_set():
                    break
                view = service.run(goal["id"], max_waves=max_waves)
                results.append({"goal_id": goal["id"], "state": view["goal"]["state"], "outcome": view["goal"].get("outcome")})
            result = {"outcome": "processed" if results else "no_runnable_goal", "goals": results}
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                return result
            stop.wait(poll_sec)
    except KeyboardInterrupt:
        pass
    return {"outcome": "user_stopped", "goals": result["goals"]}

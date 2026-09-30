"""Independent Qt background tasks for local issues and PoC artifacts."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal


@dataclass
class ArtifactTask:
    job: Path
    marker_id: str
    kind: str
    label: str
    future: Future
    done: Callable[[Any], None]
    failed: Callable[[Exception], None]


class ArtifactTasks(QObject):
    changed = Signal()

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="qt-artifact")
        self.active: dict[tuple[Path, str], ArtifactTask] = {}
        self.closed = False
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.drain)

    def is_active(self, job: Path, marker_id: str) -> bool:
        return (job.resolve(), marker_id) in self.active

    def for_job(self, job: Path) -> bool:
        return any(key[0] == job.resolve() for key in self.active)

    def start(self, job: Path, marker_id: str, work: Callable[[], Any],
              done: Callable[[Any], None], failed: Callable[[Exception], None],
              *, kind: str, label: str) -> bool:
        key = (job.resolve(), marker_id)
        if self.closed:
            return False
        if key in self.active:
            return False
        self.active[key] = ArtifactTask(key[0], marker_id, kind, label,
                                        self.executor.submit(work), done, failed)
        self.timer.start()
        self.changed.emit()
        return True

    def drain(self) -> None:
        for key, task in list(self.active.items()):
            if not task.future.done():
                continue
            del self.active[key]
            try:
                result = task.future.result()
            except (Exception, SystemExit) as exc:
                task.failed(RuntimeError(str(exc)))
            else:
                task.done(result)
            self.changed.emit()
        if not self.active:
            self.timer.stop()

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.timer.timeout.disconnect(self.drain)
        self.changed.disconnect()
        self.executor.shutdown(wait=False, cancel_futures=True)

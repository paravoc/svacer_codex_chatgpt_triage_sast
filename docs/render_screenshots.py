"""Render README screenshots from disposable, invented data only.

No Codex, Svacer, network connection, user job, or credentials are consulted.
Requires the locked Poetry desktop dependency group.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
os.environ["SVACER_REDUCE_MOTION"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from PySide6.QtCore import QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import triage_gui_qt as gui  # noqa: E402
from triage_queue import GOST_FILTER  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def demo_job(root: Path) -> Path:
    app = root / "app"
    app.mkdir()
    job = root / "RESULTS" / "demo-project"
    job.mkdir(parents=True)
    write_json(job / "job.json", {
        "repository_url": "https://example.invalid/demo-gateway.git",
        "git_ref": "demo-v1", "parallel_workers": 1,
        "advanced_filter": GOST_FILTER,
    })
    examples = [
        ("m1", "NULL_DEREF", "src/example.go", 42, "Confirmed"),
        ("m2", "BOUNDS", "src/parser.go", 87, "False Positive"),
        ("m3", "NULL_DEREF", "src/helper.go", 63, "Won't fix"),
        ("m4", "RESOURCE_LEAK", "src/worker.go", 119, None),
        ("m5", "NULL_DEREF", "src/routes.go", 205, None),
        ("m6", "BOUNDS", "src/config.go", 74, None),
    ]
    markers = [{
        "id": mid, "review": "Undecided", "warnClass": kind,
        "file": file, "line": line,
        "msg": "Учебный пример для демонстрации интерфейса; это не реальное срабатывание Svace.",
    } for mid, kind, file, line, _ in examples]
    write_json(job / "markers.inventory.json", {
        "markers": markers, "truncated": False, "total_count": len(markers),
        "returned_count": len(markers),
        "filters_applied": {"advanced_filter": GOST_FILTER},
    })
    decisions = []
    for mid, kind, file, line, verdict in examples:
        row = {"marker_id": mid, "warnClass": kind, "file": file,
               "line": line, "verdict": verdict}
        if verdict:
            row.update({
                "entrypoint": "Демонстрационный вход: example()",
                "source": "Тестовое значение в учебном примере.",
                "control": "Показан разбор проверки перед операцией.",
                "sink": f"{file}:{line}",
                "build_reachability": "Учебная конфигурация.",
                "product_reachability": "Показана оценка достижимости для демонстрации.",
                "impact": "Учебный пример — не оценка реального продукта.",
                "evidence": ["Пример записи доказательства с указанием файла и строки."],
                "comment": "Демонстрационный комментарий. Не отправлять в Svacer.",
            })
            if verdict == "Confirmed":
                row.update(severity="Major", action="Fix Required")
        decisions.append(row)
    (job / "decisions.jsonl").write_text("".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in decisions
    ), encoding="utf-8")
    write_json(job / "control.json", {"priority_marker_ids": ["m4", "m5", "m6"]})
    (job / "marker-history.jsonl").write_text("".join(
        json.dumps({
            "attempt_id": f"demo-{index}", "job_id": "demo-project",
            "marker_id": mid, "warnClass": kind, "file": file, "line": line,
            "verdict": verdict, "status": "completed", "worker": 1,
            "started_at": f"2026-09-19T10:{index:02d}:00",
            "duration_seconds": 120 + index * 30,
            "attributed_tokens": 1500 + index * 200, "tokens_exact": True,
            "batch_marker_count": 1,
            "batch_usage": {"input_tokens": 1200 + index * 200, "output_tokens": 500},
            "batch_total_tokens": 1700 + index * 200,
            "requested_model": "Демонстрационная модель",
        }, ensure_ascii=False) + "\n"
        for index, (mid, kind, file, line, verdict) in enumerate(examples[:3], 1)
    ), encoding="utf-8")
    return job


def main() -> None:
    gui.read_codex_rate_limits = lambda: {
        "rateLimits": {"primary": {"usedPercent": 28, "windowDurationMins": 10080}}
    }
    gui.read_codex_models = lambda: [
        {"model": "demo-model", "display_name": "Демонстрационная модель"},
    ]
    gui.TriageQtWindow.check_connection = lambda self: None
    application = QApplication([])
    windows_font = Path("C:/Windows/Fonts/segoeui.ttf")
    if windows_font.is_file():
        QFontDatabase.addApplicationFont(str(windows_font))
    application.setStyle("Fusion")
    application.setStyleSheet(gui.STYLE)
    output = Path(__file__).resolve().parent / "screenshots"
    output.mkdir(exist_ok=True)

    def capture(window: gui.TriageQtWindow, name: str) -> None:
        QTest.qWait(240)
        application.processEvents()
        path = output / f"{name}.png"
        if not window.grab().save(str(path), "PNG"):
            raise RuntimeError(f"Не удалось сохранить {path}")
        print(path)

    with TemporaryDirectory(prefix="svacer-readme-demo-") as temporary:
        root = Path(temporary)
        job = demo_job(root)
        window = gui.TriageQtWindow(job, root / "app")
        try:
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            window.resize(1440, 900)
            window.show()
            if window._codex_limit_future is not None:
                window._codex_limit_future.result(timeout=5)
                window.drain_codex_limit()
            if window._jobs_future is not None:
                window._jobs_future.result(timeout=5)
                window.drain_jobs()
            window.connection.setText("Svacer: демонстрационный режим")
            for name, tab in (
                ("overview", window.overview_tab),
                ("markers", window.markers_tab),
                ("history", window.history_tab),
                ("settings", window.settings_tab),
            ):
                window.tabs.setCurrentWidget(tab)
                if name == "markers":
                    window.select_marker_row("m3")
                capture(window, name)

            window.tabs.setCurrentWidget(window.markers_tab)
            selection = window.marker_table.selectionModel()
            selection.clearSelection()
            for row in range(3, 6):
                selection.select(window.marker_table.model().index(row, 0),
                                 QItemSelectionModel.SelectionFlag.Select |
                                 QItemSelectionModel.SelectionFlag.Rows)
            window.update_action_states()
            capture(window, "multi-select")

            # An in-memory active worker and invented agent messages illustrate
            # the live view without launching Codex or touching a real job.
            (job / "codex-events.jsonl").write_text("".join(
                json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message", "text": message,
                }}, ensure_ascii=False) + "\n"
                for message in (
                    "Проверяю входную точку и происхождение значения в учебном примере.",
                    "Сопоставляю путь выполнения с условием перед операцией.",
                    "Фиксирую строки и условия для демонстрационного результата.",
                )
            ), encoding="utf-8")
            window.state.update({
                "codex_run": {"active": True, "phase": "analysis",
                              "phase_detail": "Разбираю маркер 1 из 3"},
                "workers": {1: {"marker_ids": ["m4"], "current_status": "working"}},
            })
            window.current_live_id = "m4"
            window.populate_work_queue()
            window.tabs.setCurrentWidget(window.overview_tab)
            window.run_status.setText("Фоновая задача: работает  •  Разбираю маркер 1 из 3")
            window.scope.setText("В снимке 6  •  ранее размечено 0  •  в этой задаче 6  •  очередь работает")
            window.update_action_states()
            if window.jobs_table.rowCount():
                window.jobs_table.item(0, 1).setText("Работает")
                window.jobs_table.item(0, 3).setText("1")
            capture(window, "in-progress")

            # Two local sample notifications show the persistent panel and its
            # colours. They contain no production finding or Svacer address.
            write_json(job / "ui-notifications.json", {
                "schema_version": 1,
                "seen": [f"attempt:demo-{index}" for index in range(1, 4)] +
                        ["attempt:demo-failure"],
                "pending": [
                    {"id": "attempt:demo-1", "marker_id": "m1",
                     "title": "Маркер просканирован", "tone": "green",
                     "subject": "NULL_DEREF · example.go:42",
                     "detail": "Результат: Confirmed",
                     "created_at": "2026-09-19T10:01:00"},
                    {"id": "attempt:demo-failure", "marker_id": "m6",
                     "title": "Ошибка маркера", "tone": "red",
                     "subject": "BOUNDS · config.go:74",
                     "detail": "Анализ завершился с ошибкой",
                     "created_at": "2026-09-19T10:04:00"},
                ],
            })
            window.refresh_notifications()
            capture(window, "notifications")
        finally:
            window.close()


if __name__ == "__main__":
    main()

"""Record a 20-second, silent UI walkthrough using disposable demo data only.

This grabs the Qt application window, not the user's desktop. It never connects
to Svacer or Codex and does not inspect any real job. Pass --ffmpeg if the
encoder is not on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import triage_gui_qt as gui  # noqa: E402
from render_screenshots import demo_job, write_json  # noqa: E402


FPS = 12
DURATION = 20
WIDTH = 1280
WINDOW_HEIGHT = 800
CAPTION_HEIGHT = 64


def caption_for(second: float) -> str:
    if second < 4:
        return "Очередь: выбраны только нужные маркеры"
    if second < 6:
        return "Ненужный маркер убираем из очереди"
    if second < 9:
        return "Карточка: статус, описание и доказательства"
    if second < 11:
        return "Выбранный маркер возвращаем в очередь"
    if second < 15:
        return "Один агент — один маркер в работе"
    if second < 17:
        return "История результатов, времени и токенов"
    if second < 19:
        return "Настройки агентов и модели"
    return "Уведомления остаются до открытия или скрытия"


def render_frame(window: gui.TriageQtWindow, second: float, target=None) -> bytes:
    source = window.grab().toImage().convertToFormat(QImage.Format_RGB888)
    scaled = source.scaled(WIDTH, WINDOW_HEIGHT, Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(1.0)
    frame = QImage(WIDTH, WINDOW_HEIGHT + CAPTION_HEIGHT, QImage.Format.Format_RGB888)
    frame.fill(QColor("#111317"))
    painter = QPainter(frame)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawImage(0, CAPTION_HEIGHT, scaled)
    if target is not None and target.isVisible():
        top_left = target.mapTo(window, QPoint(0, 0))
        sx, sy = WIDTH / window.width(), WINDOW_HEIGHT / window.height()
        rect = QRect(int(top_left.x() * sx) - 4,
                     CAPTION_HEIGHT + int(top_left.y() * sy) - 4,
                     int(target.width() * sx) + 8, int(target.height() * sy) + 8)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#f0c777"), 3))
        painter.drawRoundedRect(rect, 9, 9)
    painter.setPen(QColor("#e7e9ed"))
    painter.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
    painter.drawText(QRect(21, 8, WIDTH - 240, 47),
                     Qt.AlignmentFlag.AlignVCenter, caption_for(second))
    painter.setPen(QColor("#8abfb0"))
    painter.setFont(QFont("Segoe UI", 10))
    painter.drawText(QRect(WIDTH - 227, 8, 203, 47),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                     "ДЕМО · вымышленные данные")
    painter.end()
    if frame.bytesPerLine() != WIDTH * 3:
        raise RuntimeError("Неверный шаг RGB-кадра")
    return bytes(frame.bits())


def record(ffmpeg: Path, output: Path) -> None:
    if not ffmpeg.is_file():
        raise FileNotFoundError(f"Не найден FFmpeg: {ffmpeg}")
    gui.read_codex_rate_limits = lambda: {
        "rateLimits": {"primary": {"usedPercent": 28, "windowDurationMins": 10080}}
    }
    gui.read_codex_models = lambda: [
        {"model": "demo-model", "display_name": "Демонстрационная модель"},
    ]
    gui.TriageQtWindow.check_connection = lambda self: None
    gui.QMessageBox.question = lambda *_args: gui.QMessageBox.StandardButton.Yes
    application = QApplication([])
    windows_font = Path("C:/Windows/Fonts/segoeui.ttf")
    if windows_font.is_file():
        QFontDatabase.addApplicationFont(str(windows_font))
    application.setStyle("Fusion")
    application.setStyleSheet(gui.STYLE)
    with TemporaryDirectory(prefix="svacer-video-demo-") as temporary:
        root = Path(temporary)
        job = demo_job(root)
        window = gui.TriageQtWindow(job, root / "app")
        temporary_output = root / "demo.mp4"
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{WINDOW_HEIGHT + CAPTION_HEIGHT}",
            "-r", str(FPS), "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(temporary_output),
        ]
        try:
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            window.resize(1440, 900)
            window.show()
            window.timer.stop()
            if window._codex_limit_future is not None:
                window._codex_limit_future.result(timeout=5)
                window.drain_codex_limit()
            if window._jobs_future is not None:
                window._jobs_future.result(timeout=5)
                window.drain_jobs()
            window.connection.setText("Svacer: демонстрационный режим")
            window.tabs.setCurrentWidget(window.overview_tab)

            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            try:
                for index in range(FPS * DURATION):
                    second = index / FPS
                    target = None
                    if index == 24:
                        window.queue_table.selectRow(1)
                    if 24 <= index < 60:
                        target = window.remove_queue_button
                    if index == 48:
                        window.remove_queue_button.click()
                        window.connection.setText("Svacer: демонстрационный режим")
                    if index == 72:
                        window.tabs.setCurrentWidget(window.markers_tab)
                        window.select_marker_row("m3")
                    if index == 108:
                        window.select_marker_row("m5")
                    if 108 <= index < 132:
                        target = window.add_queue_button
                    if index == 120:
                        window.add_queue_button.click()
                        window.connection.setText("Svacer: демонстрационный режим")
                    if index == 132:
                        window.tabs.setCurrentWidget(window.overview_tab)
                    if index == 144:
                        (job / "codex-events.jsonl").write_text("".join(
                            json.dumps({"type": "item.completed", "item": {
                                "type": "agent_message", "text": message,
                            }}, ensure_ascii=False) + "\n"
                            for message in (
                                "Проверяю входную точку и происхождение значения в учебном примере.",
                                "Сопоставляю путь выполнения с условием перед операцией.",
                            )
                        ), encoding="utf-8")
                        window.state.update({
                            "codex_run": {"active": True, "phase": "analysis",
                                          "phase_detail": "Разбираю маркер 1 из 3"},
                            "paused": False,
                            "workers": {1: {"marker_ids": ["m4"], "current_status": "working"}},
                        })
                        window.current_live_id = "m4"
                        window.populate_work_queue()
                        window.run_status.setText("Фоновая задача: работает · Разбираю маркер 1 из 3")
                        window.scope.setText("В снимке 6 · в этой задаче 6 · очередь работает")
                        window.update_action_states()
                        if window.jobs_table.rowCount():
                            window.jobs_table.item(0, 1).setText("Работает")
                            window.jobs_table.item(0, 3).setText("1")
                    if index == 180:
                        window.tabs.setCurrentWidget(window.history_tab)
                        window.populate_history()
                    if index == 204:
                        window.tabs.setCurrentWidget(window.settings_tab)
                    if index == 228:
                        write_json(job / "ui-notifications.json", {
                            "schema_version": 1,
                            "seen": [f"attempt:demo-{number}" for number in range(1, 4)] +
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
                    application.processEvents()
                    if process.stdin is None:
                        raise RuntimeError("FFmpeg не принимает кадры")
                    process.stdin.write(render_frame(window, second, target))
                    QTest.qWait(round(1000 / FPS))
                process.stdin.close()
                error = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
                if process.wait(timeout=30) != 0:
                    raise RuntimeError(f"FFmpeg не смог сохранить видео: {error[-1000:]}")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(temporary_output, output)
            print(output)
        finally:
            window.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать демонстрационное видео интерфейса")
    parser.add_argument("--ffmpeg", type=Path, default=shutil.which("ffmpeg"))
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "demo.mp4")
    arguments = parser.parse_args()
    if arguments.ffmpeg is None:
        parser.error("FFmpeg не найден в PATH; укажите --ffmpeg путь-к-ffmpeg.exe")
    record(Path(arguments.ffmpeg), arguments.output)


if __name__ == "__main__":
    main()

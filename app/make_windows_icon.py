"""Build a multi-resolution Windows icon from a square source image."""
from __future__ import annotations

import argparse
from pathlib import Path
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage


SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def png_payload(image: QImage, size: int) -> bytes:
    scaled = image.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not scaled.save(buffer, "PNG"):
        raise ValueError(f"Не удалось подготовить слой иконки {size}x{size}.")
    buffer.close()
    return bytes(data)


def make_icon(source: Path, destination: Path) -> None:
    image = QImage(str(source))
    if image.isNull():
        raise ValueError(f"Не удалось прочитать изображение: {source}")
    payloads = [(size, png_payload(image, size)) for size in SIZES]
    offset = 6 + 16 * len(payloads)
    directory, images = [], []
    for size, payload in payloads:
        dimension = 0 if size == 256 else size
        directory.append(struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset,
        ))
        images.append(payload)
        offset += len(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(
        struct.pack("<HHH", 0, 1, len(payloads)) + b"".join(directory) + b"".join(images)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    make_icon(args.source, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

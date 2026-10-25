"""Line icons (Lucide, ISC licence) tinted to the theme, and the Sightline mark.

The Lucide SVGs in ``assets/icons/lucide`` use ``currentColor``; we swap in
the ink colour and render them with QSvgRenderer at the device pixel ratio,
so they stay sharp at 125% and 150% scaling.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

from sightline.core import paths

log = logging.getLogger(__name__)


def icon_dir():  # noqa: ANN201 - Path
    return paths.assets_dir() / "icons" / "lucide"


@lru_cache(maxsize=64)
def _svg_source(name: str) -> str | None:
    path = icon_dir() / f"{name}.svg"
    try:
        return path.read_text("utf-8")
    except OSError:
        log.warning("Missing icon %s", path)
        return None


@lru_cache(maxsize=256)
def _render(name: str, color: str, size: int, dpr_milli: int, stroke: float) -> QImage | None:
    source = _svg_source(name)
    if source is None:
        return None
    svg = source.replace("currentColor", color).replace('stroke-width="2"', f'stroke-width="{stroke:g}"')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        return None
    dpr = dpr_milli / 1000.0
    px = max(1, round(size * dpr))
    image = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    image.setDevicePixelRatio(dpr)
    return image


def pixmap(name: str, color: QColor, size: int, dpr: float = 1.0, stroke: float = 2.0) -> QPixmap:
    image = _render(name, color.name(), int(size), int(round(dpr * 1000)), float(stroke))
    if image is None:
        empty = QPixmap(QSize(1, 1))
        empty.fill(Qt.GlobalColor.transparent)
        return empty
    return QPixmap.fromImage(image)


def draw_icon(
    painter: QPainter, name: str, rect: QRectF, color: QColor, stroke: float = 2.0
) -> None:
    """Paint an icon centred in ``rect`` (logical units) at the painter's device pixel ratio."""
    dev = painter.device()
    dpr = dev.devicePixelRatioF() if dev is not None else 1.0
    size = int(min(rect.width(), rect.height()))
    pm = pixmap(name, color, size, dpr, stroke)
    x = rect.x() + (rect.width() - size) / 2.0
    y = rect.y() + (rect.height() - size) / 2.0
    painter.drawPixmap(QPointF(x, y), pm)


# --------------------------------------------------------------------------- the Sightline mark


def draw_mark(painter: QPainter, rect: QRectF, ink: QColor, ground: QColor | None = None, paused: bool = False) -> None:
    """The mark: a ring with a solid centre (a pupil, a target).  Paused shows two bars instead."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = min(rect.width(), rect.height())
    c = rect.center()
    if ground is not None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ground)
        painter.drawEllipse(c, side / 2.0, side / 2.0)
    ring_w = max(1.5, side * 0.12)
    r = side / 2.0 - ring_w / 2.0 - side * 0.04
    painter.setPen(QPen(ink, ring_w))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(c, r, r)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(ink)
    if paused:
        bar_w, bar_h = side * 0.11, side * 0.36
        gap = side * 0.07
        painter.drawRect(QRectF(c.x() - gap - bar_w, c.y() - bar_h / 2, bar_w, bar_h))
        painter.drawRect(QRectF(c.x() + gap, c.y() - bar_h / 2, bar_w, bar_h))
    else:
        dot = side * 0.17
        painter.drawEllipse(c, dot, dot)
    painter.restore()


def app_icon(ink: QColor, ground: QColor | None = None, paused: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        draw_mark(painter, QRectF(0, 0, size, size), ink, ground, paused)
        painter.end()
        icon.addPixmap(QPixmap.fromImage(image))
    return icon

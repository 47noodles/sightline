"""The Sightline design system: palette, type scale, spacing, fonts and the base style sheet.

Rules this file encodes (see docs/modules/ui.md):

* Near-monochrome.  One accent colour, amber, used only where it carries
  meaning: something is armed (an eye click, a dwell, a badge under the
  pointer) or the microphone is hearing you.  Everything else is ink on
  ground.
* Hairline rules and ruled, tabular layouts instead of floating cards.
* Atkinson Hyperlegible Next (designed by the Braille Institute for low
  vision), body text never below 14 px before ``ui.scale``.
* Readable text is always near-black on near-white or the reverse; the
  secondary ink is still well above 7:1 contrast.
* Overlays sit on top of arbitrary screen content, so they use solid fills
  with a hairline border, and every stroke drawn over the desktop gets a
  contrasting halo so it shows on black and on white.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPalette
from PySide6.QtWidgets import QApplication

from sightline.core import paths

log = logging.getLogger(__name__)

FONT_FAMILY = "Atkinson Hyperlegible Next"
FALLBACK_FAMILIES = ("Segoe UI", "Arial")
FONT_FILES = (
    "AtkinsonHyperlegibleNext-Regular.ttf",
    "AtkinsonHyperlegibleNext-Medium.ttf",
    "AtkinsonHyperlegibleNext-SemiBold.ttf",
    "AtkinsonHyperlegibleNext-Bold.ttf",
)

# --------------------------------------------------------------------------- palette


@dataclass(frozen=True, slots=True)
class Palette:
    name: str  # "dark" or "light"
    ground: QColor  # window background
    surface: QColor  # overlay panels, table bodies
    raised: QColor  # button fill
    ink: QColor  # primary text and strokes
    ink_2: QColor  # secondary text; still high contrast, never mid-grey
    rule: QColor  # hairline dividers
    rule_strong: QColor  # control borders
    halo: QColor  # contrast edge drawn around strokes that sit on the desktop
    accent: QColor  # armed and listening, nothing else
    on_accent: QColor  # text and edges on an accent fill

    @property
    def dark(self) -> bool:
        return self.name == "dark"


def _c(hex_rgb: str) -> QColor:
    return QColor(hex_rgb)


ACCENT = "#FFB000"  # amber; distinct from typical UI blues, safe for the common colour-vision differences

DARK = Palette(
    name="dark",
    ground=_c("#111214"),
    surface=_c("#17181B"),
    raised=_c("#202226"),
    ink=_c("#F4F4F1"),
    ink_2=_c("#C3C6CB"),
    rule=_c("#34373C"),
    rule_strong=_c("#6E737A"),
    halo=_c("#08090A"),
    accent=_c(ACCENT),
    on_accent=_c("#111214"),
)

LIGHT = Palette(
    name="light",
    ground=_c("#F5F5F2"),
    surface=_c("#FFFFFF"),
    raised=_c("#ECECE8"),
    ink=_c("#121315"),
    ink_2=_c("#3A3D42"),
    rule=_c("#D6D7D9"),
    rule_strong=_c("#878B91"),
    halo=_c("#FFFFFF"),
    accent=_c(ACCENT),
    on_accent=_c("#121315"),
)

# --------------------------------------------------------------------------- type, space

# role: (pixel size at scale 1.0, weight)
TYPE_SCALE: dict[str, tuple[int, int]] = {
    "display": (30, 600),  # wizard page titles
    "title": (22, 600),  # window titles, help sheet title
    "heading": (18, 600),  # section headings
    "body": (16, 400),  # running text
    "label": (15, 500),  # buttons, dock labels, table headers
    "small": (14, 400),  # secondary lines; the floor for anything readable
    "status": (14, 500),  # HUD rows
    "caption": (22, 500),  # live captions over the desktop
    "badge": (15, 700),  # numbered labels
    "grid": (26, 700),  # grid cell numbers
}

SPACE = (0, 4, 8, 12, 16, 24, 32, 48)  # index with theme.space(n)
RADIUS = 2  # controls and badges; panels are square
HAIRLINE = 1
STROKE = 2


# --------------------------------------------------------------------------- fonts

_fonts_loaded: bool | None = None


def fonts_dir() -> Path:
    return paths.assets_dir() / "fonts"


def load_fonts() -> bool:
    """Register the bundled typeface once.  Returns False (and logs) if it could not be loaded."""
    global _fonts_loaded
    if _fonts_loaded is not None:
        return _fonts_loaded
    ok = True
    for name in FONT_FILES:
        path = fonts_dir() / name
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            log.warning("Could not load font %s", path)
            ok = False
    _fonts_loaded = ok and FONT_FAMILY in QFontDatabase.families()
    return _fonts_loaded


# --------------------------------------------------------------------------- theme


def system_prefers_dark() -> bool:
    """Windows "app mode" (Settings, Personalisation, Colours), through Qt with a registry fallback."""
    app = QGuiApplication.instance()
    if app is not None:
        scheme = QGuiApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(value) == 0
        except OSError:
            pass
    return True  # dark is the calmer default over unknown content


def taskbar_prefers_dark() -> bool:
    """The taskbar follows the "Windows mode" setting, which can differ from the app mode."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
                return int(value) == 0
        except OSError:
            pass
    return True


class Theme(QObject):
    """The active palette and scale.  Emits ``changed`` when either changes so widgets can restyle."""

    changed = Signal()

    def __init__(self, choice: str = "system", scale: float = 1.0, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._choice = choice
        self._scale = max(0.5, float(scale))
        self.palette = self._resolve()
        self.has_font = load_fonts()
        hints = QGuiApplication.styleHints() if QGuiApplication.instance() else None
        if hints is not None:
            hints.colorSchemeChanged.connect(self._on_system_scheme)

    # ------------------------------------------------------------------ configuration

    @property
    def scale(self) -> float:
        return self._scale

    @property
    def choice(self) -> str:
        return self._choice

    def configure(self, choice: str, scale: float) -> None:
        scale = max(0.5, float(scale))
        if choice == self._choice and abs(scale - self._scale) < 1e-6:
            return
        self._choice = choice
        self._scale = scale
        self.palette = self._resolve()
        self.changed.emit()

    def _resolve(self) -> Palette:
        if self._choice == "dark":
            return DARK
        if self._choice == "light":
            return LIGHT
        return DARK if system_prefers_dark() else LIGHT

    def _on_system_scheme(self, _scheme: object) -> None:
        if self._choice != "system":
            return
        palette = self._resolve()
        if palette is not self.palette:
            self.palette = palette
            self.changed.emit()

    # ------------------------------------------------------------------ helpers

    def px(self, value: float) -> int:
        """A logical pixel length scaled by the user's size setting."""
        return max(1, round(value * self._scale))

    def pxf(self, value: float) -> float:
        return value * self._scale

    def space(self, step: int) -> int:
        return self.px(SPACE[max(0, min(step, len(SPACE) - 1))])

    def font(self, role: str = "body", *, size: float | None = None, weight: int | None = None) -> QFont:
        base_size, base_weight = TYPE_SCALE.get(role, TYPE_SCALE["body"])
        f = QFont(FONT_FAMILY if self.has_font else FALLBACK_FAMILIES[0])
        f.setFamilies([FONT_FAMILY, *FALLBACK_FAMILIES] if self.has_font else list(FALLBACK_FAMILIES))
        f.setPixelSize(self.px(size if size is not None else base_size))
        f.setWeight(QFont.Weight(weight if weight is not None else base_weight))
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        f.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
        return f

    # ------------------------------------------------------------------ application style

    def apply_to_app(self, app: QApplication) -> None:
        """Fusion as a neutral base, our palette, and the style sheet for ordinary windows."""
        if app.style().name().lower() != "fusion":
            app.setStyle("Fusion")
        app.setFont(self.font("body"))
        app.setPalette(self.qpalette())
        app.setStyleSheet(self.style_sheet())

    def qpalette(self) -> QPalette:
        p = self.palette
        qp = QPalette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            qp.setColor(group, QPalette.ColorRole.Window, p.ground)
            qp.setColor(group, QPalette.ColorRole.WindowText, p.ink)
            qp.setColor(group, QPalette.ColorRole.Base, p.surface)
            qp.setColor(group, QPalette.ColorRole.AlternateBase, p.raised)
            qp.setColor(group, QPalette.ColorRole.Text, p.ink)
            qp.setColor(group, QPalette.ColorRole.Button, p.raised)
            qp.setColor(group, QPalette.ColorRole.ButtonText, p.ink)
            qp.setColor(group, QPalette.ColorRole.Highlight, p.ink)
            qp.setColor(group, QPalette.ColorRole.HighlightedText, p.ground)
            qp.setColor(group, QPalette.ColorRole.ToolTipBase, p.surface)
            qp.setColor(group, QPalette.ColorRole.ToolTipText, p.ink)
            qp.setColor(group, QPalette.ColorRole.PlaceholderText, p.ink_2)
            qp.setColor(group, QPalette.ColorRole.Link, p.ink)
        disabled = QPalette.ColorGroup.Disabled
        for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
            qp.setColor(disabled, role, p.ink_2)
        qp.setColor(disabled, QPalette.ColorRole.Window, p.ground)
        qp.setColor(disabled, QPalette.ColorRole.Base, p.surface)
        qp.setColor(disabled, QPalette.ColorRole.Button, p.ground)
        return qp

    def style_sheet(self) -> str:
        p = self.palette
        px = self.px
        family = FONT_FAMILY if self.has_font else FALLBACK_FAMILIES[0]

        def css(color: QColor) -> str:
            return color.name()

        type_rules = []
        for role, (size, weight) in TYPE_SCALE.items():
            type_rules.append(f'QLabel[role="{role}"] {{ font-size: {px(size)}px; font-weight: {weight}; }}')
        return "\n".join(
            [
                f'QWidget {{ font-family: "{family}"; font-size: {px(16)}px; color: {css(p.ink)}; }}',
                f"QDialog, QMainWindow, QWidget#Window {{ background: {css(p.ground)}; }}",
                f'QLabel[role="secondary"] {{ color: {css(p.ink_2)}; font-size: {px(14)}px; }}',
                *type_rules,
                # Buttons: flat, square-ish, hairline border; hover thickens the border, press inverts.
                f"QPushButton {{ background: {css(p.raised)}; color: {css(p.ink)}; border: 1px solid {css(p.rule_strong)};"
                f" border-radius: {RADIUS}px; padding: {px(10)}px {px(22)}px; min-height: {px(28)}px;"
                f" font-size: {px(15)}px; font-weight: 500; }}",
                f"QPushButton:hover {{ border: 2px solid {css(p.ink)}; padding: {px(10) - 1}px {px(22) - 1}px; }}",
                f"QPushButton:pressed {{ background: {css(p.ink)}; color: {css(p.ground)}; }}",
                f"QPushButton:disabled {{ color: {css(p.ink_2)}; border: 1px dashed {css(p.rule_strong)}; background: {css(p.ground)}; }}",
                f'QPushButton[primary="true"] {{ background: {css(p.ink)}; color: {css(p.ground)}; border: 1px solid {css(p.ink)}; }}',
                f'QPushButton[primary="true"]:hover {{ border: 2px solid {css(p.ink)}; }}',
                f'QPushButton[primary="true"]:pressed {{ background: {css(p.ground)}; color: {css(p.ink)}; }}',
                f'QPushButton[primary="true"]:disabled {{ background: {css(p.ground)}; color: {css(p.ink_2)}; border: 1px dashed {css(p.rule_strong)}; }}',
                # Scroll bars: a plain track with a solid thumb, wide enough to hit with a head pointer.
                "QScrollArea { background: transparent; border: none; }",
                f"QScrollBar:vertical {{ background: {css(p.ground)}; width: {px(14)}px; margin: 0; border-left: 1px solid {css(p.rule)}; }}",
                f"QScrollBar::handle:vertical {{ background: {css(p.rule_strong)}; min-height: {px(40)}px; margin: {px(3)}px; }}",
                f"QScrollBar::handle:vertical:hover {{ background: {css(p.ink)}; }}",
                "QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }",
                "QScrollBar::add-page, QScrollBar::sub-page { background: none; }",
                f"QScrollBar:horizontal {{ background: {css(p.ground)}; height: {px(14)}px; border-top: 1px solid {css(p.rule)}; }}",
                f"QScrollBar::handle:horizontal {{ background: {css(p.rule_strong)}; min-width: {px(40)}px; margin: {px(3)}px; }}",
                # Menus (tray): ruled rows, inverted selection.
                f"QMenu {{ background: {css(p.surface)}; color: {css(p.ink)}; border: 1px solid {css(p.ink)}; padding: {px(4)}px 0; }}",
                f"QMenu::item {{ padding: {px(9)}px {px(28)}px {px(9)}px {px(16)}px; font-size: {px(15)}px; }}",
                f"QMenu::item:selected {{ background: {css(p.ink)}; color: {css(p.ground)}; }}",
                f"QMenu::item:disabled {{ color: {css(p.ink_2)}; }}",
                f"QMenu::separator {{ height: 1px; background: {css(p.rule)}; margin: {px(4)}px 0; }}",
                f"QToolTip {{ background: {css(p.surface)}; color: {css(p.ink)}; border: 1px solid {css(p.ink)}; padding: {px(6)}px; }}",
                f"QProgressBar {{ background: {css(p.ground)}; border: 1px solid {css(p.ink)}; border-radius: 0; height: {px(10)}px; text-align: center; color: transparent; }}",
                f"QProgressBar::chunk {{ background: {css(p.ink)}; }}",
                f"QCheckBox {{ spacing: {px(10)}px; }}",
                f"QCheckBox::indicator {{ width: {px(20)}px; height: {px(20)}px; border: 1px solid {css(p.ink)}; background: {css(p.surface)}; }}",
                f"QCheckBox::indicator:checked {{ background: {css(p.ink)}; }}",
            ]
        )


# --------------------------------------------------------------------------- painting helpers


def hairline_pen_width(painter: QPainter) -> float:
    """One device pixel, in the painter's logical units, so rules stay crisp at 125% and 150%."""
    dpr = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
    return 1.0 / max(dpr, 1.0)


def fill_hairline_h(painter: QPainter, x1: float, x2: float, y: float, color: QColor) -> None:
    painter.fillRect(QRectF(x1, y, x2 - x1, hairline_pen_width(painter)), color)


def fill_hairline_v(painter: QPainter, x: float, y1: float, y2: float, color: QColor) -> None:
    painter.fillRect(QRectF(x, y1, hairline_pen_width(painter), y2 - y1), color)


def frame_rect(painter: QPainter, rect: QRectF, color: QColor, width: float | None = None) -> None:
    """A square frame drawn inside ``rect`` with solid fills (no antialiasing blur)."""
    w = width if width is not None else hairline_pen_width(painter)
    painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), w), color)
    painter.fillRect(QRectF(rect.left(), rect.bottom() - w, rect.width(), w), color)
    painter.fillRect(QRectF(rect.left(), rect.top(), w, rect.height()), color)
    painter.fillRect(QRectF(rect.right() - w, rect.top(), w, rect.height()), color)

"""Icono en la bandeja del sistema (v0.11): abrir, activar/desactivar la grabación automática, salir y avisos.

pystray corre en su propio hilo; todo lo que toca la ventana se manda al hilo de tkinter con `app.ui(...)`.
Si pystray no está disponible (Linux sin bandeja, dependencia ausente) la app funciona igual sin icono.
"""
from __future__ import annotations

from .i18n import _


class Tray:
    def __init__(self, app, icon_path, on_open, on_toggle_auto, is_auto, on_quit):
        self.app = app
        self.icon = None
        try:
            import pystray
            from PIL import Image
        except Exception:
            return
        try:
            img = Image.open(icon_path)
        except Exception:
            img = Image.new("RGBA", (64, 64), (255, 122, 26, 255))
        menu = pystray.Menu(
            pystray.MenuItem(_("Abrir iRacing Optimizer"), lambda i, it: app.ui(on_open), default=True),
            pystray.MenuItem(_("Grabación automática"), lambda i, it: app.ui(on_toggle_auto),
                             checked=lambda it: bool(is_auto())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_("Salir"), lambda i, it: app.ui(on_quit)),
        )
        try:
            self.icon = pystray.Icon("iRacingOptimizer", img, "iRacing Optimizer", menu)
            self.icon.run_detached()
        except Exception:
            self.icon = None

    @property
    def ok(self) -> bool:
        return self.icon is not None

    def notify(self, text: str, title: str = "iRacing Optimizer"):
        if self.icon:
            try:
                self.icon.notify(text, title)
            except Exception:
                pass

    def refresh(self):
        if self.icon:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None

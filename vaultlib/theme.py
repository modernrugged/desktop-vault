"""Dark theme, ttk styling and a few small shared widgets."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ------------------------------------------------------------------ palette --

BG        = "#14161B"     # window background
PANEL     = "#1B1E25"     # sidebars, toolbars
SURFACE   = "#232730"     # inputs, list background
SURFACE_2 = "#2C313C"     # hover
BORDER    = "#333947"
TEXT      = "#E7E9EE"
MUTED     = "#8A92A4"
FAINT     = "#5C6478"
ACCENT    = "#5B8DEF"
ACCENT_HI = "#7BA4F5"
ACCENT_LO = "#3F6BC4"
DANGER    = "#E5484D"
DANGER_HI = "#F2686C"
WARN      = "#E2A03F"
OK        = "#35B37E"
SELECT    = "#2D4470"

FONT       = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_TINY  = ("Segoe UI", 8)
FONT_BOLD  = ("Segoe UI Semibold", 10)
FONT_H1    = ("Segoe UI Light", 26)
FONT_H2    = ("Segoe UI Semibold", 14)
FONT_MONO  = ("Consolas", 10)


def apply(root: tk.Misc) -> ttk.Style:
    """Install the dark theme on ``root`` and return the configured style."""
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(bg=BG)

    style.configure(".", background=BG, foreground=TEXT, font=FONT,
                    borderwidth=0, focuscolor=BG)

    # ------------------------------------------------------------- frames --
    for name, colour in (("TFrame", BG), ("Panel.TFrame", PANEL),
                         ("Surface.TFrame", SURFACE), ("Card.TFrame", PANEL)):
        style.configure(name, background=colour)
    style.configure("Sep.TFrame", background=BORDER)

    # ------------------------------------------------------------- labels --
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                    font=FONT_SMALL)
    style.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED,
                    font=FONT_SMALL)
    style.configure("H1.TLabel", background=BG, foreground=TEXT, font=FONT_H1)
    style.configure("H2.TLabel", background=BG, foreground=TEXT, font=FONT_H2)
    style.configure("Danger.TLabel", background=BG, foreground=DANGER,
                    font=FONT_SMALL)
    style.configure("Ok.TLabel", background=BG, foreground=OK, font=FONT_SMALL)
    style.configure("Warn.TLabel", background=BG, foreground=WARN,
                    font=FONT_SMALL)
    style.configure("Accent.TLabel", background=BG, foreground=ACCENT)
    style.configure("Placeholder.TLabel", background=SURFACE, foreground=FAINT,
                    font=FONT_SMALL)
    style.configure("Drop.TLabel", background=ACCENT, foreground="#FFFFFF",
                    font=FONT_BOLD, padding=(18, 10))

    # ------------------------------------------------------------ buttons --
    style.configure("TButton", background=SURFACE, foreground=TEXT,
                    padding=(14, 7), relief="flat", font=FONT, borderwidth=0)
    style.map("TButton",
              background=[("disabled", PANEL), ("pressed", BORDER),
                          ("active", SURFACE_2)],
              foreground=[("disabled", FAINT)])

    style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                    padding=(16, 8), font=FONT_BOLD)
    style.map("Accent.TButton",
              background=[("disabled", "#33405C"), ("pressed", ACCENT_LO),
                          ("active", ACCENT_HI)],
              foreground=[("disabled", "#7C879C")])

    # Same metrics as Accent.TButton so the two can sit together as a pair.
    style.configure("Secondary.TButton", background=SURFACE, foreground=TEXT,
                    padding=(16, 8), font=FONT_BOLD)
    style.map("Secondary.TButton",
              background=[("disabled", PANEL), ("pressed", BORDER),
                          ("active", SURFACE_2)],
              foreground=[("disabled", FAINT)])

    style.configure("Danger.TButton", background=SURFACE, foreground=DANGER,
                    padding=(14, 7))
    style.map("Danger.TButton",
              background=[("active", "#3A2429"), ("pressed", "#2E1D21")],
              foreground=[("disabled", FAINT), ("active", DANGER_HI)])

    # Flat toolbar button sitting on the PANEL colour.
    style.configure("Tool.TButton", background=PANEL, foreground=TEXT,
                    padding=(11, 6), font=FONT_SMALL)
    style.map("Tool.TButton",
              background=[("disabled", PANEL), ("pressed", BORDER),
                          ("active", SURFACE_2)],
              foreground=[("disabled", FAINT)])

    style.configure("Link.TButton", background=BG, foreground=ACCENT,
                    padding=(2, 2), font=FONT_SMALL)
    style.map("Link.TButton", background=[("active", BG)],
              foreground=[("active", ACCENT_HI)])

    style.configure("Crumb.TButton", background=PANEL, foreground=MUTED,
                    padding=(6, 3), font=FONT_SMALL)
    style.map("Crumb.TButton", background=[("active", SURFACE_2)],
              foreground=[("active", TEXT)])

    # ------------------------------------------------------------- entries --
    style.configure("TEntry", fieldbackground=SURFACE, background=SURFACE,
                    foreground=TEXT, insertcolor=TEXT, borderwidth=1,
                    relief="flat", padding=8, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER)
    style.map("TEntry",
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)])

    style.configure("Search.TEntry", padding=6)

    # ------------------------------------------------------------ treeview --
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=TEXT, borderwidth=0, rowheight=26,
                    font=FONT_SMALL)
    style.map("Treeview",
              background=[("selected", SELECT)],
              foreground=[("selected", "#FFFFFF")])
    style.configure("Treeview.Heading", background=PANEL, foreground=MUTED,
                    relief="flat", font=FONT_SMALL, padding=(8, 6),
                    borderwidth=0)
    style.map("Treeview.Heading", background=[("active", SURFACE_2)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    style.configure("Side.Treeview", background=PANEL, fieldbackground=PANEL,
                    rowheight=25)
    style.map("Side.Treeview", background=[("selected", SELECT)])

    # ------------------------------------------------------- misc controls --
    for name, base in (("TCheckbutton", BG), ("Panel.TCheckbutton", PANEL)):
        style.configure(name, background=base, foreground=TEXT,
                        font=FONT_SMALL, focuscolor=base,
                        indicatorbackground=SURFACE, indicatorforeground=TEXT,
                        indicatorrelief="flat", indicatormargin=(0, 0, 8, 0),
                        bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, padding=(0, 3))
        style.map(name,
                  background=[("active", base)],
                  foreground=[("disabled", FAINT)],
                  indicatorbackground=[("selected", ACCENT),
                                       ("active", SURFACE_2),
                                       ("!selected", SURFACE)],
                  indicatorforeground=[("selected", "#FFFFFF")],
                  bordercolor=[("selected", ACCENT)],
                  lightcolor=[("selected", ACCENT)],
                  darkcolor=[("selected", ACCENT)])

    for name in ("TProgressbar", "Horizontal.TProgressbar"):
        style.configure(name, background=ACCENT, troughcolor=SURFACE,
                        borderwidth=0, thickness=6, bordercolor=SURFACE,
                        lightcolor=ACCENT, darkcolor=ACCENT,
                        troughrelief="flat", relief="flat")
    # One style per strength score so the meter is colour-coded.
    for score, colour in enumerate((DANGER, DANGER, WARN, OK, OK)):
        style.configure("S%d.Horizontal.TProgressbar" % score, thickness=5,
                        background=colour, troughcolor=SURFACE, borderwidth=0,
                        bordercolor=SURFACE, lightcolor=colour,
                        darkcolor=colour, troughrelief="flat", relief="flat")
    style.configure("S-.Horizontal.TProgressbar", thickness=5,
                    background=SURFACE, troughcolor=SURFACE, borderwidth=0,
                    bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE,
                    troughrelief="flat", relief="flat")

    style.configure("Vertical.TScrollbar", background=SURFACE_2,
                    troughcolor=BG, borderwidth=0, arrowcolor=MUTED,
                    relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", BORDER)])
    style.configure("Horizontal.TScrollbar", background=SURFACE_2,
                    troughcolor=BG, borderwidth=0, arrowcolor=MUTED)

    style.configure("TPanedwindow", background=BORDER)
    style.configure("Sash", sashthickness=4, gripcount=0)

    # A readonly combobox draws through its own state map, so without these it
    # renders as an unthemed white field with the value invisible inside it.
    style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                    foreground=TEXT, arrowcolor=MUTED, borderwidth=1,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    selectbackground=SURFACE, selectforeground=TEXT,
                    insertcolor=TEXT, padding=6)
    style.map("TCombobox",
              fieldbackground=[("readonly", SURFACE), ("disabled", PANEL)],
              background=[("readonly", SURFACE), ("active", SURFACE_2)],
              foreground=[("readonly", TEXT), ("disabled", FAINT)],
              selectbackground=[("readonly", SURFACE)],
              selectforeground=[("readonly", TEXT)],
              bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)],
              darkcolor=[("focus", ACCENT)],
              arrowcolor=[("active", TEXT)])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT)

    return style


# ------------------------------------------------------------------ widgets --

class AutoScrollbar(ttk.Scrollbar):
    """A scrollbar that removes itself while the whole view is visible."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._pack_kw = None

    def pack(self, **kw):
        self._pack_kw = kw
        super().pack(**kw)

    def set(self, first, last):
        try:
            fits = float(first) <= 0.0 and float(last) >= 1.0
        except (TypeError, ValueError):
            fits = False
        if fits:
            if self.winfo_ismapped():
                super().pack_forget()
        elif self._pack_kw is not None and not self.winfo_ismapped():
            super().pack(**self._pack_kw)
        ttk.Scrollbar.set(self, first, last)


class Divider(ttk.Frame):
    """A one-pixel horizontal or vertical rule."""

    def __init__(self, master, vertical: bool = False, **kw):
        if vertical:
            kw.setdefault("width", 1)
        else:
            kw.setdefault("height", 1)
        super().__init__(master, style="Sep.TFrame", **kw)


class PasswordEntry(ttk.Frame):
    """Masked entry with a show/hide toggle."""

    def __init__(self, master, textvariable=None, width=32, **kw):
        super().__init__(master, style="TFrame")
        self.var = textvariable or tk.StringVar()
        self._shown = False
        self.entry = ttk.Entry(self, textvariable=self.var, show="•",
                               width=width, font=FONT, **kw)
        self.entry.pack(side="left", fill="x", expand=True)
        self.toggle = ttk.Button(self, text="Show", width=5, style="Tool.TButton",
                                 command=self._toggle, takefocus=False)
        self.toggle.pack(side="left", padx=(6, 0))

    def _toggle(self):
        self._shown = not self._shown
        self.entry.configure(show="" if self._shown else "•")
        self.toggle.configure(text="Hide" if self._shown else "Show")

    def get(self) -> str:
        return self.var.get()

    def clear(self):
        self.var.set("")

    def focus_set(self):
        self.entry.focus_set()

    def bind_return(self, callback):
        self.entry.bind("<Return>", callback)


def center(window: tk.Misc, width: int, height: int) -> None:
    """Place a window at the centre of the screen."""
    window.update_idletasks()
    sw = window.winfo_screenwidth()
    sh = window.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 3)
    window.geometry("%dx%d+%d+%d" % (width, height, x, y))


def human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % int(n)

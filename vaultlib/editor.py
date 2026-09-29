"""A text editor that lives entirely inside the vault.

Opening a document through Windows means writing plaintext to disk so another
program can read it.  For text this is avoidable: the bytes are decrypted into
memory, edited here, and re-encrypted straight back.  Nothing is ever written
to a temporary file, so there is nothing to shred afterwards.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme
from .theme import AutoScrollbar, human_size

# Extensions that open in here rather than being handed to Windows.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".env",
    ".py", ".js", ".ts", ".html", ".htm", ".css", ".sql", ".sh", ".bat",
    ".ps1", ".c", ".h", ".cpp", ".java", ".cs", ".go", ".rs", ".rb", ".php",
    ".srt", ".tex", ".gitignore", ".properties", "",
}

MAX_EDIT_BYTES = 8 * 1024 * 1024


def looks_editable(name: str, size: int) -> bool:
    import os
    if size > MAX_EDIT_BYTES:
        return False
    return os.path.splitext(name)[1].lower() in TEXT_EXTENSIONS


def decode(data: bytes):
    """Best-effort decode; returns ``(text, encoding, had_bom)``."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8", True
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding), encoding, False
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", "replace"), "latin-1", False


def is_probably_text(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return False
    sample = data[:8192]
    if not sample:
        return True
    printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b < 127 or b >= 160)
    return printable / len(sample) > 0.85


class EditorWindow(tk.Toplevel):
    """Edit one vault file in memory and save it straight back."""

    def __init__(self, app, vault, path, on_saved=None):
        super().__init__(app)
        self.app = app
        self.vault = vault
        self.path = list(path)
        self.on_saved = on_saved
        self.encoding = "utf-8"
        self.had_bom = False
        self._dirty = False

        name = self.path[-1]
        self.title("%s - Desktop Vault" % name)
        self.configure(bg=theme.BG)
        self.geometry("880x640")
        self.minsize(520, 360)
        self.protocol("WM_DELETE_WINDOW", self.close)
        from . import shell
        shell.use_dark_titlebar(self)

        data = vault.read_bytes(self.path, limit=MAX_EDIT_BYTES)
        if not is_probably_text(data):
            self.destroy()
            raise ValueError(
                "%s does not look like text - opening it here would corrupt "
                "it. Use Open with Windows instead." % name)
        text, self.encoding, self.had_bom = decode(data)
        self.original = text

        bar = ttk.Frame(self, style="Panel.TFrame", padding=(10, 7))
        bar.pack(fill="x")
        self.save_btn = ttk.Button(bar, text="Save to vault",
                                   style="Accent.TButton", command=self.save)
        self.save_btn.pack(side="left")
        ttk.Button(bar, text="Revert", style="Tool.TButton",
                   command=self.revert).pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="", style="PanelMuted.TLabel")
        self.status.pack(side="right")

        wrap = ttk.Frame(self, style="TFrame")
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(
            wrap, wrap="none", undo=True, maxundo=-1, font=theme.FONT_MONO,
            bg=theme.SURFACE, fg=theme.TEXT, insertbackground=theme.ACCENT,
            selectbackground=theme.SELECT, selectforeground="#FFFFFF",
            relief="flat", borderwidth=0, padx=12, pady=10,
            tabs="1c", spacing1=1, spacing3=1)
        vsb = AutoScrollbar(wrap, orient="vertical", command=self.text.yview)
        hsb = AutoScrollbar(wrap, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        self.text.pack(side="left", fill="both", expand=True)

        self.text.insert("1.0", text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<Control-s>", lambda _e: (self.save(), "break")[1])
        self.bind("<Control-s>", lambda _e: (self.save(), "break")[1])
        self.text.focus_set()

        self._refresh_status(len(data))
        self.transient(app)

    # ------------------------------------------------------------- plumbing --

    def _on_modified(self, _event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if not self._dirty:
                self._dirty = True
                self.title("* " + self.title())
            self._refresh_status()

    def _current(self) -> str:
        # Tk always reports a trailing newline the user did not type.
        return self.text.get("1.0", "end-1c")

    def _encode(self) -> bytes:
        body = self._current()
        try:
            raw = body.encode(self.encoding)
        except (UnicodeEncodeError, LookupError):
            self.encoding = "utf-8"
            raw = body.encode("utf-8")
        return (b"\xef\xbb\xbf" + raw) if self.had_bom else raw

    def _refresh_status(self, size=None):
        if size is None:
            size = len(self._encode())
        lines = int(self.text.index("end-1c").split(".")[0])
        self.status.configure(
            text="%s  ·  %d lines  ·  %s%s"
                 % (human_size(size), lines, self.encoding,
                    "  ·  unsaved changes" if self._dirty else ""))

    # -------------------------------------------------------------- actions --

    def save(self):
        from . import dialogs
        try:
            self.vault.write_bytes(self.path, self._encode())
        except Exception as exc:
            dialogs.error(self, "Could not save", str(exc))
            return
        self.original = self._current()
        self._dirty = False
        self.title(self.title().lstrip("* "))
        self._refresh_status()
        if self.on_saved:
            self.on_saved()

    def revert(self):
        from . import dialogs
        if self._dirty and not dialogs.confirm(
                self, "Discard changes?",
                "Go back to the version stored in the vault?",
                "Everything typed since the last save is lost.",
                confirm_text="Discard", danger=True):
            return
        self.text.delete("1.0", "end")
        self.text.insert("1.0", self.original)
        self.text.edit_modified(False)
        self._dirty = False
        self.title(self.title().lstrip("* "))
        self._refresh_status()

    def close(self):
        from . import dialogs
        if self._dirty:
            choice = dialogs.SaveCloseDialog(self, self.path[-1]).show()
            if choice is None:
                return
            if choice == "save":
                self.save()
                if self._dirty:          # the save failed; stay open
                    return
        self.destroy()

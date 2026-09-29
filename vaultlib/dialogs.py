"""Modal dialogs: background task runner, prompts and settings."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from . import theme
from .theme import PasswordEntry, human_size
from .strength import estimate


class _Modal(tk.Toplevel):
    """Base class: themed, centred, modal, escape-to-close."""

    def __init__(self, parent, title: str, width: int, height: int,
                 closable: bool = True):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(bg=theme.BG)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        self._closable = closable
        self.protocol("WM_DELETE_WINDOW",
                      self._on_close if closable else (lambda: None))
        if closable:
            self.bind("<Escape>", lambda _e: self._on_close())
        theme.center(self, width, height)
        from . import shell
        shell.use_dark_titlebar(self)

    def show(self):
        self.deiconify()
        self.grab_set()
        self.focus_force()
        self.wait_window()
        return self.result

    def _on_close(self):
        self.result = None
        self.destroy()


# ----------------------------------------------------------- task with progress --

class TaskDialog(_Modal):
    """Runs ``worker(report, cancel)`` on a thread with a progress readout.

    ``report(text=None, done=None, total=None)`` may be called from the worker
    thread; updates are marshalled back through a queue so only the Tk thread
    ever touches a widget.
    """

    POLL_MS = 60

    def __init__(self, parent, title: str, worker: Callable,
                 cancellable: bool = True):
        super().__init__(parent, title, 460, 168, closable=False)
        self.worker = worker
        self.cancel_event = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self.error: Optional[BaseException] = None
        self._total = 0

        body = ttk.Frame(self, padding=(theme.px(24), theme.px(20), theme.px(24), theme.px(18)))
        body.pack(fill="both", expand=True)

        self.title_label = ttk.Label(body, text=title, style="H2.TLabel")
        self.title_label.pack(anchor="w")

        self.detail = ttk.Label(body, text="Starting...", style="Muted.TLabel",
                                wraplength=theme.px(400))
        self.detail.pack(anchor="w", pady=(6, 14))

        self.bar = ttk.Progressbar(body, mode="indeterminate", length=theme.px(412))
        self.bar.pack(fill="x")
        self.bar.start(14)

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(14, 0))
        self.count = ttk.Label(row, text="", style="Muted.TLabel")
        self.count.pack(side="left")
        if cancellable:
            ttk.Button(row, text="Cancel", command=self._cancel).pack(side="right")

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.after(self.POLL_MS, self._poll)

    # ------------------------------------------------------- worker plumbing --

    def _report(self, text=None, done=None, total=None):
        self.queue.put((text, done, total))

    def _run(self):
        try:
            self.result = self.worker(self._report, self.cancel_event)
        except BaseException as exc:                       # surfaced to caller
            self.error = exc
        finally:
            self.queue.put(("__done__", None, None))

    def _cancel(self):
        self.cancel_event.set()
        self.detail.configure(text="Cancelling...")

    def _poll(self):
        finished = False
        try:
            while True:
                text, done, total = self.queue.get_nowait()
                if text == "__done__":
                    finished = True
                    continue
                if text is not None:
                    self.detail.configure(text=text)
                if total is not None and total > 0:
                    if self._total != total:
                        self._total = total
                        self.bar.stop()
                        self.bar.configure(mode="determinate", maximum=total)
                if done is not None and self._total:
                    self.bar.configure(value=min(done, self._total))
                    self.count.configure(
                        text="%s of %s" % (human_size(done),
                                           human_size(self._total)))
        except queue.Empty:
            pass

        if finished and not self.thread.is_alive():
            self.bar.stop()
            self.destroy()
            return
        self.after(self.POLL_MS, self._poll)


def run_task(parent, title: str, worker: Callable, cancellable: bool = True):
    """Show a progress dialog; return ``(ok, result_or_exception)``."""
    dialog = TaskDialog(parent, title, worker, cancellable)
    dialog.deiconify()
    dialog.grab_set()
    parent.wait_window(dialog)
    if dialog.error is not None:
        return False, dialog.error
    return True, dialog.result


# ------------------------------------------------------------------ prompts --

class TextPrompt(_Modal):
    """One-line text input, used for New Folder and Rename."""

    def __init__(self, parent, title: str, prompt: str, initial: str = "",
                 ok_text: str = "OK"):
        super().__init__(parent, title, 420, 190)
        body = ttk.Frame(self, padding=theme.px(24))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, text=prompt, style="Muted.TLabel").pack(
            anchor="w", pady=(4, 12))

        self.var = tk.StringVar(value=initial)
        entry = ttk.Entry(body, textvariable=self.var, font=theme.FONT)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _e: self._accept())
        entry.focus_set()
        entry.select_range(0, "end")

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(18, 0))
        ttk.Button(row, text=ok_text, style="Accent.TButton",
                   command=self._accept).pack(side="right")
        ttk.Button(row, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))

    def _accept(self):
        value = self.var.get().strip()
        if value:
            self.result = value
            self.destroy()


class ConfirmDialog(_Modal):
    """Yes/no confirmation with an optional destructive styling."""

    def __init__(self, parent, title: str, message: str, detail: str = "",
                 confirm_text: str = "Delete", danger: bool = True):
        super().__init__(parent, title, 460, 220 if detail else 180)
        body = ttk.Frame(self, padding=theme.px(24))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=theme.px(400),
                  style="TLabel").pack(anchor="w", pady=(10, 0))
        if detail:
            ttk.Label(body, text=detail, wraplength=theme.px(400),
                      style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom")
        style = "Danger.TButton" if danger else "Accent.TButton"
        ttk.Button(row, text=confirm_text, style=style,
                   command=self._accept).pack(side="right")
        ttk.Button(row, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))

    def _accept(self):
        self.result = True
        self.destroy()


class ExternalOpenDialog(_Modal):
    """The honest warning before handing a document to another program.

    Editing outside the app is the one place where the vault cannot guarantee
    the result comes back, so say so plainly rather than letting someone find
    out after losing an afternoon's work.
    """

    def __init__(self, parent, name: str):
        super().__init__(parent, "Editing outside the vault", 560, 468)
        body = ttk.Frame(self, padding=theme.px(26))
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Editing outside the vault",
                  style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, wraplength=theme.px(500), justify="left", style="TLabel",
                  text="%s cannot be edited inside Desktop Vault. It has to be "
                       "decrypted to a temporary copy and handed to whichever "
                       "program Windows uses for it." % name).pack(
            anchor="w", pady=(10, 0))

        warn = ttk.Frame(body, style="Panel.TFrame", padding=theme.px(14))
        warn.pack(fill="x", pady=(14, 0))
        ttk.Label(warn, text="Changes are NOT saved back automatically.",
                  style="Panel.TLabel", font=theme.FONT_BOLD,
                  foreground=theme.WARN).pack(anchor="w")
        ttk.Label(warn, style="PanelMuted.TLabel", wraplength=theme.px(480),
                  justify="left",
                  text="Saving in the other program only writes to the "
                       "temporary copy. The change reaches the vault when you "
                       "come back here and press Save changes in the toolbar, "
                       "or confirm the prompt when you lock.").pack(
            anchor="w", pady=(6, 0))

        ttk.Label(body, style="Muted.TLabel", wraplength=theme.px(500), justify="left",
                  text="Some programs - Office in particular - can defeat this "
                       "entirely by saving somewhere else, keeping the file "
                       "open, or writing after the vault has closed. If the "
                       "document matters, the reliable route is Export, edit "
                       "the exported copy, then add it back. Plain text files "
                       "avoid the problem completely: open them with Edit and "
                       "they never leave the vault.").pack(anchor="w",
                                                           pady=(14, 0))

        self.dont_ask = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, variable=self.dont_ask,
                        text="Do not warn me about this again").pack(
            anchor="w", pady=(14, 0))

        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Open anyway", style="Accent.TButton",
                   command=self._accept).pack(side="right")
        ttk.Button(row, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))

    def _accept(self):
        self.result = {"open": True, "dont_ask": bool(self.dont_ask.get())}
        self.destroy()


class ConflictDialog(_Modal):
    """Asked when an incoming file or folder has a name already in the vault.

    Returns ``{"action": "overwrite"|"keep_both"|"skip", "all": bool}``, or
    None if the whole import should be abandoned.
    """

    def __init__(self, parent, name: str, where: str, is_dir: bool,
                 existing: dict, incoming: dict, remaining: int = 0):
        super().__init__(parent, "Name already in use", 560,
                         378 if remaining else 346)
        body = ttk.Frame(self, padding=theme.px(26))
        body.pack(fill="both", expand=True)

        kind = "folder" if is_dir else "file"
        ttk.Label(body, text="Name already in use", style="H2.TLabel").pack(
            anchor="w")
        ttk.Label(body, wraplength=theme.px(500), justify="left", style="TLabel",
                  text="A %s called %s is already in %s."
                       % (kind, name, where)).pack(anchor="w", pady=(10, 14))

        table = ttk.Frame(body, style="Panel.TFrame", padding=theme.px(14))
        table.pack(fill="x")
        table.columnconfigure(0, weight=1, uniform="col")
        table.columnconfigure(1, weight=1, uniform="col")

        for column, (heading, info) in enumerate(
                (("Already in the vault", existing), ("Being added", incoming))):
            ttk.Label(table, text=heading.upper(), style="PanelMuted.TLabel").grid(
                row=0, column=column, sticky="w", padx=(0, 16))
            ttk.Label(table, text=info.get("size", "-"), style="Panel.TLabel",
                      font=theme.FONT_BOLD).grid(row=1, column=column,
                                                 sticky="w", padx=(0, 16),
                                                 pady=(4, 0))
            ttk.Label(table, text=info.get("modified", "-"),
                      style="PanelMuted.TLabel").grid(row=2, column=column,
                                                      sticky="w", padx=(0, 16))

        ttk.Label(body, style="Muted.TLabel", wraplength=theme.px(500), justify="left",
                  text="Overwrite replaces what is stored now and shreds the "
                       "old encrypted data - there is no undo. Keep both adds "
                       "the new one alongside under a numbered name."
                  ).pack(anchor="w", pady=(14, 0))

        self.apply_all = tk.BooleanVar(value=False)
        if remaining:
            ttk.Checkbutton(
                body, variable=self.apply_all,
                text="Do the same for the other %d name clash%s"
                     % (remaining, "" if remaining == 1 else "es")).pack(
                anchor="w", pady=(14, 0))

        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Overwrite", style="Danger.TButton",
                   command=lambda: self._pick("overwrite")).pack(side="right")
        ttk.Button(row, text="Keep both", style="Accent.TButton",
                   command=lambda: self._pick("keep_both")).pack(
            side="right", padx=(0, 8))
        ttk.Button(row, text="Skip", style="Secondary.TButton",
                   command=lambda: self._pick("skip")).pack(
            side="right", padx=(0, 8))
        ttk.Button(row, text="Cancel import", command=self._on_close).pack(
            side="left")

    def _pick(self, action):
        self.result = {"action": action, "all": bool(self.apply_all.get())}
        self.destroy()


class SaveCloseDialog(_Modal):
    """Save / discard / cancel, for closing an editor with unsaved changes."""

    def __init__(self, parent, name: str):
        super().__init__(parent, "Unsaved changes", 460, 200)
        body = ttk.Frame(self, padding=theme.px(24))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Unsaved changes", style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, wraplength=theme.px(400), justify="left", style="TLabel",
                  text="%s has changes that are not in the vault yet."
                       % name).pack(anchor="w", pady=(10, 0))
        ttk.Label(body, wraplength=theme.px(400), justify="left", style="Muted.TLabel",
                  text="Saving re-encrypts it in place.").pack(anchor="w",
                                                               pady=(6, 0))
        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Save", style="Accent.TButton",
                   command=lambda: self._pick("save")).pack(side="right")
        ttk.Button(row, text="Discard", style="Danger.TButton",
                   command=lambda: self._pick("discard")).pack(
            side="right", padx=(0, 8))
        ttk.Button(row, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))

    def _pick(self, choice):
        self.result = choice
        self.destroy()


class MessageDialog(_Modal):
    """Informational box; ``kind`` tints the heading."""

    def __init__(self, parent, title: str, message: str, detail: str = "",
                 kind: str = "info", mono: bool = False):
        lines = 1 + message.count("\n") + (detail.count("\n") if detail else 0)
        super().__init__(parent, title, 520, min(560, 170 + 19 * lines))
        body = ttk.Frame(self, padding=theme.px(24))
        body.pack(fill="both", expand=True)
        heading = {"error": "Danger.TLabel", "warn": "Warn.TLabel",
                   "ok": "Ok.TLabel"}.get(kind)
        ttk.Label(body, text=title, style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, text=message, wraplength=theme.px(420), style="TLabel",
                  justify="left").pack(anchor="w", pady=(10, 0))
        if detail:
            label = ttk.Label(body, text=detail, wraplength=theme.px(460),
                              style=heading or "Muted.TLabel", justify="left")
            if mono:
                label.configure(font=theme.FONT_MONO, foreground=theme.TEXT)
            label.pack(anchor="w", pady=(10, 0))
        ttk.Button(body, text="Close", style="Accent.TButton",
                   command=self._on_close).pack(side="bottom", anchor="e")


def info(parent, title, message, detail="", kind="info", mono=False):
    MessageDialog(parent, title, message, detail, kind, mono).show()


def error(parent, title, message, detail=""):
    MessageDialog(parent, title, message, detail, "error").show()


def confirm(parent, title, message, detail="", confirm_text="Delete",
            danger=True):
    return bool(ConfirmDialog(parent, title, message, detail,
                              confirm_text, danger).show())


def ask_text(parent, title, prompt, initial="", ok_text="OK"):
    return TextPrompt(parent, title, prompt, initial, ok_text).show()


# -------------------------------------------------------- change password --

class ChangePasswordDialog(_Modal):
    def __init__(self, parent):
        super().__init__(parent, "Change password", 470, 400)
        body = ttk.Frame(self, padding=theme.px(26))
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Change password", style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, style="Muted.TLabel", wraplength=theme.px(410), justify="left",
                  text="Only the master key is re-wrapped, so this is instant "
                       "no matter how large the vault is. Your files are not "
                       "re-encrypted.").pack(anchor="w", pady=(6, 18))

        ttk.Label(body, text="Current password", style="Muted.TLabel").pack(anchor="w")
        self.current = PasswordEntry(body)
        self.current.pack(fill="x", pady=(4, 14))

        ttk.Label(body, text="New password", style="Muted.TLabel").pack(anchor="w")
        self.new_var = tk.StringVar()
        self.new_var.trace_add("write", lambda *_: self._rate())
        self.new = PasswordEntry(body, textvariable=self.new_var)
        self.new.pack(fill="x", pady=(4, 6))

        self.meter = ttk.Progressbar(body, maximum=5, length=theme.px(340),
                                     style="S-.Horizontal.TProgressbar")
        self.meter.pack(fill="x")
        self.rating = ttk.Label(body, text=" ", style="Muted.TLabel")
        self.rating.pack(anchor="w", pady=(4, 12))

        ttk.Label(body, text="Confirm new password", style="Muted.TLabel").pack(anchor="w")
        self.confirm = PasswordEntry(body)
        self.confirm.pack(fill="x", pady=(4, 10))

        self.err = ttk.Label(body, text="", style="Danger.TLabel", wraplength=theme.px(410))
        self.err.pack(anchor="w")

        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom")
        ttk.Button(row, text="Change password", style="Accent.TButton",
                   command=self._accept).pack(side="right")
        ttk.Button(row, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))
        self.current.focus_set()

    def _rate(self):
        result = estimate(self.new_var.get())
        self.meter.configure(
            value=result["score"] + (1 if result["bits"] else 0),
            style=("S%d.Horizontal.TProgressbar" % result["score"])
            if result["label"] else "S-.Horizontal.TProgressbar")
        text = result["label"]
        if result["hint"]:
            text += " - " + result["hint"]
        self.rating.configure(text=text or " ",
                              foreground=result["colour"] if text else theme.MUTED)

    def _accept(self):
        if not self.current.get():
            self.err.configure(text="Enter your current password.")
            return
        if len(self.new.get()) < 8:
            self.err.configure(text="The new password must be at least 8 characters.")
            return
        if self.new.get() != self.confirm.get():
            self.err.configure(text="The new passwords do not match.")
            return
        self.result = (self.current.get(), self.new.get())
        self.destroy()


# ---------------------------------------------------------------- settings --

class SettingsDialog(_Modal):
    def __init__(self, parent, settings):
        super().__init__(parent, "Settings", 560, 560)
        self.settings = settings
        body = ttk.Frame(self, padding=theme.px(26))
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Settings", style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, text="Stored unencrypted in your user profile; contains "
                             "no passwords or key material.",
                  style="Muted.TLabel", wraplength=theme.px(500),
                  justify="left").pack(anchor="w", pady=(6, 20))

        # ------------------------------------------------------- library --
        ttk.Label(body, text="Vault library folder", style="TLabel").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(6, 2))
        self.library = tk.StringVar(value=settings.library)
        ttk.Entry(row, textvariable=self.library, font=theme.FONT).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", style="Tool.TButton",
                   command=self._browse).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Default", style="Tool.TButton",
                   command=self._reset_library).pack(side="left", padx=(6, 0))
        ttk.Label(body, style="Muted.TLabel", wraplength=theme.px(500), justify="left",
                  text="New vaults are created here and every vault inside it "
                       "is listed on the home screen. Changing this does not "
                       "move any existing vault - it only changes where the "
                       "app looks.").pack(anchor="w", pady=(0, 18))

        # -------------------------------------------------------- locking --
        ttk.Label(body, text="Lock automatically when idle",
                  style="TLabel").pack(anchor="w")
        self.autolock = tk.StringVar(value=str(settings["autolock_minutes"]))
        combo = ttk.Combobox(body, textvariable=self.autolock, state="readonly",
                             values=["0", "1", "2", "5", "10", "15", "30", "60"],
                             width=10)
        combo.pack(anchor="w", pady=(6, 2))
        ttk.Label(body, style="Muted.TLabel", wraplength=theme.px(500), justify="left",
                  text="Minutes of inactivity; 0 turns the idle timer off. The "
                       "lock is held while a document is still open in another "
                       "program, so an edit in progress is never destroyed."
                  ).pack(anchor="w", pady=(0, 16))

        self.on_minimize = tk.BooleanVar(value=bool(settings["lock_on_minimize"]))
        ttk.Checkbutton(body, text="Lock when the window is minimised",
                        variable=self.on_minimize).pack(anchor="w", pady=3)

        self.confirm_delete = tk.BooleanVar(value=bool(settings["confirm_delete"]))
        ttk.Checkbutton(body, text="Ask before deleting items",
                        variable=self.confirm_delete).pack(anchor="w", pady=3)

        self.clear_exit = tk.BooleanVar(value=bool(settings["clear_on_exit"]))
        ttk.Checkbutton(body,
                        text="Shred decrypted working copies on lock and exit",
                        variable=self.clear_exit).pack(anchor="w", pady=3)

        self.warn_external = tk.BooleanVar(
            value=bool(settings["warn_external_open"]))
        ttk.Checkbutton(
            body, variable=self.warn_external,
            text="Warn me before opening a file in another program").pack(
            anchor="w", pady=3)

        self.error = ttk.Label(body, text="", style="Danger.TLabel",
                               wraplength=theme.px(500), justify="left")
        self.error.pack(anchor="w", pady=(10, 0))

        row2 = ttk.Frame(body)
        row2.pack(fill="x", side="bottom")
        ttk.Button(row2, text="Save", style="Accent.TButton",
                   command=self._accept).pack(side="right")
        ttk.Button(row2, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))

    def _browse(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(
            parent=self, mustexist=True,
            title="Choose a folder to keep your vaults in")
        if chosen:
            self.library.set(os.path.abspath(chosen))

    def _reset_library(self):
        from .session import default_library
        self.library.set(default_library())

    def _accept(self):
        library = self.library.get().strip()
        if library:
            library = os.path.abspath(library)
            if not os.path.isdir(library):
                try:
                    os.makedirs(library, exist_ok=True)
                except OSError as exc:
                    self.error.configure(
                        text="That folder could not be created: %s" % exc)
                    return
            if not os.access(library, os.W_OK):
                self.error.configure(
                    text="That folder cannot be written to. Pick another.")
                return
            from .session import default_library
            self.settings["library"] = ("" if os.path.normcase(library)
                                        == os.path.normcase(default_library())
                                        else library)
        else:
            self.settings["library"] = ""

        try:
            minutes = int(self.autolock.get())
        except ValueError:
            minutes = 5
        self.settings["autolock_minutes"] = minutes
        self.settings["lock_on_minimize"] = self.on_minimize.get()
        self.settings["confirm_delete"] = self.confirm_delete.get()
        self.settings["clear_on_exit"] = self.clear_exit.get()
        self.settings["warn_external_open"] = self.warn_external.get()
        self.settings.save()
        self.result = True
        self.destroy()


# ------------------------------------------------------- save-back on close --

class SaveBackDialog(_Modal):
    """Asks what to do about working copies that were edited while open."""

    def __init__(self, parent, handles):
        super().__init__(parent, "Save changes back?", 500,
                         min(430, 230 + 22 * len(handles)))
        body = ttk.Frame(self, padding=theme.px(26))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Save changes back?", style="H2.TLabel").pack(anchor="w")
        ttk.Label(body, wraplength=theme.px(440), justify="left", style="Muted.TLabel",
                  text="These files were changed since you opened them. Saving "
                       "re-encrypts them into the vault; discarding shreds the "
                       "working copy and keeps the stored version.").pack(
            anchor="w", pady=(6, 14))

        listing = ttk.Frame(body, style="Surface.TFrame", padding=theme.px(12))
        listing.pack(fill="x")
        for handle in handles[:8]:
            ttk.Label(listing, text=handle.display_path or handle.name,
                      background=theme.SURFACE, foreground=theme.TEXT,
                      font=theme.FONT_SMALL).pack(anchor="w", pady=1)
        if len(handles) > 8:
            ttk.Label(listing, text="and %d more..." % (len(handles) - 8),
                      background=theme.SURFACE, foreground=theme.MUTED,
                      font=theme.FONT_SMALL).pack(anchor="w", pady=1)

        row = ttk.Frame(body)
        row.pack(fill="x", side="bottom", pady=(18, 0))
        ttk.Button(row, text="Save to vault", style="Accent.TButton",
                   command=lambda: self._pick("save")).pack(side="right")
        ttk.Button(row, text="Discard", style="Danger.TButton",
                   command=lambda: self._pick("discard")).pack(
            side="right", padx=(0, 8))
        ttk.Button(row, text="Cancel", command=self._on_close).pack(
            side="right", padx=(0, 8))

    def _pick(self, choice):
        self.result = choice
        self.destroy()

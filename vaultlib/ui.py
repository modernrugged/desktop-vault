"""Desktop Vault - Tkinter front end."""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from . import dialogs, editor, icons, shell, theme
from .crypto import CryptoError
from .session import Settings, Workspace
from .shamir import ShareError
from .store import (VAULT_SUFFIX, BadPassword, Vault, VaultError, is_dir,
                    list_vaults, move_vault, sanitize)
from .strength import estimate
from .theme import AutoScrollbar, Divider, PasswordEntry, human_size

APP_NAME = "Desktop Vault"
VERSION = "1.1"

# Drag-and-drop from Explorer needs the tkdnd Tcl package, which tkinterdnd2
# ships.  Without it the app still works; the Add buttons are the only route in.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _TkRoot = TkinterDnD.Tk
    DND_READY = True
except Exception:                                   # package or Tcl lib absent
    _TkRoot = tk.Tk
    DND_FILES = None
    DND_READY = False


def stamp(ts: int) -> str:
    try:
        return time.strftime("%d %b %Y  %H:%M", time.localtime(ts))
    except (ValueError, OSError):
        return "-"


def known_vaults(settings) -> list:
    """Every vault the app knows about, as ``(path, is_in_library)``.

    Library vaults are found by scanning; anything else is remembered in the
    settings.  Shared by the library screen and the Switch-to menu so the two
    can never disagree about what exists.
    """
    found = [(path, True) for path in list_vaults(settings.library)]
    seen = {os.path.normcase(path) for path, _lib in found}
    for path in settings["recent"]:
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isdir(path) and Vault.looks_like_vault(path):
            found.append((path, False))
    return found


def vault_label(path: str) -> str:
    name = os.path.basename(path.rstrip("\\/"))
    if name.lower().endswith(VAULT_SUFFIX):
        name = name[: -len(VAULT_SUFFIX)]
    return name or path


def kind_of(node: dict) -> str:
    if is_dir(node):
        return "Folder"
    _, ext = os.path.splitext(node["name"])
    return (ext[1:].upper() + " file") if len(ext) > 1 else "File"


# ============================================================== application ==

class App(_TkRoot):
    def __init__(self, initial_vault: str | None = None):
        # Before Tk opens anything: otherwise Windows reports 96 DPI and the
        # whole window gets bitmap-stretched on a scaled display.
        shell.enable_dpi_awareness()
        super().__init__()
        self.title(APP_NAME)
        self.settings = Settings()
        # theme.apply() measures the display and sets the scale, so everything
        # that depends on theme.px() has to come after it.
        self.style = theme.apply(self)
        # Clamped to the display: at 200% an unclamped minimum would be wider
        # than the screen and the window could not be resized to fit it.
        self.minsize(min(theme.px(940), self.winfo_screenwidth() - theme.px(40)),
                     min(theme.px(600), self.winfo_screenheight() - theme.px(80)))
        self.icons = icons.load(self)
        # Prefer the multi-resolution .ico on Windows; fall back to the
        # bitmap drawn in icons.py so the app still has an identity anywhere.
        ico = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "app.ico")
        try:
            if os.name == "nt" and os.path.isfile(ico):
                self.iconbitmap(default=ico)
            else:
                self.iconphoto(True, self.icons["lock_app"])
        except tk.TclError:
            try:
                self.iconphoto(True, self.icons["lock_app"])
            except tk.TclError:
                pass

        self.workspace = Workspace()
        self.vault: Vault | None = None
        self.screen: ttk.Frame | None = None
        self._last_activity = time.time()

        geometry = self.settings["geometry"]
        if geometry:
            try:
                self.geometry(geometry)
            except tk.TclError:
                theme.center(self, 1120, 720)
        else:
            theme.center(self, 1120, 720)

        shell.use_dark_titlebar(self)

        self.container = ttk.Frame(self, style="TFrame")
        self.container.pack(fill="both", expand=True)

        for sequence in ("<Any-KeyPress>", "<Button>", "<MouseWheel>",
                         "<Motion>"):
            self.bind_all(sequence, self._touch, add="+")
        self.bind("<Unmap>", self._on_unmap)
        self.bind_all("<Control-l>", self._hotkey("lock"))
        self.bind_all("<Control-f>", self._hotkey("_focus_search"))
        self.bind_all("<F5>", self._hotkey("refresh"))
        self.protocol("WM_DELETE_WINDOW", self.quit_app)

        self.show_start()
        if initial_vault:
            # Opened via a shortcut or the command line: go straight to the
            # password prompt for that vault.
            self.after(0, lambda: self.open_vault(os.path.abspath(initial_vault)))
        self.after(1000, self._idle_tick)

    # ------------------------------------------------------------- screens --

    def _swap(self, factory):
        if self.screen is not None:
            self.screen.destroy()
        self.screen = factory(self.container)
        self.screen.pack(fill="both", expand=True)
        return self.screen

    def show_start(self):
        self.title(APP_NAME)
        self._swap(lambda parent: StartScreen(parent, self))

    def show_unlock(self, vault: Vault):
        self.title("%s - %s" % (vault.name, APP_NAME))
        self._swap(lambda parent: UnlockScreen(parent, self, vault))

    def show_create(self):
        self.title("New vault - %s" % APP_NAME)
        self._swap(lambda parent: CreateScreen(parent, self))

    def show_browser(self, vault: Vault):
        self.vault = vault
        self.settings.push_recent(vault.path)
        self.title("%s - %s" % (vault.name, APP_NAME))
        self._touch()
        self._swap(lambda parent: BrowserScreen(parent, self, vault))

    # ------------------------------------------------------- vault plumbing --

    def open_vault_dialog(self):
        """Register a vault that lives outside the library, then open it."""
        path = filedialog.askdirectory(
            parent=self, mustexist=True,
            title="Select a vault folder (the one named YourVault.locker)")
        if not path:
            return
        path = os.path.abspath(path)

        if Vault.looks_like_vault(path):
            # Listed straight away, so it is remembered whether or not the
            # password is entered now.
            self.settings.push_recent(path)
            self.open_vault(path)
            return

        # A very easy mistake is picking the folder that holds the vaults.
        inside = list_vaults(path)
        if inside:
            names = "\n".join("    " + os.path.basename(p) for p in inside[:8])
            if len(inside) > 8:
                names += "\n    ... and %d more" % (len(inside) - 8)
            if dialogs.confirm(
                    self, "Add these vaults?",
                    "%s is not a vault itself, but it contains %d:"
                    % (os.path.basename(path), len(inside)),
                    names + "\n\nAdd all of them to your list?",
                    confirm_text="Add all", danger=False):
                for found in inside:
                    self.settings.push_recent(found)
                self.show_start()
            return

        dialogs.error(
            self, "Not a vault",
            "%s is not a Desktop Vault." % os.path.basename(path),
            "Pick the folder whose name ends in .locker - the one that "
            "contains vault.json.")

    def open_vault(self, path: str):
        if not os.path.isdir(path):
            dialogs.error(self, "Vault not found",
                          "This folder no longer exists:", path)
            self.settings.drop_recent(path)
            self.show_start()
            return
        if not Vault.looks_like_vault(path):
            dialogs.error(self, "Not a vault",
                          "That folder does not contain a Desktop Vault.",
                          "Pick the folder that holds vault.json.")
            return
        try:
            self.show_unlock(Vault(path))
        except VaultError as exc:
            dialogs.error(self, "Cannot open vault", str(exc))

    def lock_vault(self, back_to_start: bool = True):
        """Close working copies, wipe keys, return to the lock screen."""
        if self.vault is None:
            return
        if not self.settle_open_files():
            return False
        vault = self.vault
        try:
            vault.lock()
        except Exception:
            pass
        self.vault = None
        if back_to_start:
            self.show_unlock(Vault(vault.path))
        return True

    def switch_vault(self, target: str = None) -> bool:
        """Close the open vault, then show the library (or another vault).

        Returns False if the user cancelled at the unsaved-changes prompt, in
        which case nothing is locked and nothing has moved.
        """
        if self.vault is not None:
            if not self.settle_open_files():
                return False
            try:
                self.vault.lock()          # flushes a dirty index on the way
            except Exception:
                pass
            self.vault = None
        if target:
            self.open_vault(target)
        else:
            self.show_start()
        return True

    def settle_open_files(self) -> bool:
        """Deal with edited working copies.  False means the user cancelled."""
        changed = self.workspace.modified()
        if changed and self.vault is not None:
            choice = dialogs.SaveBackDialog(self, changed).show()
            if choice is None:
                return False
            if choice == "save":
                ok, err = dialogs.run_task(
                    self, "Saving changes",
                    lambda report, cancel: self._save_back(changed, report, cancel))
                if not ok:
                    dialogs.error(self, "Could not save everything",
                                  "Some changes were not written back.", str(err))
        self.workspace.close_all()
        return True

    def _save_back(self, handles, report, cancel):
        total = 0
        for handle in handles:
            try:
                total += os.path.getsize(handle.disk_path)
            except OSError:
                pass
        report(total=max(total, 1))
        done = 0
        for handle in handles:
            report(text="Re-encrypting %s" % handle.name)
            base = done
            self.vault.replace_file(
                handle.vault_path, handle.disk_path,
                progress=lambda n, b=base: report(done=b + n), cancel=cancel)
            try:
                done = base + os.path.getsize(handle.disk_path)
            except OSError:
                done = base
        return len(handles)

    # --------------------------------------------------------- idle / exit --

    def _hotkey(self, method):
        """Route a global shortcut to the browser screen, if one is showing."""
        def handler(_event=None):
            if isinstance(self.screen, BrowserScreen):
                getattr(self.screen, method)()
                return "break"
        return handler

    def _touch(self, _event=None):
        self._last_activity = time.time()

    def _on_unmap(self, _event=None):
        if (self.settings["lock_on_minimize"] and self.vault is not None
                and self.state() == "iconic"):
            self.after(50, lambda: self.lock_vault())

    def idle_seconds(self) -> float:
        return time.time() - self._last_activity

    def _idle_tick(self):
        minutes = int(self.settings["autolock_minutes"] or 0)
        if self.vault is not None and minutes > 0:
            if self.idle_seconds() >= minutes * 60:
                self._touch()
                if isinstance(self.screen, BrowserScreen):
                    self.screen.auto_lock()
        if isinstance(self.screen, BrowserScreen):
            self.screen.update_idle_readout()
        self.after(1000, self._idle_tick)

    def quit_app(self):
        if self.vault is not None:
            if not self.settle_open_files():
                return
            try:
                self.vault.lock()
            except Exception:
                pass
        elif self.settings["clear_on_exit"]:
            self.workspace.close_all()
        try:
            if self.state() == "normal":
                self.settings["geometry"] = self.geometry()
            self.settings.save()
        except tk.TclError:
            pass
        self.destroy()


# =============================================================== start page ==

class StartScreen(ttk.Frame):
    """The vault library: one window listing every vault, none of them open.

    This screen is deliberately not password protected -- it holds no vault
    contents, only names and locations.  Each vault below has its own password
    and is opened separately.
    """

    def __init__(self, parent, app: "App"):
        super().__init__(parent, style="TFrame")
        self.app = app

        outer = ttk.Frame(self, style="TFrame", padding=(theme.px(56), theme.px(34), theme.px(56), theme.px(24)))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer, style="TFrame")
        head.pack(fill="x")
        ttk.Label(head, image=app.icons["lock_big"], style="TLabel").pack(
            side="left", padx=(0, 16))
        titles = ttk.Frame(head, style="TFrame")
        titles.pack(side="left", anchor="w")
        ttk.Label(titles, text=APP_NAME, style="H1.TLabel").pack(anchor="w")
        ttk.Label(titles, style="Muted.TLabel",
                  text="Keys, recovery phrases and documents you cannot "
                       "replace. Each vault opens with its own password."
                  ).pack(anchor="w", pady=(2, 0))

        # fill="x" makes both buttons take the frame's width, which is set by
        # the wider of the two, so they always match whatever the labels say.
        buttons = ttk.Frame(head, style="TFrame")
        buttons.pack(side="right", anchor="e")
        ttk.Button(buttons, text="Create new vault", style="Accent.TButton",
                   command=app.show_create).pack(side="top", fill="x")
        ttk.Button(buttons, text="Add an existing vault...",
                   style="Secondary.TButton",
                   command=app.open_vault_dialog).pack(side="top", fill="x",
                                                       pady=(6, 0))

        ttk.Frame(outer, height=theme.px(22), style="TFrame").pack()

        row = ttk.Frame(outer, style="TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="VAULTS", style="Muted.TLabel").pack(side="left")
        self.library_label = ttk.Label(row, style="Muted.TLabel",
                                       cursor="hand2")
        self.library_label.pack(side="right")
        self.library_label.bind("<Button-1>", lambda _e: self._open_library())

        board = ttk.Frame(outer, style="Panel.TFrame", padding=theme.px(6))
        board.pack(fill="both", expand=True, pady=(8, 0))
        self.list_area = ttk.Frame(board, style="Panel.TFrame")
        self.list_area.pack(fill="both", expand=True)

        ttk.Label(outer, style="Muted.TLabel", wraplength=theme.px(760), justify="left",
                  text="Everything stays on this computer. There is no "
                       "account, no cloud sync and no recovery key - a vault's "
                       "password is the only way into it."
                  ).pack(anchor="w", pady=(16, 0))

        self.reload()

    # ------------------------------------------------------------- listing --

    def entries(self) -> list:
        """Library vaults first, then any registered from elsewhere."""
        return known_vaults(self.app.settings)

    def reload(self):
        for child in self.list_area.winfo_children():
            child.destroy()

        library = self.app.settings.library
        shown = library if len(library) <= 58 else "..." + library[-55:]
        self.library_label.configure(text="Library:  %s" % shown)

        entries = self.entries()
        if not entries:
            empty = ttk.Frame(self.list_area, style="Panel.TFrame", padding=theme.px(34))
            empty.pack(fill="both", expand=True)
            ttk.Label(empty, text="No vaults yet", style="Panel.TLabel",
                      font=theme.FONT_BOLD).pack()
            ttk.Label(empty, style="PanelMuted.TLabel", justify="center",
                      wraplength=theme.px(460),
                      text="Create one and it is kept in your library folder, "
                           "so the only thing you need on the desktop is this "
                           "app.").pack(pady=(6, 0))
            return

        for path, in_library in entries:
            self._row(path, in_library)

    def _row(self, path, in_library):
        row = ttk.Frame(self.list_area, style="Panel.TFrame", padding=(theme.px(12), theme.px(10)))
        row.pack(fill="x", pady=1)

        name = os.path.basename(path.rstrip("\\/"))
        if name.lower().endswith(VAULT_SUFFIX):
            name = name[: -len(VAULT_SUFFIX)]

        ttk.Label(row, image=self.app.icons["lock"],
                  style="Panel.TLabel").pack(side="left", padx=(2, 12))

        text = ttk.Frame(row, style="Panel.TFrame")
        text.pack(side="left", fill="x", expand=True)
        ttk.Label(text, text=name, style="Panel.TLabel",
                  font=theme.FONT_BOLD).pack(anchor="w")

        where = path if in_library else path
        if len(where) > 70:
            where = "..." + where[-67:]
        detail = where if not in_library else "in your library"
        size = self._size_on_disk(path)
        if size:
            detail += "  ·  %s" % human_size(size)
        ttk.Label(text, text=detail, style="PanelMuted.TLabel").pack(anchor="w")

        ttk.Button(row, text="Open", style="Accent.TButton",
                   command=lambda: self.app.open_vault(path)).pack(side="right")
        menu_btn = ttk.Button(row, text="⋯", style="Tool.TButton", width=3)
        menu_btn.pack(side="right", padx=(0, 8))
        menu_btn.configure(
            command=lambda b=menu_btn, p=path, lib=in_library:
            self._row_menu(b, p, lib))

        for widget in (row, text) + tuple(text.winfo_children()):
            widget.bind("<Double-Button-1>",
                        lambda _e, p=path: self.app.open_vault(p))

    @staticmethod
    def _size_on_disk(path) -> int:
        total = 0
        for root, _dirs, files in os.walk(os.path.join(path, "data")):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    # ------------------------------------------------------------- actions --

    def _row_menu(self, button, path, in_library):
        menu = tk.Menu(self, tearoff=0, bg=theme.SURFACE, fg=theme.TEXT,
                       activebackground=theme.SELECT,
                       activeforeground="#FFFFFF", bd=0, font=theme.FONT_SMALL)
        menu.add_command(label="Open", command=lambda: self.app.open_vault(path))
        menu.add_separator()
        if not in_library:
            menu.add_command(label="Move into my library",
                             command=lambda: self._move_in(path))
        menu.add_command(label="Show in Explorer",
                         command=lambda: self._reveal(path))
        if not in_library:
            menu.add_command(label="Remove from this list",
                             command=lambda: self._forget(path))
        menu.tk_popup(button.winfo_rootx(),
                      button.winfo_rooty() + button.winfo_height() + 2)

    def _move_in(self, path):
        destination = self.app.settings.ensure_library()
        if not dialogs.confirm(
                self.app, "Move this vault?",
                "Move the vault folder into your library at:\n%s" % destination,
                "The encrypted files are moved as they are. Nothing is "
                "decrypted and no password is needed.",
                confirm_text="Move", danger=False):
            return

        def worker(report, _cancel):
            return move_vault(path, destination,
                              progress=lambda text: report(text=text))

        ok, result = dialogs.run_task(self.app, "Moving vault", worker,
                                      cancellable=False)
        if not ok:
            dialogs.error(self.app, "The vault was not moved", str(result))
            return
        self.app.settings.drop_recent(path)
        shell.set_folder_icon(result)
        self.app.show_start()

    @staticmethod
    def _reveal(path):
        shell.open_path(path)

    def _open_library(self):
        shell.open_path(self.app.settings.ensure_library())

    def _forget(self, path):
        self.app.settings.drop_recent(path)
        self.app.show_start()


# ============================================================== unlock page ==

class UnlockScreen(ttk.Frame):
    """Password prompt.  Argon2 runs off the Tk thread so the UI stays live."""

    def __init__(self, parent, app: App, vault: Vault):
        super().__init__(parent, style="TFrame")
        self.app = app
        self.vault = vault
        self.failures = 0
        self.locked_until = 0.0
        self._busy = False

        wrap = ttk.Frame(self, style="TFrame", padding=(theme.px(50), theme.px(40)))
        wrap.place(relx=0.5, rely=0.45, anchor="center")

        ttk.Label(wrap, image=app.icons["lock_big"], style="TLabel").pack()
        ttk.Label(wrap, text=vault.name, style="H2.TLabel").pack(pady=(14, 2))
        ttk.Label(wrap, text=vault.path, style="Muted.TLabel").pack()

        ttk.Frame(wrap, height=theme.px(26), style="TFrame").pack()

        self.password = PasswordEntry(wrap, width=34)
        self.password.pack()
        self.password.bind_return(lambda _e: self.attempt())
        self.password.focus_set()

        self.status = ttk.Label(wrap, text="", style="Danger.TLabel",
                                wraplength=theme.px(380), justify="center")
        self.status.pack(pady=(12, 0))

        self.bar = ttk.Progressbar(wrap, mode="indeterminate", length=theme.px(300))

        buttons = ttk.Frame(wrap, style="TFrame")
        buttons.pack(pady=(18, 0))
        self.unlock_btn = ttk.Button(buttons, text="Unlock",
                                     style="Accent.TButton", command=self.attempt)
        self.unlock_btn.pack(side="left")
        ttk.Button(buttons, text="Back", command=app.show_start).pack(
            side="left", padx=(10, 0))

        if vault.has_recovery():
            ttk.Button(wrap, text="Use recovery shares instead",
                       style="Link.TButton", command=self.use_recovery).pack(
                pady=(14, 0))

    # ------------------------------------------------------------- attempts --

    def attempt(self):
        if self._busy:
            return
        remaining = self.locked_until - time.time()
        if remaining > 0:
            self.status.configure(
                text="Too many attempts. Try again in %d seconds."
                     % (int(remaining) + 1))
            return
        password = self.password.get()
        if not password:
            self.status.configure(text="Enter the vault password.")
            return

        self._busy = True
        self.unlock_btn.configure(state="disabled", text="Unlocking...")
        self.status.configure(text="")
        self.bar.pack(pady=(14, 0))
        self.bar.start(12)

        outcome = {}

        def work():
            try:
                self.vault.unlock(password)
                outcome["ok"] = True
            except BaseException as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.after(80, lambda: self._poll(thread, outcome))

    def _poll(self, thread, outcome):
        if thread.is_alive():
            self.after(80, lambda: self._poll(thread, outcome))
            return

        self.bar.stop()
        self.bar.pack_forget()
        self._busy = False
        self.unlock_btn.configure(state="normal", text="Unlock")

        if outcome.get("ok"):
            self.password.clear()
            self.app.show_browser(self.vault)
            return

        error = outcome.get("error")
        if isinstance(error, BadPassword):
            self.failures += 1
            self.password.clear()
            self.password.focus_set()
            if self.failures >= 3:
                delay = min(60, 5 * (2 ** (self.failures - 3)))
                self.locked_until = time.time() + delay
                self.status.configure(
                    text="Incorrect password. Locked out for %d seconds after "
                         "%d failed attempts." % (delay, self.failures))
                self._countdown()
            else:
                self.status.configure(text="Incorrect password.")
        else:
            self.status.configure(text=str(error) or "Could not open the vault.")

    def use_recovery(self):
        """Open the vault from shares, then offer to reset the password."""
        info = self.vault.recovery_info()
        if not info:
            dialogs.error(self.app, "No recovery shares",
                          "This vault does not have a recovery split.")
            return
        shares = dialogs.RecoveryUnlockDialog(
            self.app, info["threshold"], info["shares"]).show()
        if not shares:
            return
        try:
            self.vault.unlock_with_shares(shares)
        except (ShareError, VaultError) as exc:
            dialogs.error(self.app, "Could not unlock", str(exc))
            return

        new_password = dialogs.SetPasswordDialog(
            self.app, self.vault.name).show()
        if new_password:
            try:
                self.vault.set_password(new_password)
                dialogs.info(self.app, "Password set",
                             "This vault now opens with the new password.",
                             "Your recovery shares are unchanged and still "
                             "work.", kind="ok")
            except VaultError as exc:
                dialogs.error(self.app, "Password not changed", str(exc))
        else:
            dialogs.info(
                self.app, "Password left as it was",
                "The vault is open, but the old password is still the one it "
                "expects.",
                "Set a new one from Vault > Change password whenever you are "
                "ready.", kind="warn")
        self.app.show_browser(self.vault)

    def _countdown(self):
        remaining = self.locked_until - time.time()
        if remaining <= 0:
            self.status.configure(text="You can try again now.")
            self.unlock_btn.configure(state="normal")
            return
        self.unlock_btn.configure(state="disabled")
        self.status.configure(
            text="Incorrect password. Try again in %d seconds."
                 % (int(remaining) + 1))
        self.after(500, self._countdown)


# ============================================================== create page ==

class CreateScreen(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, style="TFrame")
        self.app = app

        wrap = ttk.Frame(self, style="TFrame", padding=(theme.px(50), theme.px(30)))
        wrap.place(relx=0.5, rely=0.5, anchor="center")

        ttk.Label(wrap, text="Create a new vault", style="H1.TLabel").pack(anchor="w")
        ttk.Label(wrap, style="Muted.TLabel", wraplength=theme.px(520), justify="left",
                  text="A vault is a folder on your disk. Everything inside it "
                       "is encrypted with a key derived from your password "
                       "alone.").pack(anchor="w", pady=(4, 24))

        ttk.Label(wrap, text="Vault name", style="Muted.TLabel").pack(anchor="w")
        self.name_var = tk.StringVar(value="My Vault")
        name_entry = ttk.Entry(wrap, textvariable=self.name_var, width=52,
                               font=theme.FONT)
        name_entry.pack(anchor="w", pady=(4, 16), fill="x")

        ttk.Label(wrap, text="Location", style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(wrap, style="TFrame")
        row.pack(fill="x", pady=(4, 16))
        self.dir_var = tk.StringVar(value=app.settings.ensure_library())
        ttk.Entry(row, textvariable=self.dir_var, font=theme.FONT).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse...", command=self._browse).pack(
            side="left", padx=(8, 0))

        ttk.Label(wrap, style="Muted.TLabel",
                  text="Your library folder - vaults kept here are listed on "
                       "the app's home screen.").pack(anchor="w", pady=(0, 12))

        # Off by default: the point of the library is that the app icon is the
        # only thing that needs to be on the desktop.
        self.make_shortcut = tk.BooleanVar(
            value=bool(app.settings["desktop_shortcuts"]))
        if shell.available():
            ttk.Checkbutton(
                wrap, variable=self.make_shortcut,
                text="Also put a shortcut to this vault on my Desktop"
                ).pack(anchor="w", pady=(0, 14))

        ttk.Label(wrap, text="Password", style="Muted.TLabel").pack(anchor="w")
        self.pw_var = tk.StringVar()
        self.pw_var.trace_add("write", lambda *_: self._rate())
        self.password = PasswordEntry(wrap, textvariable=self.pw_var, width=44)
        self.password.pack(fill="x", pady=(4, 6))

        self.meter = ttk.Progressbar(wrap, maximum=5, length=theme.px(400),
                                     style="S-.Horizontal.TProgressbar")
        self.meter.pack(fill="x")
        self.rating = ttk.Label(wrap, text=" ", style="Muted.TLabel",
                                wraplength=theme.px(520), justify="left")
        self.rating.pack(anchor="w", pady=(4, 14))

        ttk.Label(wrap, text="Confirm password", style="Muted.TLabel").pack(anchor="w")
        self.confirm = PasswordEntry(wrap, width=44)
        self.confirm.pack(fill="x", pady=(4, 18))
        self.confirm.bind_return(lambda _e: self.create())

        warning = ttk.Frame(wrap, style="Panel.TFrame", padding=theme.px(14))
        warning.pack(fill="x")
        ttk.Label(warning, text="There is no way to recover this password.",
                  style="Panel.TLabel", font=theme.FONT_BOLD).pack(anchor="w")
        ttk.Label(warning, style="PanelMuted.TLabel", wraplength=theme.px(500),
                  justify="left",
                  text="No reset link, no backup key, no support line. If you "
                       "forget it, the files in this vault are gone for good. "
                       "Write it down and keep the note somewhere safe.").pack(
            anchor="w", pady=(4, 8))
        self.acknowledged = tk.BooleanVar(value=False)
        ttk.Checkbutton(warning, variable=self.acknowledged,
                        style="Panel.TCheckbutton",
                        text="I understand and have a way to remember this "
                             "password").pack(anchor="w")

        self.error = ttk.Label(wrap, text="", style="Danger.TLabel",
                               wraplength=theme.px(520), justify="left")
        self.error.pack(anchor="w", pady=(10, 0))

        buttons = ttk.Frame(wrap, style="TFrame")
        buttons.pack(anchor="w", pady=(16, 0))
        ttk.Button(buttons, text="Create vault", style="Accent.TButton",
                   command=self.create).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=app.show_start).pack(
            side="left", padx=(10, 0))

        name_entry.focus_set()

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="Where to put the vault",
                                       mustexist=True)
        if path:
            self.dir_var.set(path)

    def _rate(self):
        result = estimate(self.pw_var.get())
        self.meter.configure(
            value=result["score"] + (1 if result["bits"] else 0),
            style=("S%d.Horizontal.TProgressbar" % result["score"])
            if result["label"] else "S-.Horizontal.TProgressbar")
        if not result["label"]:
            self.rating.configure(text=" ", foreground=theme.MUTED)
            return
        text = "%s  -  about %d bits of entropy" % (result["label"],
                                                    int(result["bits"]))
        if result["hint"]:
            text += "\n" + result["hint"]
        self.rating.configure(text=text, foreground=result["colour"])

    # -------------------------------------------------------------- create --

    def create(self):
        name = sanitize(self.name_var.get())
        parent_dir = self.dir_var.get().strip()
        password = self.password.get()

        if not name:
            self.error.configure(text="Give the vault a name.")
            return
        if not os.path.isdir(parent_dir):
            self.error.configure(text="That location does not exist.")
            return
        if len(password) < 8:
            self.error.configure(
                text="Use at least 8 characters. A passphrase of several "
                     "unrelated words is the easiest strong choice.")
            return
        if password != self.confirm.get():
            self.error.configure(text="The passwords do not match.")
            return
        if not self.acknowledged.get():
            self.error.configure(
                text="Please confirm you understand the password cannot be "
                     "recovered.")
            return

        target = os.path.join(parent_dir, name + VAULT_SUFFIX)
        if os.path.exists(target) and os.listdir(target):
            self.error.configure(text="A folder named %s already exists there."
                                      % os.path.basename(target))
            return

        def worker(report, _cancel):
            report(text="Measuring how hard this machine can make the key "
                        "derivation...")
            from .crypto import calibrate_kdf
            params = calibrate_kdf()
            report(text="Generating master key and writing the vault...")
            return Vault.create(target, password, params)

        ok, result = dialogs.run_task(self.app, "Creating vault", worker,
                                      cancellable=False)
        if not ok:
            dialogs.error(self.app, "Could not create the vault",
                          "Nothing was written.", str(result))
            return

        params = result.header["kdf"]
        detail = ("Key derivation: Argon2id, %d MiB memory, %d passes.\n"
                  "Content cipher: AES-256-GCM."
                  % (params["memory_cost"] // 1024, params["time_cost"]))

        if shell.available():
            shell.set_folder_icon(target)
            if self.make_shortcut.get():
                link = shell.create_shortcut(target)
                if link:
                    detail += "\n\nShortcut placed at:\n%s" % link
            detail += ("\n\nThe vault folder itself stays on disk and Explorer "
                       "can still open it, but everything inside is "
                       "ciphertext.")

        dialogs.info(self.app, "Vault created",
                     "%s is ready at:\n%s" % (name, target), detail, kind="ok")
        self.app.show_browser(result)


# ============================================================= browser page ==

class BrowserScreen(ttk.Frame):
    COLUMNS = (("name", "Name", 400, "w"),
               ("size", "Size", 100, "e"),
               ("modified", "Modified", 165, "w"),
               ("kind", "Type", 110, "w"))

    def __init__(self, parent, app: App, vault: Vault):
        super().__init__(parent, style="TFrame")
        self.app = app
        self.vault = vault
        self.cwd: list = []
        self.searching = False
        self.sort_key = "name"
        self.sort_desc = False
        self._tree_paths: dict = {}
        self._tree_iids: dict = {}
        self._list_paths: dict = {}
        self._expanded = {()}

        self._build_toolbar()
        Divider(self).pack(fill="x")
        self._build_breadcrumb()
        Divider(self).pack(fill="x")
        self._build_body()
        Divider(self).pack(fill="x", side="bottom")
        self._build_status()

        self._build_menus()
        self._bind_keys()
        self._enable_drop()
        self.refresh(full=True)
        self._watch_open_files()

    # ------------------------------------------------------------ chrome --

    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(theme.px(10), theme.px(8)))
        bar.pack(fill="x")

        def tool(text, command, width=None):
            btn = ttk.Button(bar, text=text, style="Tool.TButton",
                             command=command)
            if width:
                btn.configure(width=width)
            btn.pack(side="left", padx=2)
            return btn

        tool("Add files", self.add_files)
        tool("Add folder", self.add_folder)
        tool("New folder", self.new_folder)
        ttk.Frame(bar, width=1, style="Sep.TFrame").pack(
            side="left", fill="y", padx=8, pady=4)
        self.btn_open = tool("Open", self.open_selected)
        self.btn_edit = tool("Edit", self.edit_selected)
        self.btn_export = tool("Export...", self.export_selected)
        self.btn_rename = tool("Rename", self.rename_selected)
        self.btn_delete = tool("Delete", self.delete_selected)

        # Appears only while an externally-opened working copy has changes.
        self.btn_save = ttk.Button(bar, text="Save changes",
                                   style="Accent.TButton",
                                   command=self.save_open_changes)

        ttk.Button(bar, text="Lock now", style="Accent.TButton",
                   command=self.lock).pack(side="right", padx=(6, 0))
        # Secondary actions live behind one button so the toolbar still fits
        # at the minimum window width.
        self.vault_btn = ttk.Button(bar, text="Vault ▾", style="Tool.TButton",
                                    command=self._post_vault_menu)
        self.vault_btn.pack(side="right", padx=2)

    def _build_breadcrumb(self):
        # Search sits on this row rather than in the toolbar: the toolbar runs
        # out of width on a narrow window and squeezes the entry shut.
        row = ttk.Frame(self, style="Panel.TFrame", padding=(theme.px(12), theme.px(4)))
        row.pack(fill="x")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._search_changed())
        search = ttk.Entry(row, textvariable=self.search_var, width=30,
                           style="Search.TEntry", font=theme.FONT_SMALL)
        search.pack(side="right")
        search.bind("<Escape>", lambda _e: self.clear_search())
        self.search_entry = search
        self.search_hint = ttk.Label(row, text="Search files and folders",
                                     style="Placeholder.TLabel")
        self.search_hint.bind("<Button-1>", lambda _e: self._focus_search())

        self.crumbs = ttk.Frame(row, style="Panel.TFrame")
        self.crumbs.pack(side="left", fill="x", expand=True)
        self._sync_search_hint()

    def _build_body(self):
        body = ttk.Frame(self, style="TFrame")
        body.pack(fill="both", expand=True)

        side = ttk.Frame(body, style="Panel.TFrame", width=theme.px(250))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        ttk.Label(side, text="FOLDERS", style="PanelMuted.TLabel").pack(
            anchor="w", padx=14, pady=(12, 6))

        tree_wrap = ttk.Frame(side, style="Panel.TFrame")
        tree_wrap.pack(fill="both", expand=True, padx=(6, 6), pady=(0, 8))
        self.tree = ttk.Treeview(tree_wrap, show="tree", selectmode="browse",
                                 style="Side.Treeview")
        tree_scroll = AutoScrollbar(tree_wrap, orient="vertical",
                                    command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_toggle)
        self.tree.bind("<<TreeviewClose>>", self._on_tree_toggle)

        Divider(body, vertical=True).pack(side="left", fill="y")

        right = ttk.Frame(body, style="TFrame")
        right.pack(side="left", fill="both", expand=True)

        self.listing = ttk.Treeview(
            right, columns=[c[0] for c in self.COLUMNS],
            show="tree headings", selectmode="extended")
        self.listing.column("#0", width=theme.px(30), stretch=False,
                            anchor="center")
        self.listing.heading("#0", text="")
        for key, label, width, anchor in self.COLUMNS:
            self.listing.column(key, width=theme.px(width), anchor=anchor,
                                stretch=(key == "name"))
            self.listing.heading(key, text=label, anchor=anchor,
                                 command=lambda k=key: self.sort_by(k))
        scroll = AutoScrollbar(right, orient="vertical",
                               command=self.listing.yview)
        self.listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.listing.pack(side="left", fill="both", expand=True)

        self.listing.tag_configure("folder", foreground=theme.TEXT)
        self.listing.tag_configure("file", foreground="#C9CEDA")
        self.listing.bind("<Double-Button-1>", self._on_activate)
        self.listing.bind("<Return>", self._on_activate)
        self.listing.bind("<Button-3>", self._on_context)
        self.listing.bind("<<TreeviewSelect>>", lambda _e: self._sync_buttons())

        self.empty = ttk.Label(right, style="Muted.TLabel", justify="center",
                               text="")
        self.drop_hint = ttk.Label(right, style="Drop.TLabel",
                                   text="Drop to add to this vault")

    def _build_status(self):
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(theme.px(12), theme.px(6)))
        bar.pack(fill="x", side="bottom")
        self.status_left = ttk.Label(bar, text="", style="PanelMuted.TLabel")
        self.status_left.pack(side="left")
        self.status_right = ttk.Label(bar, text="", style="PanelMuted.TLabel")
        self.status_right.pack(side="right")
        self.status_open = ttk.Label(bar, text="", style="PanelMuted.TLabel",
                                     cursor="hand2")
        self.status_open.pack(side="right", padx=(0, 18))
        self.status_open.bind("<Button-1>", lambda _e: self.close_open_files())

    def _build_menus(self):
        self.menu = tk.Menu(self, tearoff=0, bg=theme.SURFACE, fg=theme.TEXT,
                            activebackground=theme.SELECT,
                            activeforeground="#FFFFFF", bd=0,
                            font=theme.FONT_SMALL)
        self.menu.add_command(label="Open", command=self.open_selected)
        self.menu.add_command(label="Edit in Desktop Vault",
                              command=self.edit_selected)
        self.menu.add_command(label="Open with Windows",
                              command=self.open_externally)
        self.menu.add_command(label="Export...", command=self.export_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Rename", command=self.rename_selected)
        self.menu.add_command(label="Delete", command=self.delete_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Details", command=self.show_details)

        self.vault_menu = tk.Menu(self, tearoff=0, bg=theme.SURFACE,
                                  fg=theme.TEXT, activebackground=theme.SELECT,
                                  activeforeground="#FFFFFF", bd=0,
                                  font=theme.FONT_SMALL)
        self.switch_menu = tk.Menu(self.vault_menu, tearoff=0,
                                   bg=theme.SURFACE, fg=theme.TEXT,
                                   activebackground=theme.SELECT,
                                   activeforeground="#FFFFFF", bd=0,
                                   font=theme.FONT_SMALL)
        self.vault_menu.add_cascade(label="Switch to", menu=self.switch_menu)
        self.vault_menu.add_command(label="Lock and show all vaults",
                                    command=self.switch_vault)
        self.vault_menu.add_separator()
        self.vault_menu.add_command(label="Change password...",
                                    command=self.change_password)
        self.vault_menu.add_command(label="Recovery shares...",
                                    command=self.manage_recovery)
        self.vault_menu.add_command(label="Settings...",
                                    command=self.open_settings)
        self.vault_menu.add_separator()
        self.vault_menu.add_command(label="Check vault integrity",
                                    command=self.check_integrity)
        self.vault_menu.add_command(label="Save open working copies",
                                    command=self.save_open_changes)
        self.vault_menu.add_command(label="Close open working copies",
                                    command=self.close_open_files)
        self.vault_menu.add_separator()
        self.vault_menu.add_command(label="New text file",
                                    command=self.new_text_file)
        self.vault_menu.add_separator()
        self._hide_index = None
        if shell.available():
            self.vault_menu.add_command(label="Put a shortcut on the Desktop",
                                        command=self.make_desktop_shortcut)
            self.vault_menu.add_command(label="Hide the vault folder in Explorer",
                                        command=self.toggle_hidden)
            self._hide_index = self.vault_menu.index("end")
            self.vault_menu.add_separator()
        self.vault_menu.add_command(label="About this vault",
                                    command=self.show_about)

    def _sync_hide_label(self):
        if self._hide_index is None:
            return
        self.vault_menu.entryconfigure(
            self._hide_index,
            label="Show the vault folder in Explorer"
            if shell.is_hidden(self.vault.path)
            else "Hide the vault folder in Explorer")

    def _sync_switch_menu(self):
        """Rebuild the Switch-to list; vaults can appear or vanish any time."""
        self.switch_menu.delete(0, "end")
        current = os.path.normcase(self.vault.path)
        others = [(path, in_lib)
                  for path, in_lib in known_vaults(self.app.settings)
                  if os.path.normcase(path) != current]
        if not others:
            self.switch_menu.add_command(label="No other vaults",
                                         state="disabled")
            return
        for path, in_library in others[:20]:
            label = vault_label(path)
            if not in_library:
                label += "   (%s)" % os.path.dirname(path)
            self.switch_menu.add_command(
                label=label, command=lambda p=path: self.switch_vault(p))
        if len(others) > 20:
            self.switch_menu.add_separator()
            self.switch_menu.add_command(
                label="Show all %d vaults..." % len(others),
                command=self.switch_vault)

    def _post_vault_menu(self):
        self._sync_switch_menu()
        self._sync_hide_label()
        self.vault_menu.tk_popup(
            self.vault_btn.winfo_rootx(),
            self.vault_btn.winfo_rooty() + self.vault_btn.winfo_height() + 2)

    # ------------------------------------------------------- drag and drop --

    def _enable_drop(self):
        """Accept files and folders dropped from Explorer."""
        if not DND_READY:
            return
        for widget in (self.listing, self.tree):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            except tk.TclError:
                return

    def _on_drop_enter(self, event):
        self.drop_hint.place(relx=0.5, rely=0.06, anchor="n")
        return event.action

    def _on_drop_leave(self, event):
        self.drop_hint.place_forget()
        return event.action

    def _on_drop(self, event):
        self.drop_hint.place_forget()
        try:
            paths = [p for p in self.tk.splitlist(event.data) if p]
        except tk.TclError:
            paths = []
        paths = [os.path.normpath(p) for p in paths if os.path.exists(p)]
        if not paths:
            return event.action

        # A drop onto a folder row goes into that folder, not the current one.
        target = list(self.cwd)
        if event.widget is self.tree:
            iid = self.tree.identify_row(
                event.y_root - self.tree.winfo_rooty())
            if iid in self._tree_paths:
                target = list(self._tree_paths[iid])
        else:
            iid = self.listing.identify_row(
                event.y_root - self.listing.winfo_rooty())
            if iid in self._list_paths and not self.searching:
                candidate = self._list_paths[iid]
                try:
                    if is_dir(self.vault.node_at(candidate)):
                        target = list(candidate)
                except VaultError:
                    pass

        self.import_paths(paths, target)
        return event.action

    def _source_info(self, path) -> dict:
        try:
            st = os.stat(path)
        except OSError:
            return {"size": "-", "modified": "-"}
        if os.path.isdir(path):
            total = 0
            for root, _dirs, names in os.walk(path):
                for name in names:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
            return {"size": "%s in total" % human_size(total),
                    "modified": stamp(int(st.st_mtime))}
        return {"size": human_size(st.st_size),
                "modified": stamp(int(st.st_mtime))}

    def _node_info(self, node) -> dict:
        if is_dir(node):
            return {"size": "%s in total" % human_size(self._tree_bytes(node)),
                    "modified": stamp(int(node.get("modified", 0)))}
        return {"size": human_size(int(node.get("size", 0))),
                "modified": stamp(int(node.get("modified", 0)))}

    def _resolve_conflicts(self, paths, target):
        """Ask about every name that is already taken, before anything is written.

        Deciding up front keeps the encrypting worker free of UI prompts, and
        means the user answers while they can still abandon the whole import.
        """
        try:
            folder = self.vault.dir_at(target)
        except VaultError:
            return [(path, "keep_both") for path in paths]
        existing = folder["children"]
        where = "/".join(target) if target else self.vault.name

        clashes = [p for p in paths if sanitize(os.path.basename(
            p.rstrip("\\/"))) in existing]
        decisions = []
        blanket = None
        left = len(clashes)

        for path in paths:
            name = sanitize(os.path.basename(path.rstrip("\\/")))
            if name not in existing:
                decisions.append((path, "keep_both"))
                continue
            left -= 1
            if blanket is not None:
                decisions.append((path, blanket))
                continue
            answer = dialogs.ConflictDialog(
                self.app, name, where, os.path.isdir(path),
                self._node_info(existing[name]), self._source_info(path),
                remaining=left).show()
            if answer is None:
                return None                      # whole import abandoned
            if answer.get("all"):
                blanket = answer["action"]
            decisions.append((path, answer["action"]))
        return decisions

    def import_paths(self, paths, target=None):
        """Encrypt a mixed list of files and folders into ``target``."""
        target = list(self.cwd) if target is None else list(target)
        paths = [p for p in paths if os.path.isfile(p) or os.path.isdir(p)]
        if not paths:
            return

        decisions = self._resolve_conflicts(paths, target)
        if decisions is None:
            return
        keep = [(p, how) for p, how in decisions if how != "skip"]
        if not keep:
            return
        replace = {p for p, how in keep if how == "overwrite"}
        files = [p for p, _how in keep if os.path.isfile(p)]
        folders = [p for p, _how in keep if os.path.isdir(p)]

        def worker(report, cancel):
            report(text="Scanning...")
            total = 0
            for path in files:
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
            for path in folders:
                for root, _dirs, names in os.walk(path):
                    for name in names:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            pass
            report(total=max(total, 1))

            state = {"base": 0, "current": 0}

            def on_file(path):
                state["base"] += state["current"]
                state["current"] = 0
                report(text="Encrypting %s" % os.path.basename(path))

            def progress(n):
                state["current"] = n
                report(done=state["base"] + n)

            def clear_the_way(path):
                """Overwrite means the stored copy goes first, blobs and all."""
                if path not in replace:
                    return
                name = sanitize(os.path.basename(path.rstrip("\\/")))
                try:
                    self.vault.delete(target + [name])
                except VaultError:
                    pass

            for path in files:
                if cancel.is_set():
                    break
                on_file(path)
                clear_the_way(path)
                self.vault.add_file(target, path, progress=progress,
                                    cancel=cancel)
            for path in folders:
                if cancel.is_set():
                    break
                report(text="Adding %s" % os.path.basename(path))
                clear_the_way(path)
                self.vault.add_folder(target, path, progress, cancel, on_file)
            return len(files) + len(folders)

        ok, result = dialogs.run_task(self.app, "Adding to vault", worker)
        self._after_import(ok, result, "items")

    def _bind_keys(self):
        self.listing.bind("<Delete>", lambda _e: self.delete_selected())
        self.listing.bind("<F2>", lambda _e: self.rename_selected())
        self.listing.bind("<BackSpace>", lambda _e: self.go_up())
        self.listing.bind("<Control-a>", self._select_all)

    # ------------------------------------------------------------ refresh --

    def refresh(self, full: bool = False):
        if not self.vault.is_unlocked:
            return
        if full:
            self._rebuild_tree()
        self._rebuild_crumbs()
        self._rebuild_listing()
        self._update_status()
        self._sync_buttons()

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        self._tree_paths.clear()
        self._tree_iids.clear()
        root_iid = self.tree.insert("", "end", text="  " + self.vault.name,
                                    image=self.app.icons["folder"], open=True)
        self._tree_paths[root_iid] = []
        self._tree_iids[()] = root_iid
        self._insert_children(root_iid, [], self.vault.root())
        self._select_tree_path(self.cwd)

    def _insert_children(self, parent_iid, path, node):
        folders = [c for c in node["children"].values() if is_dir(c)]
        folders.sort(key=lambda n: n["name"].lower())
        for child in folders:
            child_path = path + [child["name"]]
            iid = self.tree.insert(parent_iid, "end", text="  " + child["name"],
                                   image=self.app.icons["folder"],
                                   open=tuple(child_path) in self._expanded)
            self._tree_paths[iid] = child_path
            self._tree_iids[tuple(child_path)] = iid
            self._insert_children(iid, child_path, child)

    def _select_tree_path(self, path):
        iid = self._tree_iids.get(tuple(path))
        if iid:
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _rebuild_crumbs(self):
        for widget in self.crumbs.winfo_children():
            widget.destroy()
        if self.searching:
            ttk.Label(self.crumbs, text="Search results for %r"
                                        % self.search_var.get().strip(),
                      style="PanelMuted.TLabel").pack(side="left")
            ttk.Button(self.crumbs, text="Clear", style="Crumb.TButton",
                       command=self.clear_search).pack(side="left", padx=(8, 0))
            return

        ttk.Button(self.crumbs, text=self.vault.name, style="Crumb.TButton",
                   command=lambda: self.navigate([])).pack(side="left")
        for depth, part in enumerate(self.cwd):
            ttk.Label(self.crumbs, text="/", style="PanelMuted.TLabel").pack(
                side="left", padx=2)
            target = self.cwd[:depth + 1]
            ttk.Button(self.crumbs, text=part, style="Crumb.TButton",
                       command=lambda t=target: self.navigate(t)).pack(side="left")

    def _rebuild_listing(self):
        self.listing.delete(*self.listing.get_children())
        self._list_paths.clear()

        if self.searching:
            query = self.search_var.get().strip()
            rows = [(path, node) for path, node in self.vault.search(query)]
        else:
            try:
                folder = self.vault.dir_at(self.cwd)
            except VaultError:
                self.cwd = []
                folder = self.vault.root()
            rows = [(self.cwd + [n["name"]], n)
                    for n in folder["children"].values()]

        rows.sort(key=self._sort_value, reverse=self.sort_desc)
        rows.sort(key=lambda r: 0 if is_dir(r[1]) else 1)

        for path, node in rows:
            folder = is_dir(node)
            values = (
                node["name"] if not self.searching else "/".join(path),
                "-" if folder else human_size(int(node.get("size", 0))),
                stamp(int(node.get("modified", 0))),
                kind_of(node),
            )
            iid = self.listing.insert(
                "", "end", values=values,
                image=self.app.icons["folder" if folder else "file"],
                tags=("folder" if folder else "file",))
            self._list_paths[iid] = path

        if not rows:
            message = ("Nothing matches that search."
                       if self.searching else
                       "This folder is empty.\nUse Add files or Add folder to "
                       "put something in it.")
            self.empty.configure(text=message)
            self.empty.place(relx=0.5, rely=0.42, anchor="center")
        else:
            self.empty.place_forget()

    def _sort_value(self, row):
        path, node = row
        if self.sort_key == "size":
            return int(node.get("size", 0))
        if self.sort_key == "modified":
            return int(node.get("modified", 0))
        if self.sort_key == "kind":
            return kind_of(node).lower()
        return node["name"].lower()

    def sort_by(self, key):
        self.sort_desc = (not self.sort_desc) if key == self.sort_key else False
        self.sort_key = key
        for column, label, _w, anchor in self.COLUMNS:
            arrow = ""
            if column == key:
                arrow = "  ▾" if self.sort_desc else "  ▴"
            self.listing.heading(column, text=label + arrow, anchor=anchor)
        self._rebuild_listing()

    def _update_status(self):
        try:
            info = self.vault.stats()
        except VaultError:
            return
        self.status_left.configure(
            text="%d files  ·  %d folders  ·  %s stored  ·  %s on disk"
                 % (info["files"], info["folders"], human_size(info["bytes"]),
                    human_size(info["on_disk"])))
        self._update_open_count()
        self.update_idle_readout()

    def _update_open_count(self):
        count = len(self.app.workspace.opened)
        self.status_open.configure(
            text="%d open working %s - click to close"
                 % (count, "copy" if count == 1 else "copies")
            if count else "")

    def update_idle_readout(self):
        minutes = int(self.app.settings["autolock_minutes"] or 0)
        if not minutes:
            self.status_right.configure(text="Auto-lock off",
                                        foreground=theme.MUTED)
            return
        if getattr(self, "_autolock_held", False) and self.app.workspace.opened:
            count = len(self.app.workspace.opened)
            self.status_right.configure(
                text="Auto-lock held - %d file%s still open elsewhere"
                     % (count, "" if count == 1 else "s"),
                foreground=theme.WARN)
            return
        self._autolock_held = False
        remaining = max(0, int(minutes * 60 - self.app.idle_seconds()))
        self.status_right.configure(
            text="Auto-lock in %d:%02d" % (remaining // 60, remaining % 60),
            foreground=theme.MUTED)

    def _sync_buttons(self):
        selection = self.selected_paths()
        one = len(selection) == 1
        any_selected = bool(selection)
        editable = False
        try:
            single_file = one and not is_dir(self.vault.node_at(selection[0]))
            if single_file:
                node = self.vault.node_at(selection[0])
                editable = editor.looks_editable(node["name"],
                                                 int(node.get("size", 0)))
        except VaultError:
            single_file = False
        self.btn_open.configure(state="normal" if single_file else "disabled")
        self.btn_edit.configure(state="normal" if editable else "disabled")
        self.btn_export.configure(state="normal" if any_selected else "disabled")
        self.btn_rename.configure(
            state="normal" if one and not self.searching else "disabled")
        self.btn_delete.configure(state="normal" if any_selected else "disabled")

    # --------------------------------------------------------- navigation --

    def navigate(self, path):
        self.cwd = list(path)
        self._expanded.add(tuple(path))
        if self.searching:
            self.searching = False
            self.search_var.set("")
        self.refresh()
        self._select_tree_path(self.cwd)

    def go_up(self):
        if self.cwd:
            self.navigate(self.cwd[:-1])

    def _on_tree_select(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        path = self._tree_paths.get(selection[0])
        if path is not None and path != self.cwd:
            self.cwd = list(path)
            self.searching = False
            if self.search_var.get():
                self.search_var.set("")
            self.refresh()

    def _on_tree_toggle(self, _event):
        for iid, path in self._tree_paths.items():
            if self.tree.item(iid, "open"):
                self._expanded.add(tuple(path))
            else:
                self._expanded.discard(tuple(path))

    def _on_activate(self, _event=None):
        paths = self.selected_paths()
        if len(paths) != 1:
            return
        node = self.vault.node_at(paths[0])
        if is_dir(node):
            self.navigate(paths[0])
        else:
            self.open_selected()

    def _on_context(self, event):
        row = self.listing.identify_row(event.y)
        if row:
            if row not in self.listing.selection():
                self.listing.selection_set(row)
            self._sync_buttons()
            self.menu.tk_popup(event.x_root, event.y_root)

    def _select_all(self, _event=None):
        self.listing.selection_set(self.listing.get_children())
        return "break"

    def selected_paths(self):
        return [self._list_paths[iid] for iid in self.listing.selection()
                if iid in self._list_paths]

    # ------------------------------------------------------------- search --

    def _focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")
        return "break"

    def _sync_search_hint(self):
        if self.search_var.get():
            self.search_hint.place_forget()
        else:
            self.search_hint.place(in_=self.search_entry, x=7, rely=0.5,
                                   anchor="w")

    def _search_changed(self):
        self._sync_search_hint()
        query = self.search_var.get().strip()
        was = self.searching
        self.searching = bool(query)
        if self.searching or was:
            self._rebuild_crumbs()
            self._rebuild_listing()
            self._sync_buttons()

    def clear_search(self):
        self.search_var.set("")
        self.searching = False
        self.refresh()
        self.listing.focus_set()

    # -------------------------------------------------------------- import --

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.app,
                                            title="Add files to the vault")
        if paths:
            self.import_paths(list(paths))

    def add_folder(self):
        source = filedialog.askdirectory(parent=self.app,
                                         title="Add a folder to the vault",
                                         mustexist=True)
        if source:
            self.import_paths([source])

    def _after_import(self, ok, result, what):
        self.refresh(full=True)
        if not ok and not isinstance(result, CryptoError):
            dialogs.error(self.app, "Import problem",
                          "Some %s could not be added." % what, str(result))

    def new_folder(self):
        name = dialogs.ask_text(self.app, "New folder", "Name for the folder",
                                "New folder", "Create")
        if not name:
            return
        try:
            self.vault.mkdir(self.cwd, name)
        except VaultError as exc:
            dialogs.error(self.app, "Could not create folder", str(exc))
            return
        self.refresh(full=True)

    # -------------------------------------------------------------- export --

    def export_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        destination = filedialog.askdirectory(
            parent=self.app, title="Export decrypted copies to", mustexist=True)
        if not destination:
            return
        if not dialogs.confirm(
                self.app, "Export unencrypted?",
                "%d item%s will be written to %s as ordinary, unprotected "
                "files." % (len(paths), "" if len(paths) == 1 else "s",
                            destination),
                "Anyone with access to that location will be able to read them.",
                confirm_text="Export", danger=False):
            return

        def worker(report, cancel):
            total = 0
            for path in paths:
                node = self.vault.node_at(path)
                total += self._tree_bytes(node)
            report(total=max(total, 1))
            state = {"base": 0, "current": 0}

            def progress(n):
                state["current"] = n
                report(done=state["base"] + n)

            for path in paths:
                if cancel.is_set():
                    break
                report(text="Decrypting %s" % path[-1])
                node = self.vault.node_at(path)
                before = state["base"]
                self.vault.extract_tree(path, destination, progress, cancel)
                state["base"] = before + self._tree_bytes(node)
                state["current"] = 0
            return destination

        ok, result = dialogs.run_task(self.app, "Exporting", worker)
        if ok:
            dialogs.info(self.app, "Export finished",
                         "Decrypted copies were written to:\n%s" % destination,
                         "Those copies are no longer protected by the vault.",
                         kind="warn")
        elif not isinstance(result, CryptoError):
            dialogs.error(self.app, "Export failed", str(result))

    def _tree_bytes(self, node) -> int:
        if not is_dir(node):
            return int(node.get("size", 0))
        return sum(self._tree_bytes(c) for c in node["children"].values())

    # ---------------------------------------------------------------- open --

    def open_selected(self):
        """Text opens in the built-in editor; anything else goes to Windows."""
        paths = self.selected_paths()
        if len(paths) != 1:
            return
        node = self.vault.node_at(paths[0])
        if is_dir(node):
            self.navigate(paths[0])
            return
        if editor.looks_editable(node["name"], int(node.get("size", 0))):
            self.edit_selected()
        else:
            self.open_externally()

    def open_externally(self):
        """Decrypt to a tracked working copy and hand it to Windows."""
        paths = self.selected_paths()
        if len(paths) != 1:
            return
        path = paths[0]
        node = self.vault.node_at(path)
        if is_dir(node):
            return

        if self.app.settings["warn_external_open"]:
            answer = dialogs.ExternalOpenDialog(self.app, node["name"]).show()
            if not answer:
                return
            if answer.get("dont_ask"):
                self.app.settings["warn_external_open"] = False
                self.app.settings.save()

        disk_path = self.app.workspace.reserve(sanitize(node["name"]))

        def worker(report, cancel):
            report(text="Decrypting %s" % node["name"],
                   total=max(int(node.get("size", 0)), 1))
            self.vault.extract_file(path, disk_path,
                                    progress=lambda n: report(done=n),
                                    cancel=cancel)
            return disk_path

        ok, result = dialogs.run_task(self.app, "Opening file", worker)
        if not ok:
            if not isinstance(result, CryptoError):
                dialogs.error(self.app, "Could not open the file", str(result))
            return

        handle = self.app.workspace.track(path, disk_path)
        try:
            self.app.workspace.launch(disk_path)
        except OSError as exc:
            dialogs.info(
                self.app, "No program is associated with this file",
                "A decrypted working copy is here:\n%s" % disk_path,
                "It will be shredded when the vault locks.\n(%s)" % exc,
                kind="warn")
        self._update_status()

    def edit_selected(self):
        """Open a text file in the built-in editor - no plaintext on disk."""
        paths = self.selected_paths()
        if len(paths) != 1:
            return
        path = paths[0]
        node = self.vault.node_at(path)
        if is_dir(node):
            self.navigate(path)
            return
        try:
            window = editor.EditorWindow(self.app, self.vault, path,
                                         on_saved=self.refresh)
        except ValueError as exc:
            dialogs.error(self.app, "Not a text file", str(exc))
            return
        except (VaultError, CryptoError) as exc:
            dialogs.error(self.app, "Could not open the editor", str(exc))
            return
        window.focus_set()

    def new_text_file(self):
        name = dialogs.ask_text(self.app, "New text file",
                                "Name for the new file", "Untitled.txt",
                                "Create")
        if not name:
            return
        try:
            created = self.vault.create_text_file(self.cwd, name)
        except VaultError as exc:
            dialogs.error(self.app, "Could not create the file", str(exc))
            return
        self.refresh(full=True)
        editor.EditorWindow(self.app, self.vault, self.cwd + [created],
                            on_saved=self.refresh).focus_set()

    # ------------------------------------------------- external working copies --

    def _watch_open_files(self):
        """Poll the working copies so edits can be saved without locking."""
        if not self.winfo_exists() or self.vault is None \
                or not self.vault.is_unlocked:
            return
        changed = self.app.workspace.modified()
        if changed:
            if not self.btn_save.winfo_ismapped():
                self.btn_save.pack(side="right", padx=(6, 0))
            self.btn_save.configure(
                text="Save changes (%d)" % len(changed) if len(changed) > 1
                else "Save changes")
        elif self.btn_save.winfo_ismapped():
            self.btn_save.pack_forget()
        self._update_open_count()
        self.after(2000, self._watch_open_files)

    def save_open_changes(self):
        """Re-encrypt every modified working copy, leaving the vault open."""
        changed = self.app.workspace.modified()
        if not changed:
            return
        ok, result = dialogs.run_task(
            self.app, "Saving changes",
            lambda report, cancel: self.app._save_back(changed, report, cancel))
        if ok:
            for handle in changed:
                handle.restamp()
            self.refresh()
            dialogs.info(self.app, "Saved",
                         "%d file%s re-encrypted into the vault."
                         % (len(changed), "" if len(changed) == 1 else "s"),
                         "The working copies stay open; they are shredded when "
                         "you lock.", kind="ok")
        else:
            dialogs.error(self.app, "Could not save", str(result))

    def close_open_files(self):
        if not self.app.workspace.opened:
            return
        if self.app.settle_open_files():
            self.refresh()

    # -------------------------------------------------------------- modify --

    def rename_selected(self):
        paths = self.selected_paths()
        if len(paths) != 1 or self.searching:
            return
        path = paths[0]
        node = self.vault.node_at(path)
        new_name = dialogs.ask_text(self.app, "Rename",
                                    "New name for %r" % node["name"],
                                    node["name"], "Rename")
        if not new_name or new_name == node["name"]:
            return
        try:
            self.vault.rename(path, new_name)
        except VaultError as exc:
            dialogs.error(self.app, "Could not rename", str(exc))
            return
        self.refresh(full=True)

    def delete_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        if self.app.settings["confirm_delete"]:
            if len(paths) == 1:
                node = self.vault.node_at(paths[0])
                what = "the folder %r and everything in it" % node["name"] \
                    if is_dir(node) else "%r" % node["name"]
            else:
                what = "%d items" % len(paths)
            if not dialogs.confirm(
                    self.app, "Delete permanently?",
                    "Delete %s from the vault?" % what,
                    "The encrypted data is overwritten and removed. There is "
                    "no recycle bin and no undo."):
                return

        def worker(report, _cancel):
            report(text="Shredding encrypted data...")
            removed = 0
            for path in sorted(paths, key=len, reverse=True):
                try:
                    removed += self.vault.delete(path)
                except VaultError:
                    continue
            return removed

        ok, result = dialogs.run_task(self.app, "Deleting", worker,
                                      cancellable=False)
        self.refresh(full=True)
        if not ok:
            dialogs.error(self.app, "Delete failed", str(result))

    def show_details(self):
        paths = self.selected_paths()
        if len(paths) != 1:
            return
        node = self.vault.node_at(paths[0])
        lines = [
            "Name:      %s" % node["name"],
            "Location:  /%s" % "/".join(paths[0][:-1]),
            "Type:      %s" % kind_of(node),
        ]
        if is_dir(node):
            lines.append("Contains:  %s" % human_size(self._tree_bytes(node)))
        else:
            lines += [
                "Size:      %s (%d bytes)" % (human_size(int(node["size"])),
                                              int(node["size"])),
                "SHA-256:   %s" % node.get("sha256", "-")[:32] + "...",
                "Blob id:   %s" % node.get("blob", "-"),
            ]
        lines += [
            "Added:     %s" % stamp(int(node.get("created", 0))),
            "Modified:  %s" % stamp(int(node.get("modified", 0))),
        ]
        dialogs.info(self.app, "Details", "", "\n".join(lines), mono=True)

    # ------------------------------------------------------------- vault ops --

    def change_password(self):
        result = dialogs.ChangePasswordDialog(self.app).show()
        if not result:
            return
        old, new = result

        def worker(report, _cancel):
            report(text="Re-deriving the key encryption key...")
            self.vault.change_password(old, new)
            return True

        ok, outcome = dialogs.run_task(self.app, "Changing password", worker,
                                       cancellable=False)
        if ok:
            dialogs.info(self.app, "Password changed",
                         "The vault now opens with the new password only.",
                         kind="ok")
        elif isinstance(outcome, BadPassword):
            dialogs.error(self.app, "Password not changed", str(outcome))
        else:
            dialogs.error(self.app, "Password not changed", str(outcome))

    def check_integrity(self):
        """Decrypt everything in place and re-check every authentication tag."""
        def worker(report, cancel):
            report(text="Re-checking every encrypted file...")
            total = sum(int(n.get("size", 0))
                        for _p, n in self.vault.iter_files())
            report(total=max(total, 1))
            return self.vault.verify_all(
                progress=lambda n: report(done=n), cancel=cancel)

        ok, result = dialogs.run_task(self.app, "Checking vault", worker)
        if not ok:
            if not isinstance(result, CryptoError):
                dialogs.error(self.app, "Check failed", str(result))
            return

        lines = ["Files checked:   %d" % result["checked"],
                 "Data verified:   %s" % human_size(result["bytes"]),
                 "Orphaned blobs:  %d" % result["orphans"]]
        if result["problems"]:
            for name, reason in result["problems"][:12]:
                lines.append("  ! %s - %s" % (name, reason))
            if len(result["problems"]) > 12:
                lines.append("  ... and %d more"
                             % (len(result["problems"]) - 12))
            dialogs.error(self.app, "Problems found",
                          "%d of %d files failed verification."
                          % (len(result["problems"]), result["checked"]),
                          "\n".join(lines))
        else:
            dialogs.info(self.app, "Vault is intact",
                         "Every file decrypted cleanly and matched its "
                         "recorded hash.", "\n".join(lines), kind="ok",
                         mono=True)

    def make_desktop_shortcut(self):
        shell.set_folder_icon(self.vault.path)
        link = shell.create_shortcut(self.vault.path)
        if link:
            dialogs.info(
                self.app, "Shortcut created",
                "Double-clicking it opens this vault's password prompt.",
                link, kind="ok")
        else:
            dialogs.error(
                self.app, "Could not create the shortcut",
                "Windows would not let the shortcut be written to your "
                "Desktop.",
                "You can make one by hand: right-click the Desktop, choose "
                "New > Shortcut, and point it at\n%s"
                % os.path.join(shell.app_dir(), "Desktop Vault.bat"))

    def toggle_hidden(self):
        """Hide or unhide the vault folder in Explorer.

        Purely cosmetic - a hidden folder is still readable by anything that
        looks for it, which is why the contents are encrypted regardless.
        """
        hidden = shell.is_hidden(self.vault.path)
        if not hidden and not dialogs.confirm(
                self.app, "Hide the vault folder?",
                "Explorer will stop showing %s unless hidden items are turned "
                "on." % os.path.basename(self.vault.path),
                "This only changes what Explorer displays. It is not a "
                "security measure, and you can undo it from this same menu.",
                confirm_text="Hide it", danger=False):
            return
        if shell.set_hidden(self.vault.path, not hidden):
            self._sync_hide_label()
            dialogs.info(self.app,
                         "Folder shown" if hidden else "Folder hidden",
                         "%s is now %s in Explorer."
                         % (os.path.basename(self.vault.path),
                            "visible" if hidden else "hidden"),
                         self.vault.path, kind="ok")
        else:
            dialogs.error(self.app, "Could not change the folder",
                          "Windows refused the attribute change.",
                          self.vault.path)

    def show_about(self):
        header = self.vault.header
        kdf = header.get("kdf", {})
        info = self.vault.stats()
        lines = [
            "Vault:        %s" % self.vault.name,
            "Location:     %s" % self.vault.path,
            "Created:      %s" % stamp(int(header.get("created", 0))),
            "Format:       version %s" % header.get("format", "?"),
            "",
            "Content:      %s" % header.get("cipher", "AES-256-GCM"),
            "Key stretch:  %s, %d MiB memory, %d passes, %d lanes"
            % (kdf.get("algorithm", "?"), int(kdf.get("memory_cost", 0)) // 1024,
               int(kdf.get("time_cost", 0)), int(kdf.get("parallelism", 0))),
            "Sub-keys:     HKDF-SHA256 from a random 256-bit master key",
            "",
            "Holds:        %d files in %d folders" % (info["files"],
                                                      info["folders"]),
            "Plaintext:    %s" % human_size(info["bytes"]),
            "On disk:      %s" % human_size(info["on_disk"]),
        ]
        dialogs.info(self.app, "About this vault", "", "\n".join(lines),
                     mono=True)

    def manage_recovery(self):
        """Create, replace or revoke this vault's recovery split."""
        existing = self.vault.recovery_info()
        answer = dialogs.RecoverySetupDialog(
            self.app, existing, self.vault.name).show()
        if not answer:
            return

        if answer["action"] == "revoke":
            if not dialogs.confirm(
                    self.app, "Revoke recovery shares?",
                    "Every existing share stops working immediately.",
                    "The password becomes the only way into this vault "
                    "again. This cannot be undone.",
                    confirm_text="Revoke"):
                return
            if self.vault.revoke_recovery():
                dialogs.info(self.app, "Shares revoked",
                             "The recovery block has been shredded.",
                             "Those shares are now worthless.", kind="ok")
            else:
                dialogs.error(self.app, "Nothing to revoke",
                              "This vault has no recovery shares.")
            return

        def worker(report, _cancel):
            report(text="Splitting a new recovery secret...")
            return self.vault.create_recovery(
                answer["password"], answer["threshold"], answer["count"])

        ok, result = dialogs.run_task(self.app, "Creating recovery shares",
                                      worker, cancellable=False)
        if not ok:
            dialogs.error(self.app, "Shares were not created", str(result))
            return
        dialogs.RecoveryShowDialog(self.app, result, answer["threshold"],
                                   self.vault.name).show()

    def open_settings(self):
        if dialogs.SettingsDialog(self.app, self.app.settings).show():
            self.update_idle_readout()

    def switch_vault(self, target: str = None) -> bool:
        """Leave this vault: lock it, then show the library or another vault.

        False means the user backed out at the unsaved-changes prompt and this
        vault is still open.
        """
        self.vault.flush()
        return self.app.switch_vault(target)

    def lock(self):
        self.vault.flush()
        self.app.lock_vault()
        return "break"

    def auto_lock(self):
        """Idle timeout fired.

        If another program still has a working copy open, locking would shred
        that copy underneath it: the user then saves in Word, the save appears
        to succeed, and the change has nowhere to go because the vault is shut.
        So the lock is held while anything is open, and anything already saved
        is captured now rather than waited on.
        """
        if self.app.workspace.opened:
            changed = self.app.workspace.modified()
            if changed:
                try:
                    self.app._save_back(changed, lambda **_kw: None,
                                        threading.Event())
                    for handle in changed:
                        handle.restamp()
                    self.refresh()
                except Exception:
                    pass          # the toolbar Save changes button still works
            self._autolock_held = True
            self.update_idle_readout()
            return
        self._autolock_held = False
        self.vault.flush()
        self.app.lock_vault()


def main(initial_vault: str | None = None):
    app = App(initial_vault)
    app.mainloop()

"""Runtime support: app settings and the temporary workspace.

Opening a document means writing plaintext to disk so another program can read
it.  That is unavoidable, so this module confines it: every decrypted file goes
into a per-user directory whose ACL is stripped down to the current account
alone, is tracked while open, and is overwritten and deleted the moment the
vault locks or the app exits.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional

from . import crypto
from .store import shred_file

APP_DIR_NAME = "DesktopVault"
LEGACY_APP_DIR_NAME = "SecretVault"
MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
CREATE_NO_WINDOW = 0x08000000


def app_data_dir() -> str:
    """Where settings and decrypted working copies live.

    The app used to be called Secret Vault.  If only the old folder exists it
    is renamed once, so settings survive the rename rather than silently
    reverting to defaults.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_DIR_NAME)
    if not os.path.isdir(path):
        legacy = os.path.join(base, LEGACY_APP_DIR_NAME)
        if os.path.isdir(legacy):
            try:
                os.rename(legacy, path)
            except OSError:
                return legacy          # in use; keep working from the old one
    os.makedirs(path, exist_ok=True)

    # If the new folder got created before the old one could be renamed -- a
    # half-finished upgrade -- the preferences would silently revert to
    # defaults.  Adopt the old file, but only when there is nothing to lose.
    current = os.path.join(path, "settings.json")
    if not os.path.exists(current):
        inherited = os.path.join(base, LEGACY_APP_DIR_NAME, "settings.json")
        if os.path.isfile(inherited):
            try:
                shutil.copy2(inherited, current)
            except OSError:
                pass
    return path


# ---------------------------------------------------------------- settings --

DEFAULTS = {
    "recent": [],               # vaults kept outside the library, most recent first
    "library": "",              # "" means the default location below
    "autolock_minutes": 5,      # 0 disables the idle timer
    "lock_on_minimize": False,
    "clear_on_exit": True,
    "confirm_delete": True,
    "desktop_shortcuts": False,
    "warn_external_open": True,
    "geometry": "",
}


LEGACY_LIBRARY_NAME = "Secret Vault"


def default_library() -> str:
    """Where vaults live unless the user says otherwise.

    Documents rather than AppData: a vault is the user's own data, it should be
    somewhere they can find it and somewhere their backups already cover.

    If the folder from the app's former name is the only one present, that is
    used as-is.  Nothing is moved: a library can hold real vaults, and quietly
    relocating someone's encrypted data on startup is not a decision this
    function should be making.
    """
    from . import shell
    documents = shell.documents_dir()
    current = os.path.join(documents, "Desktop Vault")
    if not os.path.isdir(current):
        legacy = os.path.join(documents, LEGACY_LIBRARY_NAME)
        if os.path.isdir(legacy):
            return legacy
    return current


class Settings:
    """Small JSON settings file.  Never contains passwords or key material."""

    def __init__(self, path: str = None):
        # ``path`` exists so tests (and anything else) can use a settings file
        # that is not the user's own.
        self.path = path or os.path.join(app_data_dir(), "settings.json")
        self.data = dict(DEFAULTS)
        # utf-8-sig so a byte-order mark written by another tool (PowerShell
        # defaults to one) does not silently reset every preference.  Values
        # are type-checked against the defaults so a hand-edited or truncated
        # file cannot feed None into the UI later on.
        try:
            with open(self.path, "r", encoding="utf-8-sig") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        for key, default in DEFAULTS.items():
            if key not in loaded:
                continue
            value = loaded[key]
            if isinstance(default, bool):
                if isinstance(value, bool):
                    self.data[key] = value
            elif isinstance(default, int):
                if isinstance(value, int) and not isinstance(value, bool):
                    self.data[key] = value
            elif isinstance(default, list):
                if isinstance(value, list):
                    self.data[key] = [p for p in value if isinstance(p, str)]
            elif isinstance(default, str):
                if isinstance(value, str):
                    self.data[key] = value

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @property
    def library(self) -> str:
        return self.data.get("library") or default_library()

    def ensure_library(self) -> str:
        path = self.library
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path

    def in_library(self, vault_path: str) -> bool:
        try:
            return os.path.normcase(os.path.dirname(
                os.path.abspath(vault_path))) == os.path.normcase(self.library)
        except (OSError, ValueError):
            return False

    def push_recent(self, vault_path: str, limit: int = 8) -> None:
        vault_path = os.path.abspath(vault_path)
        if self.in_library(vault_path):
            return          # library vaults are found by scanning, not listed here
        recent = [p for p in self.data.get("recent", [])
                  if os.path.normcase(p) != os.path.normcase(vault_path)]
        recent.insert(0, vault_path)
        self.data["recent"] = recent[:limit]
        self.save()

    def drop_recent(self, vault_path: str) -> None:
        self.data["recent"] = [
            p for p in self.data.get("recent", [])
            if os.path.normcase(p) != os.path.normcase(vault_path)]
        self.save()


# --------------------------------------------------------------- workspace --

def _lock_down(path: str) -> None:
    """Remove inherited ACEs so only the current user can read ``path``."""
    user = os.environ.get("USERNAME")
    if not user or os.name != "nt":
        return
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", "%s:(OI)(CI)F" % user],
            check=False, capture_output=True,
            creationflags=CREATE_NO_WINDOW, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass        # the directory is still inside the user profile


def _delete_on_reboot(path: str) -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.MoveFileExW(
            ctypes.c_wchar_p(path), None,
            ctypes.c_uint(MOVEFILE_DELAY_UNTIL_REBOOT))
    except Exception:
        pass


class OpenedFile:
    """One decrypted document currently sitting in the workspace."""

    def __init__(self, vault_path: List[str], disk_path: str):
        self.vault_path = list(vault_path)
        self.disk_path = disk_path
        self.opened_at = time.time()
        self.stamp = self._stamp()

    def _stamp(self):
        try:
            st = os.stat(self.disk_path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    @property
    def name(self) -> str:
        return self.vault_path[-1] if self.vault_path else \
            os.path.basename(self.disk_path)

    @property
    def display_path(self) -> str:
        return "/".join(self.vault_path)

    def is_modified(self) -> bool:
        current = self._stamp()
        return current is not None and current != self.stamp

    def restamp(self) -> None:
        self.stamp = self._stamp()


class Workspace:
    """Holds and cleans up every plaintext file the app writes out."""

    def __init__(self):
        self.root = os.path.join(app_data_dir(), "open")
        os.makedirs(self.root, exist_ok=True)
        _lock_down(self.root)
        self.opened: List[OpenedFile] = []
        self.purge_stale()

    def purge_stale(self) -> None:
        """Clear anything left behind by a crash or a hard kill."""
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return
        for entry in entries:
            self._destroy(entry.path)

    def reserve(self, filename: str) -> str:
        slot = os.path.join(self.root, crypto.random_bytes(8).hex())
        os.makedirs(slot, exist_ok=True)
        return os.path.join(slot, filename)

    def track(self, vault_path: List[str], disk_path: str) -> OpenedFile:
        handle = OpenedFile(vault_path, disk_path)
        self.opened.append(handle)
        return handle

    @staticmethod
    def launch(path: str) -> None:
        """Hand the file to whatever program the OS associates with it."""
        if os.name == "nt":
            os.startfile(path)                       # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    @staticmethod
    def reveal(path: str) -> None:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def modified(self) -> List[OpenedFile]:
        return [h for h in self.opened if h.is_modified()]

    def forget(self, handle: OpenedFile) -> None:
        if handle in self.opened:
            self.opened.remove(handle)
        self._destroy(os.path.dirname(handle.disk_path))

    def close_all(self) -> None:
        """Shred every tracked file, then sweep the workspace directory."""
        for handle in list(self.opened):
            self._destroy(os.path.dirname(handle.disk_path))
        self.opened.clear()
        self.purge_stale()

    @staticmethod
    def _destroy(slot_dir: str) -> None:
        if not os.path.isdir(slot_dir):
            return
        for root, _, names in os.walk(slot_dir, topdown=False):
            for n in names:
                target = os.path.join(root, n)
                for attempt in range(3):
                    try:
                        shred_file(target)
                        break
                    except OSError:
                        time.sleep(0.15)
                if os.path.exists(target):
                    # Still held open by the viewer; queue it for boot time.
                    _delete_on_reboot(target)
            try:
                os.rmdir(root)
            except OSError:
                pass

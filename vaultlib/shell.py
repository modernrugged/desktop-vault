"""Windows shell integration.

A vault is a directory, so Explorer will always be willing to open it. That is
harmless -- everything inside is ciphertext -- but a plain manila folder on the
desktop reads like unprotected files, which is the wrong signal entirely.

This module makes the container look like what it is (padlock icon, "contents
are encrypted" tooltip) and creates a shortcut that opens the app straight at
the vault's password prompt, so there is something to double-click that does
the expected thing.

Everything here is cosmetic and fully reversible. None of it is a security
control, and nothing in the app depends on it succeeding.
"""

from __future__ import annotations

import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

DESKTOP_INI = """[.ShellClassInfo]
IconResource={icon},0
InfoTip={tip}
[ViewState]
Mode=
Vid=
FolderType=Generic
"""


def available() -> bool:
    return os.name == "nt"


def app_dir() -> str:
    """The folder holding DesktopVault.pyw (or the packaged .exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def icon_path() -> str:
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.join(app_dir(), "app.ico")


# Known folders, as (GUID fields, registry name, profile-relative fallback).
_DESKTOP = ((0xB4BFCC3A, 0xDB2C, 0x424C,
             (0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41)),
            "Desktop", "Desktop")
_DOCUMENTS = ((0xFDD39AD0, 0x238F, 0x46AF,
               (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7)),
              "Personal", "Documents")


def _known_folder(spec) -> str:
    """Resolve a Windows known folder properly rather than guessing.

    A profile can have both ``%USERPROFILE%\\Desktop`` and
    ``%USERPROFILE%\\OneDrive\\Desktop``, only one of which is live; the same
    goes for Documents.  Guessing puts files somewhere the user never looks.
    Ask the shell, fall back to the registry, and only then guess.
    """
    (d1, d2, d3, d4), reg_name, fallback = spec
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD),
                            ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(folder_id), 0, None, ctypes.byref(out)) == 0:
                path = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if path and os.path.isdir(path):
                    return path
        except Exception:
            pass
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                    r"\User Shell Folders") as key:
                raw, _ = winreg.QueryValueEx(key, reg_name)
            path = os.path.expandvars(raw)
            if os.path.isdir(path):
                return path
        except Exception:
            pass

    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(profile, fallback)


def desktop_dir() -> str:
    return _known_folder(_DESKTOP)


def documents_dir() -> str:
    return _known_folder(_DOCUMENTS)


def open_path(path: str) -> bool:
    """Show a file or folder with the desktop's default handler."""
    try:
        if os.name == "nt":
            os.startfile(path)                      # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except (OSError, AttributeError):
        return False


# Windows 10 1809 used attribute 19 for the dark title bar; 2004 and Windows 11
# use 20.  Anything older simply refuses both, and the title bar stays light.
_DARK_TITLEBAR_ATTRS = (20, 19)


def use_dark_titlebar(window) -> bool:
    """Ask DWM to draw this window's title bar dark, to match the app."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        window.update_idletasks()
        handle = ctypes.windll.user32.GetParent(window.winfo_id())
        if not handle:
            handle = window.winfo_id()
        enabled = ctypes.c_int(1)
        for attribute in _DARK_TITLEBAR_ATTRS:
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(handle), ctypes.c_int(attribute),
                ctypes.byref(enabled), ctypes.sizeof(enabled))
            if result == 0:
                return True
    except Exception:
        pass
    return False


def _run(args) -> bool:
    try:
        result = subprocess.run(args, capture_output=True,
                                creationflags=CREATE_NO_WINDOW, timeout=25)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _powershell(script: str) -> bool:
    return _run(["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script])


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def set_folder_icon(vault_path: str, tip: str = None) -> bool:
    """Give the vault folder the padlock icon and an explanatory tooltip.

    Explorer only honours desktop.ini when the folder carries the ReadOnly (or
    System) attribute and the ini itself is hidden and system.  ReadOnly is the
    right one here: on a directory it just means "customised", and unlike
    System it does not combine with Hidden to make the folder a protected
    operating-system file that stays invisible even when the user has asked to
    see hidden items.
    """
    if not available() or not os.path.isdir(vault_path):
        return False
    icon = icon_path()
    if not os.path.exists(icon):
        return False

    ini = os.path.join(vault_path, "desktop.ini")
    tip = tip or "Desktop Vault - the contents are encrypted"
    try:
        # desktop.ini is read in the system ANSI code page, not UTF-8.
        with open(ini, "w", encoding="mbcs", errors="replace", newline="\r\n") as fh:
            fh.write(DESKTOP_INI.format(icon=icon, tip=tip))
    except OSError:
        return False

    _run(["attrib", "+h", "+s", ini])
    _run(["attrib", "+r", vault_path])
    return True


def clear_folder_icon(vault_path: str) -> bool:
    """Undo :func:`set_folder_icon`."""
    if not available():
        return False
    ini = os.path.join(vault_path, "desktop.ini")
    _run(["attrib", "-r", vault_path])
    if os.path.isfile(ini):
        _run(["attrib", "-h", "-s", ini])
        try:
            os.remove(ini)
        except OSError:
            return False
    return True


def launcher() -> tuple:
    """Return ``(target, arguments_prefix)`` for launching the app.

    Prefers pythonw.exe directly so no console window flashes; falls back to
    the .bat, and to the packaged .exe when frozen.
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), ""

    entry = os.path.join(app_dir(), "DesktopVault.pyw")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.isfile(pythonw) and os.path.isfile(entry):
        return pythonw, '"%s" ' % entry

    bat = os.path.join(app_dir(), "Desktop Vault.bat")
    if os.path.isfile(bat):
        return bat, ""
    return sys.executable, '"%s" ' % entry


def create_shortcut(vault_path: str, dest_dir: str = None,
                    name: str = None) -> str:
    """Put a ``<name>.lnk`` that opens this vault's password prompt.

    Returns the shortcut path, or an empty string if it could not be made.
    """
    if not available():
        return ""
    vault_path = os.path.abspath(vault_path)
    dest_dir = dest_dir or desktop_dir()
    if not os.path.isdir(dest_dir):
        return ""

    base = os.path.basename(vault_path.rstrip("\\/"))
    if base.lower().endswith(".locker"):
        base = base[:-7]
    link = os.path.join(dest_dir, (name or base) + ".lnk")

    target, prefix = launcher()
    arguments = prefix + '"%s"' % vault_path
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(%s); "
        "$s.TargetPath = %s; "
        "$s.Arguments = %s; "
        "$s.WorkingDirectory = %s; "
        "$s.IconLocation = %s; "
        "$s.Description = %s; "
        "$s.Save()"
        % (_ps_quote(link), _ps_quote(target), _ps_quote(arguments),
           _ps_quote(app_dir()), _ps_quote(icon_path() + ",0"),
           _ps_quote("Open %s in Desktop Vault" % base))
    )
    if _powershell(script) and os.path.isfile(link):
        return link
    return ""


def set_hidden(path: str, hidden: bool = True) -> bool:
    """Toggle the Hidden attribute on the vault folder itself."""
    if not available() or not os.path.exists(path):
        return False
    return _run(["attrib", "+h" if hidden else "-h", path])


def is_hidden(path: str) -> bool:
    if not available() or not os.path.exists(path):
        return False
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x2)
    except Exception:
        return False

"""Desktop Vault - launcher.

An offline encrypted vault for API keys, webhooks, secret keys, crypto
recovery phrases and two-factor backup codes, alongside tax returns, passport
scans, contracts and medical records.

Run with pythonw (double-click the .pyw, or use the Desktop Vault.bat shortcut)
so no console window appears.

An optional argument is the path of a vault folder to open straight away:

    pythonw DesktopVault.pyw "D:\Vaults\Personal.locker"
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _missing(package: str, error: Exception) -> None:
    message = (
        "Desktop Vault needs the %s package.\n\n"
        "Install the dependencies with:\n\n"
        "    python -m pip install -r requirements.txt\n\n"
        "(%s)" % (package, error)
    )
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Missing dependency", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)
    sys.exit(1)


def _diagnose() -> None:
    """Write a support report: `Desktop Vault.exe --diagnose`.

    A packaged build can lose an optional dependency without any visible
    symptom beyond a feature quietly not working, so this states plainly what
    actually loaded.
    """
    import platform
    import tempfile

    lines = ["Desktop Vault diagnostics", "=" * 25, ""]

    def add(label, value):
        lines.append("%-22s %s" % (label + ":", value))

    add("Frozen build", bool(getattr(sys, "frozen", False)))
    add("Executable", sys.executable)
    add("Python", platform.python_version())
    # platform.release() says "10" on Windows 11 too; the build number is the
    # only reliable way to tell them apart, and 22000 is the dividing line.
    try:
        build = sys.getwindowsversion().build
        add("Windows", "%s (build %d)"
            % ("11" if build >= 22000 else "10", build))
    except AttributeError:
        add("Operating system", "%s %s" % (platform.system(),
                                           platform.release()))

    try:
        import tkinter
        add("Tk", tkinter.TkVersion)
    except Exception as exc:
        add("Tk", "MISSING - %s" % exc)

    for dist, module_name in (("cryptography", "cryptography"),
                              ("argon2-cffi", "argon2"),
                              ("tkinterdnd2", "tkinterdnd2")):
        try:
            __import__(module_name)
        except Exception as exc:
            add(dist, "MISSING - %s" % exc)
            continue
        try:
            from importlib.metadata import version as dist_version
            add(dist, dist_version(dist))
        except Exception:
            add(dist, "present")

    try:
        from vaultlib.ui import DND_READY
        add("Drag and drop", "working" if DND_READY else "NOT AVAILABLE")
    except Exception as exc:
        add("Drag and drop", "unknown - %s" % exc)

    try:
        from vaultlib.session import Settings, app_data_dir
        from vaultlib.store import list_vaults
        add("Settings folder", app_data_dir())
        settings = Settings()
        add("Library", settings.library)
        add("Vaults in library", len(list_vaults(settings.library)))
    except Exception as exc:
        add("Settings", "unknown - %s" % exc)

    # One Tk root for both the display probe and the report window: creating
    # a second after destroying the first upsets ttk's theme bookkeeping.
    root = None
    try:
        from tkinter import Tk
        root = Tk()
        root.withdraw()
        from vaultlib import theme
        add("Display scale", "%.2fx" % theme.detect_scale(root))
        add("Screen", "%dx%d" % (root.winfo_screenwidth(),
                                 root.winfo_screenheight()))
    except Exception as exc:
        add("Display", "unknown - %s" % exc)

    report = "\n".join(lines) + "\n"
    path = os.path.join(tempfile.gettempdir(), "desktop-vault-diagnostics.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
    except OSError:
        path = "(could not be written)"

    print(report)
    if root is not None:
        try:
            from tkinter import messagebox
            messagebox.showinfo("Desktop Vault diagnostics",
                                report + "\nSaved to:\n" + path)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


def main() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        _missing("cryptography", exc)
    try:
        import argon2  # noqa: F401
    except ImportError as exc:
        _missing("argon2-cffi", exc)

    if "--diagnose" in sys.argv[1:]:
        _diagnose()
        return

    from vaultlib.ui import main as run
    target = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        run(target)
    except Exception:
        crash = traceback.format_exc()
        try:
            from tkinter import Tk, messagebox
            root = Tk()
            root.withdraw()
            messagebox.showerror("Desktop Vault stopped unexpectedly", crash)
            root.destroy()
        except Exception:
            print(crash, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

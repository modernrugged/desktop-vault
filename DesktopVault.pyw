"""Desktop Vault - launcher.

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


def main() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        _missing("cryptography", exc)
    try:
        import argon2  # noqa: F401
    except ImportError as exc:
        _missing("argon2-cffi", exc)

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

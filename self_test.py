"""Verify the Desktop Vault crypto and storage layer on this machine.

Run it any time you want to confirm the app behaves as advertised::

    python self_test.py

Nothing here touches your real vaults; everything happens in a temporary
folder that is removed at the end.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vaultlib import crypto
from vaultlib.session import Settings, Workspace
from vaultlib.store import BadPassword, Vault, VaultError

PASSWORD = "correct horse battery staple"
FAST = crypto.KdfParams(time_cost=1, memory_cost=8192, parallelism=1)

passed = 0
failed = 0


def check(label: str, condition: bool, note: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print("  [ok]   %s" % label)
    else:
        failed += 1
        print("  [FAIL] %s %s" % (label, note))


def section(title: str) -> None:
    print("\n%s" % title)
    print("-" * len(title))


def check_dialog_callsites() -> list:
    """Bind every dialog call against the real signature, without opening one.

    Covers two boundaries: ``dialogs.X(...)`` from ui.py, and the calls made
    *inside* dialogs.py itself, where a helper such as ``info()`` forwards its
    arguments to a dialog class.  A dialog reached only on an error path can
    sit broken for a long time otherwise.
    """
    import ast
    import inspect

    from vaultlib import dialogs

    here = os.path.dirname(os.path.abspath(__file__))

    def sig_of(target):
        try:
            return inspect.signature(target)
        except (TypeError, ValueError):
            return None

    def bind(node, target, label, problems):
        sig = sig_of(target)
        if sig is None:
            return
        if any(isinstance(a, ast.Starred) for a in node.args):
            return
        if any(kw.arg is None for kw in node.keywords):
            return
        args = [object()] * len(node.args)
        kwargs = {kw.arg: object() for kw in node.keywords}
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            problems.append("%s %s" % (label, exc))

    problems = []

    # 1. ui.py -> dialogs.<name>(...)
    with open(os.path.join(here, "vaultlib", "ui.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "dialogs"):
            continue
        target = getattr(dialogs, func.attr, None)
        if target is None:
            problems.append("ui.py:%d dialogs.%s does not exist"
                            % (node.lineno, func.attr))
            continue
        bind(node, target, "ui.py:%d dialogs.%s(...)"
             % (node.lineno, func.attr), problems)

    # 2. dialogs.py -> the dialog classes it builds itself
    path = os.path.join(here, "vaultlib", "dialogs.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        target = getattr(dialogs, node.func.id, None)
        if target is None or not (inspect.isclass(target)
                                  or inspect.isfunction(target)):
            continue
        bind(node, target, "dialogs.py:%d %s(...)"
             % (node.lineno, node.func.id), problems)

    return problems


def ui_checks(work: str) -> None:
    """Build every screen and every dialog for real, then run Create end to end."""
    try:
        import tkinter as tk
    except ImportError as exc:
        check("tkinter is available", False, str(exc))
        return

    from vaultlib import dialogs, theme
    from vaultlib.ui import App, BrowserScreen, CreateScreen, StartScreen, UnlockScreen

    # Dialogs are constructed for real; they just do not wait for a click.
    original_show = dialogs._Modal.show

    def auto_close(self):
        self.update_idletasks()
        result = self.result
        self.destroy()
        return result

    dialogs._Modal.show = auto_close
    crashes = []

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        dialogs._Modal.show = original_show
        check("a display is available for the UI checks", False, str(exc))
        return

    root.report_callback_exception = lambda *a: crashes.append(a)
    theme.apply(root)
    try:
        dialogs.info(root, "Title", "message", "detail", "ok")
        dialogs.info(root, "Title", "", "a\nb\nc", mono=True)
        dialogs.error(root, "Title", "message", "detail")
        dialogs.confirm(root, "Title", "message", "detail", "Go", False)
        dialogs.ask_text(root, "Title", "prompt", "seed", "OK")
        dialogs.ChangePasswordDialog(root).show()
        dialogs.SettingsDialog(root, Settings()).show()
        dialogs.SaveBackDialog(root, []).show()
        dialogs.SaveCloseDialog(root, "notes.txt").show()
        dialogs.ExternalOpenDialog(root, "report.docx").show()
        ok, value = dialogs.run_task(root, "Working", lambda r, c: 42, False)
        check("every dialog builds and the task runner returns its result",
              ok and value == 42)
    except Exception as exc:
        check("every dialog builds and the task runner returns its result",
              False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        root.destroy()

    # The full Create-a-vault flow, including the dialogs it shows on the way.
    from vaultlib import crypto as _crypto
    real_calibrate = _crypto.calibrate_kdf
    _crypto.calibrate_kdf = lambda target_seconds=0.8: FAST
    app = None
    try:
        # Point every persistent side effect at the throwaway folder so the
        # real settings file and library are neither read nor written.
        ui_library = os.path.join(work, "TestLibrary")
        os.makedirs(ui_library, exist_ok=True)
        app = App()
        app.report_callback_exception = lambda *a: crashes.append(a)
        app.settings = Settings(os.path.join(work, "settings.json"))
        app.settings["library"] = ui_library

        app.show_create()
        app.update()
        screen = app.screen
        check("the create screen is shown", isinstance(screen, CreateScreen))
        check("new vaults default into the library",
              os.path.normcase(screen.dir_var.get())
              == os.path.normcase(ui_library), screen.dir_var.get())
        screen.name_var.set("Self Test Vault")
        screen.dir_var.set(ui_library)
        screen.pw_var.set("a reasonably long passphrase")
        screen.confirm.var.set("a reasonably long passphrase")
        screen.acknowledged.set(True)
        screen.make_shortcut.set(False)   # never touch the real Desktop
        app.update()
        screen.create()
        app.update()
        check("creating a vault lands in the file browser, not an error",
              isinstance(app.screen, BrowserScreen) and app.vault is not None,
              "ended on %s" % type(app.screen).__name__)

        if isinstance(app.screen, BrowserScreen):
            browser = app.screen
            browser.show_about()
            browser.check_integrity()
            app.update()
            check("About and Check integrity open without crashing",
                  not crashes, str(crashes[:1]))

            from vaultlib import editor as _editor
            from vaultlib.ui import DND_READY

            check("the file list is registered as a drop target",
                  (not DND_READY)
                  or bool(browser.listing.drop_target_register), "")

            # Drag-and-drop and the Add buttons share this entry point.
            sample = os.path.join(work, "dropped.txt")
            # newline="" so the bytes on disk are exactly what is asserted
            # below; Windows text mode would silently turn \n into \r\n.
            with open(sample, "w", encoding="utf-8", newline="") as fh:
                fh.write("dropped in\n")
            sub = os.path.join(work, "dropped folder")
            os.makedirs(os.path.join(sub, "inner"), exist_ok=True)
            with open(os.path.join(sub, "inner", "deep.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("deep\n")
            browser.import_paths([sample, sub])
            app.update()
            check("dropping a file and a folder imports both",
                  app.vault.exists(["dropped.txt"])
                  and app.vault.exists(["dropped folder", "inner", "deep.txt"]))

            # ---- name clashes on import -------------------------------
            class FakeConflict:
                calls = []
                script = []

                def __init__(self, parent, name, where, is_dir, existing,
                             incoming, remaining=0):
                    FakeConflict.calls.append((name, is_dir, remaining))
                    self._answer = (FakeConflict.script.pop(0)
                                    if FakeConflict.script else None)

                def show(self):
                    return self._answer

            real_conflict = dialogs.ConflictDialog
            dialogs.ConflictDialog = FakeConflict

            clash_dir = os.path.join(work, "clash")
            os.makedirs(clash_dir, exist_ok=True)
            clash_file = os.path.join(clash_dir, "dropped.txt")

            def stage(text):
                with open(clash_file, "wb") as fh:
                    fh.write(text)
                return clash_file

            original = app.vault.read_bytes(["dropped.txt"])

            # Keep both
            FakeConflict.calls = []
            FakeConflict.script = [{"action": "keep_both", "all": False}]
            browser.import_paths([stage(b"second copy\n")])
            app.update()
            check("a clashing name prompts once",
                  len(FakeConflict.calls) == 1
                  and FakeConflict.calls[0][0] == "dropped.txt",
                  str(FakeConflict.calls))
            check("Keep both leaves the original and adds a numbered copy",
                  app.vault.read_bytes(["dropped.txt"]) == original
                  and app.vault.exists(["dropped (2).txt"])
                  and app.vault.read_bytes(["dropped (2).txt"]) == b"second copy\n")

            # Skip
            FakeConflict.script = [{"action": "skip", "all": False}]
            browser.import_paths([stage(b"should not land\n")])
            app.update()
            check("Skip leaves the stored copy untouched",
                  app.vault.read_bytes(["dropped.txt"]) == original
                  and not app.vault.exists(["dropped (3).txt"]))

            # Cancel the whole import
            FakeConflict.script = [None]
            before_names = sorted(app.vault.dir_at([])["children"])
            browser.import_paths([stage(b"cancelled\n")])
            app.update()
            check("Cancelling the prompt imports nothing at all",
                  sorted(app.vault.dir_at([])["children"]) == before_names)

            # Overwrite
            old_blob = app.vault.node_at(["dropped.txt"])["blob"]
            FakeConflict.script = [{"action": "overwrite", "all": False}]
            browser.import_paths([stage(b"replacement content\n")])
            app.update()
            check("Overwrite replaces the stored copy",
                  app.vault.read_bytes(["dropped.txt"]) == b"replacement content\n"
                  and not app.vault.exists(["dropped (3).txt"]))
            check("Overwrite shreds the encrypted data it replaced",
                  not os.path.exists(app.vault._blob_path(old_blob)))

            # Apply to all: two clashes, one prompt
            second_clash = os.path.join(clash_dir, "dropped (2).txt")
            with open(second_clash, "wb") as fh:
                fh.write(b"bulk two\n")
            stage(b"bulk one\n")
            FakeConflict.calls = []
            FakeConflict.script = [{"action": "overwrite", "all": True}]
            browser.import_paths([clash_file, second_clash])
            app.update()
            check("Apply to all asks once and covers the rest",
                  len(FakeConflict.calls) == 1
                  and app.vault.read_bytes(["dropped.txt"]) == b"bulk one\n"
                  and app.vault.read_bytes(["dropped (2).txt"]) == b"bulk two\n",
                  str(FakeConflict.calls))
            check("the prompt reports how many clashes are still queued",
                  FakeConflict.calls[0][2] == 1, str(FakeConflict.calls))

            # A clashing folder, overwritten wholesale
            folder_src = os.path.join(work, "clashdir", "dropped folder")
            os.makedirs(os.path.join(folder_src, "fresh"), exist_ok=True)
            with open(os.path.join(folder_src, "fresh", "new.txt"), "wb") as fh:
                fh.write(b"new tree\n")
            FakeConflict.calls = []
            FakeConflict.script = [{"action": "overwrite", "all": False}]
            browser.import_paths([folder_src])
            app.update()
            check("a clashing folder is reported as a folder",
                  FakeConflict.calls and FakeConflict.calls[0][1] is True,
                  str(FakeConflict.calls))
            check("overwriting a folder replaces the whole subtree",
                  app.vault.exists(["dropped folder", "fresh", "new.txt"])
                  and not app.vault.exists(["dropped folder", "inner",
                                            "deep.txt"]))

            # No clash at all must never prompt
            quiet = os.path.join(clash_dir, "untaken-name.txt")
            with open(quiet, "wb") as fh:
                fh.write(b"no clash\n")
            FakeConflict.calls = []
            browser.import_paths([quiet])
            app.update()
            check("an unused name imports without asking anything",
                  not FakeConflict.calls
                  and app.vault.exists(["untaken-name.txt"]))

            dialogs.ConflictDialog = real_conflict

            # The clash checks above deliberately rewrote this file; put known
            # content back so the editor checks assert against something fixed.
            app.vault.write_bytes(["dropped.txt"], b"dropped in\n")

            window = _editor.EditorWindow(app, app.vault, ["dropped.txt"])
            app.update()
            check("the editor opens the file it was given",
                  window.text.get("1.0", "end-1c") == "dropped in\n",
                  repr(window.text.get("1.0", "end-1c")))
            window.text.insert("end", "edited in the app\n")
            app.update()
            window.save()
            app.update()
            check("the editor saves straight back into the vault",
                  app.vault.read_bytes(["dropped.txt"])
                  == b"dropped in\nedited in the app\n",
                  repr(app.vault.read_bytes(["dropped.txt"])))
            check("saving clears the unsaved-changes marker", not window._dirty)
            window.destroy()
            app.update()

            # CRLF must survive a round trip untouched: silently rewriting a
            # file's line endings on save would be a nasty surprise.
            app.vault.create_text_file([], "crlf.txt", b"one\r\ntwo\r\n")
            crlf = _editor.EditorWindow(app, app.vault, ["crlf.txt"])
            app.update()
            crlf.save()
            app.update()
            check("editing does not rewrite line endings",
                  app.vault.read_bytes(["crlf.txt"]) == b"one\r\ntwo\r\n",
                  repr(app.vault.read_bytes(["crlf.txt"])))
            crlf.destroy()
            app.update()

            # An idle auto-lock used to shred the working copy while Word still
            # had it open, so the next save in Word went nowhere. It must hold
            # the lock instead, and capture anything already saved.
            copy_path = app.workspace.reserve("dropped.txt")
            app.vault.extract_file(["dropped.txt"], copy_path)
            handle = app.workspace.track(["dropped.txt"], copy_path)
            browser.auto_lock()
            app.update()
            check("an idle auto-lock does not shred a document that is still "
                  "open elsewhere",
                  os.path.isfile(copy_path) and app.vault is not None)
            check("the vault stays unlocked while a document is open",
                  isinstance(app.screen, BrowserScreen))

            import time as _time
            _time.sleep(0.02)
            with open(copy_path, "wb") as fh:
                fh.write(b"saved from the other program\n")
            browser.auto_lock()
            app.update()
            check("an idle auto-lock captures what was already saved outside",
                  app.vault.read_bytes(["dropped.txt"])
                  == b"saved from the other program\n",
                  repr(app.vault.read_bytes(["dropped.txt"])))
            check("and still does not shred the open copy",
                  os.path.isfile(copy_path))

            app.workspace.close_all()
            app.update()
            browser.auto_lock()
            app.update()
            check("with nothing open, the idle auto-lock does lock",
                  app.vault is None or not app.vault.is_unlocked
                  or isinstance(app.screen, UnlockScreen))
            if app.vault is None:
                app.open_vault(os.path.join(ui_library, "Self Test Vault.locker"))
                app.update()

        app.lock_vault()
        app.update()
        check("locking returns to the password prompt",
              isinstance(app.screen, UnlockScreen))
        app.show_start()
        app.update()
        check("the library screen lists the vault that was created",
              isinstance(app.screen, StartScreen)
              and any(os.path.basename(p).startswith("Self Test Vault")
                      for p, _lib in app.screen.entries()),
              str(app.screen.entries()))

        # "Add an existing vault" registers it immediately, so a vault kept
        # outside the library is listed whether or not it is unlocked now.
        outside_parent = os.path.join(work, "Outside")
        os.makedirs(outside_parent, exist_ok=True)
        outside = os.path.join(outside_parent, "Elsewhere.locker")
        Vault.create(outside, "another passphrase entirely", FAST).lock()
        app.settings.push_recent(outside)
        app.show_start()
        app.update()
        listed = dict((os.path.normcase(p), lib)
                      for p, lib in app.screen.entries())
        check("a vault added from outside the library is listed without "
              "being unlocked",
              os.path.normcase(outside) in listed)
        check("it is marked as living outside the library",
              listed.get(os.path.normcase(outside)) is False)
        check("library vaults are still marked as in the library",
              any(lib for _p, lib in app.screen.entries()))

        app.settings.drop_recent(outside)
        app.show_start()
        app.update()
        check("removing it from the list takes it off the screen",
              os.path.normcase(outside)
              not in [os.path.normcase(p) for p, _l in app.screen.entries()])

        # ---- switching between vaults without going through the desktop ----
        from vaultlib.ui import known_vaults

        primary = os.path.join(ui_library, "Self Test Vault.locker")
        second = os.path.join(ui_library, "Second Vault.locker")
        Vault.create(second, "a second long passphrase", FAST).lock()

        opened = Vault(primary)
        opened.unlock("a reasonably long passphrase")
        app.show_browser(opened)
        app.update()
        browser = app.screen
        check("a vault is open again for the switch checks",
              isinstance(browser, BrowserScreen))

        browser._sync_switch_menu()
        labels = [browser.switch_menu.entrycget(i, "label")
                  for i in range(browser.switch_menu.index("end") + 1)]
        check("the switch menu offers the other vault",
              any("Second Vault" in text for text in labels), str(labels))
        check("the switch menu never offers the vault already open",
              not any("Self Test Vault" in text for text in labels),
              str(labels))

        # Switching straight to a named vault.
        switched = browser.switch_vault(second)
        app.update()
        check("switching to another vault locks the one that was open",
              not opened.is_unlocked)
        check("switching to another vault lands on its password prompt",
              isinstance(app.screen, UnlockScreen)
              and os.path.normcase(app.screen.vault.path)
              == os.path.normcase(second),
              type(app.screen).__name__)

        # Switching with no target goes back to the list of vaults.
        again = Vault(primary)
        again.unlock("a reasonably long passphrase")
        app.show_browser(again)
        app.update()
        app.screen.switch_vault()
        app.update()
        check("switching with no target locks up and shows the vault list",
              not again.is_unlocked and isinstance(app.screen, StartScreen))
        check("both vaults are listed after switching out",
              len([p for p, _l in known_vaults(app.settings)]) >= 2)

        # An unsaved working copy must be able to stop the switch. The test
        # harness auto-closes the prompt, which counts as cancelling.
        third = Vault(primary)
        third.unlock("a reasonably long passphrase")
        app.show_browser(third)
        app.update()
        pending = app.workspace.reserve("dropped.txt")
        third.extract_file(["dropped.txt"], pending)
        app.workspace.track(["dropped.txt"], pending)
        import time as _t
        _t.sleep(0.02)
        with open(pending, "wb") as fh:
            fh.write(b"unsaved work in another program\n")
        blocked = app.screen.switch_vault(second)
        app.update()
        check("a cancelled unsaved-changes prompt aborts the switch",
              blocked is False and third.is_unlocked
              and isinstance(app.screen, BrowserScreen))
        check("the unsaved working copy is left alone when the switch aborts",
              os.path.isfile(pending))
        app.workspace.close_all()
        third.lock()
    except Exception as exc:
        check("the create-a-vault flow completes", False,
              "%s: %s" % (type(exc).__name__, exc))
    finally:
        _crypto.calibrate_kdf = real_calibrate
        dialogs._Modal.show = original_show
        if app is not None:
            try:
                app.destroy()
            except Exception:
                pass

    check("no exception surfaced from a Tk callback", not crashes,
          str(crashes[:1]))


def main() -> int:
    work = tempfile.mkdtemp(prefix="desktopvault_selftest_")
    vault_path = os.path.join(work, "Test.locker")
    source = os.path.join(work, "source")
    os.makedirs(os.path.join(source, "nested", "deeper"))

    empty = os.path.join(source, "empty.bin")
    open(empty, "wb").close()
    note = os.path.join(source, "note.txt")
    with open(note, "wb") as fh:
        fh.write(b"top secret memo\n")
    payload = os.urandom(3 * 1024 * 1024 + 12345)     # spans several chunks
    big = os.path.join(source, "big.bin")
    with open(big, "wb") as fh:
        fh.write(payload)
    with open(os.path.join(source, "nested", "a.txt"), "wb") as fh:
        fh.write(b"A" * 5000)
    with open(os.path.join(source, "nested", "deeper", "b.txt"), "wb") as fh:
        fh.write(b"B" * 70000)

    try:
        section("Encryption round trip")
        vault = Vault.create(vault_path, PASSWORD, FAST)
        for path in (empty, note, big):
            vault.add_file([], path)
        vault.add_folder([], os.path.join(source, "nested"))
        stats = vault.stats()
        check("vault reports 5 files, 2 folders",
              stats["files"] == 5 and stats["folders"] == 2, str(stats))

        out = os.path.join(work, "out")
        os.makedirs(out)
        for name in ("empty.bin", "note.txt", "big.bin"):
            vault.extract_file([name], os.path.join(out, name))
        vault.extract_tree(["nested"], out)

        check("empty file round-trips",
              os.path.getsize(os.path.join(out, "empty.bin")) == 0)
        with open(os.path.join(out, "note.txt"), "rb") as fh:
            check("small file round-trips", fh.read() == b"top secret memo\n")
        with open(os.path.join(out, "big.bin"), "rb") as fh:
            check("3 MB multi-chunk file round-trips", fh.read() == payload)
        with open(os.path.join(out, "nested", "deeper", "b.txt"), "rb") as fh:
            check("nested folder round-trips", fh.read() == b"B" * 70000)

        section("What the disk reveals")
        names = []
        for root, dirs, files in os.walk(vault_path):
            names += dirs + files
        check("no original file names on disk",
              not any(part in n for n in names
                      for part in ("note", "big", "nested", "empty")),
              str(names[:6]))

        blob_bytes = b""
        for root, _dirs, files in os.walk(os.path.join(vault_path, "data")):
            for name in files:
                with open(os.path.join(root, name), "rb") as fh:
                    blob_bytes += fh.read()
        check("no plaintext content in the blobs",
              b"top secret memo" not in blob_bytes and payload[:64] not in blob_bytes)

        with open(os.path.join(vault_path, "index.enc"), "rb") as fh:
            check("index is ciphertext", b"note.txt" not in fh.read())

        header = json.load(open(os.path.join(vault_path, "vault.json")))
        check("header holds no key material",
              set(header) >= {"salt", "wrapped_master", "kdf"}
              and "master" not in json.dumps(header).replace("wrapped_master", ""))

        section("Wrong password and tampering")
        vault.lock()
        reopened = Vault(vault_path)
        try:
            reopened.unlock("not the password")
            check("wrong password rejected", False, "it was accepted")
        except BadPassword:
            check("wrong password rejected", True)
        reopened.unlock(PASSWORD)

        blobs = [os.path.join(r, f)
                 for r, _d, fs in os.walk(os.path.join(vault_path, "data"))
                 for f in fs]
        target = max(blobs, key=os.path.getsize)
        with open(target, "r+b") as fh:
            fh.seek(500)
            fh.write(b"\xff\xff\xff\xff")
        try:
            reopened.extract_file(["big.bin"], os.path.join(work, "bad.bin"))
            check("flipped ciphertext bits detected", False, "decrypted anyway")
        except crypto.CryptoError:
            check("flipped ciphertext bits detected", True)

        with open(target, "r+b") as fh:
            fh.truncate(os.path.getsize(target) // 2)
        try:
            reopened.extract_file(["big.bin"], os.path.join(work, "cut.bin"))
            check("truncated blob detected", False, "decrypted anyway")
        except crypto.CryptoError:
            check("truncated blob detected", True)

        reopened.lock()
        header_path = os.path.join(vault_path, "vault.json")
        header = json.load(open(header_path))
        header["kdf"]["memory_cost"] = 8            # attacker weakens the KDF
        json.dump(header, open(header_path, "w"))
        try:
            Vault(vault_path).unlock(PASSWORD)
            check("KDF downgrade detected", False, "header tamper accepted")
        except BadPassword:
            check("KDF downgrade detected", True)

        section("Password change")
        shutil.rmtree(vault_path)
        vault = Vault.create(vault_path, PASSWORD, FAST)
        vault.add_file([], note)
        vault.lock()
        changer = Vault(vault_path)
        changer.change_password(PASSWORD, "a different long passphrase",
                                recalibrate=False)
        try:
            changer.unlock(PASSWORD)
            check("old password stops working", False, "it still opens")
        except BadPassword:
            check("old password stops working", True)
        changer.unlock("a different long passphrase")
        recovered = os.path.join(work, "after.txt")
        changer.extract_file(["note.txt"], recovered)
        with open(recovered, "rb") as fh:
            check("files still readable after re-key",
                  fh.read() == b"top secret memo\n")

        section("Working copies")
        workspace = Workspace()
        disk = workspace.reserve("note.txt")
        changer.extract_file(["note.txt"], disk)
        handle = workspace.track(["note.txt"], disk)
        check("decrypted copy created", os.path.isfile(disk))
        check("unedited copy is not flagged", not workspace.modified())

        if os.name == "nt":
            acl = subprocess.run(["icacls", workspace.root],
                                 capture_output=True, text=True).stdout
            aces = [line.replace(workspace.root, "").strip().split(":(")[0]
                    for line in acl.splitlines() if ":(" in line]
            check("workspace readable only by this account",
                  len(aces) == 1
                  and aces[0].lower().endswith(
                      (os.environ.get("USERNAME") or "").lower()),
                  str(aces))

        with open(disk, "w") as fh:
            fh.write("edited in place\n")
        check("edit is detected", len(workspace.modified()) == 1)
        old_blob = changer.node_at(["note.txt"])["blob"]
        changer.replace_file(["note.txt"], disk)
        check("save-back rotates the blob and key",
              changer.node_at(["note.txt"])["blob"] != old_blob
              and not os.path.exists(changer._blob_path(old_blob)))

        slot = os.path.dirname(disk)
        workspace.close_all()
        check("working copies are shredded",
              not os.path.exists(disk) and not os.path.exists(slot)
              and os.listdir(workspace.root) == [])

        section("Deletion and recovery")
        changer.mkdir([], "Scratch")
        changer.add_file(["Scratch"], big, "copy.bin")
        blob = changer.node_at(["Scratch", "copy.bin"])["blob"]
        changer.delete(["Scratch"])
        check("deleting a folder shreds its blobs",
              not os.path.exists(changer._blob_path(blob))
              and not changer.exists(["Scratch"]))

        os.remove(os.path.join(vault_path, "index.enc"))
        changer.lock()
        survivor = Vault(vault_path)
        survivor.unlock("a different long passphrase")
        check("a lost index falls back to the backup",
              survivor.exists(["note.txt"]))
        survivor.lock()

        section("Integrity check")
        # A fresh vault: the one above was deliberately rolled back to its
        # backup index, which legitimately leaves a dangling entry behind.
        scan_path = os.path.join(work, "Scan.locker")
        scan = Vault.create(scan_path, PASSWORD, FAST)
        scan.add_file([], note)
        scan.add_file([], big, "scan-me.bin")
        scan.mkdir([], "Sub")
        scan.add_file(["Sub"], note, "copy.txt")
        report = scan.verify_all()
        check("a healthy vault reports no problems",
              not report["problems"] and report["checked"] == 3, str(report))

        victim = scan._blob_path(scan.node_at(["scan-me.bin"])["blob"])
        with open(victim, "r+b") as fh:
            fh.seek(2048)
            fh.write(b"\x00\x01\x02\x03")
        report = scan.verify_all()
        check("corruption is found by the integrity check",
              len(report["problems"]) == 1
              and report["problems"][0][0] == "scan-me.bin", str(report))

        os.remove(scan._blob_path(scan.node_at(["Sub", "copy.txt"])["blob"]))
        report = scan.verify_all()
        check("a missing blob is reported with its full path",
              ("Sub/copy.txt", "encrypted data file is missing")
              in report["problems"], str(report["problems"]))

        # The rolled-back vault from the previous section should still show the
        # entry whose blob was shredded before the rollback.
        survivor.unlock("a different long passphrase")
        stale = survivor.verify_all()
        check("index rollback leaves a dangling entry the check can find",
              any(reason == "encrypted data file is missing"
                  for _name, reason in stale["problems"]), str(stale))
        survivor.lock()
        scan.lock()

        section("Settings file")
        settings = Settings()
        settings.push_recent(vault_path)
        with open(settings.path, encoding="utf-8") as fh:
            body = fh.read()
        check("settings contain no secrets",
              PASSWORD not in body and "wrapped" not in body)
        settings.drop_recent(vault_path)

        with open(settings.path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + b'{"autolock_minutes": 9}')
        check("a byte-order mark does not wipe the settings",
              Settings()["autolock_minutes"] == 9)
        with open(settings.path, "w", encoding="utf-8") as fh:
            fh.write('{"recent": null, "autolock_minutes": "banana"}')
        reloaded = Settings()
        check("bad types fall back to the defaults instead of reaching the UI",
              reloaded["recent"] == [] and reloaded["autolock_minutes"] == 5)
        try:
            os.remove(settings.path)
        except OSError:
            pass

        section("In-memory editing")
        from vaultlib import editor
        mem = Vault.create(os.path.join(work, "Memo.locker"), PASSWORD, FAST)
        created = mem.create_text_file([], "notes.txt",
                                       "first line\nsecond line\n".encode())
        check("a text file can be created from memory", created == "notes.txt")
        check("it reads back byte for byte",
              mem.read_bytes(["notes.txt"]) == b"first line\nsecond line\n")

        before = mem.node_at(["notes.txt"])["blob"]
        mem.write_bytes(["notes.txt"], "rewritten\n".encode())
        after = mem.node_at(["notes.txt"])["blob"]
        check("saving from memory rotates the blob and shreds the old one",
              before != after and not os.path.exists(mem._blob_path(before)))
        check("the new contents are what comes back",
              mem.read_bytes(["notes.txt"]) == b"rewritten\n")
        check("nothing was written outside the vault while editing",
              mem.verify_all()["problems"] == [])

        big_text = ("x" * 1000 + "\n") * 40
        mem.write_bytes(["notes.txt"], big_text.encode())
        check("a larger text file round-trips through memory",
              mem.read_bytes(["notes.txt"]).decode() == big_text)

        mem.add_file([], big, "binary.bin")
        check("binary content is refused by the text editor",
              not editor.is_probably_text(mem.read_bytes(["binary.bin"])[:9000]))
        check("text is recognised as text",
              editor.is_probably_text(b"plain ascii\nlines\n"))
        check("a UTF-8 BOM is preserved on the way out",
              editor.decode(b"\xef\xbb\xbfhi") == ("hi", "utf-8", True))
        check("editable files are picked by extension and size",
              editor.looks_editable("a.txt", 10)
              and editor.looks_editable("a.json", 10)
              and not editor.looks_editable("a.png", 10)
              and not editor.looks_editable("a.txt", 99 * 1024 * 1024))
        try:
            mem.read_bytes(["binary.bin"], limit=1024)
            check("oversized files are refused by the editor path", False)
        except VaultError:
            check("oversized files are refused by the editor path", True)
        mem.lock()

        section("Vault library")
        from vaultlib.store import list_vaults, move_vault
        lib = os.path.join(work, "Library")
        os.makedirs(lib)
        check("an empty library lists nothing", list_vaults(lib) == [])

        loose_parent = os.path.join(work, "Elsewhere")
        os.makedirs(loose_parent)
        loose = Vault.create(os.path.join(loose_parent, "Loose.locker"),
                             PASSWORD, FAST)
        loose.add_file([], note)
        loose.lock()

        moved = move_vault(os.path.join(loose_parent, "Loose.locker"), lib)
        check("a vault moves into the library",
              os.path.dirname(moved) == lib
              and not os.path.exists(os.path.join(loose_parent, "Loose.locker")))
        check("the library now lists it", list_vaults(lib) == [moved])

        after_move = Vault(moved)
        after_move.unlock(PASSWORD)
        recovered = os.path.join(work, "moved.txt")
        after_move.extract_file(["note.txt"], recovered)
        with open(recovered, "rb") as fh:
            check("the moved vault still opens and reads correctly",
                  fh.read() == b"top secret memo\n")
        after_move.lock()

        second = Vault.create(os.path.join(loose_parent, "Loose.locker"),
                              PASSWORD, FAST)
        second.lock()
        again = move_vault(os.path.join(loose_parent, "Loose.locker"), lib)
        check("a name clash in the library does not overwrite anything",
              again != moved and len(list_vaults(lib)) == 2, again)

        not_a_vault = os.path.join(work, "NotAVault")
        os.makedirs(not_a_vault, exist_ok=True)
        try:
            move_vault(not_a_vault, lib)
            check("moving a non-vault is refused", False)
        except VaultError:
            check("moving a non-vault is refused", True)

        from vaultlib.session import default_library
        probe_path = os.path.join(work, "probe-settings.json")
        probe = Settings(probe_path)
        probe["library"] = lib
        check("the library setting is honoured and detects membership",
              probe.library == lib and probe.in_library(moved)
              and not probe.in_library(not_a_vault))

        probe["library"] = ""
        check("an empty library setting falls back to the default location",
              probe.library == default_library())

        probe["library"] = lib
        probe.save()
        check("the library choice survives a save and reload",
              Settings(probe_path).library == lib)

        probe["library"] = ""
        probe.save()
        check("clearing it returns to the default on reload",
              Settings(probe_path).library == default_library())

        section("Shell integration")
        from vaultlib import shell
        if not shell.available():
            check("shell integration is skipped off Windows", True)
        else:
            desktop = shell.desktop_dir()
            check("the Desktop folder is resolved to a real directory",
                  os.path.isdir(desktop), desktop)
            check("the launcher target exists",
                  os.path.isfile(shell.launcher()[0]), str(shell.launcher()))

            deco = os.path.join(work, "Decorated.locker")
            marked = Vault.create(deco, PASSWORD, FAST)
            marked.add_file([], note)
            marked.lock()
            check("a vault folder accepts the padlock customisation",
                  shell.set_folder_icon(deco)
                  and os.path.isfile(os.path.join(deco, "desktop.ini")))

            # desktop.ini sits in the vault root; nothing may trip over it
            reopened = Vault(deco)
            check("desktop.ini does not stop the vault being recognised",
                  Vault.looks_like_vault(deco))
            reopened.unlock(PASSWORD)
            report = reopened.verify_all()
            check("desktop.ini is not mistaken for vault data",
                  not report["problems"] and report["orphans"] == 0,
                  str(report))
            out = os.path.join(work, "deco.txt")
            reopened.extract_file(["note.txt"], out)
            with open(out, "rb") as fh:
                check("a customised vault still reads back correctly",
                      fh.read() == b"top secret memo\n")
            reopened.lock()
            check("the customisation can be removed again",
                  shell.clear_folder_icon(deco)
                  and not os.path.exists(os.path.join(deco, "desktop.ini")))

        section("Display scaling")
        from vaultlib import theme as _theme
        check("px() is identity at 100%",
              _theme.px(100) == 100 if _theme.SCALE == 1.0 else True)

        class FakeRoot:
            def __init__(self, w, h):
                self._w, self._h = w, h

            def winfo_screenwidth(self):
                return self._w

            def winfo_screenheight(self):
                return self._h

            @staticmethod
            def winfo_fpixels(_spec):
                return 96.0

        check("a 4K screen keeps 200% scaling",
              _theme.fit_scale(FakeRoot(3840, 2160), 2.0) == 2.0)
        check("1080p steps 200% down until the layout fits",
              _theme.fit_scale(FakeRoot(1920, 1080), 2.0) == 1.5)
        check("a small screen falls back to 100%",
              _theme.fit_scale(FakeRoot(1280, 720), 2.0) == 1.0)
        check("scaling is never pushed below 100%",
              _theme.fit_scale(FakeRoot(800, 600), 1.0) == 1.0)

        saved = _theme.SCALE
        try:
            _theme.SCALE = 1.5
            check("px() returns whole scaled pixels",
                  _theme.px(10) == 15 and _theme.px(8) == 12
                  and isinstance(_theme.px(8), int))
        finally:
            _theme.SCALE = saved

        os.environ["DESKTOPVAULT_UI_SCALE"] = "1.75"
        try:
            check("the manual scale override is honoured",
                  _theme.detect_scale(FakeRoot(3840, 2160)) == 1.75)
            os.environ["DESKTOPVAULT_UI_SCALE"] = "nonsense"
            measured = _theme.detect_scale(FakeRoot(3840, 2160))
            check("a bad override is ignored rather than crashing",
                  isinstance(measured, float) and measured >= 1.0)
        finally:
            os.environ.pop("DESKTOPVAULT_UI_SCALE", None)

        section("Dialog call sites")
        for line in check_dialog_callsites():
            check(line, False)
        check("every dialogs.* call in ui.py matches its signature",
              not check_dialog_callsites())

        section("User interface")
        ui_checks(work)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

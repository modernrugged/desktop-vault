"""Vault container: header, encrypted index and encrypted blob storage.

On-disk layout of ``MyVault.locker/``::

    vault.json      plaintext header - format, KDF params, salt, wrapped
                    master key.  Contains no secrets and no file names.
    index.enc       AES-256-GCM encrypted JSON tree: every file and folder
                    name, size, timestamp and per-file key lives in here.
    index.enc.bak   previous index, kept so a crash mid-write is survivable
    data/xx/<id>.blob   one encrypted blob per file, named with 128 random
                    bits.  Nothing about the original name or location is
                    recoverable from the file system alone.

An observer without the password learns only: that a vault exists, roughly how
much data it holds, and how many files it contains.  Names, folder structure,
content and exact sizes are all inside the authenticated ciphertext.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
from typing import Iterable, Optional

from . import crypto, shamir
from .crypto import CryptoError

# The app is called Desktop Vault, but these three identifiers are part of the
# stored format and must never be renamed with it.  VAULT_MAGIC is written into
# vault.json and fed to the AEAD as associated data when the master key is
# wrapped; INDEX_INFO and INDEX_AAD derive and authenticate the index key.
# Changing any of them would make every vault created so far permanently
# unopenable, with no error message that would explain why.
VAULT_MAGIC = "SecretVault"
FORMAT_VERSION = 1
HEADER_NAME = "vault.json"
INDEX_NAME = "index.enc"
INDEX_BAK = "index.enc.bak"
DATA_DIR = "data"
VAULT_SUFFIX = ".locker"

# Recovery shares live in their own file. Adding a field to vault.json would
# change the associated data the master key is wrapped under, which would make
# every vault created before this feature refuse to open.
RECOVERY_NAME = "recovery.enc"
RECOVERY_MAGIC = "DesktopVaultRecovery"
RECOVERY_INFO = b"desktopvault/recovery/v1"

INDEX_INFO = b"secretvault/index/v1"
INDEX_AAD = b"secretvault/index"

SHRED_PASSES = 1        # see shred_file() for why this is not higher


def list_vaults(directory: str) -> list:
    """Every valid vault directly inside ``directory``, sorted by name."""
    found = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return found
    for entry in entries:
        if entry.is_dir(follow_symlinks=False) and Vault.looks_like_vault(entry.path):
            found.append(entry.path)
    found.sort(key=lambda p: os.path.basename(p).lower())
    return found


def move_vault(src: str, dest_dir: str, progress=None) -> str:
    """Relocate a whole vault, verifying the copy before removing the original.

    A rename is used when the destination is on the same volume, so the common
    case is instant and cannot half-finish.  Across volumes the vault is copied
    first and only deleted once the copy is confirmed readable.
    """
    src = os.path.abspath(src)
    dest_dir = os.path.abspath(dest_dir)
    if not Vault.looks_like_vault(src):
        raise VaultError("%s is not a vault." % src)
    os.makedirs(dest_dir, exist_ok=True)

    base = os.path.basename(src.rstrip("\\/"))
    target = os.path.join(dest_dir, base)
    if os.path.normcase(target) == os.path.normcase(src):
        return src
    n = 2
    while os.path.exists(target):
        stem = base[:-len(VAULT_SUFFIX)] if base.lower().endswith(VAULT_SUFFIX) else base
        target = os.path.join(dest_dir, "%s (%d)%s" % (stem, n, VAULT_SUFFIX))
        n += 1

    same_volume = (os.path.splitdrive(src)[0].lower()
                   == os.path.splitdrive(target)[0].lower())
    if same_volume:
        if progress:
            progress("Moving %s..." % base)
        os.rename(src, target)
        if not Vault.looks_like_vault(target):
            os.rename(target, src)
            raise VaultError("The vault did not survive the move; nothing changed.")
        return target

    if progress:
        progress("Copying %s to another drive..." % base)
    shutil.copytree(src, target)
    if not Vault.looks_like_vault(target):
        shutil.rmtree(target, ignore_errors=True)
        raise VaultError("The copy could not be verified; the original is untouched.")
    src_files = sum(len(f) for _r, _d, f in os.walk(src))
    new_files = sum(len(f) for _r, _d, f in os.walk(target))
    if new_files < src_files:
        shutil.rmtree(target, ignore_errors=True)
        raise VaultError("The copy is incomplete; the original is untouched.")
    if progress:
        progress("Removing the original...")
    shutil.rmtree(src, ignore_errors=True)
    return target


class VaultError(Exception):
    """User-facing vault problem (locked, bad path, name clash, ...)."""


class BadPassword(VaultError):
    pass


# ------------------------------------------------------------------- helpers --

def now() -> int:
    return int(time.time())


def canonical(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


def shred_file(path: str, passes: int = SHRED_PASSES) -> None:
    """Overwrite then unlink.

    On an SSD with wear levelling the overwrite does not reliably reach the
    original cells, so this is a best-effort measure that defeats casual
    undelete tools, not forensic recovery.  Never rely on it for plaintext you
    could have avoided writing in the first place.
    """
    try:
        if not os.path.isfile(path):
            return
        size = os.path.getsize(path)
        if size:
            with open(path, "r+b", buffering=0) as fh:
                for _ in range(max(1, passes)):
                    fh.seek(0)
                    remaining = size
                    while remaining > 0:
                        block = min(remaining, 1 << 20)
                        fh.write(crypto.random_bytes(block))
                        remaining -= block
                    fh.flush()
                    os.fsync(fh.fileno())
        os.remove(path)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def unique_name(existing: Iterable[str], name: str) -> str:
    """Return ``name``, or ``name (2)``/``name (3)``... if it is taken."""
    taken = set(existing)
    if name not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = "%s (%d)%s%s" % (stem, n, "." if ext else "", ext)
        if candidate not in taken:
            return candidate
        n += 1


def sanitize(name: str) -> str:
    """Strip characters that would be dangerous when exported to disk."""
    name = name.replace("\\", "_").replace("/", "_").strip()
    for ch in '<>:"|?*':
        name = name.replace(ch, "_")
    name = "".join(c for c in name if ord(c) >= 32)
    name = name.rstrip(" .")
    return name or "unnamed"


# --------------------------------------------------------------- tree nodes --

def new_dir(name: str) -> dict:
    t = now()
    return {"t": "d", "name": name, "created": t, "modified": t, "children": {}}


def new_file(name: str, size: int, blob: str, key_hex: str, sha: str) -> dict:
    t = now()
    return {"t": "f", "name": name, "created": t, "modified": t,
            "size": size, "blob": blob, "key": key_hex, "sha256": sha}


def is_dir(node: dict) -> bool:
    return node.get("t") == "d"


# ---------------------------------------------------------------- the vault --

class Vault:
    """An encrypted vault directory.

    All secret state (`_master`, `_index_key`, `_index`) exists only between
    :meth:`unlock` and :meth:`lock`.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.header: dict = {}
        self._master: Optional[bytearray] = None
        self._index_key: Optional[bytearray] = None
        self._index: Optional[dict] = None
        self._dirty = False
        if os.path.isdir(self.path):
            self._load_header()

    # ------------------------------------------------------------- discovery --

    @property
    def name(self) -> str:
        base = os.path.basename(self.path.rstrip("\\/"))
        if base.lower().endswith(VAULT_SUFFIX):
            base = base[: -len(VAULT_SUFFIX)]
        return base or self.path

    @property
    def is_unlocked(self) -> bool:
        return self._index is not None

    @staticmethod
    def looks_like_vault(path: str) -> bool:
        try:
            with open(os.path.join(path, HEADER_NAME), "r", encoding="utf-8") as fh:
                return json.load(fh).get("magic") == VAULT_MAGIC
        except (OSError, ValueError):
            return False

    def _load_header(self) -> None:
        hp = os.path.join(self.path, HEADER_NAME)
        if not os.path.isfile(hp):
            raise VaultError("No vault header found in %s" % self.path)
        with open(hp, "r", encoding="utf-8") as fh:
            header = json.load(fh)
        if header.get("magic") != VAULT_MAGIC:
            raise VaultError("%s is not a Desktop Vault" % self.path)
        if int(header.get("format", 0)) > FORMAT_VERSION:
            raise VaultError("This vault was made by a newer version of the app.")
        self.header = header

    # ---------------------------------------------------------------- create --

    @staticmethod
    def create(path: str, password: str,
               kdf_params: Optional[crypto.KdfParams] = None) -> "Vault":
        path = os.path.abspath(path)
        if os.path.exists(path) and os.listdir(path):
            raise VaultError("Folder already exists and is not empty:\n%s" % path)
        os.makedirs(os.path.join(path, DATA_DIR), exist_ok=True)

        params = kdf_params or crypto.calibrate_kdf()
        salt = crypto.random_bytes(crypto.SALT_LEN)
        vault_id = crypto.random_bytes(16)
        master = bytearray(crypto.random_bytes(crypto.KEY_LEN))

        header = {
            "magic": VAULT_MAGIC,
            "format": FORMAT_VERSION,
            "vault_id": vault_id.hex(),
            "cipher": "AES-256-GCM",
            "created": now(),
            "kdf": params.to_dict(),
            "salt": salt.hex(),
        }
        kek = crypto.derive_kek(password, salt, params)
        try:
            # The AAD is the header itself, so KDF parameters and salt cannot
            # be downgraded or swapped without breaking the unwrap.
            header["wrapped_master"] = crypto.aead_encrypt(
                bytes(kek), bytes(master), canonical(header)).hex()
        finally:
            crypto.wipe(kek)

        with open(os.path.join(path, HEADER_NAME), "w", encoding="utf-8") as fh:
            json.dump(header, fh, indent=2)

        vault = Vault(path)
        vault._master = master
        vault._index_key = crypto.subkey(bytes(master), INDEX_INFO)
        vault._index = {"version": 1, "root": new_dir("")}
        vault._save_index()
        return vault

    # ---------------------------------------------------------------- unlock --

    def _header_aad(self) -> bytes:
        return canonical({k: v for k, v in self.header.items()
                          if k != "wrapped_master"})

    def _unwrap_master(self, password: str) -> bytes:
        """Recover the master key from the password without adopting it."""
        params = crypto.KdfParams.from_dict(self.header["kdf"])
        salt = bytes.fromhex(self.header["salt"])
        kek = crypto.derive_kek(password, salt, params)
        try:
            return crypto.aead_decrypt(
                bytes(kek), bytes.fromhex(self.header["wrapped_master"]),
                self._header_aad())
        except CryptoError:
            raise BadPassword("Incorrect password.")
        finally:
            crypto.wipe(kek)

    def _adopt_master(self, master: bytes) -> None:
        """Take a recovered master key into use and load the index."""
        self._master = bytearray(master)
        self._index_key = crypto.subkey(bytes(self._master), INDEX_INFO)
        try:
            self._index = self._load_index()
        except Exception:
            self.lock()
            raise

    def unlock(self, password: str) -> None:
        if self.is_unlocked:
            return
        self._adopt_master(self._unwrap_master(password))

    def lock(self) -> None:
        if self._dirty and self._index is not None:
            try:
                self._save_index()
            except Exception:
                pass
        crypto.wipe(self._master)
        crypto.wipe(self._index_key)
        self._master = None
        self._index_key = None
        self._index = None
        self._dirty = False

    def _require_unlocked(self) -> None:
        if not self.is_unlocked:
            raise VaultError("The vault is locked.")

    # ----------------------------------------------------------------- index --

    def _index_path(self) -> str:
        return os.path.join(self.path, INDEX_NAME)

    def _load_index(self) -> dict:
        for candidate in (self._index_path(),
                          os.path.join(self.path, INDEX_BAK)):
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, "rb") as fh:
                    raw = crypto.aead_decrypt(
                        bytes(self._index_key), fh.read(), INDEX_AAD)
                return json.loads(raw.decode("utf-8"))
            except (CryptoError, ValueError, OSError):
                continue
        raise VaultError(
            "The vault index is missing or damaged and no usable backup "
            "was found.  File data may still be present in data/, but the "
            "names and keys that describe it are gone.")

    def _save_index(self) -> None:
        self._require_unlocked()
        raw = json.dumps(self._index, separators=(",", ":")).encode("utf-8")
        blob = crypto.aead_encrypt(bytes(self._index_key), raw, INDEX_AAD)

        target = self._index_path()
        tmp = target + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.isfile(target):
            backup = os.path.join(self.path, INDEX_BAK)
            try:
                os.replace(target, backup)
            except OSError:
                pass
        os.replace(tmp, target)
        self._dirty = False

    def flush(self) -> None:
        if self._dirty:
            self._save_index()

    # ------------------------------------------------------------ navigation --

    def root(self) -> dict:
        self._require_unlocked()
        return self._index["root"]

    def node_at(self, path: list) -> dict:
        """Resolve a path given as a list of names.  ``[]`` is the root."""
        node = self.root()
        for part in path:
            if not is_dir(node):
                raise VaultError("Not a folder: %s" % "/".join(path))
            child = node["children"].get(part)
            if child is None:
                raise VaultError("No such item: %s" % "/".join(path))
            node = child
        return node

    def dir_at(self, path: list) -> dict:
        node = self.node_at(path)
        if not is_dir(node):
            raise VaultError("Not a folder: %s" % "/".join(path))
        return node

    def exists(self, path: list) -> bool:
        try:
            self.node_at(path)
            return True
        except VaultError:
            return False

    # ------------------------------------------------------------- tree edits --

    def mkdir(self, parent: list, name: str) -> str:
        node = self.dir_at(parent)
        name = unique_name(node["children"], sanitize(name))
        node["children"][name] = new_dir(name)
        node["modified"] = now()
        self._dirty = True
        self._save_index()
        return name

    def rename(self, path: list, new_name: str) -> str:
        if not path:
            raise VaultError("The root folder cannot be renamed.")
        parent = self.dir_at(path[:-1])
        old = path[-1]
        new_name = sanitize(new_name)
        if new_name == old:
            return old
        siblings = [k for k in parent["children"] if k != old]
        new_name = unique_name(siblings, new_name)
        node = parent["children"].pop(old)
        node["name"] = new_name
        node["modified"] = now()
        parent["children"][new_name] = node
        self._dirty = True
        self._save_index()
        return new_name

    def move(self, src: list, dest_dir: list) -> str:
        if not src:
            raise VaultError("The root folder cannot be moved.")
        if src[:-1] == dest_dir:
            return src[-1]
        if dest_dir[: len(src)] == src:
            raise VaultError("A folder cannot be moved inside itself.")
        parent = self.dir_at(src[:-1])
        target = self.dir_at(dest_dir)
        node = parent["children"].pop(src[-1])
        name = unique_name(target["children"], node["name"])
        node["name"] = name
        target["children"][name] = node
        target["modified"] = now()
        self._dirty = True
        self._save_index()
        return name

    def delete(self, path: list) -> int:
        """Remove a file or folder and shred every blob underneath it."""
        if not path:
            raise VaultError("The root folder cannot be deleted.")
        parent = self.dir_at(path[:-1])
        node = parent["children"].pop(path[-1])
        parent["modified"] = now()
        self._dirty = True
        self._save_index()          # index first: a crash must not orphan keys

        removed = 0
        for f in self._walk_files(node):
            shred_file(self._blob_path(f["blob"]))
            removed += 1
        return removed

    # ------------------------------------------------------------------ blobs --

    def _blob_path(self, blob_id: str) -> str:
        return os.path.join(self.path, DATA_DIR, blob_id[:2], blob_id + ".blob")

    def _vault_id(self) -> bytes:
        return bytes.fromhex(self.header["vault_id"])

    def _walk_files(self, node: dict):
        if is_dir(node):
            for child in list(node["children"].values()):
                for f in self._walk_files(child):
                    yield f
        else:
            yield node

    # ------------------------------------------------------------ import/export --

    def add_file(self, dest_dir: list, src_path: str, name: Optional[str] = None,
                 progress=None, cancel=None) -> str:
        """Encrypt ``src_path`` into the vault under ``dest_dir``."""
        self._require_unlocked()
        parent = self.dir_at(dest_dir)
        name = unique_name(parent["children"],
                           sanitize(name or os.path.basename(src_path)))

        blob_id = crypto.random_bytes(16).hex()
        blob_path = self._blob_path(blob_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        file_key = bytearray(crypto.random_bytes(crypto.KEY_LEN))
        aad = crypto.blob_aad_base(self._vault_id(), bytes.fromhex(blob_id))

        tmp = blob_path + ".tmp"
        try:
            with open(src_path, "rb") as src, open(tmp, "wb") as dst:
                size, sha = crypto.encrypt_stream(
                    src, dst, bytes(file_key), aad, progress, cancel)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, blob_path)
            parent["children"][name] = new_file(
                name, size, blob_id, bytes(file_key).hex(), sha)
            parent["modified"] = now()
            self._dirty = True
            self._save_index()
        except BaseException:
            for leftover in (tmp, blob_path):
                if os.path.exists(leftover):
                    shred_file(leftover)
            raise
        finally:
            crypto.wipe(file_key)
        return name

    def add_folder(self, dest_dir: list, src_dir: str,
                   progress=None, cancel=None, on_file=None) -> str:
        """Recursively import a folder from disk."""
        self._require_unlocked()
        base = sanitize(os.path.basename(src_dir.rstrip("\\/")) or "folder")
        top = self.mkdir(dest_dir, base)
        stack = [(dest_dir + [top], src_dir)]
        while stack:
            vpath, dpath = stack.pop()
            try:
                entries = sorted(os.scandir(dpath), key=lambda e: e.name.lower())
            except OSError:
                continue
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    raise CryptoError("cancelled")
                try:
                    if entry.is_dir(follow_symlinks=False):
                        sub = self.mkdir(vpath, entry.name)
                        stack.append((vpath + [sub], entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        if on_file:
                            on_file(entry.path)
                        self.add_file(vpath, entry.path, entry.name,
                                      progress, cancel)
                except OSError:
                    continue        # unreadable file: skip, keep importing
        return top

    def extract_file(self, path: list, dest_path: str,
                     progress=None, cancel=None, verify: bool = True) -> None:
        """Decrypt one vault file to ``dest_path`` on disk."""
        self._require_unlocked()
        node = self.node_at(path)
        if is_dir(node):
            raise VaultError("%s is a folder." % node["name"])
        key = bytes.fromhex(node["key"])
        aad = crypto.blob_aad_base(self._vault_id(),
                                   bytes.fromhex(node["blob"]))
        blob_path = self._blob_path(node["blob"])
        if not os.path.isfile(blob_path):
            raise VaultError("The encrypted data for %r is missing." % node["name"])

        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        tmp = dest_path + ".part"
        try:
            with open(blob_path, "rb") as src, open(tmp, "wb") as dst:
                sha = crypto.decrypt_stream(src, dst, key, aad,
                                            int(node["size"]), progress, cancel)
            if verify and node.get("sha256") and sha != node["sha256"]:
                raise CryptoError("content hash mismatch")
            os.replace(tmp, dest_path)
        except BaseException:
            if os.path.exists(tmp):
                shred_file(tmp)
            raise

    def extract_tree(self, path: list, dest_dir: str,
                     progress=None, cancel=None) -> str:
        """Decrypt a file or a whole folder into ``dest_dir``."""
        node = self.node_at(path)
        if not is_dir(node):
            out = os.path.join(dest_dir, sanitize(node["name"]))
            self.extract_file(path, out, progress, cancel)
            return out

        root_out = os.path.join(dest_dir, sanitize(node["name"]))
        os.makedirs(root_out, exist_ok=True)
        stack = [(path, root_out)]
        while stack:
            vpath, opath = stack.pop()
            folder = self.node_at(vpath)
            for child in folder["children"].values():
                if cancel is not None and cancel.is_set():
                    raise CryptoError("cancelled")
                child_out = os.path.join(opath, sanitize(child["name"]))
                if is_dir(child):
                    os.makedirs(child_out, exist_ok=True)
                    stack.append((vpath + [child["name"]], child_out))
                else:
                    self.extract_file(vpath + [child["name"]], child_out,
                                      progress, cancel)
        return root_out

    def replace_file(self, path: list, src_path: str,
                     progress=None, cancel=None) -> None:
        """Re-encrypt an existing vault file from ``src_path``.

        Used when a file that was opened for viewing comes back modified.  A
        fresh blob and a fresh key are generated, then the old blob is shred.
        """
        self._require_unlocked()
        node = self.node_at(path)
        if is_dir(node):
            raise VaultError("%s is a folder." % node["name"])
        old_blob = node["blob"]

        blob_id = crypto.random_bytes(16).hex()
        blob_path = self._blob_path(blob_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        file_key = bytearray(crypto.random_bytes(crypto.KEY_LEN))
        aad = crypto.blob_aad_base(self._vault_id(), bytes.fromhex(blob_id))

        tmp = blob_path + ".tmp"
        try:
            with open(src_path, "rb") as src, open(tmp, "wb") as dst:
                size, sha = crypto.encrypt_stream(
                    src, dst, bytes(file_key), aad, progress, cancel)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, blob_path)
            node.update(blob=blob_id, key=bytes(file_key).hex(),
                        size=size, sha256=sha, modified=now())
            self._dirty = True
            self._save_index()
        except BaseException:
            for leftover in (tmp, blob_path):
                if os.path.exists(leftover):
                    shred_file(leftover)
            raise
        finally:
            crypto.wipe(file_key)
        shred_file(self._blob_path(old_blob))

    # --------------------------------------------------- in-memory contents --

    def read_bytes(self, path: list, limit: int = None) -> bytes:
        """Decrypt one file straight into memory.

        Used by the built-in editor so that editing a text file never puts
        plaintext on the disk at all.
        """
        self._require_unlocked()
        node = self.node_at(path)
        if is_dir(node):
            raise VaultError("%s is a folder." % node["name"])
        size = int(node["size"])
        if limit is not None and size > limit:
            raise VaultError(
                "%s is %.1f MB - too large to open in the built-in editor."
                % (node["name"], size / (1024.0 * 1024.0)))
        blob_path = self._blob_path(node["blob"])
        if not os.path.isfile(blob_path):
            raise VaultError("The encrypted data for %r is missing." % node["name"])
        sink = io.BytesIO()
        with open(blob_path, "rb") as src:
            digest = crypto.decrypt_stream(
                src, sink, bytes.fromhex(node["key"]),
                crypto.blob_aad_base(self._vault_id(),
                                     bytes.fromhex(node["blob"])), size)
        if node.get("sha256") and digest != node["sha256"]:
            raise CryptoError("content hash mismatch")
        return sink.getvalue()

    def write_bytes(self, path: list, data: bytes) -> None:
        """Re-encrypt one file from memory, rotating its blob and key."""
        self._require_unlocked()
        node = self.node_at(path)
        if is_dir(node):
            raise VaultError("%s is a folder." % node["name"])
        old_blob = node["blob"]

        blob_id = crypto.random_bytes(16).hex()
        blob_path = self._blob_path(blob_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        file_key = bytearray(crypto.random_bytes(crypto.KEY_LEN))
        aad = crypto.blob_aad_base(self._vault_id(), bytes.fromhex(blob_id))

        tmp = blob_path + ".tmp"
        try:
            with open(tmp, "wb") as dst:
                size, sha = crypto.encrypt_stream(
                    io.BytesIO(data), dst, bytes(file_key), aad)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, blob_path)
            node.update(blob=blob_id, key=bytes(file_key).hex(),
                        size=size, sha256=sha, modified=now())
            self._dirty = True
            self._save_index()
        except BaseException:
            for leftover in (tmp, blob_path):
                if os.path.exists(leftover):
                    shred_file(leftover)
            raise
        finally:
            crypto.wipe(file_key)
        shred_file(self._blob_path(old_blob))

    def create_text_file(self, dest_dir: list, name: str,
                         data: bytes = b"") -> str:
        """Add a brand-new file whose contents come from memory."""
        self._require_unlocked()
        parent = self.dir_at(dest_dir)
        name = unique_name(parent["children"], sanitize(name))

        blob_id = crypto.random_bytes(16).hex()
        blob_path = self._blob_path(blob_id)
        os.makedirs(os.path.dirname(blob_path), exist_ok=True)
        file_key = bytearray(crypto.random_bytes(crypto.KEY_LEN))
        aad = crypto.blob_aad_base(self._vault_id(), bytes.fromhex(blob_id))
        tmp = blob_path + ".tmp"
        try:
            with open(tmp, "wb") as dst:
                size, sha = crypto.encrypt_stream(
                    io.BytesIO(data), dst, bytes(file_key), aad)
            os.replace(tmp, blob_path)
            parent["children"][name] = new_file(
                name, size, blob_id, bytes(file_key).hex(), sha)
            parent["modified"] = now()
            self._dirty = True
            self._save_index()
        except BaseException:
            for leftover in (tmp, blob_path):
                if os.path.exists(leftover):
                    shred_file(leftover)
            raise
        finally:
            crypto.wipe(file_key)
        return name

    # ------------------------------------------------------------- passwords --

    def change_password(self, old_password: str, new_password: str,
                        recalibrate: bool = True) -> None:
        """Re-wrap the master key under a new password.

        File data is untouched, so this is fast regardless of vault size, and
        any recovery shares keep working: they wrap the master key, not the
        password.
        """
        try:
            master = self._unwrap_master(old_password)
        except BadPassword:
            raise BadPassword("The current password is incorrect.")
        self._rewrap_master(master, new_password, recalibrate)

    def set_password(self, new_password: str, recalibrate: bool = True) -> None:
        """Set a new password using the already-unlocked master key.

        Used after unlocking with recovery shares, where by definition the old
        password is not available.
        """
        self._require_unlocked()
        self._rewrap_master(bytes(self._master), new_password, recalibrate)

    def _rewrap_master(self, master: bytes, new_password: str,
                       recalibrate: bool = True) -> None:
        params = crypto.KdfParams.from_dict(self.header["kdf"])
        new_params = crypto.calibrate_kdf() if recalibrate else params
        new_salt = crypto.random_bytes(crypto.SALT_LEN)
        header = dict(self.header)
        header.pop("wrapped_master", None)
        header["kdf"] = new_params.to_dict()
        header["salt"] = new_salt.hex()
        header["rekeyed"] = now()

        new_kek = crypto.derive_kek(new_password, new_salt, new_params)
        try:
            header["wrapped_master"] = crypto.aead_encrypt(
                bytes(new_kek), master, canonical(header)).hex()
        finally:
            crypto.wipe(new_kek)

        hp = os.path.join(self.path, HEADER_NAME)
        tmp = hp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(header, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copy2(hp, hp + ".bak")
        os.replace(tmp, hp)
        self.header = header

    # -------------------------------------------------------- recovery shares --

    def _recovery_path(self) -> str:
        return os.path.join(self.path, RECOVERY_NAME)

    def recovery_info(self) -> Optional[dict]:
        """Read the recovery block's public metadata, locked or not."""
        try:
            with open(self._recovery_path(), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return None
        if meta.get("magic") != RECOVERY_MAGIC:
            return None
        return {"threshold": int(meta.get("threshold", 0)),
                "shares": int(meta.get("shares", 0)),
                "created": int(meta.get("created", 0))}

    def has_recovery(self) -> bool:
        return self.recovery_info() is not None

    def create_recovery(self, password: str, threshold: int,
                        count: int) -> list:
        """Split a fresh recovery secret and wrap the master key under it.

        Requires the password: holding an unlocked vault is not on its own a
        reason to be able to mint a permanent second way in.  Returns the
        share strings, which are never stored anywhere.
        """
        if not shamir.MIN_THRESHOLD <= threshold <= count:
            raise VaultError("The threshold must be between %d and the number "
                             "of shares." % shamir.MIN_THRESHOLD)
        if count > shamir.MAX_SHARES:
            raise VaultError("At most %d shares." % shamir.MAX_SHARES)

        master = self._unwrap_master(password)
        secret = bytearray(crypto.random_bytes(crypto.KEY_LEN))
        kek = crypto.subkey(bytes(secret), RECOVERY_INFO)
        try:
            meta = {
                "magic": RECOVERY_MAGIC,
                "format": 1,
                "vault_id": self.header["vault_id"],
                "threshold": int(threshold),
                "shares": int(count),
                "created": now(),
            }
            meta["wrapped_master"] = crypto.aead_encrypt(
                bytes(kek), master, canonical(meta)).hex()

            target = self._recovery_path()
            tmp = target + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)

            vault_id = bytes.fromhex(self.header["vault_id"])
            return [shamir.encode_share(index, data, threshold, vault_id)
                    for index, data in shamir.split(bytes(secret), threshold,
                                                    count)]
        finally:
            crypto.wipe(secret)
            crypto.wipe(kek)

    def revoke_recovery(self) -> bool:
        """Destroy the recovery block; the shares become worthless."""
        path = self._recovery_path()
        if not os.path.isfile(path):
            return False
        shred_file(path)
        return not os.path.exists(path)

    def unlock_with_shares(self, share_texts: list) -> None:
        """Open the vault from recovery shares instead of the password."""
        if self.is_unlocked:
            return
        try:
            with open(self._recovery_path(), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            raise VaultError("This vault has no recovery shares.")
        if meta.get("magic") != RECOVERY_MAGIC:
            raise VaultError("The recovery block is damaged.")

        vault_id = bytes.fromhex(self.header["vault_id"])
        threshold = int(meta.get("threshold", 0))
        points, seen = [], set()
        for text in share_texts:
            if not (text or "").strip():
                continue
            index, data, _t = shamir.decode_share(text, vault_id)
            if index in seen:
                raise shamir.ShareError(
                    "the same share was entered twice (share %d)" % index)
            seen.add(index)
            points.append((index, data))

        if len(points) < threshold:
            raise shamir.ShareError(
                "%d of %d shares entered - %d more needed."
                % (len(points), threshold, threshold - len(points)))

        secret = shamir.combine(points[:threshold])
        kek = crypto.subkey(secret, RECOVERY_INFO)
        aad = canonical({k: v for k, v in meta.items()
                         if k != "wrapped_master"})
        try:
            master = crypto.aead_decrypt(
                bytes(kek), bytes.fromhex(meta["wrapped_master"]), aad)
        except CryptoError:
            raise BadPassword(
                "Those shares did not open the vault. They may be from a "
                "different split, or one of them is wrong.")
        finally:
            crypto.wipe(kek)
        self._adopt_master(master)

    # ----------------------------------------------------------------- stats --

    def stats(self) -> dict:
        self._require_unlocked()
        files = dirs = 0
        total = 0
        stack = [self.root()]
        while stack:
            node = stack.pop()
            if is_dir(node):
                dirs += 1
                stack.extend(node["children"].values())
            else:
                files += 1
                total += int(node.get("size", 0))
        on_disk = 0
        data_root = os.path.join(self.path, DATA_DIR)
        for root, _, names in os.walk(data_root):
            for n in names:
                try:
                    on_disk += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
        return {"files": files, "folders": max(0, dirs - 1),
                "bytes": total, "on_disk": on_disk}

    def search(self, query: str, limit: int = 500) -> list:
        """Find nodes whose name contains ``query`` (case-insensitive)."""
        self._require_unlocked()
        q = query.strip().lower()
        if not q:
            return []
        hits = []
        stack = [([], self.root())]
        while stack and len(hits) < limit:
            path, node = stack.pop()
            if is_dir(node):
                for child in node["children"].values():
                    stack.append((path + [child["name"]], child))
            if path and q in node["name"].lower():
                hits.append((path, node))
        hits.sort(key=lambda item: (is_dir(item[1]) is False, item[1]["name"].lower()))
        return hits

    def iter_files(self, node=None, path=None):
        """Yield ``(path, node)`` for every file in the tree."""
        if node is None:
            node, path = self.root(), []
        if is_dir(node):
            for child in node["children"].values():
                for item in self.iter_files(child, path + [child["name"]]):
                    yield item
        else:
            yield path, node

    def verify_all(self, progress=None, cancel=None) -> dict:
        """Decrypt every blob and re-check its authentication tag and hash.

        Nothing is written to disk: the plaintext is discarded as it is
        produced.  This is what proves a vault is still intact and untampered
        without exporting anything.
        """
        self._require_unlocked()

        class _Discard:
            def write(self, chunk):
                return len(chunk)

        files = list(self.iter_files())
        total = sum(int(n.get("size", 0)) for _p, n in files)
        problems = []
        done = 0

        for path, node in files:
            if cancel is not None and cancel.is_set():
                break
            label = "/".join(path)
            blob_path = self._blob_path(node["blob"])
            if not os.path.isfile(blob_path):
                problems.append((label, "encrypted data file is missing"))
                done += int(node.get("size", 0))
                continue
            try:
                with open(blob_path, "rb") as src:
                    digest = crypto.decrypt_stream(
                        src, _Discard(), bytes.fromhex(node["key"]),
                        crypto.blob_aad_base(self._vault_id(),
                                             bytes.fromhex(node["blob"])),
                        int(node["size"]),
                        progress=(lambda n, b=done: progress(b + n))
                        if progress else None,
                        cancel=cancel)
                if node.get("sha256") and digest != node["sha256"]:
                    problems.append((label, "content hash does not match"))
            except CryptoError as exc:
                problems.append((label, str(exc)))
            except OSError as exc:
                problems.append((label, "could not be read (%s)" % exc))
            done += int(node.get("size", 0))

        return {"checked": len(files), "bytes": total,
                "problems": problems, "orphans": len(self.find_orphan_blobs())}

    def find_orphan_blobs(self) -> list:
        """Blobs on disk that the index no longer references."""
        self._require_unlocked()
        referenced = {f["blob"] for f in self._walk_files(self.root())}
        orphans = []
        data_root = os.path.join(self.path, DATA_DIR)
        for root, _, names in os.walk(data_root):
            for n in names:
                if not n.endswith(".blob"):
                    continue
                if n[:-5] not in referenced:
                    orphans.append(os.path.join(root, n))
        return orphans

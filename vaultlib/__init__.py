"""Desktop Vault - an offline encrypted vault.

Built for the secrets that unlock everything else - API keys, webhook URLs,
signing keys, crypto recovery phrases, two-factor backup codes - and for the
documents that cannot be replaced if they leak: tax returns, passport scans,
contracts, medical records.

Argon2id derives a key-encryption key from the password; a random 256-bit
master key is wrapped under it, and AES-256-GCM encrypts the index and every
file. Nothing leaves the machine and there is no recovery path.
"""

__all__ = ["crypto", "store"]

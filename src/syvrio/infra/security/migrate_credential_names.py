"""离线将旧 Linux 凭据重新绑定到可读条目名。"""

from __future__ import annotations

import argparse
import hashlib

from .credentials import (
    CredentialStoreError,
    _credential_path,
    _has_file,
    _read_encrypted,
    _write_encrypted,
)


def migrate(name: str) -> bool:
    target = _credential_path(name)
    if target is None:
        raise CredentialStoreError("Run under the Linux account that owns the credentials.")
    old = target.with_name("syvrio-" + hashlib.sha256(name.encode()).hexdigest() + ".cred")
    if not _has_file(old):
        return False
    value = _read_encrypted(old)
    if not _has_file(target):
        _write_encrypted(target, value)
    if _read_encrypted(target) != value:
        raise CredentialStoreError("Credential values differ. Both files were retained.")
    old.unlink()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+", help="credential entry names")
    for name in parser.parse_args().names:
        print(f"{name}: {'migrated' if migrate(name) else 'no legacy file'}")


if __name__ == "__main__":
    main()

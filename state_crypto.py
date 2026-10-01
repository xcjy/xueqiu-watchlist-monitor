#!/usr/bin/env python3
"""Encrypt/decrypt the SQLite state artifact used by GitHub Actions."""
from __future__ import annotations

import argparse
import gzip
import os
import sqlite3
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def cipher() -> Fernet:
    key = os.environ.get("STATE_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("缺少 STATE_ENCRYPTION_KEY 环境变量")
    return Fernet(key.encode("ascii"))


def encrypt_database(database: Path, output: Path) -> None:
    if not database.is_file():
        raise FileNotFoundError(f"找不到 SQLite 数据库：{database}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        snapshot_path = Path(temp_dir) / "snapshot.sqlite3"
        source = sqlite3.connect(str(database), timeout=30)
        destination = sqlite3.connect(str(snapshot_path), timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        compressed = gzip.compress(snapshot_path.read_bytes(), compresslevel=6)
    output.write_bytes(cipher().encrypt(compressed))


def decrypt_database(source: Path, database: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"找不到加密状态文件：{source}")
    payload = cipher().decrypt(source.read_bytes())
    sqlite_bytes = gzip.decompress(payload)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(sqlite_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Encrypt or restore the SQLite state artifact")
    subparsers = parser.add_subparsers(dest="command", required=True)
    encrypt_parser = subparsers.add_parser("encrypt")
    encrypt_parser.add_argument("--db", required=True, type=Path)
    encrypt_parser.add_argument("--output", required=True, type=Path)
    decrypt_parser = subparsers.add_parser("decrypt")
    decrypt_parser.add_argument("--input", required=True, type=Path)
    decrypt_parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "encrypt":
        encrypt_database(args.db, args.output)
    else:
        decrypt_database(args.input, args.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

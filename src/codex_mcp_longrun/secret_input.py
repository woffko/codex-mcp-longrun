"""One-time private stdin handles for longrun commands."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import stat
import sys
import time
import uuid
from pathlib import Path


DEFAULT_SECRET_TTL_SEC = 300
DEFAULT_MAX_SECRET_BYTES = 64 * 1024
SECRET_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def default_state_dir() -> Path:
    return Path(
        os.environ.get(
            "LONGRUN_STATE_DIR",
            str(Path.home() / ".local" / "state" / "codex-longrun"),
        )
    ).expanduser().resolve()


def _read_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise RuntimeError("longrun secret storage is not a real directory")
    if details.st_uid != os.getuid():
        raise RuntimeError("longrun secret storage is not owned by the current user")
    path.chmod(0o700)


def secret_dir(state_dir: Path) -> Path:
    resolved = state_dir.expanduser().resolve()
    _ensure_private_dir(resolved)
    result = resolved / "secrets"
    _ensure_private_dir(result)
    return result


def _secret_path(state_dir: Path, secret_id: str) -> Path:
    if not SECRET_ID_RE.fullmatch(secret_id):
        raise ValueError("invalid one-time stdin handle")
    return secret_dir(state_dir) / f"{secret_id}.stdin"


def _prune_expired(directory: Path, ttl_sec: int) -> None:
    cutoff = time.time() - ttl_sec
    for candidate in directory.glob("*.stdin"):
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            continue
        if details.st_uid != os.getuid():
            continue
        if details.st_mtime <= cutoff:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def create_one_time_secret(
    state_dir: Path,
    payload: bytes,
    *,
    ttl_sec: int = DEFAULT_SECRET_TTL_SEC,
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES,
) -> str:
    if not payload:
        raise ValueError("secret stdin payload must not be empty")
    if len(payload) > max_bytes:
        raise ValueError(f"secret stdin payload exceeds {max_bytes} bytes")
    directory = secret_dir(state_dir)
    _prune_expired(directory, ttl_sec)
    secret_id = uuid.uuid4().hex
    path = directory / f"{secret_id}.stdin"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while staging secret stdin")
            view = view[written:]
        os.fsync(descriptor)
        details = os.fstat(descriptor)
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise RuntimeError("secret stdin file permissions are not 0600")
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(descriptor)
    return secret_id


def claim_one_time_secret(
    state_dir: Path,
    secret_id: str,
    *,
    ttl_sec: int = DEFAULT_SECRET_TTL_SEC,
    max_bytes: int = DEFAULT_MAX_SECRET_BYTES,
) -> int:
    """Open, validate, and unlink a staged secret, returning the owned fd."""
    path = _secret_path(state_dir, secret_id)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ValueError("one-time stdin handle was not found or was already consumed") from exc
    except OSError as exc:
        raise ValueError("one-time stdin handle is not a safe regular file") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("one-time stdin handle is not a regular file")
        if details.st_uid != os.getuid():
            raise ValueError("one-time stdin handle has the wrong owner")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ValueError("one-time stdin handle permissions must be 0600")
        if details.st_nlink != 1:
            raise ValueError("one-time stdin handle must have exactly one link")
        if details.st_size <= 0 or details.st_size > max_bytes:
            raise ValueError("one-time stdin payload has an invalid size")
        age = time.time() - details.st_mtime
        if age < -5 or age > ttl_sec:
            raise ValueError("one-time stdin handle has expired")
        current = path.lstat()
        if current.st_dev != details.st_dev or current.st_ino != details.st_ino:
            raise ValueError("one-time stdin handle changed during validation")
        path.unlink()
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prompt for one secret value and stage it as a short-lived, one-time "
            "stdin handle for codex-mcp-longrun."
        )
    )
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--no-newline", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    ttl_sec = _read_int_env("LONGRUN_SECRET_TTL_SEC", DEFAULT_SECRET_TTL_SEC, 30, 3600)
    max_bytes = _read_int_env(
        "LONGRUN_MAX_STDIN_SECRET_BYTES",
        DEFAULT_MAX_SECRET_BYTES,
        1,
        1024 * 1024,
    )
    first = getpass.getpass("Secret stdin value: ")
    if args.confirm:
        second = getpass.getpass("Confirm secret stdin value: ")
        if first != second:
            raise SystemExit("secret values did not match")
    payload = first.encode("utf-8") + (b"" if args.no_newline else b"\n")
    secret_id = create_one_time_secret(
        args.state_dir,
        payload,
        ttl_sec=ttl_sec,
        max_bytes=max_bytes,
    )
    print(secret_id)
    print(
        f"Handle expires after {ttl_sec} seconds and is deleted when first consumed.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

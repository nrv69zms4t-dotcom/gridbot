"""Encrypted storage for the Binance API key pair (Windows DPAPI).

The key+secret are serialized to JSON and encrypted with
``CryptProtectData`` (user-scoped, ``CRYPTPROTECT_UI_FORBIDDEN``), then
written to ``<data_dir>/credentials.bin``.  Only the same Windows user
on the same machine can decrypt the file.

Non-Windows platforms refuse with a clear Russian message — the desktop
app is Windows-only by design.

Rules enforced here and by callers:
* the secret is NEVER logged and NEVER sent back to the frontend;
* only a masked key preview (``AbCd…1234``) may leave this layer.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

CRED_FILENAME = "credentials.bin"
CRYPTPROTECT_UI_FORBIDDEN = 0x01

_NOT_WINDOWS_MSG = ("Хранилище API-ключей работает только в Windows "
                    "(шифрование DPAPI). Приложение поддерживает только "
                    "Windows.")


def _check_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(_NOT_WINDOWS_MSG)


if sys.platform == "win32":
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob_in(data: bytes) -> "_DATA_BLOB":
        buf = ctypes.create_string_buffer(data, len(data))
        blob = _DATA_BLOB(len(data),
                          ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob._buf = buf  # keep the buffer alive while the blob lives
        return blob

    def _blob_out_bytes(blob: "_DATA_BLOB") -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob.pbData)

    def _protect(data: bytes) -> bytes:
        blob_in, blob_out = _blob_in(data), _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise RuntimeError("CryptProtectData failed "
                               f"(WinError {ctypes.GetLastError()})")
        return _blob_out_bytes(blob_out)

    def _unprotect(data: bytes) -> bytes:
        blob_in, blob_out = _blob_in(data), _DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None,
            CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
        if not ok:
            raise RuntimeError("CryptUnprotectData failed "
                               f"(WinError {ctypes.GetLastError()})")
        return _blob_out_bytes(blob_out)


def _cred_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / CRED_FILENAME


def key_preview(api_key: str) -> str:
    """Masked, non-secret preview of the API key: ``AbCd…1234``."""
    k = str(api_key or "")
    if len(k) <= 8:
        return (k[:1] + "…") if k else "…"
    return f"{k[:4]}…{k[-4:]}"


def save_credentials(data_dir: Path | str, api_key: str,
                     api_secret: str) -> None:
    """Encrypt and persist the pair (atomic write)."""
    _check_windows()
    payload = json.dumps({"key": api_key, "secret": api_secret},
                         ensure_ascii=False).encode("utf-8")
    blob = _protect(payload)
    path = _cred_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)


def load_credentials(data_dir: Path | str) -> tuple[str, str] | None:
    """Decrypt and return ``(api_key, api_secret)``; None if not saved."""
    _check_windows()
    path = _cred_path(data_dir)
    if not path.exists():
        return None
    try:
        d = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
        return str(d["key"]), str(d["secret"])
    except Exception as exc:
        raise RuntimeError(
            "Не удалось расшифровать сохранённые API-ключи (файл "
            "повреждён или создан другим пользователем Windows). "
            f"Удалите ключи и сохраните заново. (детали: {exc})") from exc


def clear_credentials(data_dir: Path | str) -> bool:
    """Delete the stored pair; True if a file was removed."""
    path = _cred_path(data_dir)
    if path.exists():
        path.unlink()
        return True
    return False

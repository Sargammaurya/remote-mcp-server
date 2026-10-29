"""
Per-user dataset store.

A dataset is one uploaded file, kept at
    <workspace_root>/users/<user_id>/datasets/<dataset_id>/
        meta.json      what was uploaded (display name, kind, size, sha256)
        original.csv | original.xlsx
        data.csv       what inspection and later Bruin work read (UTF-8 CSV)

The client never chooses a path: dataset ids are server-generated
(`ds_` + 16 hex), the upload name is only a label, and every lookup goes
through paths.resolve_user_path. A dataset is staged in a temp directory and
renamed into place only when complete, then made read-only (dir 0500,
files 0400), so a failed upload leaves nothing and later queries running as
the service user cannot change it.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .paths import resolve_user_path, user_root

_DATASET_ID = re.compile(r"ds_[0-9a-f]{16}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# uncompressed-size cap for .xlsx (a zip); guards against decompression bombs
XLSX_MAX_UNCOMPRESSED_FACTOR = 20


class DatasetError(ValueError):
    """Rejected input or missing dataset. The message is safe to show the caller."""


@dataclass(frozen=True)
class Dataset:
    dataset_id: str
    dir: Path
    meta: dict

    @property
    def data_csv(self) -> Path:
        return self.dir / "data.csv"


def _datasets_root(settings: Settings, user_id: str) -> Path:
    root = user_root(settings.workspace_root, user_id) / "datasets"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except FileNotFoundError:
                pass
    return total


def _display_name(filename: str) -> str:
    if not isinstance(filename, str):
        raise DatasetError("filename must be a string")
    name = _CONTROL.sub("", os.path.basename(filename.replace("\\", "/"))).strip()
    if not name or len(name) > 255:
        raise DatasetError("filename must be 1..255 characters")
    return name


def _decode(content_base64: str, max_bytes: int) -> bytes:
    if not isinstance(content_base64, str):
        raise DatasetError("content_base64 must be a string")
    if len(content_base64) > (max_bytes + 2) // 3 * 4 + 4:
        raise DatasetError(f"upload exceeds the {max_bytes // (1024 * 1024)} MB limit")
    try:
        data = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise DatasetError("content_base64 is not valid base64") from None
    if not data:
        raise DatasetError("upload is empty")
    if len(data) > max_bytes:
        raise DatasetError(f"upload exceeds the {max_bytes // (1024 * 1024)} MB limit")
    return data


def _check_csv(data: bytes) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DatasetError(f"CSV must be UTF-8 encoded (byte {exc.start} is not)") from None
    if "\x00" in text:
        raise DatasetError("CSV contains NUL bytes")
    first = text.splitlines()[0] if text.strip() else ""
    if not first.strip():
        raise DatasetError("CSV has no header row")


def _xlsx_to_csv(src: Path, dst: Path, sheet: str | None, max_bytes: int) -> str:
    try:
        with zipfile.ZipFile(src) as zf:
            total = sum(i.file_size for i in zf.infolist())
    except zipfile.BadZipFile:
        raise DatasetError("file is not a valid .xlsx workbook") from None
    if total > max_bytes * XLSX_MAX_UNCOMPRESSED_FACTOR:
        raise DatasetError("workbook expands to more than the allowed uncompressed size")

    import openpyxl  # imported here: only uploads of .xlsx need it

    try:
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for bad files
        raise DatasetError(f"cannot read workbook: {type(exc).__name__}") from None
    try:
        if sheet is None:
            ws = wb.worksheets[0]
        elif sheet in wb.sheetnames:
            ws = wb[sheet]
        else:
            raise DatasetError(f"sheet {sheet!r} not found; sheets: {wb.sheetnames}")
        written = 0
        with open(dst, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            for row in ws.iter_rows(values_only=True):
                writer.writerow(["" if v is None else v for v in row])
                written = fh.tell()
                if written > max_bytes:
                    raise DatasetError("sheet converts to a CSV larger than the upload limit")
        if written == 0:
            raise DatasetError(f"sheet {ws.title!r} is empty")
        return ws.title
    finally:
        wb.close()


def _make_read_only(path: Path) -> None:
    for child in path.iterdir():
        os.chmod(child, 0o400)
    os.chmod(path, 0o500)


def upload(settings: Settings, user_id: str, filename: str, content_base64: str,
           sheet: str | None = None) -> dict:
    name = _display_name(filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in settings.allowed_input_ext:
        raise DatasetError(f"file type {ext or '(none)'} not allowed: the filename's extension sets the type, "
                           f"so it must end in one of {sorted(settings.allowed_input_ext)} (got {name!r})")
    if sheet is not None and ext != ".xlsx":
        raise DatasetError("sheet only applies to .xlsx uploads")
    data = _decode(content_base64, settings.max_upload_bytes)

    root = _datasets_root(settings, user_id)
    user_dir = user_root(settings.workspace_root, user_id)
    # worst case the stored dataset is original + converted copy
    if _dir_bytes(user_dir) + 2 * len(data) > settings.user_quota_bytes:
        raise DatasetError(f"upload would exceed the {settings.user_quota_gb} GB per-user quota")

    dataset_id = "ds_" + secrets.token_hex(8)
    staging = root / f".staging-{dataset_id}"
    staging.mkdir(mode=0o700)
    try:
        original = staging / f"original{ext}"
        original.write_bytes(data)
        meta = {
            "dataset_id": dataset_id,
            "filename": name,
            "kind": ext.lstrip("."),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "uploaded_at": int(time.time()),
        }
        if ext == ".csv":
            _check_csv(data)
            shutil.copyfile(original, staging / "data.csv")
        else:
            meta["sheet"] = _xlsx_to_csv(original, staging / "data.csv", sheet, settings.max_upload_bytes)
        (staging / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        _make_read_only(staging)
        os.rename(staging, root / dataset_id)
    except BaseException:
        _force_remove(staging)
        raise
    return meta


def get(settings: Settings, user_id: str, dataset_id: str) -> Dataset:
    # one message for malformed, missing and other users' ids alike
    if not isinstance(dataset_id, str) or not _DATASET_ID.fullmatch(dataset_id):
        raise DatasetError(f"no dataset {dataset_id!r}")
    _datasets_root(settings, user_id)
    path = resolve_user_path(settings.workspace_root, user_id, f"datasets/{dataset_id}")
    meta_file = path / "meta.json"
    if not meta_file.is_file():
        raise DatasetError(f"no dataset {dataset_id!r}")
    return Dataset(dataset_id, path, json.loads(meta_file.read_text(encoding="utf-8")))


def list_all(settings: Settings, user_id: str) -> list[dict]:
    root = _datasets_root(settings, user_id)
    out = []
    for entry in sorted(root.iterdir()):
        if _DATASET_ID.fullmatch(entry.name) and (entry / "meta.json").is_file():
            out.append(json.loads((entry / "meta.json").read_text(encoding="utf-8")))
    return sorted(out, key=lambda m: m["uploaded_at"])


def _force_remove(path: Path) -> None:
    if not path.exists():
        return
    for dirpath, dirs, _files in os.walk(path):
        os.chmod(dirpath, stat.S_IRWXU)
        for d in dirs:
            os.chmod(os.path.join(dirpath, d), stat.S_IRWXU)
    shutil.rmtree(path)


def delete(settings: Settings, user_id: str, dataset_id: str) -> None:
    ds = get(settings, user_id, dataset_id)
    _force_remove(ds.dir)

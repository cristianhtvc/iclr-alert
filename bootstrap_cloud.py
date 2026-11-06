"""Start the source-only monitor bundle in an official Python container."""

import base64
import gzip
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import sys

MAX_COMPRESSED = 1024 * 1024
MAX_DECOMPRESSED = 2 * 1024 * 1024
MAX_FILES = 50
MAX_FILE_BYTES = 256 * 1024


def unpack_bundle(bundle_b64: str, expected_sha256: str, destination: Path) -> None:
    """Hash the decoded gzip bytes; validate every source file before writing."""
    if not isinstance(bundle_b64, str) or len(bundle_b64) > 4 * ((MAX_COMPRESSED + 2) // 3):
        raise ValueError("Invalid source bundle.")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", expected_sha256):
        raise ValueError("Invalid source digest.")
    compressed = base64.b64decode(bundle_b64, validate=True)
    if len(compressed) > MAX_COMPRESSED or not hmac.compare_digest(hashlib.sha256(compressed).hexdigest(), expected_sha256.lower()):
        raise ValueError("Source digest or size is invalid.")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
        decoded = archive.read(MAX_DECOMPRESSED + 1)
    if len(decoded) > MAX_DECOMPRESSED:
        raise ValueError("Source bundle exceeds its size limit.")
    files = json.loads(decoded.decode("utf-8"))
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Invalid source file list.")
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError("Invalid destination.")
    root = destination.resolve()
    validated = []
    for name, encoded in files.items():
        if not re.fullmatch(r"iclr_alert/(?:[A-Za-z_][A-Za-z0-9_]*\.py|setup\.html)", name):
            raise ValueError("Source path is not allowed.")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise ValueError("Source file exceeds its size limit.")
        data = base64.b64decode(encoded, validate=True)
        path = (root / name).resolve()
        if len(data) > MAX_FILE_BYTES or not path.is_relative_to(root):
            raise ValueError("Invalid source file.")
        validated.append((path, data))
    for path, data in validated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def main() -> None:
    try:
        destination = Path("/tmp/iclr-alert-app")
        unpack_bundle(os.environ["ICLR_SOURCE_B64"], os.environ["ICLR_SOURCE_SHA256"], destination)
        os.chdir(destination)
        os.execv(sys.executable, [sys.executable, "-m", "iclr_alert", "watch"])
    except Exception:
        raise SystemExit("Cloud source initialization failed.") from None


if __name__ == "__main__":
    main()

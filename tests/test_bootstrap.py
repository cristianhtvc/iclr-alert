"""Source-bundle integrity and extraction limits; no real cloud process."""

import base64
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import bootstrap_cloud as bootstrap

WORK_DIR = Path(__file__).resolve().parents[1] / "work"


def bundle(files):
    compressed = gzip.compress(json.dumps(files).encode("utf-8"))
    return base64.b64encode(compressed).decode("ascii"), hashlib.sha256(compressed).hexdigest()


def encoded(content):
    return base64.b64encode(content).decode("ascii")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        directory = TemporaryDirectory(dir=WORK_DIR)
        self.addCleanup(directory.cleanup)
        self.destination = Path(directory.name) / "app"

    def test_valid_bundle_extracts_exact_file_bytes(self):
        files = {"iclr_alert/__init__.py": encoded(b""), "iclr_alert/core.py": encoded(b"# source\n"), "iclr_alert/setup.html": encoded("验证".encode("utf-8"))}
        bootstrap.unpack_bundle(*bundle(files), self.destination)
        for name, content in files.items():
            self.assertEqual((self.destination / name).read_bytes(), base64.b64decode(content))

    def test_hash_mismatch_writes_nothing(self):
        source, _ = bundle({"iclr_alert/__init__.py": encoded(b"safe")})
        with self.assertRaises(ValueError):
            bootstrap.unpack_bundle(source, "0" * 64, self.destination)
        self.assertFalse(self.destination.exists())

    def test_unsafe_paths_are_rejected_before_any_write(self):
        for unsafe in ("../outside.py", "/absolute.py", "C:/absolute.py", "iclr_alert/../outside.py", "iclr_alert\\core.py", "iclr_alert/nested/code.py", "other.py"):
            files = {"iclr_alert/core.py": encoded(b"safe"), unsafe: encoded(b"bad")}
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                bootstrap.unpack_bundle(*bundle(files), self.destination)
            self.assertFalse(self.destination.exists())

    def test_oversized_file_and_too_many_files_are_rejected(self):
        oversized = {"iclr_alert/core.py": encoded(b"x" * (bootstrap.MAX_FILE_BYTES + 1))}
        too_many = {f"iclr_alert/module_{index}.py": encoded(b"") for index in range(bootstrap.MAX_FILES + 1)}
        for files in (oversized, too_many):
            with self.assertRaises(ValueError):
                bootstrap.unpack_bundle(*bundle(files), self.destination)
            self.assertFalse(self.destination.exists())

    def test_compressed_and_decompressed_limits_prevent_expansion(self):
        compressed = b"x" * (bootstrap.MAX_COMPRESSED + 1)
        with self.assertRaises(ValueError):
            bootstrap.unpack_bundle(base64.b64encode(compressed).decode(), hashlib.sha256(compressed).hexdigest(), self.destination)
        expanded = gzip.compress(b" " * (bootstrap.MAX_DECOMPRESSED + 1))
        with self.assertRaises(ValueError):
            bootstrap.unpack_bundle(base64.b64encode(expanded).decode(), hashlib.sha256(expanded).hexdigest(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_main_executes_monitor_without_emitting_environment_values(self):
        with patch.dict(bootstrap.os.environ, {"ICLR_SOURCE_B64": "private-bundle", "ICLR_SOURCE_SHA256": "private-digest"}), patch.object(bootstrap, "unpack_bundle") as unpack, patch.object(bootstrap.os, "chdir"), patch.object(bootstrap.os, "execv") as execute:
            bootstrap.main()
        unpack.assert_called_once_with("private-bundle", "private-digest", Path("/tmp/iclr-alert-app"))
        execute.assert_called_once_with(bootstrap.sys.executable, [bootstrap.sys.executable, "-m", "iclr_alert", "watch"])
        with patch.object(bootstrap, "unpack_bundle", side_effect=ValueError("private-secret")), patch.dict(bootstrap.os.environ, {"ICLR_SOURCE_B64": "private", "ICLR_SOURCE_SHA256": "private"}):
            with self.assertRaises(SystemExit) as captured:
                bootstrap.main()
        self.assertEqual(str(captured.exception), "Cloud source initialization failed.")


if __name__ == "__main__":
    unittest.main()

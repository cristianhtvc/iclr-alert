"""Private JSON files stay private across atomic replacements and failed writes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from iclr_alert.web import write_json


@contextmanager
def with_umask(mask):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


class PrivateJsonTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_unicode_roundtrip_leaves_only_the_destination(self):
        destination = self.root / "config.json"
        value = {"paper": "测试论文", "bark_key": "private-test-key"}
        write_json(destination, value)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), value)
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_failed_serialization_preserves_existing_file_and_removes_temporary(self):
        destination = self.root / "config.json"
        original = '{"bark_key": "previous-key"}'
        destination.write_text(original, encoding="utf-8")
        with self.assertRaises(TypeError):
            write_json(destination, {"invalid": object()})
        self.assertEqual(destination.read_text(encoding="utf-8"), original)
        self.assertEqual(list(self.root.iterdir()), [destination])

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_new_private_directory_and_file_ignore_permissive_default_umask(self):
        destination = self.root / ".local" / "config.json"
        with with_umask(0o022):
            write_json(destination, {"bark_key": "private-test-key"})
        self.assertEqual(stat.S_IMODE(destination.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_replacement_restricts_file_without_changing_an_existing_shared_directory(self):
        destination = self.root / "config.json"
        self.root.chmod(0o755)
        destination.write_text("{}", encoding="utf-8")
        destination.chmod(0o644)
        with with_umask(0o022):
            write_json(destination, {"bark_key": "private-test-key"})
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_replacement_keeps_private_file_private_even_with_umask_zero(self):
        destination = self.root / "config.json"
        destination.write_text("{}", encoding="utf-8")
        destination.chmod(0o600)
        with with_umask(0):
            write_json(destination, {"bark_key": "new-private-test-key"})
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks")
    def test_predictable_temporary_symlink_cannot_redirect_a_private_write(self):
        destination = self.root / "config.json"
        other_file = self.root / "other.txt"
        other_file.write_text("untouched", encoding="utf-8")
        destination.with_suffix(".tmp").symlink_to(other_file)
        write_json(destination, {"bark_key": "private-test-key"})
        self.assertEqual(other_file.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["bark_key"], "private-test-key")


if __name__ == "__main__":
    unittest.main()

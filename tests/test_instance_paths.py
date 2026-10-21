"""Startup path checks use synthetic directories, never production data."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from instance_paths import InstancePathError, resolve_instance_data_directory


class InstanceDataPathsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.own = self.root / "pecados"
        self.other = self.root / "another-brand"
        self.own.mkdir()
        self.other.mkdir()
        self.data = self.own / "data"

    def link(self, source, destination, *, directory=False):
        os.symlink(source, destination, target_is_directory=directory)

    def test_missing_data_is_resolved_without_creating_it(self):
        self.assertEqual(self.data, resolve_instance_data_directory(self.own))
        self.assertFalse(self.data.exists())

    def test_regular_nested_files_are_allowed_without_reading_contents(self):
        nested = self.data / "wa_auth"
        nested.mkdir(parents=True)
        (nested / "fixture.json").write_text("fixture", encoding="utf-8")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected content read")), \
             patch.object(Path, "read_text", side_effect=AssertionError("unexpected content read")):
            self.assertEqual(self.data, resolve_instance_data_directory(self.own))

    def test_data_link_to_other_instance_is_rejected(self):
        self.link(self.other, self.data, directory=True)
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    def test_data_link_to_alternate_directory_inside_checkout_is_rejected(self):
        alternate = self.own / "shared"
        alternate.mkdir()
        self.link(alternate, self.data, directory=True)
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    def test_nested_directory_link_outside_data_is_rejected(self):
        self.data.mkdir()
        self.link(self.other, self.data / "audios", directory=True)
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows junction fixture requires Node")
    def test_windows_junction_to_other_instance_is_rejected(self):
        self.data.mkdir()
        junction = self.data / "audios"
        result = subprocess.run([
            shutil.which("node"), "-e",
            "require('node:fs').symlinkSync(process.argv[1],process.argv[2],'junction')",
            str(self.other), str(junction),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(junction.is_junction())
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    def test_nested_credential_file_link_outside_data_is_rejected(self):
        nested = self.data / "wa_auth"
        nested.mkdir(parents=True)
        target = self.other / "fixture.json"
        target.write_text("fixture", encoding="utf-8")
        self.link(target, nested / "creds.json")
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    def test_dangling_link_inside_data_is_rejected(self):
        self.data.mkdir()
        self.link(self.data / "missing.json", self.data / "dangling.json")
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    def test_dangling_data_directory_link_is_rejected(self):
        self.link(self.other / "missing", self.data, directory=True)
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)

    def test_internal_directory_link_is_not_followed(self):
        target = self.data / "local"
        target.mkdir(parents=True)
        self.link(target, self.data / "alias", directory=True)
        seen = []
        real_walk = os.walk

        def traced_walk(*args, **kwargs):
            for row in real_walk(*args, **kwargs):
                seen.append(Path(row[0]))
                yield row

        with patch("instance_paths.os.walk", side_effect=traced_walk):
            self.assertEqual(self.data, resolve_instance_data_directory(self.own))
        self.assertNotIn(self.data / "alias", seen)

    def test_unavailable_metadata_fails_without_exposing_paths(self):
        self.data.mkdir()
        with patch("instance_paths.os.walk", side_effect=PermissionError("private-path")):
            with self.assertRaisesRegex(InstancePathError, "^instance_path_unavailable$"):
                resolve_instance_data_directory(self.own)

    def test_non_directory_data_is_rejected(self):
        self.data.write_text("fixture", encoding="utf-8")
        with self.assertRaises(InstancePathError):
            resolve_instance_data_directory(self.own)


if __name__ == "__main__":
    unittest.main()

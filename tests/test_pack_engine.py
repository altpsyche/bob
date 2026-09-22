"""The release packer (.github/scripts/pack_engine.py): one archive format for every engine asset.

What matters here is the contract between CI and the client: the archive the publish jobs produce must
be exactly what lifecycle._install_prebuilt can fetch, verify and stage. So the round trip is tested
end to end (pack -> _install_prebuilt over file://), including the symlink case that used to cost a
second copy of a half-gigabyte CUDA lib on disk, and the sha/bytes the manifest row is built from."""
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ on sys.path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".github" / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

import pack_engine  # noqa: E402
from bob import lifecycle  # noqa: E402


class TestPackEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.staging = self.tmp / "llama-server-linux-x86_64-cuda"
        self.staging.mkdir()
        (self.staging / "llama-server").write_bytes(b"ELF" * 1000)
        (self.staging / "libcublasLt.so.12.8.4.1").write_bytes(b"lib" * 5000)

    def _pack(self):
        return pack_engine.pack(self.staging, self.tmp / "engine.tar.xz")

    def test_pack_reports_size_and_hash_of_the_archive(self):
        info = self._pack()
        out = Path(info["path"])
        self.assertTrue(out.exists())
        self.assertEqual(info["bytes"], out.stat().st_size)
        self.assertEqual(info["sha256"], hashlib.sha256(out.read_bytes()).hexdigest())

    def test_archive_is_xz_and_keeps_the_top_level_directory(self):
        info = self._pack()
        with tarfile.open(info["path"]) as t:          # auto-detects xz; fails loudly if it is not
            names = t.getnames()
        self.assertIn("llama-server-linux-x86_64-cuda/llama-server", names)

    def test_leaves_no_intermediate_tar_behind(self):
        self._pack()
        self.assertFalse((self.tmp / "engine.tar").exists())

    @unittest.skipIf(os.name == "nt", "symlinks need privilege on Windows; the CUDA DLLs carry none")
    def test_symlink_stays_a_link_through_pack_and_install(self):
        """A SONAME link must survive as a link: dereferencing it would stage a second copy of a lib
        that is hundreds of megabytes."""
        os.symlink("libcublasLt.so.12.8.4.1", self.staging / "libcublasLt.so.12")
        info = self._pack()
        bin_dir = self.tmp / "bin"
        row = {"url": Path(info["path"]).as_uri(), "sha256": info["sha256"],
               "bytes": info["bytes"], "component": "llama-server", "tier": "gpu"}
        lifecycle._install_prebuilt(row, bin_dir)
        link = bin_dir / "libcublasLt.so.12"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "libcublasLt.so.12.8.4.1")
        self.assertTrue((bin_dir / "llama-server").exists())

    def test_python_fallback_matches_the_xz_cli(self):
        """No xz binary (a bare Windows runner) must still produce an archive the client can read."""
        with mock.patch.object(pack_engine.shutil, "which", return_value=None):
            info = pack_engine.pack(self.staging, self.tmp / "fallback.tar.xz")
        with tarfile.open(info["path"]) as t:
            self.assertIn("llama-server-linux-x86_64-cuda/llama-server", t.getnames())

    def test_missing_staging_dir_fails_loudly(self):
        with self.assertRaises(SystemExit):
            pack_engine.pack(self.tmp / "nope", self.tmp / "x.tar.xz")


if __name__ == "__main__":
    unittest.main()

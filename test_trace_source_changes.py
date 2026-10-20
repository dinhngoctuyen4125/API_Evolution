"""Integration checks for source tracing on a small local Git history."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from trace_source_changes import ROOT, SourceReader, scan_pair


def command(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class SourceTracingTests(unittest.TestCase):
    def setUp(self):
        fixture_dir = ROOT / "tests"
        fixture_dir.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixture_dir)
        self.repo = Path(self.temp.name)
        command(self.repo, "init", "-q")
        command(self.repo, "config", "user.name", "Test")
        command(self.repo, "config", "user.email", "test@example.invalid")
        core = self.repo / "numpy" / "core"
        core.mkdir(parents=True)
        (self.repo / "numpy" / "__init__.py").write_text(
            "from .core import *\n", encoding="utf-8")
        (core / "__init__.py").write_text(
            "from .fromnumeric import *\n", encoding="utf-8")
        self.module = core / "fromnumeric.py"
        self.versions = []

    def tearDown(self):
        self.temp.cleanup()

    def commit(self, version, source):
        self.module.write_text(source, encoding="utf-8")
        command(self.repo, "add", ".")
        command(self.repo, "commit", "-qm", version)
        self.versions.append({"version": version, "commit_sha": command(self.repo, "rev-parse", "HEAD")})

    def pair(self):
        return {"id": 3, "library": "numpy", "deprecated_api": "numpy.product",
                "replacement_api": "numpy.prod"}

    def test_detects_reexported_signature_warning_and_removal(self):
        self.commit("1.0", """__all__ = ['product', 'prod']
def prod(a):
    return a
def product(a):
    return prod(a)
""")
        self.commit("1.1", """__all__ = ['product', 'prod']
import warnings
def prod(a, where=None):
    return a
def product(*args, **kwargs):
    warnings.warn('deprecated', DeprecationWarning)
    return prod(*args, **kwargs)
""")
        self.commit("2.0", """__all__ = ['prod']
def prod(a, where=None):
    return a
""")
        reader = SourceReader(self.repo, "numpy")
        status = {version["version"]: "tag_matches_commit" for version in self.versions}
        records, unresolved = scan_pair(self.pair(), self.versions, reader, status)
        self.assertEqual(unresolved, [])
        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first["library"], "numpy")
        self.assertEqual({change["kind"] for change in first["changes"]},
                         {"parameter_removed", "parameter_added", "warning_path_added"})
        self.assertTrue(any(item["kind"] == "git_diff" for item in first["evidence"]))
        self.assertEqual(records[1]["changes"][0]["kind"], "public_api_removed")

    def test_dynamic_public_namespace_is_unresolved(self):
        self.commit("1.0", """__all__ = ['product', 'prod']
def prod(a): return a
def product(a): return prod(a)
""")
        (self.repo / "numpy" / "__init__.py").write_text(
            "from .core import *\ndef __getattr__(name):\n    return globals()[name]\n",
            encoding="utf-8")
        self.commit("2.0", """__all__ = ['prod']
def prod(a): return a
""")
        reader = SourceReader(self.repo, "numpy")
        status = {version["version"]: "tag_matches_commit" for version in self.versions}
        records, unresolved = scan_pair(self.pair(), self.versions, reader, status)
        self.assertEqual(records, [])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["reason"], "source_resolution_incomplete")


if __name__ == "__main__":
    unittest.main()

"""Integration checks for source tracing on a small local Git history."""

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from trace_source_changes import ROOT, SourceReader, getattr_excludes_name, literal_exports, scan_pair


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
        progress = []
        records, unresolved = scan_pair(self.pair(), self.versions, reader, status,
                                        progress=progress.append, progress_every=1)
        self.assertEqual(unresolved, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(len(progress), 2)
        self.assertIn("interval 1/2", progress[0])
        first = records[0]
        self.assertEqual(first["library"], "numpy")
        self.assertEqual({change["kind"] for change in first["changes"]},
                         {"parameter_added", "wrapper_forwarding_changed", "warning_path_added"})
        self.assertEqual(first["api_states_after"]["numpy.product"]["effective_signature_status"],
                         "forwarded")
        self.assertEqual([p["name"] for p in first["api_states_after"]["numpy.product"]["effective_parameters"]],
                         ["a", "where"])
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

    def test_python2_intra_package_import_finds_keepdims(self):
        (self.repo / "numpy" / "__init__.py").write_text("from core import *\n", encoding="utf-8")
        (self.repo / "numpy" / "core" / "__init__.py").write_text(
            "from fromnumeric import *\n", encoding="utf-8")
        self.commit("1.6", """__all__ = ['alltrue', 'all']
def all(a, axis=None): return a
def alltrue(a, axis=None): return a
""")
        self.commit("1.7", """__all__ = ['alltrue', 'all']
def all(a, axis=None, keepdims=False): return a
def alltrue(a, axis=None, keepdims=False): return all(a, axis, keepdims)
""")
        pair = {"id": 1, "library": "numpy", "deprecated_api": "numpy.alltrue",
                "replacement_api": "numpy.all"}
        reader = SourceReader(self.repo, "numpy")
        status = {v["version"]: "tag_matches_commit" for v in self.versions}
        records, unresolved = scan_pair(pair, self.versions, reader, status)
        self.assertFalse(unresolved)
        self.assertEqual(len(records), 1)
        self.assertEqual({(c["api"], c["parameter"]) for c in records[0]["changes"]
                          if c["kind"] == "parameter_added"},
                         {("numpy.alltrue", "keepdims"), ("numpy.all", "keepdims")})

    def test_bounded_getattr_allows_proven_removal(self):
        self.commit("1.0", """__all__ = ['product', 'prod']
def prod(a): return a
def product(a): return prod(a)
""")
        (self.repo / "numpy" / "__init__.py").write_text(
            "from .core import prod\n"
            "def __getattr__(name):\n"
            "    if name == 'special':\n"
            "        return prod\n"
            "    raise AttributeError(name)\n", encoding="utf-8")
        self.commit("2.0", """__all__ = ['prod']
def prod(a): return a
""")
        reader = SourceReader(self.repo, "numpy")
        status = {v["version"]: "tag_matches_commit" for v in self.versions}
        records, unresolved = scan_pair(self.pair(), self.versions, reader, status)
        self.assertFalse(unresolved)
        self.assertEqual(records[0]["changes"][0]["kind"], "public_api_removed")
        self.assertEqual(records[0]["api_states_after"]["numpy.product"]["reason"],
                         "getattr_excludes_name")

    def test_dynamic_package_loader_does_not_imply_api_introduction(self):
        (self.repo / "numpy" / "__init__.py").write_text(
            "pkgload = PackageLoader()\npkgload('core')\n", encoding="utf-8")
        self.commit("0.9", "def prod(a): return a\ndef product(a): return prod(a)\n")
        (self.repo / "numpy" / "__init__.py").write_text(
            "from .core import *\n", encoding="utf-8")
        self.commit("1.0", "__all__ = ['product', 'prod']\n"
                            "def prod(a): return a\ndef product(a): return prod(a)\n")
        reader = SourceReader(self.repo, "numpy")
        status = {v["version"]: "tag_matches_commit" for v in self.versions}
        records, unresolved = scan_pair(self.pair(), self.versions, reader, status)
        self.assertFalse(records)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(reader.state(self.versions[0]["commit_sha"], "numpy.product")["reason"],
                         "dynamic_package_loader")

    def test_unhandled_getattr_branch_remains_unknown(self):
        self.commit("1.0", "__all__ = ['prod']\ndef prod(a): return a\n")
        (self.repo / "numpy" / "__init__.py").write_text(
            "from .core import prod\n"
            "def __getattr__(name):\n"
            "    match name:\n"
            "        case 'product': return prod\n"
            "    raise AttributeError(name)\n", encoding="utf-8")
        self.commit("2.0", "__all__ = ['prod']\ndef prod(a): return a\n")
        reader = SourceReader(self.repo, "numpy")
        self.assertEqual(reader.state(self.versions[-1]["commit_sha"], "numpy.product")
                         ["public_status"], "unknown")

    def test_diff_evidence_excludes_unrelated_hunks(self):
        self.commit("1.0", """__all__ = ['product', 'prod']
def prod(a): return a
def product(a): return prod(a)

def unrelated():
    return 'old_unrelated_marker'
""")
        self.commit("1.1", """__all__ = ['product', 'prod']
def prod(a): return a
def product(a, where=None): return prod(a)

def unrelated():
    return 'new_unrelated_marker'
""")
        reader = SourceReader(self.repo, "numpy")
        status = {v["version"]: "tag_matches_commit" for v in self.versions}
        records, unresolved = scan_pair(self.pair(), self.versions, reader, status)
        self.assertFalse(unresolved)
        diff = next(item for item in records[0]["evidence"] if item["kind"] == "git_diff")
        excerpt = "\n".join(item["excerpt"] for item in diff["relevant_hunks"])
        self.assertIn("where", excerpt)
        self.assertNotIn("unrelated_marker", excerpt)

    def test_literal_all_updates_are_evaluated_only_when_static(self):
        self.assertEqual(literal_exports(ast.parse("__all__ = ['all']; __all__.extend(['alltrue'])")),
                         {"all", "alltrue"})
        self.assertIsNone(literal_exports(ast.parse("__all__ = ['all']; __all__.extend(other)")))

    def test_identity_comparison_does_not_prove_getattr_absence(self):
        getter = ast.parse("def __getattr__(name):\n"
                           "    if name is not 'product': return object()\n"
                           "    raise AttributeError(name)\n").body[0]
        self.assertFalse(getattr_excludes_name(getter, "product"))


if __name__ == "__main__":
    unittest.main()

"""Focused checks for static public binding and release coverage."""

import ast
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from build_api_source_tracks import (build_history, compare_states,
                                     read_api_state)
from source_evolution_rules import SourceReader, body_hash


class SourceEvolutionTest(unittest.TestCase):
    def reader(self, sources, library="example"):
        def fake_git(_repo, *args, **_kwargs):
            if args[0] == "ls-tree":
                return SimpleNamespace(stdout="\n".join(sources) + "\n")
            if args[0] == "show":
                return SimpleNamespace(stdout=sources[args[1].split(":", 1)[1]])
            raise AssertionError(args)

        mocked = patch("source_evolution_rules.git", side_effect=fake_git)
        self.addCleanup(mocked.stop)
        mocked.start()
        return SourceReader("unused", library)

    def test_transformers_legacy_lazy_registry_resolves_method(self):
        reader = self.reader({
            "src/transformers/__init__.py": (
                '_import_structure = {"models.gadget": []}\n'
                'if is_torch_available():\n'
                '    _import_structure["models.gadget"].extend(["WidgetService"])\n'
                'sys.modules[__name__] = _LazyModule(__name__, __file__, _import_structure)\n'),
            "src/transformers/models/__init__.py": "pass\n",
            "src/transformers/models/gadget/__init__.py": (
                '_import_structure = {"modeling_gadget": ["WidgetService"]}\n'
                'sys.modules[__name__] = _LazyModule(__name__, __file__, _import_structure)\n'),
            "src/transformers/models/gadget/modeling_gadget.py": (
                "class WidgetService:\n    def run(self, value):\n"
                "        return value\n"),
        }, library="transformers")
        status, result, _ = read_api_state(
            reader, {"version": "4.15.0", "commit_sha": "s1"},
            "transformers.WidgetService.run", None)
        self.assertEqual(status, "present")
        binding = result[0]["public_binding"]
        self.assertEqual(binding["kind"], "lazy_export")
        self.assertEqual(binding["conditions"], ["is_torch_available()"])

    def test_multilevel_renamed_import_resolves_definition(self):
        reader = self.reader({
            "example/__init__.py": "from .bridge import public\n",
            "example/bridge.py": "from .impl import hidden as public\n",
            "example/impl.py": "def hidden(value=1):\n    return value\n",
        })
        status, result, _ = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"}, "example.public", None)
        self.assertEqual(status, "present")
        self.assertEqual(result[0]["source_location"]["path"], "example/impl.py")
        self.assertEqual(result[0]["declared_parameters"][0]["name"], "value")

    def test_class_local_alias_chain_keeps_public_declaration(self):
        reader = self.reader({
            "example/__init__.py": "from .core import Service\n",
            "example/core.py": ("class Service:\n"
                                "    def impl(self, value=1):\n"
                                "        return value\n"
                                "    alias = impl\n"
                                "    public = alias\n"),
        })
        status, result, _ = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"},
            "example.Service.public", None)
        self.assertEqual(status, "present")
        self.assertIsNone(result[0]["declared_parameters"])
        self.assertEqual([p["name"] for p in result[0]["effective_parameters"]],
                         ["self", "value"])
        self.assertEqual(result[0]["public_binding"]["kind"], "class_member")

    def test_literal_getattr_export_can_rename_target(self):
        reader = self.reader({
            "example/__init__.py": ("def __getattr__(name):\n"
                                    "    if name == 'public':\n"
                                    "        from .impl import hidden as exposed\n"
                                    "        return exposed\n"
                                    "    raise AttributeError(name)\n"),
            "example/impl.py": "def hidden(value):\n    return value\n",
        })
        status, result, _ = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"}, "example.public", None)
        self.assertEqual(status, "present")
        self.assertEqual(result[0]["public_binding"]["kind"], "getattr_export")
        self.assertEqual(result[0]["public_binding"]["target"],
                         "example.impl.hidden")

    def test_unresolved_getattr_does_not_become_absent(self):
        reader = self.reader({
            "example/__init__.py": "def __getattr__(name):\n    return lookup(name)\n",
        })
        status, _, _ = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"}, "example.public", None)
        self.assertEqual(status, "unknown")

    def test_conditional_public_import_is_not_proven_present(self):
        reader = self.reader({
            "example/__init__.py": "if enabled:\n    from .impl import hidden as public\n",
            "example/impl.py": "def hidden(value):\n    return value\n",
        })
        status, _, reason = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"}, "example.public", None)
        self.assertEqual(status, "unknown")
        self.assertEqual(reason, "conditional_binding_unresolved")

    def test_conditional_public_class_method_is_not_proven_present(self):
        reader = self.reader({
            "example/__init__.py": "if enabled:\n    from .impl import Service\n",
            "example/impl.py": "class Service:\n    def run(self):\n        pass\n",
        })
        status, _, reason = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"},
            "example.Service.run", None)
        self.assertEqual(status, "unknown")
        self.assertEqual(reason, "conditional_binding_unresolved")

    def test_normal_package_branch_is_confirmed(self):
        reader = self.reader({
            "example/__init__.py": ("if __EXAMPLE_SETUP__:\n"
                                    "    pass\n"
                                    "else:\n"
                                    "    from .impl import hidden as public\n"),
            "example/impl.py": "def hidden(value):\n    return value\n",
        })
        status, result, _ = read_api_state(
            reader, {"version": "1.0", "commit_sha": "s1"}, "example.public", None)
        self.assertEqual(status, "present")
        self.assertEqual(result[0]["resolution_note"], "conditional_import")

    def test_coverage_marks_unchecked_and_comparison_gaps(self):
        selected = [{"version": "1.0"}, {"version": "1.1"}]
        all_versions = [{"version": "0.9"}, *selected, {"version": "1.2"}]
        history = build_history(
            None, "example.public", selected, {},
            observations=[("absent", None, None),
                          ("unknown", None, "dynamic_export")],
            all_versions=all_versions)
        coverage = history["coverage"]
        self.assertEqual(coverage["checked_versions"], ["1.0", "1.1"])
        self.assertEqual(coverage["not_checked_versions"], ["0.9", "1.2"])
        self.assertEqual(len(coverage["comparison_gaps"]), 1)
        self.assertEqual(coverage["comparison_gaps"][0]["reason"],
                         "api_presence_unresolved")

    def test_relocation_is_not_implementation_change(self):
        old = {"source_location": {"path": "example/old.py"},
               "declared_parameters": [], "effective_parameters": [],
               "public_binding": {"kind": "import", "target": "example.public"},
               "return_annotation": None}
        new = copy.deepcopy(old)
        new["source_location"]["path"] = "example/new.py"
        private = {"source_structure": {"kind": "FunctionDef"},
                   "body_hash": "same"}
        kinds = [change["kind"] for change in compare_states(
            old, new, private, private)]
        self.assertIn("source_path_changed", kinds)
        self.assertNotIn("implementation_changed", kinds)

    def test_alias_detects_target_body_change(self):
        old = {"version": "1.0", "source_location": {"path": "example/__init__.py"},
               "implementation_location": {"path": "example/impl.py"},
               "declared_parameters": None, "effective_parameters": [],
               "public_binding": {"kind": "alias", "target": "impl"},
               "return_annotation": None}
        new = copy.deepcopy(old)
        new["version"] = "1.1"
        old_private = {"source_structure": {"kind": "Assign"},
                       "body_hash": None, "effective_body_hash": "body_v1"}
        new_private = {**old_private, "effective_body_hash": "body_v2"}
        changes = compare_states(old, new, old_private, new_private)
        implementation = next(item for item in changes
                              if item["kind"] == "implementation_changed")
        self.assertEqual(implementation["before"]["path"], "example/impl.py")

    def test_alias_tracks_changed_target_across_commits(self):
        files = {
            "s1": {"example/__init__.py": "from .core import public\n",
                   "example/core.py": "def impl():\n    return 1\npublic = impl\n"},
            "s2": {"example/__init__.py": "from .core import public\n",
                   "example/core.py": "def impl():\n    return 2\npublic = impl\n"},
        }

        def fake_git(_repo, *args, **_kwargs):
            if args[0] == "ls-tree":
                return SimpleNamespace(stdout="\n".join(files[args[-1]]) + "\n")
            if args[0] == "show":
                sha, path = args[1].split(":", 1)
                return SimpleNamespace(stdout=files[sha][path])
            raise AssertionError(args)

        with patch("source_evolution_rules.git", side_effect=fake_git):
            reader = SourceReader("unused", "example")
            old_status, old, _ = read_api_state(
                reader, {"version": "1.0", "commit_sha": "s1"},
                "example.public", None)
            new_status, new, _ = read_api_state(
                reader, {"version": "1.1", "commit_sha": "s2"},
                "example.public", None)
        self.assertEqual((old_status, new_status), ("present", "present"))
        self.assertEqual(old[0]["implementation_location"]["path"],
                         "example/core.py")
        self.assertNotEqual(old[0]["implementation_ast_fingerprint"],
                            new[0]["implementation_ast_fingerprint"])
        self.assertIn("implementation_changed", [item["kind"] for item in
                      compare_states(old[0], new[0], old[1], new[1])])

    def test_body_fingerprint_ignores_docstrings_but_keeps_code_literals(self):
        before = ast.parse('def f():\n    "old docs"\n    return "result"\n').body[0]
        docs_changed = ast.parse('def f():\n    "new docs"\n    return "result"\n').body[0]
        code_changed = ast.parse('def f():\n    "old docs"\n    return "other"\n').body[0]
        self.assertEqual(body_hash(before), body_hash(docs_changed))
        self.assertNotEqual(body_hash(before), body_hash(code_changed))


if __name__ == "__main__":
    unittest.main()

import ast
import copy
import unittest

from trace_source_changes import (
    SourceReader,
    body_hash,
    compare_parameters,
    compare_states,
    body_equal_after_parameter_rename,
    implementation_relationship,
    parse_with_legacy_syntax,
    summarize_coverage,
    transitive_dependency_changes,
)


class MemoryReader(SourceReader):
    def __init__(self, source):
        super().__init__(None, "pkg")
        self.source = source

    def module_file(self, sha, module):
        return "pkg/__init__.py" if module == "pkg" else None

    def exact_module_file(self, sha, module):
        return self.module_file(sha, module)

    def parse(self, sha, path):
        return ast.parse(self.source)


def parameter(name, default=None):
    return {"name": name, "kind": "positional_or_keyword",
            "default": default, "annotation": None}


class SourceRuleTests(unittest.TestCase):
    def test_last_unconditional_definition_wins(self):
        reader = MemoryReader("def f(): return 1\ndef f(): return 2\n")
        result = reader.resolve_name("sha", "pkg", "f")
        self.assertEqual(result.node.lineno, 2)

    def test_last_unconditional_assignment_wins(self):
        reader = MemoryReader("def f(): return 1\nf = replacement\n")
        result = reader.resolve_name("sha", "pkg", "f")
        self.assertEqual(result.node.lineno, 2)
        self.assertEqual(result.alias_target, "replacement")

    def test_conditional_rebinding_is_unresolved(self):
        reader = MemoryReader("def f(): return 1\nif flag:\n    def f(): return 2\n")
        result = reader.resolve_name("sha", "pkg", "f")
        self.assertEqual((result.status, result.reason),
                         ("unknown", "conditional_rebinding_unresolved"))

    def test_order_change_survives_default_change(self):
        before = [parameter("a"), parameter("b", "0")]
        after = [parameter("b", "1"), parameter("a")]
        self.assertEqual({change["kind"] for change in compare_parameters(before, after)},
                         {"parameter_default_changed", "parameter_order_changed"})

    def test_added_parameter_does_not_count_as_reordering(self):
        before = [parameter("a"), parameter("b")]
        after = [parameter("a"), parameter("new"), parameter("b")]
        self.assertEqual([change["kind"] for change in compare_parameters(before, after)],
                         ["parameter_added"])

    def test_parameter_rename_does_not_create_delegation_or_implementation_event(self):
        old = MemoryReader("def f(x, axis=None):\n"
                           "    method = x.all\n"
                           "    return method(axis)\n")
        new = MemoryReader("def f(a, axis=None):\n"
                           "    method = a.all\n"
                           "    return method(axis)\n")
        old_state, new_state = old.state("old", "pkg.f"), new.state("new", "pkg.f")
        changes, reason = compare_states(old_state, new_state,
                                         old.resolve_api("old", "pkg.f").node,
                                         new.resolve_api("new", "pkg.f").node)
        self.assertIsNone(reason)
        self.assertEqual([change["kind"] for change in changes], ["parameter_renamed"])
        self.assertEqual((changes[0]["before"], changes[0]["after"], changes[0]["position"]),
                         ("x", "a", 0))
        self.assertEqual(changes[0]["implementation_change_class"],
                         "parameter_rename_only")
        self.assertEqual(changes[0]["semantic_equivalence_status"], "not_established")

    def test_parameter_rename_keeps_independent_body_change(self):
        before = ast.parse("def f(x): return x.all()").body[0]
        after = ast.parse("def f(a): return a.any()").body[0]
        self.assertFalse(body_equal_after_parameter_rename(before, after, {"x": "a"}))
        old = MemoryReader("def f(x): return x.all()\n")
        new = MemoryReader("def f(a): return a.any()\n")
        changes, _ = compare_states(old.state("old", "pkg.f"), new.state("new", "pkg.f"),
                                    before, after)
        self.assertEqual({change["kind"] for change in changes},
                         {"parameter_renamed", "delegation_changed", "implementation_changed"})

    def test_multiple_name_substitutions_remain_unclassified(self):
        changes = compare_parameters([parameter("x"), parameter("y")],
                                     [parameter("a"), parameter("b")])
        self.assertEqual({change["kind"] for change in changes},
                         {"parameter_added", "parameter_removed"})

    def test_rename_with_another_parameter_default_change(self):
        changes = compare_parameters([parameter("x"), parameter("axis", "None")],
                                     [parameter("a"), parameter("axis", "0")])
        self.assertEqual({change["kind"] for change in changes},
                         {"parameter_renamed", "parameter_default_changed"})

    def test_docstring_only_change_has_same_implementation_hash(self):
        original = ast.parse("def f(x): return x").body[0]
        before, after = copy.deepcopy(original), copy.deepcopy(original)
        before.body.insert(0, ast.Expr(value=ast.Constant("old docs")))
        after.body.insert(0, ast.Expr(value=ast.Constant("new docs")))
        self.assertEqual(body_hash(before), body_hash(after))
        self.assertEqual(compare_parameters([parameter("x")], [parameter("x")]), [])

    def test_docstring_only_change_creates_no_event(self):
        old = MemoryReader('def f(x):\n    """old docs"""\n    return x\n')
        new = MemoryReader('def f(x):\n    """new docs"""\n    return x\n')
        changes, reason = compare_states(old.state("old", "pkg.f"),
                                         new.state("new", "pkg.f"))
        self.assertIsNone(reason)
        self.assertEqual(changes, [])

    def test_same_body_without_direct_wrapper_is_not_migration_evidence(self):
        reader = MemoryReader("def old(x): return x.all()\ndef new(x): return x.all()\n")
        pair = {"deprecated_api": "pkg.old", "replacement_api": "pkg.new"}
        states = {api: reader.state("sha", api) for api in pair.values()}
        self.assertEqual(implementation_relationship(pair, reader, "sha", states)["status"],
                         "not_observed")

    def test_direct_wrapper_is_migration_evidence(self):
        reader = MemoryReader("def old(*args, **kwargs): return new(*args, **kwargs)\n"
                              "def new(x): return x\n")
        pair = {"deprecated_api": "pkg.old", "replacement_api": "pkg.new"}
        states = {api: reader.state("sha", api) for api in pair.values()}
        relation = implementation_relationship(pair, reader, "sha", states)
        self.assertEqual((relation["status"], relation["kind"]),
                         ("observed", "direct_wrapper"))

    def test_legacy_syntax_fallback_preserves_line_numbers(self):
        source = ("import sys\nprint >> sys.stderr, 'message'\n"
                  "try:\n    pass\nexcept ImportError, error:\n    print error\n")
        tree, rewrites = parse_with_legacy_syntax(source, "pkg/__init__.py")
        self.assertIsNotNone(tree)
        self.assertEqual({item["kind"] for item in rewrites},
                         {"print_redirect", "except_binding", "print_statement"})
        self.assertEqual(tree.body[-1].handlers[0].lineno, 5)

    def test_unsupported_legacy_syntax_stays_unresolved(self):
        tree, rewrites = parse_with_legacy_syntax("raise ValueError, 'x'\n", "pkg.py")
        self.assertIsNone(tree)
        self.assertEqual(rewrites, [])

    def test_removal_makes_relationship_unavailable(self):
        pair = {"deprecated_api": "pkg.old", "replacement_api": "pkg.new"}
        states = {"pkg.old": {"public_status": "absent"},
                  "pkg.new": {"public_status": "present"}}
        result = implementation_relationship(pair, None, "sha", states)
        self.assertEqual(result["status"], "unavailable_due_to_api_removal")

    def test_changed_helper_propagates_through_proved_wrapper(self):
        pair = {"deprecated_api": "pkg.old", "replacement_api": "pkg.new"}
        states = {"pkg.old": ({"public_status": "present"},
                               {"public_status": "present"})}
        relationships = [{"status": "observed", "kind": "direct_wrapper"}] * 2
        direct = [{"api": "pkg.new", "kind": "indirect_implementation_changed",
                   "before_dependency": "pkg._helper", "changed_dependency": "pkg._helper",
                   "before_hash": "a", "after_hash": "b"}]
        events = transitive_dependency_changes(pair, states, relationships, direct)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["dependency_path"],
                         ["pkg.old", "pkg.new", "pkg._helper"])
        self.assertEqual(events[0]["behavioral_impact"], "unknown")
        relationships[1] = {"status": "not_observed", "kind": None}
        self.assertEqual(transitive_dependency_changes(pair, states, relationships, direct), [])

    def test_relocation_with_same_body_is_path_change_only(self):
        body = body_hash(ast.parse("def f(x): return x").body[0])
        state = {"api": "pkg.f", "public_status": "present", "reason": None,
                 "effective_parameters": [], "effective_signature_status": "direct",
                 "source_path": "pkg/old.py", "alias_target": None,
                 "wrapper_target": None, "delegation_targets": [],
                 "delegation_resolution_status": "no_return_call_resolved",
                 "delegates_to": None, "declared_parameters": [],
                 "forwarded_arguments": {}, "deprecation_warning_scopes": [],
                 "implementation_hash": body, "normalized_implementation_hash": body}
        changes, reason = compare_states(state, {**state, "source_path": "pkg/new.py"})
        self.assertIsNone(reason)
        self.assertEqual([change["kind"] for change in changes], ["source_path_changed"])
        self.assertEqual(compare_states(state, dict(state))[0], [])

    def test_coverage_keeps_unresolved_interval(self):
        pair = {"id": 1, "library": "pkg", "deprecated_api": "pkg.old",
                "replacement_api": "pkg.new"}
        versions = [{"version": str(i)} for i in range(3)]
        unresolved = [{"from_version": "1", "to_version": "2",
                       "reason": "source_resolution_incomplete",
                       "details": [{"before": "source_syntax_not_supported",
                                    "after": "conditional_import"}]}]
        coverage = summarize_coverage(pair, versions, [], unresolved,
                                      source_compared_intervals={("0", "1"), ("1", "2")})
        self.assertEqual([item["status"] for item in coverage["interval_results"]],
                         ["unchanged", "unsupported"])


if __name__ == "__main__":
    unittest.main()

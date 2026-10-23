"""Regression checks for the pair synthesis input and output guards."""

import unittest
from collections import defaultdict

from build_migration_evolution import (
    build_facts, load_inputs, render_milestones, validate_selection, version_key,
)


class MigrationEvolutionTest(unittest.TestCase):
    def test_numpy_pair_merges_source_and_announcement(self):
        ready, missing = load_inputs("numpy", 3)
        self.assertFalse(missing)
        facts, version_states, statuses = build_facts(*ready[0])
        events = facts["candidate_events"]
        self.assertEqual(facts["deprecated_version"], "1.25.0")
        self.assertTrue(any(event["version"] == "1.15.0"
                            and event["change"] and event["change"]["type"] ==
                            "wrapper_changed" and event["change"]["delegates_to"] ==
                            "numpy.prod" for event in events))
        self.assertTrue(any(event["version"] == "2.0.0"
                            and event["change"] and event["change"]["type"] ==
                            "removed" for event in events))

        grouped = defaultdict(list)
        for event in events:
            if event["mandatory"]:
                grouped[event["version"]].append(event["id"])
        selection = {"milestones": [
            {"version": version, "event_ids": grouped[version]}
            for version in sorted(grouped, key=version_key)]}
        validate_selection(selection, events)
        output = render_milestones(facts, selection, version_states, statuses)
        self.assertEqual(output[0]["version"], "1.0")
        self.assertIsNone(output[0]["compared_to_version"])
        self.assertEqual(output[-1]["version"], "2.0.0")
        self.assertEqual(output[-1]["api_states"][0]["status"], "removed")
        self.assertEqual(output[-1]["api_states"][1]["status"], "active")
        self.assertNotIn("1.16.0", [item["version"] for item in output])

    def test_unsubstantiated_event_is_rejected(self):
        facts, _, _ = build_facts(*load_inputs("numpy", 3)[0][0])
        with self.assertRaisesRegex(ValueError, "Invalid, repeated, or misplaced"):
            validate_selection({"milestones": [
                {"version": "1.0", "event_ids": ["EV9999"]}]},
                facts["candidate_events"])

    def test_untraced_announcement_does_not_invent_api_state(self):
        facts, version_states, statuses = build_facts(
            *load_inputs("scikit-learn", 58)[0][0])
        announcement = next(event for event in facts["candidate_events"]
                            if event["category"] == "deprecation")
        self.assertEqual(announcement["version"], "0.18.0")
        milestone = render_milestones(
            facts, {"milestones": [{"version": "0.18.0",
                                    "event_ids": [announcement["id"]]}]},
            version_states, statuses)[0]
        self.assertTrue(all(state["status"] == "unknown"
                            for state in milestone["api_states"]))
        self.assertTrue(all(state["signature"] is None
                            for state in milestone["api_states"]))

    def test_missing_announcement_is_reported(self):
        with self.assertRaisesRegex(ValueError, "deprecated_version"):
            load_inputs("scipy", 51)


if __name__ == "__main__":
    unittest.main()

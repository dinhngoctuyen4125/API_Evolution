"""Focused tests for Step 6 retrieval and evidence validation."""

import io
import json
import unittest
from unittest.mock import patch

from trace_release_notes import ROOT, call_gemini, candidate_block_ids, extract_blocks, scan, validate_claims


PAIR = {"id": 3, "library": "numpy", "deprecated_api": "numpy.product", "replacement_api": "numpy.prod"}
NOTE = {"library": "numpy", "version": "1.25.0", "release_path": "cache/numpy/docs/1.25.0/release_notes.html",
        "release_url": "https://example.test/numpy/1.25.0"}


class ReleaseNoteTraceTests(unittest.TestCase):
    def test_gemini_adapter_sends_supplied_key(self):
        fake_response = {"status": "completed", "steps": [{"type": "model_output", "content": [
            {"type": "text", "text": '{"claims": []}'},
        ]}]}
        blocks = [{"block_id": "b0", "text": "np.product is deprecated. Use np.prod instead."}]
        with patch("trace_release_notes.urllib.request.urlopen",
                   return_value=io.BytesIO(json.dumps(fake_response).encode("utf-8"))) as mocked:
            result = call_gemini(PAIR, "1.25.0", blocks, "gemini-test", "secret-from-env")
        self.assertEqual(result, {"claims": []})
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://generativelanguage.googleapis.com/v1beta/interactions")
        self.assertEqual(dict((name.lower(), value) for name, value in request.header_items())["x-goog-api-key"],
                         "secret-from-env")
        self.assertEqual(json.loads(request.data)["response_format"]["mime_type"], "application/json")

    def test_html_retrieval_recognizes_numpy_alias_and_skips_navigation(self):
        html = "<nav><p>np.product is deprecated</p></nav><main><li><p>np.product is deprecated. Use np.prod instead.</p></li></main>"
        blocks = extract_blocks(html)
        self.assertEqual([b["text"] for b in blocks], ["np.product is deprecated. Use np.prod instead."])
        self.assertEqual(candidate_block_ids(blocks, PAIR), [0])

    def test_quote_must_occur_in_cited_block_and_name_api(self):
        blocks = [{"block_id": "b0", "text": "np.product is deprecated. Use np.prod instead."}]
        base = {"event_type": "deprecation_announcement", "target_api": "deprecated",
                "description": "Deprecated", "temporal_status": "current", "effective_version": None,
                "block_id": "b0", "quote": "np.product is deprecated."}
        accepted, rejected = validate_claims({"claims": [base]}, blocks, NOTE, PAIR)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["evidence_refs"], ["rn-3-1.25.0-b0"])
        self.assertFalse(rejected)
        for quote in ("The API was deprecated.", "Use instead."):
            accepted, rejected = validate_claims({"claims": [{**base, "quote": quote}]}, blocks, NOTE, PAIR)
            self.assertFalse(accepted)
            self.assertEqual(len(rejected), 1)

    def test_scan_keeps_document_claim_separate_from_missing_notes(self):
        calls = []

        def fake_extractor(pair, version, blocks, model):
            calls.append((pair["id"], version, model))
            matching = next(b for b in blocks if "np.product is deprecated" in b["text"])
            return {"claims": [{"event_type": "replacement_recommendation", "target_api": "pair",
                                "description": "Use np.prod instead", "temporal_status": "current",
                                "effective_version": None, "block_id": matching["block_id"],
                                "quote": "Use np.prod instead."}]}

        versions = [{"library": "numpy", "version": v} for v in ("1.24.4", "1.25.0")]
        output, unresolved, stats = scan([PAIR], versions, [NOTE], ROOT, fake_extractor, "fake", progress_every=0)
        self.assertEqual(calls, [(3, "1.25.0", "fake")])
        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["library"], "numpy")
        self.assertEqual(output[0]["claims"][0]["observation_type"], "documented")
        self.assertTrue(output[0]["evidence"][0]["block_id"].startswith("b"))
        self.assertEqual(unresolved[0]["reason"], "release_note_missing")
        self.assertEqual(stats["notes_with_claims"], 1)


if __name__ == "__main__":
    unittest.main()

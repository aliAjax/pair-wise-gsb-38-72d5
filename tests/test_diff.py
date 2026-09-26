import unittest

from diff import diff_cues


def cue(index, start, end, text):
    return {"cue_index": index, "start_ms": start, "end_ms": end, "text": text}


class DiffCuesTest(unittest.TestCase):
    def test_empty_parent_child(self):
        result = diff_cues([], [])
        self.assertEqual(result["summary"], {"added": 0, "removed": 0, "modified": 0, "unchanged": 0})

    def test_added_modified_removed_unchanged(self):
        parent = [
            cue(1, 1000, 2000, "保持不变"),
            cue(2, 2000, 3000, "旧文案"),
            cue(4, 4000, 5000, "将被移除"),
        ]
        child = [
            cue(1, 1000, 2000, "保持不变"),
            cue(2, 2500, 3500, "新文案"),
            cue(3, 3500, 4000, "全新句子"),
        ]
        result = diff_cues(parent, child)
        self.assertEqual([c["cue_index"] for c in result["added"]], [3])
        self.assertEqual(result["added"][0]["text"], "全新句子")
        self.assertEqual([c["cue_index"] for c in result["removed"]], [4])
        self.assertEqual(result["removed"][0]["text"], "将被移除")
        modified = result["modified"][0]
        self.assertEqual(modified["cue_index"], 2)
        self.assertEqual(modified["before"]["text"], "旧文案")
        self.assertEqual(modified["after"]["text"], "新文案")
        self.assertIn("text", modified["changes"])
        self.assertIn("timing", modified["changes"])
        self.assertEqual(result["unchanged_indexes"], [1])
        self.assertEqual(result["summary"], {"added": 1, "removed": 1, "modified": 1, "unchanged": 1})

    def test_text_only_change_does_not_flag_timing(self):
        result = diff_cues([cue(1, 0, 100, "a")], [cue(1, 0, 100, "b")])
        self.assertEqual(result["modified"][0]["changes"], ["text"])
        self.assertEqual(result["unchanged_indexes"], [])

    def test_matches_by_index_not_position(self):
        # Order must not matter; matching is on the stable cue_index.
        result = diff_cues([cue(2, 0, 100, "x"), cue(1, 0, 100, "y")],
                           [cue(1, 0, 100, "y"), cue(2, 0, 100, "x")])
        self.assertEqual(result["summary"], {"added": 0, "removed": 0, "modified": 0, "unchanged": 2})


if __name__ == "__main__":
    unittest.main()

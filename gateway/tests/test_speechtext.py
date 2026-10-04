import unittest

from gateway.backends.speechtext import NAME_OK, groups


class GroupsTests(unittest.TestCase):
    def test_sentences_are_packed_up_to_the_limit(self):
        g = groups("One. Two. Three. Four.", 12)
        self.assertEqual([t for t, _ in g], ["One. Two.", "Three. Four."])

    def test_a_long_sentence_stands_alone(self):
        g = groups("Short. " + "word " * 30 + "end. Tail.", 40)
        self.assertEqual(len(g), 3)
        self.assertTrue(g[1][0].startswith("word"))

    def test_blank_line_starts_a_new_paragraph_group(self):
        g = groups("First para. Still first.\n\nSecond para.", 300)
        self.assertEqual(g, [("First para. Still first.", False), ("Second para.", True)])

    def test_single_newline_splits_but_is_not_a_paragraph(self):
        self.assertEqual(groups("Line one\nLine two", 8), [("Line one", False), ("Line two", False)])

    def test_empty(self):
        self.assertEqual(groups("  \n\n ", 100), [])

    def test_names(self):
        self.assertTrue(NAME_OK.match("narrator-2.v1"))
        for bad in ("../x", "a/b", "", ".hidden", "a b"):
            self.assertFalse(NAME_OK.match(bad), bad)


if __name__ == "__main__":
    unittest.main()

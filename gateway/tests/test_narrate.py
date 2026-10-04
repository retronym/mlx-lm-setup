import asyncio
import unittest

from gateway.narrate import NarrateError, align, narrate, parse_scenes, split_cues


def words(*ws, step=0.5):
    return [{"word": w, "start_s": i * step, "end_s": (i + 1) * step} for i, w in enumerate(ws)]


class SplitCues(unittest.TestCase):
    def test_markers_are_removed_and_point_at_the_next_word(self):
        text, ws, cues = split_cues("One evening, [[dup]] two copies.[[end]] Done [[tail]]")
        self.assertEqual(text, "One evening, two copies. Done")
        self.assertEqual(ws, ["One", "evening,", "two", "copies.", "Done"])
        self.assertEqual(cues, {"dup": 2, "end": 4, "tail": 5})

    def test_duplicate_cue_is_an_error(self):
        with self.assertRaises(NarrateError):
            split_cues("a [[x]] b [[x]] c")


class Align(unittest.TestCase):
    def test_cue_resolves_to_the_spoken_start_of_the_next_word(self):
        _, ws, cues = split_cues("Two copies of the same thirty billion [[p]] parameter model")
        heard = words("Two", "copies", "of", "the", "same", "30", "billion", "parameter", "model")
        times, diffs = align(ws, heard, cues, 4.5)
        self.assertEqual(times, {"p": 3.5})
        self.assertEqual(diffs, [{"script": "thirty", "heard": "30"}])
        _, ws, cues = split_cues("Claude speaks M C P to the back end")
        self.assertEqual(align(ws, words("Claude", "speaks", "MCP", "to", "the", "backend"), cues, 3.0)[1], [])

    def test_unaligned_tail_falls_back_to_word_position(self):
        _, ws, cues = split_cues("alpha beta [[c]] gamma")
        times, _ = align(ws, words("alpha", "beta"), cues, 3.0)
        self.assertEqual(times, {"c": 2.0})


class ParseScenes(unittest.TestCase):
    def test_validation(self):
        for bad in ([], [{"id": "x"}], [{"id": "a/b", "text": "t"}], [{"id": "a", "text": "t"}, {"id": "a", "text": "u"}], [{"text": "[[only]]"}]):
            with self.assertRaises(NarrateError, msg=repr(bad)):
                parse_scenes(bad)
        self.assertEqual(parse_scenes([{"text": "hi"}])[0]["id"], "scene1")


class Narrate(unittest.TestCase):
    def test_scenes_run_in_order_with_running_start_times(self):
        calls = []

        async def speak(b):
            calls.append(("speak", b["name"], b["text"], b.get("ref_audio")))
            return {"path": f"/a/{b['name']}.wav", "duration_s": 2.0, "cached": True}

        async def transcribe(b):
            calls.append(("transcribe", b["path"]))
            return {"words": words("hello", "there")}

        res = asyncio.run(narrate(parse_scenes([{"id": "a", "text": "hello [[t]] there"}, {"id": "b", "text": "hello there"}]),
                                  speak, transcribe, {"ref_audio": "narrator"}))
        self.assertEqual([c[0] for c in calls], ["speak", "transcribe", "speak", "transcribe"])
        self.assertEqual(calls[0], ("speak", "narrate-a", "hello there", "narrator"))
        self.assertEqual([(s["id"], s["start_s"], s["cues"], s["file"]) for s in res["scenes"]],
                         [("a", 0.0, {"t": 0.5}, "narrate-a.wav"), ("b", 2.0, {}, "narrate-b.wav")])
        self.assertEqual(res["total_s"], 4.0)


if __name__ == "__main__":
    unittest.main()

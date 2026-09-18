"""Tests for memory_similar — lexical ordering, threshold, embedding seam."""

import unittest

import memory_similar


class LexicalTests(unittest.TestCase):
    def test_identical_text_is_one(self):
        self.assertEqual(memory_similar.similar("the cat sat", "the cat sat"), 1.0)

    def test_empty_text_is_zero(self):
        self.assertEqual(memory_similar.similar("", "the cat sat"), 0.0)
        self.assertEqual(memory_similar.similar("the cat sat", ""), 0.0)

    def test_score_is_bounded(self):
        score = memory_similar.similar("a cat sat on the mat", "a dog ran")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_ordering_follows_token_overlap(self):
        close = memory_similar.similar("the cat sat", "a cat sat down")
        far = memory_similar.similar("the cat sat", "quantum physics lecture")
        self.assertGreater(close, far)

    def test_punctuation_is_normalised(self):
        self.assertEqual(
            memory_similar.similar("the cat, sat!", "the cat sat"), 1.0
        )

    def test_reordered_variant_is_high(self):
        score = memory_similar.similar(
            "my dog's name is bruno", "dog's name is bruno"
        )
        self.assertGreaterEqual(score, memory_similar.DEFAULT_THRESHOLD)


class NearDuplicateTests(unittest.TestCase):
    def test_finds_reordered_variant(self):
        corpus = ["a completely different fact", "Dog's name is Bruno"]
        hit = memory_similar.near_duplicate("My dog's name is Bruno", corpus)
        self.assertIsNotNone(hit)
        index, score = hit
        self.assertEqual(index, 1)
        self.assertGreaterEqual(score, 0.75)

    def test_returns_none_when_unrelated(self):
        corpus = ["quantum physics is hard", "the sky is blue"]
        self.assertIsNone(memory_similar.near_duplicate("my dog is bruno", corpus))

    def test_custom_threshold_changes_answer(self):
        text = "alpha beta gamma"
        corpus = ["alpha beta"]
        self.assertIsNotNone(
            memory_similar.near_duplicate(text, corpus, threshold=0.5)
        )
        self.assertIsNone(
            memory_similar.near_duplicate(text, corpus, threshold=0.99)
        )

    def test_is_non_mutating(self):
        corpus = ["first entry", "second entry"]
        before = list(corpus)
        memory_similar.near_duplicate("first entry", corpus)
        self.assertEqual(corpus, before)


class EmbeddingSeamTests(unittest.TestCase):
    def tearDown(self):
        memory_similar.set_backend(None)

    def test_embed_defaults_to_lexical(self):
        vector = memory_similar.embed("the cat sat")
        self.assertEqual(vector, {"the": 1.0, "cat": 1.0, "sat": 1.0})

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(KeyError):
            memory_similar.set_backend("does-not-exist")

    def test_registered_backend_is_used_by_similar(self):
        def fake(text):
            return [1.0, 0.0] if "cat" in text else [0.0, 1.0]

        memory_similar.register_backend("fake", fake)
        memory_similar.set_backend("fake")
        self.assertEqual(memory_similar.active_backend(), "fake")
        self.assertAlmostEqual(
            memory_similar.similar("the cat", "a cat"), 1.0, places=6
        )
        self.assertAlmostEqual(
            memory_similar.similar("the cat", "a dog"), 0.0, places=6
        )

    def test_resetting_backend_returns_to_lexical(self):
        memory_similar.register_backend("fake2", lambda text: [1.0])
        memory_similar.set_backend("fake2")
        memory_similar.set_backend(None)
        self.assertIsNone(memory_similar.active_backend())
        self.assertEqual(memory_similar.similar("the cat sat", "the cat sat"), 1.0)

    def test_cosine_rejects_mismatched_dimensions(self):
        with self.assertRaises(ValueError):
            memory_similar._cosine([1.0], [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()

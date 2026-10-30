import unittest
from candidate import count_words, slugify, truncate


class HiddenTextToolTests(unittest.TestCase):
    def test_count_and_slug(self):
        self.assertEqual(count_words("  one  two\nthree "), 3)
        self.assertEqual(count_words("   "), 0)
        self.assertEqual(slugify("  Hello   WORLD "), "hello-world")
        self.assertEqual(slugify("   "), "")

    def test_truncate(self):
        self.assertEqual(truncate("abc", 5), "abc")
        self.assertEqual(truncate("abcdef", 5), "ab...")
        with self.assertRaises(ValueError):
            truncate("abc", 2)


if __name__ == "__main__":
    unittest.main()

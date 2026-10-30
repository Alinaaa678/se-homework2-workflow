import unittest
from candidate import average, clamp, divide


class HiddenCalculatorTests(unittest.TestCase):
    def test_divide_and_average_boundaries(self):
        self.assertEqual(divide(8, 2), 4)
        self.assertIsNone(divide(8, 0))
        self.assertEqual(average([1, 2, 3]), 2)
        self.assertIsNone(average([]))

    def test_clamp(self):
        self.assertEqual(clamp(5, 1, 3), 3)
        self.assertEqual(clamp(-1, 0, 3), 0)
        self.assertEqual(clamp(2, 0, 3), 2)
        with self.assertRaises(ValueError):
            clamp(1, 3, 0)


if __name__ == "__main__":
    unittest.main()

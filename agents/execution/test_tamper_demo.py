import unittest

from agents.execution import tamper_demo


class TamperDemo(unittest.TestCase):
    def test_valid_accepted_tampered_and_readdressed_rejected(self):
        r = tamper_demo.run()
        self.assertTrue(r["before"]["accepted"])
        self.assertFalse(r["after"]["accepted"])
        self.assertFalse(r["readdressed"]["accepted"])
        self.assertTrue(r["after"]["reasons"])
        self.assertEqual(r["offset"], 4589)


if __name__ == "__main__":
    unittest.main()

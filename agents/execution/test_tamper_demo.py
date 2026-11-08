import unittest

from agents.execution import tamper_demo


class TamperDemo(unittest.TestCase):
    def test_valid_accepted_tampered_and_readdressed_rejected(self):
        r = tamper_demo.run()
        self.assertTrue(r["before"]["accepted"])
        self.assertFalse(r["after"]["accepted"])
        self.assertFalse(r["readdressed"]["accepted"])
        self.assertTrue(r["after"]["reasons"])
        # The edit is the credit amount inside the signed body and nothing else; the byte offset
        # is display output only, since it moves with IDs and timestamps (eval ae17c2d).
        self.assertTrue(r["field"].startswith(".body."))
        self.assertTrue(r["field"].endswith(".amount_pence"))
        self.assertEqual(r["changed"], [(r["field"], 216000, 261000)])
        self.assertIsInstance(r["offset"], int)

    def test_hex_digest_containing_the_amount_is_never_edited(self):
        # A digest earlier in the bytes that contains "216000" must be skipped (eval ae17c2d).
        raw = (b'{"body":{"evidence_sha256":"ab216000cd","txn":{"amount_pence":216000,"x":1}},'
               b'"signature":"216000"}')
        bad, offset, field = tamper_demo.tamper(raw)
        self.assertEqual(field, ".body.txn.amount_pence")
        self.assertEqual(raw[offset:offset + 6], b"216000")
        self.assertIn(b'"evidence_sha256":"ab216000cd"', bad)
        self.assertIn(b'"signature":"216000"', bad)
        self.assertIn(b'"amount_pence":261000', bad)

    def test_refuses_when_no_credit_field(self):
        with self.assertRaises(SystemExit):
            tamper_demo.tamper(b'{"body":{"note":"216000"},"signature":"216000"}')


if __name__ == "__main__":
    unittest.main()

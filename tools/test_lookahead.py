import unittest

try:
    import numpy as np
    import pandas as pd

    import lookahead
except ImportError:
    pd = None


def prices(n=200, index=False):
    rng = np.random.default_rng(1)
    frame = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=n, freq="h"),
                          "price": 100 + rng.normal(0, 1, n).cumsum(),
                          "volume": rng.integers(1, 1000, n),
                          "venue": rng.choice(["a", "b"], n)})
    return frame.set_index("timestamp") if index else frame


def momentum(frame, t):
    past = frame[frame["timestamp"] <= t]
    return float(np.sign(past["price"].iloc[-1] - past["price"].iloc[-10]))


@unittest.skipIf(pd is None, "needs pandas: run with uv run --frozen python -m unittest")
class Lookahead(unittest.TestCase):
    def test_clean_strategy_passes(self):
        lookahead.assert_no_lookahead(momentum, prices(), at=15, column="timestamp")

    def test_clean_strategy_on_a_time_index(self):
        def decide(frame, t):
            return frame.loc[:t, "price"].tail(5).mean()

        lookahead.assert_no_lookahead(decide, prices(index=True), at=10)

    def test_full_sample_normalisation_is_caught(self):
        def decide(frame, t):
            z = (frame["price"] - frame["price"].mean()) / frame["price"].std()
            return float(z[frame["timestamp"] == t].iloc[0])

        problems = lookahead.find_lookahead(decide, prices(), at=5, column="timestamp")
        self.assertTrue(problems)
        self.assertTrue(any("cut off" in p for p in problems))

    def test_shift_the_wrong_way_is_caught(self):
        def decide(frame, t):
            return float(frame["price"].shift(-1)[frame["timestamp"] == t].iloc[0])

        with self.assertRaises(AssertionError):
            lookahead.assert_no_lookahead(decide, prices(), at=5, column="timestamp")

    def test_future_volume_in_a_dict_decision_is_caught(self):
        def decide(frame, t):
            return {"side": "buy", "size": int(frame["volume"].max())}

        self.assertTrue(lookahead.find_lookahead(decide, prices(), at=5, column="timestamp"))

    def test_a_crash_without_the_future_is_reported(self):
        def decide(frame, t):
            position = frame.index[frame["timestamp"] == t][0]
            return float(frame["price"].iloc[position + 1])

        problems = lookahead.find_lookahead(decide, prices(), at=3, column="timestamp")
        self.assertTrue(any("failed" in p for p in problems))

    def test_picks_times_away_from_the_ends(self):
        times = prices()["timestamp"]
        picked = lookahead.times_to_test(times, 5)
        self.assertEqual(len(picked), 5)
        self.assertGreater(picked[0], times.iloc[0])
        self.assertLess(picked[-1], times.iloc[-1])


if __name__ == "__main__":
    unittest.main()

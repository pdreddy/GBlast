"""Regression tests for the broker-independent, paper-only risk gate."""
import unittest
from dataclasses import replace
from v7.risk_guard import AccountSnapshot,evaluate
S=AccountSnapshot(100000,100000,100000,100000,0,0,0,0)
class Tests(unittest.TestCase):
    def test_sizes_whole_lots(self):
        x=evaluate(S,premium=60,lot_size=65,stop_points=3,market_data_fresh=True)
        self.assertTrue(x.allowed)
        self.assertEqual((x.lots,x.planned_loss),(2,390))
    def test_single_lot_rejected(self):
        self.assertFalse(evaluate(S,premium=100,lot_size=65,stop_points=5,market_data_fresh=True).allowed)
    def test_open_loss_counts(self):
        x=evaluate(replace(S,unrealized_today=-2000,unrealized_week=-2000),premium=60,lot_size=65,stop_points=3,market_data_fresh=True)
        self.assertEqual(x.reason,"DAILY_LOSS_LOCK")
    def test_drawdown(self):
        x=evaluate(replace(S,peak_equity=125000,realized_week=-2000),premium=60,lot_size=65,stop_points=3,market_data_fresh=True)
        self.assertEqual(x.reason,"ACCOUNT_DRAWDOWN_LOCK")
    def test_pending_risk(self):
        self.assertFalse(evaluate(replace(S,committed_risk=400),premium=60,lot_size=65,stop_points=3,market_data_fresh=True).allowed)
    def test_missing_quotes(self):
        self.assertEqual(evaluate(S,premium=60,lot_size=65,stop_points=3).reason,"STALE_OR_MISSING_MARKET_DATA")
    def test_open_limit(self):
        self.assertEqual(evaluate(replace(S,open_positions=1),premium=60,lot_size=65,stop_points=3,market_data_fresh=True).reason,"POSITION_LIMIT")
if __name__=="__main__": unittest.main()

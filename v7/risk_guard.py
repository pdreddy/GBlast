"""GBlast research-only risk gate. No trading, network, broker or webhook calls."""
from dataclasses import dataclass
from math import floor, isfinite

@dataclass(frozen=True)
class RiskLimits:
    per_trade: float = 0.005
    daily: float = 0.02
    weekly: float = 0.05
    max_drawdown: float = 0.20
    min_lots: int = 2
    max_open_positions: int = 1

@dataclass(frozen=True)
class AccountSnapshot:
    opening_equity: float
    day_start_equity: float
    week_start_equity: float
    peak_equity: float
    realized_today: float
    unrealized_today: float
    realized_week: float
    unrealized_week: float
    committed_risk: float = 0
    open_positions: int = 0

@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    lots: int
    risk_budget: float
    planned_loss: float
    reason: str

def evaluate(s: AccountSnapshot, *, premium: float, lot_size: int,
             stop_points: float, cost_and_slippage_per_lot: float = 0,
             market_data_fresh: bool = False,
             limits: RiskLimits = RiskLimits()) -> RiskDecision:
    """Before-entry eligibility. Risk estimates are not guaranteed stop fills."""
    reject = lambda reason: RiskDecision(False, 0, 0., 0., reason)
    vals = (s.opening_equity, s.day_start_equity, s.week_start_equity,
            s.peak_equity, s.realized_today, s.unrealized_today,
            s.realized_week, s.unrealized_week, s.committed_risk,
            premium, stop_points, cost_and_slippage_per_lot)
    if any(not isinstance(x, (int,float)) or not isfinite(x) for x in vals):
        return reject("INVALID_INPUT")
    if min(s.opening_equity,s.day_start_equity,s.week_start_equity,s.peak_equity,premium,stop_points)<=0:
        return reject("INVALID_INPUT")
    if lot_size<1 or s.committed_risk<0 or cost_and_slippage_per_lot<0 or stop_points>=premium:
        return reject("INVALID_INPUT")
    if not market_data_fresh:
        return reject("STALE_OR_MISSING_MARKET_DATA")
    equity = s.opening_equity + s.realized_week + s.unrealized_week
    if equity <= s.peak_equity*(1-limits.max_drawdown):
        return reject("ACCOUNT_DRAWDOWN_LOCK")
    if s.realized_today+s.unrealized_today <= -s.day_start_equity*limits.daily:
        return reject("DAILY_LOSS_LOCK")
    if s.realized_week+s.unrealized_week <= -s.week_start_equity*limits.weekly:
        return reject("WEEKLY_LOSS_LOCK")
    if s.open_positions>=limits.max_open_positions:
        return reject("POSITION_LIMIT")
    day_left = s.day_start_equity*limits.daily+s.realized_today+s.unrealized_today
    week_left = s.week_start_equity*limits.weekly+s.realized_week+s.unrealized_week
    dd_left = equity-s.peak_equity*(1-limits.max_drawdown)
    budget = max(0.,min(equity*limits.per_trade,day_left,week_left,dd_left)-s.committed_risk)
    risk_per_lot = stop_points*lot_size+cost_and_slippage_per_lot
    n = min(floor(budget/risk_per_lot),floor(equity/(premium*lot_size))) if risk_per_lot>0 else 0
    if n < limits.min_lots:
        return RiskDecision(False,0,round(budget,2),0.,"INSUFFICIENT_RISK_BUDGET_OR_LOTS")
    return RiskDecision(True,n,round(budget,2),round(n*risk_per_lot,2),"PAPER_ONLY_APPROVED")

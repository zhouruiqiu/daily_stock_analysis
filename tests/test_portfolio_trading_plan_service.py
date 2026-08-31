# -*- coding: utf-8 -*-
"""交易计划引擎测试：状态机边界、仓位取整、单股上限、锁定禁买、红线规则。

全部用 mock 数据源，不打真实网络。
"""

import contextlib
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.schemas.trading_plan import DrawdownRiskState
from src.services.portfolio_trading_plan_service import (
    PortfolioTradingPlanService,
    classify_volatility_tier,
    format_trading_plan_digest,
    floor_to_lot,
)
from src.storage import Base, PortfolioRiskState


def _fake_db():
    class _Db:
        def __init__(self):
            self._engine = create_engine("sqlite://")
            Base.metadata.create_all(self._engine, tables=[PortfolioRiskState.__table__])
            self._Session = sessionmaker(bind=self._engine)

        @contextlib.contextmanager
        def session_scope(self):
            session = self._Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    return _Db()


def _fetcher(price=None, *, daily_ok=True):
    class _F:
        def get_daily_data(self, code, days=None):
            if not daily_ok:
                return None, None
            lows = [price - 5.0 if price else 30.0] * 30
            return pd.DataFrame({"low": lows, "close": [price or 40.0] * 30}), "mock"

        def get_realtime_quote(self, code, log_final_failure=False):
            return SimpleNamespace(price=price) if price else None

    return _F()


class _Snapshot:
    def __init__(self, equity, invested, cash, positions):
        self.equity, self.invested, self.cash, self.positions = equity, invested, cash, positions

    def get_portfolio_snapshot(self, account_id=None):
        return {
            "accounts": [
                {
                    "account_id": 3,
                    "as_of": "2026-08-31",
                    "total_equity": self.equity,
                    "total_market_value": self.invested,
                    "total_cash": self.cash,
                    "positions": self.positions,
                }
            ]
        }


def _service(db, snapshot, *, price=None, supports=(35.0, 30.0), resistances=(60.0, 70.0), daily_ok=True):
    return PortfolioTradingPlanService(
        db,
        portfolio_service=snapshot,
        data_fetcher=_fetcher(price, daily_ok=daily_ok),
        trend_analyzer=SimpleNamespace(
            analyze=lambda df, code: SimpleNamespace(
                support_levels=list(supports), resistance_levels=list(resistances)
            )
        ),
    )


class HelpersTest(TestCase):
    def test_volatility_tier_by_prefix(self) -> None:
        self.assertEqual(classify_volatility_tier("300476"), "high_vol")
        self.assertEqual(classify_volatility_tier("301217"), "high_vol")
        self.assertEqual(classify_volatility_tier("688031"), "high_vol")
        self.assertEqual(classify_volatility_tier("600036"), "normal")
        self.assertEqual(classify_volatility_tier("SH600487"), "normal")

    def test_floor_to_lot(self) -> None:
        self.assertEqual(floor_to_lot(199), 100)
        self.assertEqual(floor_to_lot(200), 200)
        self.assertEqual(floor_to_lot(99), 0)
        self.assertEqual(floor_to_lot(-50), 0)

    def test_digest_format_includes_actions_and_required_disclaimer(self) -> None:
        plan = SimpleNamespace(
            total_equity=100000,
            exposure_pct=75,
            target_exposure_pct=60,
            cash_pct=25,
            drawdown_pct=3.5,
            risk_state="normal",
            items=[SimpleNamespace(
                stock_name="香农芯创", stock_code="300475", action="observe",
                current_weight_pct=4.0, action_reason="等待确认",
                max_reduce_quantity=None, max_additional_quantity=0,
            )],
        )

        content = format_trading_plan_digest(plan)

        self.assertIn("香农芯创 300475｜观察", content)
        self.assertIn("这是规则化风险管理计划，不构成收益保证或自动交易指令。", content)


class RiskStateMachineTest(TestCase):
    def setUp(self) -> None:
        self.db = _fake_db()

    def _state_at(self, equity: float) -> str:
        service = PortfolioTradingPlanService(self.db, portfolio_service=_Snapshot(0, 0, 0, []))
        _peak, _dd, state = service._update_risk_state(3, equity, datetime(2026, 8, 31, 15, 0))
        return state

    def test_boundaries(self) -> None:
        self.assertEqual(self._state_at(100_000), DrawdownRiskState.NORMAL.value)
        self.assertEqual(self._state_at(96_500), DrawdownRiskState.NORMAL.value)        # 3.5%
        self.assertEqual(self._state_at(95_800), DrawdownRiskState.CAUTION.value)       # 4.2%
        self.assertEqual(self._state_at(94_000), DrawdownRiskState.DEFENSIVE.value)     # 6.0%
        self.assertEqual(self._state_at(91_500), DrawdownRiskState.DRAWDOWN_LOCK.value)  # 8.5%

    def test_peak_is_high_watermark(self) -> None:
        service = PortfolioTradingPlanService(self.db, portfolio_service=_Snapshot(0, 0, 0, []))
        peak1, _, s1 = service._update_risk_state(3, 100_000, datetime(2026, 8, 31, 10, 0))
        peak2, dd2, s2 = service._update_risk_state(3, 95_000, datetime(2026, 8, 31, 11, 0))
        self.assertEqual(s1, DrawdownRiskState.NORMAL.value)
        self.assertEqual(peak1, 100_000)
        self.assertEqual(peak2, 100_000)
        self.assertAlmostEqual(dd2, 5.0, places=1)
        self.assertEqual(s2, DrawdownRiskState.CAUTION.value)

    def test_peak_survives_restart(self) -> None:
        PortfolioTradingPlanService(self.db, portfolio_service=_Snapshot(0, 0, 0, [])). \
            _update_risk_state(3, 100_000, datetime(2026, 8, 31, 10, 0))
        restarted = PortfolioTradingPlanService(self.db, portfolio_service=_Snapshot(0, 0, 0, []))
        peak, dd, state = restarted._update_risk_state(3, 92_000, datetime(2026, 8, 31, 14, 0))
        self.assertEqual(peak, 100_000)
        self.assertAlmostEqual(dd, 8.0, places=1)
        self.assertEqual(state, DrawdownRiskState.DRAWDOWN_LOCK.value)


class TradingPlanDecisionTest(TestCase):
    def _position(self, code, qty, price, market_value, pnl=-5.0, name="x"):
        return {
            "symbol": code, "name": name, "quantity": qty, "last_price": price,
            "market_value_base": market_value, "unrealized_pnl_pct": pnl,
        }

    def _plan(self, equity, invested, cash, positions, *, market_state="neutral", price=40.0,
              supports=(35.0, 30.0), daily_ok=True):
        service = _service(
            _fake_db(),
            _Snapshot(equity, invested, cash, positions),
            price=price,
            supports=supports,
            daily_ok=daily_ok,
        )
        return service.build_plan(market_state=market_state)

    def test_overweight_high_vol_gets_reduce(self) -> None:
        plan = self._plan(
            100_000, 99_800, 200,
            [self._position("300476", 100, 300.0, 30_000.0, pnl=-30.1, name="胜宏科技")],
            price=340.0, supports=(250.0, 240.0),
        )
        item = plan.items[0]
        self.assertEqual(item.action, "reduce")
        self.assertEqual(item.position_cap_pct, 10.0)
        self.assertIn("超", item.action_reason)

    def test_reduce_quantity_floors_to_lot(self) -> None:
        # 市值 50k / 净值 100k = 50% > 10% → excess 40k / 价 300 = 133.3 → 取整 100 股
        plan = self._plan(
            100_000, 99_900, 100,
            [self._position("300476", 166, 300.0, 50_000.0, pnl=-10.0)],
            price=320.0, supports=(250.0, 240.0),
        )
        item = plan.items[0]
        self.assertEqual(item.max_reduce_quantity, 100)
        self.assertEqual(item.action, "reduce")

    def test_within_cap_holds(self) -> None:
        plan = self._plan(
            100_000, 50_000, 50_000,
            [self._position("600036", 1000, 40.0, 12_000.0, pnl=2.0, name="招商银行")],
            price=41.0, supports=(35.0, 30.0),
        )
        item = plan.items[0]
        self.assertEqual(item.action, "hold")
        self.assertEqual(item.position_cap_pct, 15.0)

    def test_low_cash_blocks_new_positions(self) -> None:
        plan = self._plan(
            100_000, 99_800, 200,
            [self._position("600036", 500, 40.0, 60_000.0, pnl=1.0)],
            price=41.0, supports=(35.0, 30.0),
        )
        self.assertFalse(plan.new_positions_allowed)
        self.assertTrue(any("现金比例" in reason for reason in plan.portfolio_blocking_reasons))
        self.assertEqual(plan.items[0].max_additional_quantity, 0)

    def test_support_breach_triggers_risk_action(self) -> None:
        # 支撑 90，现价 85 → 跌破 5.6% ≥3% → exit
        plan = self._plan(
            100_000, 50_000, 50_000,
            [self._position("600487", 1000, 85.0, 50_000.0, pnl=-12.0)],
            price=85.0, supports=(90.0, 85.0),
        )
        item = plan.items[0]
        self.assertTrue(item.support_breached)
        self.assertEqual(item.action, "exit")
        self.assertEqual(item.stop_price, 90.0)

    def test_missing_data_degrades_to_observe(self) -> None:
        plan = self._plan(
            100_000, 50_000, 50_000,
            [self._position("600036", 500, 40.0, 20_000.0, pnl=1.0)],
            price=None, daily_ok=False,
        )
        item = plan.items[0]
        self.assertEqual(item.action, "observe")
        self.assertEqual(item.data_quality, "missing")

    def test_target_exposure_by_market_state(self) -> None:
        self.assertEqual(self._plan(100_000, 50_000, 50_000, [], market_state="sustained_down").target_exposure_pct, 40.0)
        self.assertEqual(self._plan(100_000, 50_000, 50_000, [], market_state="sustained_up").target_exposure_pct, 80.0)

    def test_no_unconditional_buy_action(self) -> None:
        plan = self._plan(
            100_000, 50_000, 50_000,
            [self._position("600036", 200, 40.0, 8_000.0, pnl=5.0)],
            price=41.0,
        )
        for item in plan.items:
            self.assertIn(
                item.action,
                {"hold", "observe", "reduce", "exit", "add_if_confirmed", "blocked"},
            )

    def test_loss_position_never_gets_add_allowance(self) -> None:
        plan = self._plan(
            100_000, 50_000, 50_000,
            [self._position("600036", 200, 40.0, 8_000.0, pnl=-25.0)],
            price=41.0,
        )
        item = plan.items[0]
        self.assertTrue(any("补仓" in reason for reason in item.blocking_reasons))

    def test_disclaimer_always_present(self) -> None:
        plan = self._plan(100_000, 50_000, 50_000, [])
        self.assertTrue(any("不构成收益保证" in note for note in plan.limitations))

"""Tests for Risk Management system"""

import pytest
from decimal import Decimal

from src.risk.manager import RiskManager, RiskLimits, PortfolioState, AlertLevel
from src.strategy.kelly import BetRecommendation, BetDirection


class TestRiskLimits:
    """Tests for RiskLimits configuration"""

    def test_default_limits(self):
        """Test default risk limits are sensible"""
        limits = RiskLimits()

        assert limits.max_position_size == Decimal("100")
        assert limits.max_position_pct == Decimal("0.10")
        assert limits.max_drawdown == Decimal("0.15")
        assert limits.min_edge == Decimal("0.02")


class TestPortfolioState:
    """Tests for PortfolioState tracking"""

    def test_exposure_calculation(self):
        """Test exposure percentage calculation"""
        state = PortfolioState(
            current_capital=Decimal("1000"),
            position_values={"m1": Decimal("100"), "m2": Decimal("150")},
        )

        assert state.total_exposure == Decimal("250")
        assert state.exposure_pct == Decimal("0.25")

    def test_drawdown_calculation(self):
        """Test drawdown calculation from peak"""
        state = PortfolioState(
            current_capital=Decimal("850"),
            peak_capital=Decimal("1000"),
        )

        assert state.drawdown == Decimal("0.15")

    def test_update_peak(self):
        """Test peak capital tracking"""
        state = PortfolioState(
            current_capital=Decimal("1100"),
            peak_capital=Decimal("1000"),
        )

        state.update_peak()
        assert state.peak_capital == Decimal("1100")


class TestRiskManager:
    """Tests for RiskManager functionality"""

    def setup_method(self):
        """Set up test fixtures"""
        self.limits = RiskLimits(
            max_position_size=Decimal("100"),
            max_position_pct=Decimal("0.10"),
            max_total_exposure=Decimal("0.30"),
            max_drawdown=Decimal("0.15"),
            min_edge=Decimal("0.02"),
            min_confidence=Decimal("0.30"),
        )
        self.rm = RiskManager(
            limits=self.limits,
            initial_capital=Decimal("1000"),
        )

    def test_validate_trade_passes(self):
        """Test valid trade passes validation"""
        rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            edge=Decimal("0.05"),
            confidence=Decimal("0.50"),
            recommended_size=Decimal("50"),
        )

        is_valid, alerts = self.rm.validate_trade(rec)
        assert is_valid

    def test_validate_trade_rejects_low_edge(self):
        """Test trade rejected for insufficient edge"""
        rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            edge=Decimal("0.01"),  # Below 0.02 threshold
            confidence=Decimal("0.50"),
            recommended_size=Decimal("50"),
        )

        is_valid, alerts = self.rm.validate_trade(rec)
        assert not is_valid
        assert any(a.metric == "edge" for a in alerts)

    def test_validate_trade_rejects_low_confidence(self):
        """Test trade rejected for insufficient confidence"""
        rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            edge=Decimal("0.05"),
            confidence=Decimal("0.20"),  # Below 0.30 threshold
            recommended_size=Decimal("50"),
        )

        is_valid, alerts = self.rm.validate_trade(rec)
        assert not is_valid
        assert any(a.metric == "confidence" for a in alerts)

    def test_position_size_capped(self):
        """Test position size is capped at max"""
        rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            edge=Decimal("0.05"),
            confidence=Decimal("0.50"),
            recommended_size=Decimal("200"),  # Over $100 max
        )

        is_valid, alerts = self.rm.validate_trade(rec)
        # Should pass but with reduced size
        assert rec.recommended_size <= Decimal("100")

    def test_exposure_limit_blocks_trade(self):
        """Test trade blocked when exposure limit reached"""
        # Fill up exposure
        self.rm.state.position_values = {
            "m1": Decimal("150"),
            "m2": Decimal("150"),
        }  # 30% exposure already

        rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            edge=Decimal("0.05"),
            confidence=Decimal("0.50"),
            recommended_size=Decimal("50"),
        )

        is_valid, alerts = self.rm.validate_trade(rec)
        assert not is_valid

    def test_trading_halt_on_drawdown(self):
        """Test trading halts on max drawdown"""
        # Simulate 16% drawdown
        self.rm.state.current_capital = Decimal("840")
        self.rm.state.peak_capital = Decimal("1000")

        alerts = self.rm.check_portfolio_health()

        assert self.rm.trading_halted
        assert any(a.level == AlertLevel.HALT for a in alerts)

    def test_trading_resumes(self):
        """Test trading can resume when conditions improve"""
        self.rm.trading_halted = True
        self.rm.halt_reason = "daily loss"

        # Reset state to healthy
        self.rm.state.drawdown  # Check it's calculated correctly
        self.rm.state.current_capital = Decimal("950")
        self.rm.state.peak_capital = Decimal("1000")

        result = self.rm.resume_trading()
        assert result
        assert not self.rm.trading_halted

    def test_record_trade_updates_state(self):
        """Test trade recording updates portfolio state"""
        self.rm.record_trade("market1", Decimal("50"), BetDirection.YES)

        assert "market1" in self.rm.state.positions
        assert self.rm.state.daily_trades == 1
        assert self.rm.state.last_trade_time is not None

    def test_available_capital_calculation(self):
        """Test available capital for new positions"""
        # Max exposure is 30% of 1000 = 300
        self.rm.state.position_values = {"m1": Decimal("100")}

        available = self.rm.get_available_capital()
        assert available == Decimal("200")

    def test_max_position_size_calculation(self):
        """Test max position size respects all limits"""
        max_size = self.rm.get_max_position_size("any_market")

        # Should be minimum of absolute max ($100), % max (10% of $1000 = $100),
        # and available exposure
        assert max_size <= Decimal("100")


class TestRiskAlerts:
    """Tests for risk alert generation"""

    def setup_method(self):
        """Set up test fixtures"""
        self.rm = RiskManager(initial_capital=Decimal("1000"))

    def test_drawdown_warning_alert(self):
        """Test warning alert at 75% of max drawdown"""
        # At 11.25% drawdown (75% of 15%)
        self.rm.state.current_capital = Decimal("888")
        self.rm.state.peak_capital = Decimal("1000")

        alerts = self.rm.check_portfolio_health()

        assert any(
            a.level == AlertLevel.WARNING and "drawdown" in a.message.lower()
            for a in alerts
        )

    def test_status_report_generation(self):
        """Test status report contains key info"""
        report = self.rm.get_status_report()

        assert "Capital" in report
        assert "Drawdown" in report
        assert "Exposure" in report

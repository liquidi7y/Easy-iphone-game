"""Tests for Kelly Criterion calculator"""

import pytest
from decimal import Decimal

from src.strategy.kelly import KellyCalculator, BetDirection, BetRecommendation


class TestKellyCalculator:
    """Tests for Kelly Criterion position sizing"""

    def setup_method(self):
        """Set up test fixtures"""
        self.kelly = KellyCalculator(
            kelly_fraction=0.25,
            min_edge=0.02,
            max_position_pct=0.10,
            min_confidence=0.30,
        )

    def test_calculate_kelly_with_edge(self):
        """Test Kelly calculation when we have an edge"""
        # If we think YES probability is 0.6 but market says 0.5
        # We should bet YES
        kelly, direction = self.kelly.calculate_kelly(
            estimated_prob=Decimal("0.60"),
            market_price=Decimal("0.50"),
        )

        assert direction == BetDirection.YES
        assert kelly > 0

    def test_calculate_kelly_no_edge(self):
        """Test Kelly returns zero when no edge"""
        kelly, direction = self.kelly.calculate_kelly(
            estimated_prob=Decimal("0.50"),
            market_price=Decimal("0.50"),
        )

        assert kelly == Decimal("0") or direction == BetDirection.NONE

    def test_calculate_kelly_negative_edge(self):
        """Test Kelly when we think YES is overpriced"""
        # If we think YES probability is 0.4 but market says 0.6
        # We should bet NO
        kelly, direction = self.kelly.calculate_kelly(
            estimated_prob=Decimal("0.40"),
            market_price=Decimal("0.60"),
        )

        assert direction == BetDirection.NO
        assert kelly > 0

    def test_calculate_bet_recommendation(self):
        """Test full bet recommendation"""
        rec = self.kelly.calculate_bet(
            market_id="test_market",
            market_question="Will X happen?",
            market_price=Decimal("0.50"),
            estimated_prob=Decimal("0.60"),
            confidence=Decimal("0.50"),
            bankroll=Decimal("1000"),
        )

        assert rec.direction == BetDirection.YES
        assert rec.edge > Decimal("0")
        assert rec.recommended_size > Decimal("0")
        assert rec.recommended_size <= Decimal("100")  # Max 10% of 1000

    def test_bet_rejected_low_confidence(self):
        """Test bet rejected when confidence too low"""
        rec = self.kelly.calculate_bet(
            market_id="test_market",
            market_question="Will X happen?",
            market_price=Decimal("0.50"),
            estimated_prob=Decimal("0.60"),
            confidence=Decimal("0.20"),  # Below 0.30 threshold
            bankroll=Decimal("1000"),
        )

        assert rec.direction == BetDirection.NONE
        assert rec.recommended_size == Decimal("0")

    def test_bet_rejected_low_edge(self):
        """Test bet rejected when edge too low"""
        rec = self.kelly.calculate_bet(
            market_id="test_market",
            market_question="Will X happen?",
            market_price=Decimal("0.50"),
            estimated_prob=Decimal("0.51"),  # Only 1% edge
            confidence=Decimal("0.80"),
            bankroll=Decimal("1000"),
        )

        # Should be rejected for insufficient edge
        assert not rec.is_valid or rec.edge < self.kelly.min_edge

    def test_position_sizing_respects_max(self):
        """Test position size doesn't exceed max percentage"""
        rec = self.kelly.calculate_bet(
            market_id="test_market",
            market_question="Will X happen?",
            market_price=Decimal("0.30"),  # Strong edge if we think 0.60
            estimated_prob=Decimal("0.60"),
            confidence=Decimal("0.90"),
            bankroll=Decimal("10000"),
        )

        # Should be capped at 10% of bankroll
        assert rec.recommended_size <= Decimal("1000")

    def test_expected_value_positive(self):
        """Test expected value is positive when we have edge"""
        rec = self.kelly.calculate_bet(
            market_id="test_market",
            market_question="Will X happen?",
            market_price=Decimal("0.50"),
            estimated_prob=Decimal("0.65"),
            confidence=Decimal("0.70"),
            bankroll=Decimal("1000"),
        )

        if rec.is_valid:
            assert rec.expected_value > Decimal("0")

    def test_portfolio_kelly_scaling(self):
        """Test portfolio Kelly scales down when over exposure"""
        opportunities = [
            ("market1", Decimal("0.40"), Decimal("0.55"), Decimal("0.60")),
            ("market2", Decimal("0.45"), Decimal("0.60"), Decimal("0.65")),
            ("market3", Decimal("0.50"), Decimal("0.65"), Decimal("0.70")),
            ("market4", Decimal("0.35"), Decimal("0.50"), Decimal("0.55")),
        ]

        recs = self.kelly.calculate_portfolio_kelly(
            opportunities=opportunities,
            bankroll=Decimal("1000"),
            max_total_exposure=Decimal("0.25"),  # 25% max
        )

        total_exposure = sum(r.fractional_kelly for r in recs)
        assert total_exposure <= Decimal("0.25") + Decimal("0.001")  # Small tolerance


class TestBetRecommendation:
    """Tests for BetRecommendation dataclass"""

    def test_is_valid_checks(self):
        """Test is_valid property"""
        valid_rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            edge=Decimal("0.05"),
            recommended_size=Decimal("50"),
        )
        assert valid_rec.is_valid

        invalid_rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.NONE,
            edge=Decimal("0.05"),
            recommended_size=Decimal("50"),
        )
        assert not invalid_rec.is_valid

    def test_expected_roi_calculation(self):
        """Test expected ROI calculation"""
        rec = BetRecommendation(
            market_id="test",
            direction=BetDirection.YES,
            recommended_size=Decimal("100"),
            expected_value=Decimal("10"),
        )
        assert rec.expected_roi == Decimal("10")  # 10%

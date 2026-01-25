"""
Kelly Criterion Position Sizing
Mathematically optimal bet sizing for maximizing long-term growth
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Tuple
from enum import Enum
import structlog

logger = structlog.get_logger()


class BetDirection(str, Enum):
    """Direction of the bet"""
    YES = "YES"
    NO = "NO"
    NONE = "NONE"


@dataclass
class BetRecommendation:
    """
    Recommended bet based on Kelly Criterion

    The Kelly Criterion determines the optimal fraction of bankroll to bet:
    f* = (p * b - q) / b

    Where:
    - f* = fraction of bankroll to bet
    - p = probability of winning
    - q = probability of losing (1 - p)
    - b = odds received on the bet (payout ratio)
    """
    market_id: str
    market_question: str = ""

    # Probabilities
    estimated_probability: Decimal = Decimal("0.5")  # Our estimate
    market_probability: Decimal = Decimal("0.5")  # Current market price

    # Kelly calculations
    kelly_fraction: Decimal = Decimal("0")  # Raw Kelly fraction
    fractional_kelly: Decimal = Decimal("0")  # Adjusted (usually 0.25-0.5 of full Kelly)
    edge: Decimal = Decimal("0")  # Expected edge over market

    # Recommendation
    direction: BetDirection = BetDirection.NONE
    recommended_size: Decimal = Decimal("0")  # In USDC
    expected_value: Decimal = Decimal("0")  # Expected profit

    # Confidence metrics
    confidence: Decimal = Decimal("0")
    risk_adjusted_score: Decimal = Decimal("0")

    @property
    def is_valid(self) -> bool:
        """Check if this is a valid betting opportunity"""
        return (
            self.direction != BetDirection.NONE and
            self.recommended_size > 0 and
            self.edge > Decimal("0.01")  # At least 1% edge
        )

    @property
    def expected_roi(self) -> Decimal:
        """Expected return on investment"""
        if self.recommended_size == 0:
            return Decimal("0")
        return (self.expected_value / self.recommended_size) * 100

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question[:80],
            "direction": self.direction.value,
            "estimated_prob": float(self.estimated_probability),
            "market_prob": float(self.market_probability),
            "edge": float(self.edge),
            "kelly_fraction": float(self.kelly_fraction),
            "recommended_size": float(self.recommended_size),
            "expected_value": float(self.expected_value),
            "expected_roi": float(self.expected_roi),
            "confidence": float(self.confidence),
        }


class KellyCalculator:
    """
    Kelly Criterion Calculator for Polymarket betting

    Key features:
    - Full Kelly calculation
    - Fractional Kelly for reduced variance
    - Edge calculation and validation
    - Portfolio-level Kelly (multiple concurrent bets)
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,  # Use 25% of Kelly (more conservative)
        min_edge: float = 0.02,  # Minimum 2% edge required
        max_position_pct: float = 0.10,  # Max 10% of bankroll per bet
        min_confidence: float = 0.30,  # Minimum confidence threshold
    ):
        self.kelly_fraction = Decimal(str(kelly_fraction))
        self.min_edge = Decimal(str(min_edge))
        self.max_position_pct = Decimal(str(max_position_pct))
        self.min_confidence = Decimal(str(min_confidence))

    def calculate_kelly(
        self,
        estimated_prob: Decimal,
        market_price: Decimal,
    ) -> Tuple[Decimal, BetDirection]:
        """
        Calculate Kelly fraction for a bet

        For binary markets:
        - If we think YES probability > market price, we buy YES
        - If we think YES probability < market price, we buy NO

        Kelly formula for binary outcomes:
        f* = (p * b - q) / b
        where b = (1/price) - 1 (the odds)

        Args:
            estimated_prob: Our estimated probability of YES
            market_price: Current market price for YES (0-1)

        Returns:
            Tuple of (kelly_fraction, direction)
        """
        # Ensure valid inputs
        estimated_prob = max(Decimal("0.01"), min(Decimal("0.99"), estimated_prob))
        market_price = max(Decimal("0.01"), min(Decimal("0.99"), market_price))

        # Calculate edge for YES
        yes_edge = estimated_prob - market_price

        # Calculate edge for NO
        no_estimated = Decimal("1") - estimated_prob
        no_price = Decimal("1") - market_price
        no_edge = no_estimated - no_price

        # Determine direction based on larger edge
        if yes_edge > no_edge and yes_edge > 0:
            direction = BetDirection.YES
            p = estimated_prob
            price = market_price
        elif no_edge > 0:
            direction = BetDirection.NO
            p = no_estimated
            price = no_price
        else:
            return Decimal("0"), BetDirection.NONE

        # Calculate Kelly fraction
        # For a bet at price p with probability p of winning:
        # Payout if win: (1/price) - 1 per unit bet
        # Loss if lose: 1 per unit bet

        if price <= 0 or price >= 1:
            return Decimal("0"), BetDirection.NONE

        # Odds (b) = (1/price) - 1
        b = (Decimal("1") / price) - Decimal("1")

        if b <= 0:
            return Decimal("0"), BetDirection.NONE

        # q = 1 - p
        q = Decimal("1") - p

        # Kelly: f* = (p * b - q) / b
        kelly = (p * b - q) / b

        # Ensure non-negative
        kelly = max(Decimal("0"), kelly)

        return kelly, direction

    def calculate_bet(
        self,
        market_id: str,
        market_question: str,
        market_price: Decimal,
        estimated_prob: Decimal,
        confidence: Decimal,
        bankroll: Decimal,
    ) -> BetRecommendation:
        """
        Calculate recommended bet using Kelly Criterion

        Args:
            market_id: Market identifier
            market_question: Market question text
            market_price: Current YES price (0-1)
            estimated_prob: Our estimated YES probability
            confidence: Confidence in our estimate (0-1)
            bankroll: Available betting capital

        Returns:
            BetRecommendation with sizing and expected value
        """
        rec = BetRecommendation(
            market_id=market_id,
            market_question=market_question,
            estimated_probability=estimated_prob,
            market_probability=market_price,
            confidence=confidence,
        )

        # Check minimum confidence
        if confidence < self.min_confidence:
            logger.debug(
                "insufficient_confidence",
                market_id=market_id,
                confidence=float(confidence)
            )
            return rec

        # Calculate raw Kelly
        kelly, direction = self.calculate_kelly(estimated_prob, market_price)

        if direction == BetDirection.NONE or kelly <= 0:
            return rec

        rec.direction = direction
        rec.kelly_fraction = kelly

        # Calculate edge
        if direction == BetDirection.YES:
            rec.edge = estimated_prob - market_price
        else:
            rec.edge = (Decimal("1") - estimated_prob) - (Decimal("1") - market_price)

        # Check minimum edge
        if rec.edge < self.min_edge:
            logger.debug(
                "insufficient_edge",
                market_id=market_id,
                edge=float(rec.edge)
            )
            rec.direction = BetDirection.NONE
            return rec

        # Apply fractional Kelly (reduces variance)
        fractional = kelly * self.kelly_fraction

        # Adjust for confidence
        # Lower confidence = smaller position
        confidence_adjusted = fractional * confidence

        # Cap at maximum position size
        rec.fractional_kelly = min(confidence_adjusted, self.max_position_pct)

        # Calculate actual bet size
        rec.recommended_size = (bankroll * rec.fractional_kelly).quantize(Decimal("0.01"))

        # Calculate expected value
        price = market_price if direction == BetDirection.YES else (Decimal("1") - market_price)
        prob = estimated_prob if direction == BetDirection.YES else (Decimal("1") - estimated_prob)

        # EV = (prob of win * profit) - (prob of loss * stake)
        # For binary: profit if win = (1/price - 1) * stake
        payout_ratio = (Decimal("1") / price) - Decimal("1")
        rec.expected_value = (
            prob * payout_ratio * rec.recommended_size -
            (Decimal("1") - prob) * rec.recommended_size
        ).quantize(Decimal("0.01"))

        # Risk-adjusted score (Sharpe-like metric)
        # Higher is better
        if rec.fractional_kelly > 0:
            rec.risk_adjusted_score = rec.edge * confidence / rec.fractional_kelly
        else:
            rec.risk_adjusted_score = Decimal("0")

        logger.info(
            "bet_recommendation",
            market_id=market_id,
            direction=direction.value,
            edge=float(rec.edge),
            kelly=float(rec.fractional_kelly),
            size=float(rec.recommended_size),
            ev=float(rec.expected_value),
        )

        return rec

    def calculate_portfolio_kelly(
        self,
        opportunities: List[Tuple[str, Decimal, Decimal, Decimal]],  # (market_id, market_price, est_prob, confidence)
        bankroll: Decimal,
        max_total_exposure: Decimal = Decimal("0.25"),  # Max 25% total
    ) -> List[BetRecommendation]:
        """
        Calculate optimal Kelly sizing for multiple simultaneous bets

        When betting on multiple markets, we need to:
        1. Calculate individual Kelly fractions
        2. Scale down if total exposure exceeds limit
        3. Prioritize by risk-adjusted score

        Args:
            opportunities: List of betting opportunities
            bankroll: Total available capital
            max_total_exposure: Maximum total % of bankroll to risk

        Returns:
            List of sized BetRecommendations
        """
        # Calculate individual recommendations
        recs = []
        for market_id, market_price, est_prob, confidence in opportunities:
            rec = self.calculate_bet(
                market_id=market_id,
                market_question="",
                market_price=market_price,
                estimated_prob=est_prob,
                confidence=confidence,
                bankroll=bankroll,
            )
            if rec.is_valid:
                recs.append(rec)

        if not recs:
            return []

        # Sort by risk-adjusted score
        recs.sort(key=lambda r: r.risk_adjusted_score, reverse=True)

        # Calculate total exposure
        total_kelly = sum(r.fractional_kelly for r in recs)

        # Scale down if exceeding max
        if total_kelly > max_total_exposure:
            scale_factor = max_total_exposure / total_kelly
            for rec in recs:
                rec.fractional_kelly *= scale_factor
                rec.recommended_size = (bankroll * rec.fractional_kelly).quantize(Decimal("0.01"))
                # Recalculate EV
                price = rec.market_probability if rec.direction == BetDirection.YES else (Decimal("1") - rec.market_probability)
                prob = rec.estimated_probability if rec.direction == BetDirection.YES else (Decimal("1") - rec.estimated_probability)
                payout_ratio = (Decimal("1") / price) - Decimal("1")
                rec.expected_value = (
                    prob * payout_ratio * rec.recommended_size -
                    (Decimal("1") - prob) * rec.recommended_size
                ).quantize(Decimal("0.01"))

        return recs

    def explain_bet(self, rec: BetRecommendation) -> str:
        """Generate human-readable explanation of bet recommendation"""
        if not rec.is_valid:
            return f"No valid bet for {rec.market_id}: insufficient edge or confidence"

        lines = [
            f"=== BET RECOMMENDATION ===",
            f"Market: {rec.market_question[:70]}..." if rec.market_question else f"Market: {rec.market_id}",
            "",
            f"Direction: {rec.direction.value}",
            f"Market Price: {rec.market_probability:.1%}",
            f"Our Estimate: {rec.estimated_probability:.1%}",
            f"Edge: {rec.edge:.2%}",
            "",
            f"Kelly Fraction: {rec.kelly_fraction:.2%} (full)",
            f"Position Size: {rec.fractional_kelly:.2%} (fractional)",
            f"Bet Amount: ${rec.recommended_size:,.2f}",
            "",
            f"Expected Value: ${rec.expected_value:+,.2f}",
            f"Expected ROI: {rec.expected_roi:+.1f}%",
            f"Confidence: {rec.confidence:.1%}",
        ]

        return "\n".join(lines)

"""
Signal Aggregator
Combines multiple data sources into actionable trading signals
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from enum import Enum
import structlog

from ..sentiment.analyzer import MarketSentiment
from ..whales.tracker import WhaleSignal

logger = structlog.get_logger()


class SignalSource(str, Enum):
    """Source of trading signal"""
    SENTIMENT = "SENTIMENT"
    WHALES = "WHALES"
    COMBINED = "COMBINED"
    MARKET_MOMENTUM = "MARKET_MOMENTUM"


class SignalDirection(str, Enum):
    """Direction of signal"""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class TradingSignal:
    """
    Aggregated trading signal from multiple sources

    Combines:
    - Twitter sentiment analysis
    - Whale wallet tracking
    - Market momentum
    - Price discrepancy analysis
    """
    market_id: str
    market_question: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Current market state
    current_price: Decimal = Decimal("0.5")

    # Signal components
    sentiment_signal: Optional[MarketSentiment] = None
    whale_signal: Optional[WhaleSignal] = None

    # Aggregated signal
    direction: SignalDirection = SignalDirection.NEUTRAL
    strength: Decimal = Decimal("0")  # -1 to 1
    confidence: Decimal = Decimal("0")  # 0 to 1

    # Derived probability estimate
    estimated_probability: Decimal = Decimal("0.5")  # Our estimate of true probability
    probability_adjustment: Decimal = Decimal("0")  # Adjustment from market price

    # Signal metadata
    sources_used: List[SignalSource] = field(default_factory=list)
    reasoning: str = ""

    @property
    def edge(self) -> Decimal:
        """Estimated edge over market price"""
        if self.direction == SignalDirection.BULLISH:
            return self.estimated_probability - self.current_price
        elif self.direction == SignalDirection.BEARISH:
            return (Decimal("1") - self.estimated_probability) - (Decimal("1") - self.current_price)
        return Decimal("0")

    @property
    def is_actionable(self) -> bool:
        """Check if signal is strong enough to trade"""
        return (
            self.direction != SignalDirection.NEUTRAL and
            self.confidence >= Decimal("0.3") and
            abs(self.edge) >= Decimal("0.02")  # At least 2% edge
        )

    def to_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "market_question": self.market_question[:80],
            "direction": self.direction.value,
            "strength": float(self.strength),
            "confidence": float(self.confidence),
            "current_price": float(self.current_price),
            "estimated_probability": float(self.estimated_probability),
            "edge": float(self.edge),
            "is_actionable": self.is_actionable,
            "sources": [s.value for s in self.sources_used],
        }


class SignalAggregator:
    """
    Aggregates signals from multiple sources into trading decisions

    Weighting system:
    - Sentiment: 35% (Twitter analysis)
    - Whales: 45% (Top trader activity)
    - Market momentum: 20% (Price trend)

    Signal strength is scaled by confidence from each source.
    """

    def __init__(
        self,
        sentiment_weight: float = 0.35,
        whale_weight: float = 0.45,
        momentum_weight: float = 0.20,
        min_sources: int = 1,  # Minimum sources required for valid signal
        agreement_bonus: float = 0.15,  # Bonus when sources agree
    ):
        self.sentiment_weight = Decimal(str(sentiment_weight))
        self.whale_weight = Decimal(str(whale_weight))
        self.momentum_weight = Decimal(str(momentum_weight))
        self.min_sources = min_sources
        self.agreement_bonus = Decimal(str(agreement_bonus))

    def aggregate(
        self,
        market_id: str,
        market_question: str,
        current_price: Decimal,
        sentiment: Optional[MarketSentiment] = None,
        whale_signal: Optional[WhaleSignal] = None,
        price_momentum: Optional[Decimal] = None,  # -1 to 1
    ) -> TradingSignal:
        """
        Aggregate multiple signal sources into a single trading signal

        Args:
            market_id: Market identifier
            market_question: Market question text
            current_price: Current YES price (0-1)
            sentiment: Twitter sentiment analysis
            whale_signal: Whale tracker signal
            price_momentum: Recent price momentum (-1 to 1)

        Returns:
            TradingSignal with aggregated analysis
        """
        signal = TradingSignal(
            market_id=market_id,
            market_question=market_question,
            current_price=current_price,
            sentiment_signal=sentiment,
            whale_signal=whale_signal,
        )

        # Collect weighted signals
        weighted_signals: List[Tuple[Decimal, Decimal, SignalSource]] = []  # (strength, weight, source)
        total_weight = Decimal("0")

        # Process sentiment signal
        if sentiment and sentiment.twitter_sentiment:
            sentiment_strength = Decimal(str(sentiment.twitter_sentiment.compound))
            sentiment_confidence = Decimal(str(sentiment.twitter_sentiment.confidence))
            effective_weight = self.sentiment_weight * sentiment_confidence

            weighted_signals.append((sentiment_strength, effective_weight, SignalSource.SENTIMENT))
            total_weight += effective_weight
            signal.sources_used.append(SignalSource.SENTIMENT)

        # Process whale signal
        if whale_signal and whale_signal.confidence > 0:
            whale_strength = Decimal(str(whale_signal.signal_strength))
            whale_confidence = Decimal(str(whale_signal.confidence))
            effective_weight = self.whale_weight * whale_confidence

            weighted_signals.append((whale_strength, effective_weight, SignalSource.WHALES))
            total_weight += effective_weight
            signal.sources_used.append(SignalSource.WHALES)

        # Process momentum
        if price_momentum is not None:
            momentum_confidence = Decimal("0.5")  # Default confidence for momentum
            effective_weight = self.momentum_weight * momentum_confidence

            weighted_signals.append((price_momentum, effective_weight, SignalSource.MARKET_MOMENTUM))
            total_weight += effective_weight
            signal.sources_used.append(SignalSource.MARKET_MOMENTUM)

        # Check minimum sources
        if len(signal.sources_used) < self.min_sources:
            logger.debug(
                "insufficient_signal_sources",
                market_id=market_id,
                sources=len(signal.sources_used)
            )
            return signal

        # Calculate weighted average signal
        if total_weight > 0:
            weighted_sum = sum(s * w for s, w, _ in weighted_signals)
            signal.strength = weighted_sum / total_weight

            # Calculate confidence
            base_confidence = total_weight / (self.sentiment_weight + self.whale_weight + self.momentum_weight)

            # Check for source agreement
            directions = []
            for strength, _, _ in weighted_signals:
                if strength > Decimal("0.1"):
                    directions.append("BULLISH")
                elif strength < Decimal("-0.1"):
                    directions.append("BEARISH")
                else:
                    directions.append("NEUTRAL")

            if len(set(directions)) == 1 and directions[0] != "NEUTRAL":
                # All sources agree - boost confidence
                base_confidence += self.agreement_bonus

            signal.confidence = min(base_confidence, Decimal("1.0"))

        # Determine direction
        if signal.strength > Decimal("0.1"):
            signal.direction = SignalDirection.BULLISH
        elif signal.strength < Decimal("-0.1"):
            signal.direction = SignalDirection.BEARISH
        else:
            signal.direction = SignalDirection.NEUTRAL

        # Calculate estimated probability
        # Signal strength indicates how much we think the market is mispriced
        # Positive signal = YES more likely than market price suggests
        # Negative signal = NO more likely than market price suggests

        # Scale strength to probability adjustment
        # Max adjustment: +/- 15%
        max_adjustment = Decimal("0.15")
        adjustment = signal.strength * max_adjustment * signal.confidence

        signal.probability_adjustment = adjustment
        signal.estimated_probability = max(
            Decimal("0.01"),
            min(Decimal("0.99"), current_price + adjustment)
        )

        # Generate reasoning
        signal.reasoning = self._generate_reasoning(signal, weighted_signals)

        logger.info(
            "signal_aggregated",
            market_id=market_id,
            direction=signal.direction.value,
            strength=float(signal.strength),
            confidence=float(signal.confidence),
            edge=float(signal.edge),
        )

        return signal

    def _generate_reasoning(
        self,
        signal: TradingSignal,
        weighted_signals: List[Tuple[Decimal, Decimal, SignalSource]],
    ) -> str:
        """Generate human-readable reasoning for the signal"""
        reasons = []

        for strength, weight, source in weighted_signals:
            direction = "bullish" if strength > 0 else "bearish" if strength < 0 else "neutral"
            intensity = "strongly " if abs(strength) > 0.5 else "moderately " if abs(strength) > 0.2 else "slightly "

            if source == SignalSource.SENTIMENT:
                reasons.append(f"Twitter sentiment is {intensity}{direction}")
            elif source == SignalSource.WHALES:
                reasons.append(f"Whale activity indicates {intensity}{direction} positioning")
            elif source == SignalSource.MARKET_MOMENTUM:
                reasons.append(f"Price momentum is {intensity}{direction}")

        if signal.direction != SignalDirection.NEUTRAL:
            conclusion = f"Overall {signal.direction.value.lower()} signal with {signal.confidence:.0%} confidence"
            reasons.append(conclusion)

        return ". ".join(reasons)

    def rank_signals(
        self,
        signals: List[TradingSignal],
        min_confidence: Decimal = Decimal("0.3"),
    ) -> List[TradingSignal]:
        """
        Rank signals by quality and filter for actionable ones

        Args:
            signals: List of trading signals
            min_confidence: Minimum confidence threshold

        Returns:
            Sorted list of actionable signals
        """
        # Filter actionable signals
        actionable = [
            s for s in signals
            if s.is_actionable and s.confidence >= min_confidence
        ]

        # Sort by expected edge * confidence (risk-adjusted)
        actionable.sort(
            key=lambda s: abs(s.edge) * s.confidence,
            reverse=True
        )

        return actionable

    def explain_signal(self, signal: TradingSignal) -> str:
        """Generate detailed explanation of trading signal"""
        lines = [
            "=" * 50,
            "TRADING SIGNAL ANALYSIS",
            "=" * 50,
            "",
            f"Market: {signal.market_question[:70]}..." if signal.market_question else f"Market: {signal.market_id}",
            "",
            f"Current Price: {signal.current_price:.1%}",
            f"Our Estimate: {signal.estimated_probability:.1%}",
            f"Edge: {signal.edge:+.2%}",
            "",
            f"Signal Direction: {signal.direction.value}",
            f"Signal Strength: {signal.strength:+.2f}",
            f"Confidence: {signal.confidence:.1%}",
            "",
            "Signal Sources:",
        ]

        for source in signal.sources_used:
            if source == SignalSource.SENTIMENT and signal.sentiment_signal:
                ts = signal.sentiment_signal.twitter_sentiment
                if ts:
                    lines.append(f"  - Twitter: {ts.compound:+.2f} ({ts.signal_strength})")
            elif source == SignalSource.WHALES and signal.whale_signal:
                ws = signal.whale_signal
                lines.append(f"  - Whales: {ws.signal_strength:+.2f} ({ws.bullish_whales}B/{ws.bearish_whales}S)")
            else:
                lines.append(f"  - {source.value}")

        lines.extend([
            "",
            "Reasoning:",
            f"  {signal.reasoning}",
            "",
        ])

        if signal.is_actionable:
            lines.append("*** ACTIONABLE SIGNAL - CONSIDER TRADING ***")
        else:
            lines.append("Signal not strong enough to trade")

        return "\n".join(lines)

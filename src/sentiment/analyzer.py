"""
Sentiment Engine - Combines multiple sentiment sources for market analysis
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import structlog

from .twitter import TwitterSentimentAnalyzer, SentimentResult

logger = structlog.get_logger()


@dataclass
class MarketSentiment:
    """
    Aggregated sentiment analysis for a market

    Combines:
    - Twitter sentiment
    - Sentiment trend direction
    - News sentiment (future)
    - Social volume metrics
    """
    market_id: str
    market_question: str
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Twitter analysis
    twitter_sentiment: Optional[SentimentResult] = None
    twitter_volume: int = 0  # Number of tweets analyzed

    # Derived signals
    sentiment_direction: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    sentiment_momentum: float = 0.0  # Change in sentiment over time
    signal_confidence: float = 0.0  # Overall confidence in signal

    # Predicted probability adjustment
    probability_adjustment: float = 0.0  # Suggested adjustment to market price

    @property
    def overall_score(self) -> float:
        """Get weighted overall sentiment score (-1 to 1)"""
        if self.twitter_sentiment is None:
            return 0.0

        # Currently just Twitter, but structured for multiple sources
        return self.twitter_sentiment.weighted_score

    @property
    def is_actionable(self) -> bool:
        """Check if sentiment signal is strong enough to act on"""
        return (
            self.signal_confidence >= 0.3 and
            abs(self.probability_adjustment) >= 0.02  # At least 2% edge
        )

    def to_dict(self) -> Dict:
        """Convert to dictionary for logging/storage"""
        return {
            "market_id": self.market_id,
            "market_question": self.market_question[:100],
            "timestamp": self.timestamp.isoformat(),
            "sentiment_direction": self.sentiment_direction,
            "sentiment_score": self.overall_score,
            "signal_confidence": self.signal_confidence,
            "probability_adjustment": self.probability_adjustment,
            "is_actionable": self.is_actionable,
        }


class SentimentEngine:
    """
    Main sentiment analysis engine

    Coordinates multiple sentiment sources and produces
    actionable trading signals.
    """

    def __init__(
        self,
        twitter_analyzer: Optional[TwitterSentimentAnalyzer] = None,
    ):
        self.twitter = twitter_analyzer

        # Calibration parameters (tune based on backtesting)
        self.sentiment_to_prob_factor = 0.15  # How much sentiment affects probability
        self.min_confidence_threshold = 0.25
        self.momentum_lookback_hours = 24

        # Cache for recent analyses
        self._cache: Dict[str, Tuple[datetime, MarketSentiment]] = {}
        self._cache_ttl_seconds = 600  # 10 minutes

    async def analyze_market(
        self,
        market_id: str,
        market_question: str,
        current_price: Decimal,
        custom_keywords: Optional[List[str]] = None,
    ) -> MarketSentiment:
        """
        Perform comprehensive sentiment analysis for a market

        Args:
            market_id: Polymarket market ID
            market_question: The market question text
            current_price: Current YES price (0-1)
            custom_keywords: Additional search keywords

        Returns:
            MarketSentiment with analysis results
        """
        # Check cache
        cache_key = market_id
        if cache_key in self._cache:
            cached_time, cached_result = self._cache[cache_key]
            age = (datetime.utcnow() - cached_time).total_seconds()
            if age < self._cache_ttl_seconds:
                logger.debug("sentiment_cache_hit", market_id=market_id)
                return cached_result

        logger.info("analyzing_market_sentiment", market_id=market_id)

        result = MarketSentiment(
            market_id=market_id,
            market_question=market_question,
        )

        # Analyze Twitter sentiment
        if self.twitter:
            try:
                twitter_result = await self.twitter.analyze_market_sentiment(
                    market_question,
                    custom_keywords=custom_keywords,
                    hours_back=24,
                )
                result.twitter_sentiment = twitter_result

                # Get trend for momentum
                trend = await self.twitter.get_sentiment_trend(
                    market_question,
                    intervals=4,
                    hours_per_interval=6,
                )

                if len(trend) >= 2:
                    recent = trend[-1][1].compound
                    older = trend[0][1].compound
                    result.sentiment_momentum = recent - older

            except Exception as e:
                logger.error("twitter_analysis_error", error=str(e))

        # Calculate derived signals
        result = self._calculate_signals(result, current_price)

        # Cache result
        self._cache[cache_key] = (datetime.utcnow(), result)

        return result

    def _calculate_signals(
        self,
        sentiment: MarketSentiment,
        current_price: Decimal,
    ) -> MarketSentiment:
        """Calculate derived trading signals from raw sentiment"""

        # Determine direction
        score = sentiment.overall_score
        if score > 0.1:
            sentiment.sentiment_direction = "BULLISH"
        elif score < -0.1:
            sentiment.sentiment_direction = "BEARISH"
        else:
            sentiment.sentiment_direction = "NEUTRAL"

        # Calculate confidence
        confidence_factors = []

        if sentiment.twitter_sentiment:
            confidence_factors.append(sentiment.twitter_sentiment.confidence)

        # Momentum agreement boosts confidence
        if sentiment.sentiment_momentum != 0:
            momentum_agrees = (
                (score > 0 and sentiment.sentiment_momentum > 0) or
                (score < 0 and sentiment.sentiment_momentum < 0)
            )
            if momentum_agrees:
                confidence_factors.append(0.8)
            else:
                confidence_factors.append(0.4)

        sentiment.signal_confidence = (
            sum(confidence_factors) / len(confidence_factors)
            if confidence_factors else 0.0
        )

        # Calculate probability adjustment
        # Sentiment suggests the "true" probability differs from market price
        if sentiment.signal_confidence >= self.min_confidence_threshold:
            # Positive sentiment = higher true probability
            # Scale by confidence and factor
            raw_adjustment = score * self.sentiment_to_prob_factor * sentiment.signal_confidence

            # Bound the adjustment
            sentiment.probability_adjustment = max(-0.15, min(0.15, raw_adjustment))
        else:
            sentiment.probability_adjustment = 0.0

        return sentiment

    async def batch_analyze(
        self,
        markets: List[Tuple[str, str, Decimal]],  # (id, question, price)
    ) -> List[MarketSentiment]:
        """
        Analyze multiple markets

        Args:
            markets: List of (market_id, question, current_price) tuples

        Returns:
            List of MarketSentiment results
        """
        results = []

        for market_id, question, price in markets:
            try:
                result = await self.analyze_market(market_id, question, price)
                results.append(result)
            except Exception as e:
                logger.error(
                    "batch_analyze_error",
                    market_id=market_id,
                    error=str(e)
                )

        return results

    def get_actionable_signals(
        self,
        sentiments: List[MarketSentiment],
        min_adjustment: float = 0.03,
    ) -> List[MarketSentiment]:
        """
        Filter for actionable trading signals

        Args:
            sentiments: List of sentiment analyses
            min_adjustment: Minimum probability adjustment to consider

        Returns:
            Filtered list of actionable signals
        """
        actionable = []

        for s in sentiments:
            if s.is_actionable and abs(s.probability_adjustment) >= min_adjustment:
                actionable.append(s)

        # Sort by strength of signal
        actionable.sort(
            key=lambda x: abs(x.probability_adjustment) * x.signal_confidence,
            reverse=True
        )

        return actionable

    def explain_signal(self, sentiment: MarketSentiment) -> str:
        """Generate human-readable explanation of sentiment signal"""
        lines = [
            f"Market: {sentiment.market_question[:80]}...",
            f"Direction: {sentiment.sentiment_direction}",
            f"Overall Score: {sentiment.overall_score:.3f}",
            f"Confidence: {sentiment.signal_confidence:.1%}",
        ]

        if sentiment.twitter_sentiment:
            ts = sentiment.twitter_sentiment
            lines.extend([
                f"Twitter Compound: {ts.compound:.3f} ({ts.signal_strength})",
                f"Twitter Confidence: {ts.confidence:.1%}",
            ])

        if sentiment.sentiment_momentum != 0:
            direction = "improving" if sentiment.sentiment_momentum > 0 else "declining"
            lines.append(f"Momentum: {direction} ({sentiment.sentiment_momentum:+.3f})")

        if sentiment.probability_adjustment != 0:
            lines.append(
                f"Suggested Adjustment: {sentiment.probability_adjustment:+.1%} "
                f"({'BUY YES' if sentiment.probability_adjustment > 0 else 'BUY NO'})"
            )

        return "\n".join(lines)

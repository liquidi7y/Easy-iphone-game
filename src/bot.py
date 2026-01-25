"""
Polymarket Sentinel Bot
Main orchestrator that coordinates all components
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
import structlog

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import get_settings
from .api import PolymarketClient, Market
from .sentiment import TwitterSentimentAnalyzer, SentimentEngine
from .whales import WhaleTracker
from .strategy import KellyCalculator, SignalAggregator, TradingSignal, BetRecommendation
from .risk import RiskManager, RiskLimits
from .utils import setup_logging, NotificationService
from .utils.logging import TradeLogger

logger = structlog.get_logger()


@dataclass
class BotConfig:
    """Bot configuration"""
    poll_interval: int = 60  # seconds
    dry_run: bool = True
    manual_approval_threshold: Decimal = Decimal("50")
    max_markets_to_analyze: int = 20
    min_market_volume: Decimal = Decimal("10000")  # $10k minimum
    auto_start: bool = False


class PolymarketSentinelBot:
    """
    Main bot orchestrator

    Coordinates:
    - Market discovery and filtering
    - Sentiment analysis
    - Whale tracking
    - Signal generation
    - Risk management
    - Trade execution
    """

    def __init__(self, config: Optional[BotConfig] = None):
        self.config = config or BotConfig()
        self.settings = get_settings()

        # Components (initialized in start())
        self.polymarket: Optional[PolymarketClient] = None
        self.twitter: Optional[TwitterSentimentAnalyzer] = None
        self.sentiment_engine: Optional[SentimentEngine] = None
        self.whale_tracker: Optional[WhaleTracker] = None
        self.signal_aggregator: Optional[SignalAggregator] = None
        self.kelly: Optional[KellyCalculator] = None
        self.risk_manager: Optional[RiskManager] = None
        self.notifications: Optional[NotificationService] = None
        self.trade_logger: Optional[TradeLogger] = None

        # Scheduler for periodic tasks
        self.scheduler: Optional[AsyncIOScheduler] = None

        # State
        self.is_running: bool = False
        self.markets_cache: Dict[str, Market] = {}
        self.signals_cache: Dict[str, TradingSignal] = {}
        self.pending_trades: List[BetRecommendation] = []

        # Stats
        self.stats = {
            "signals_generated": 0,
            "trades_executed": 0,
            "trades_rejected": 0,
            "total_volume": Decimal("0"),
        }

    async def initialize(self):
        """Initialize all components"""
        logger.info("initializing_bot")

        # Setup logging
        setup_logging(
            level=self.settings.bot.log_level,
            json_output=False,
        )

        # Initialize Polymarket client
        self.polymarket = PolymarketClient(
            api_key=self.settings.polymarket.api_key,
            api_secret=self.settings.polymarket.api_secret.get_secret_value(),
            api_passphrase=self.settings.polymarket.api_passphrase.get_secret_value(),
            private_key=self.settings.wallet.private_key.get_secret_value(),
            wallet_address=self.settings.wallet.address,
        )
        await self.polymarket.connect()

        # Initialize Twitter analyzer
        twitter_token = self.settings.twitter.bearer_token.get_secret_value()
        if twitter_token:
            self.twitter = TwitterSentimentAnalyzer(
                bearer_token=twitter_token,
                api_key=self.settings.twitter.api_key,
                api_secret=self.settings.twitter.api_secret.get_secret_value(),
            )
            await self.twitter.connect()
            self.sentiment_engine = SentimentEngine(twitter_analyzer=self.twitter)
        else:
            logger.warning("twitter_not_configured")

        # Initialize whale tracker
        self.whale_tracker = WhaleTracker(
            rpc_url=self.settings.blockchain.polygon_rpc_url,
            backup_rpc_url=self.settings.blockchain.polygon_rpc_backup,
        )
        await self.whale_tracker.connect()

        # Initialize strategy components
        self.signal_aggregator = SignalAggregator()
        self.kelly = KellyCalculator(
            kelly_fraction=0.25,
            min_edge=float(self.settings.risk.min_edge_threshold),
            max_position_pct=float(self.settings.risk.max_bet_percent),
        )

        # Initialize risk manager
        risk_limits = RiskLimits(
            max_position_size=self.settings.risk.max_capital_usdc * self.settings.risk.max_bet_percent,
            max_position_pct=self.settings.risk.max_bet_percent,
            max_total_exposure=self.settings.risk.max_exposure_percent,
            max_drawdown=self.settings.risk.max_drawdown_percent,
            min_edge=self.settings.risk.min_edge_threshold,
        )
        self.risk_manager = RiskManager(
            limits=risk_limits,
            initial_capital=self.settings.risk.max_capital_usdc,
        )

        # Initialize notifications
        self.notifications = NotificationService(
            telegram_token=self.settings.notifications.telegram_bot_token,
            telegram_chat_id=self.settings.notifications.telegram_chat_id,
            discord_webhook=self.settings.notifications.discord_webhook_url,
        )

        # Initialize trade logger
        self.trade_logger = TradeLogger()

        # Initialize scheduler
        self.scheduler = AsyncIOScheduler()

        logger.info("bot_initialized")

    async def start(self):
        """Start the bot"""
        if self.is_running:
            logger.warning("bot_already_running")
            return

        await self.initialize()

        self.is_running = True
        logger.info("bot_starting", dry_run=self.config.dry_run)

        # Schedule periodic tasks
        self.scheduler.add_job(
            self._analysis_loop,
            IntervalTrigger(seconds=self.config.poll_interval),
            id="analysis_loop",
            replace_existing=True,
        )

        self.scheduler.add_job(
            self._daily_reset,
            IntervalTrigger(hours=24),
            id="daily_reset",
            replace_existing=True,
        )

        self.scheduler.start()

        # Run initial analysis
        await self._analysis_loop()

        # Send startup notification
        if self.notifications:
            await self.notifications.send(
                f"Bot started in {'DRY RUN' if self.config.dry_run else 'LIVE'} mode\n"
                f"Capital: ${self.settings.risk.max_capital_usdc:,.2f}",
                level="INFO",
                title="Polymarket Sentinel Started"
            )

    async def stop(self):
        """Stop the bot"""
        logger.info("bot_stopping")
        self.is_running = False

        if self.scheduler:
            self.scheduler.shutdown()

        # Close connections
        if self.polymarket:
            await self.polymarket.close()
        if self.twitter:
            await self.twitter.close()
        if self.whale_tracker:
            await self.whale_tracker.close()

        logger.info("bot_stopped")

    async def _analysis_loop(self):
        """Main analysis loop - runs periodically"""
        if not self.is_running:
            return

        try:
            logger.info("analysis_loop_starting")

            # 1. Discover and filter markets
            markets = await self._discover_markets()

            # 2. Analyze each market
            signals = []
            for market in markets[:self.config.max_markets_to_analyze]:
                try:
                    signal = await self._analyze_market(market)
                    if signal and signal.is_actionable:
                        signals.append(signal)
                        self.signals_cache[market.condition_id] = signal
                except Exception as e:
                    logger.error("market_analysis_error", market=market.condition_id, error=str(e))

            # 3. Generate trade recommendations
            recommendations = await self._generate_recommendations(signals)

            # 4. Execute or queue trades
            for rec in recommendations:
                await self._process_recommendation(rec)

            logger.info(
                "analysis_loop_complete",
                markets_analyzed=len(markets),
                signals_found=len(signals),
                recommendations=len(recommendations),
            )

        except Exception as e:
            logger.error("analysis_loop_error", error=str(e))

    async def _discover_markets(self) -> List[Market]:
        """Discover and filter tradeable markets"""
        if not self.polymarket:
            return []

        # Get active markets
        all_markets = await self.polymarket.get_markets(active_only=True, limit=100)

        # Filter by volume and liquidity
        filtered = [
            m for m in all_markets
            if m.volume_24h >= self.config.min_market_volume
            and m.liquidity >= self.risk_manager.limits.min_market_liquidity if self.risk_manager else True
        ]

        # Sort by volume (most active first)
        filtered.sort(key=lambda m: m.volume_24h, reverse=True)

        # Cache markets
        for m in filtered:
            self.markets_cache[m.condition_id] = m

        logger.info("markets_discovered", total=len(all_markets), filtered=len(filtered))

        return filtered

    async def _analyze_market(self, market: Market) -> Optional[TradingSignal]:
        """Analyze a single market for trading opportunity"""
        logger.debug("analyzing_market", market=market.condition_id)

        # Get sentiment analysis
        sentiment = None
        if self.sentiment_engine:
            sentiment = await self.sentiment_engine.analyze_market(
                market_id=market.condition_id,
                market_question=market.question,
                current_price=market.yes_price,
            )

        # Get whale signal
        whale_signal = None
        if self.whale_tracker:
            whale_signal = await self.whale_tracker.analyze_market(
                market_id=market.condition_id,
                market_question=market.question,
            )

        # Aggregate signals
        if self.signal_aggregator:
            signal = self.signal_aggregator.aggregate(
                market_id=market.condition_id,
                market_question=market.question,
                current_price=market.yes_price,
                sentiment=sentiment,
                whale_signal=whale_signal,
            )

            if signal.is_actionable:
                self.stats["signals_generated"] += 1
                if self.trade_logger:
                    self.trade_logger.log_signal(
                        market.condition_id,
                        signal.direction.value,
                        float(signal.confidence),
                        float(signal.edge),
                    )

            return signal

        return None

    async def _generate_recommendations(
        self,
        signals: List[TradingSignal],
    ) -> List[BetRecommendation]:
        """Generate bet recommendations from signals"""
        if not self.kelly or not self.risk_manager:
            return []

        recommendations = []
        bankroll = self.risk_manager.state.current_capital

        for signal in signals:
            rec = self.kelly.calculate_bet(
                market_id=signal.market_id,
                market_question=signal.market_question,
                market_price=signal.current_price,
                estimated_prob=signal.estimated_probability,
                confidence=signal.confidence,
                bankroll=bankroll,
            )

            if rec.is_valid:
                recommendations.append(rec)

        # Sort by expected value
        recommendations.sort(key=lambda r: r.expected_value, reverse=True)

        return recommendations

    async def _process_recommendation(self, rec: BetRecommendation):
        """Process a trade recommendation"""
        if not self.risk_manager or not self.polymarket:
            return

        # Validate against risk limits
        market = self.markets_cache.get(rec.market_id)
        liquidity = market.liquidity if market else Decimal("0")

        is_valid, alerts = self.risk_manager.validate_trade(rec, liquidity)

        # Log alerts
        for alert in alerts:
            logger.info("risk_alert", level=alert.level.value, message=alert.message)
            if self.notifications and alert.level.value in ("WARNING", "CRITICAL", "HALT"):
                await self.notifications.send_risk_alert(
                    alert.level.value,
                    alert.message,
                    alert.metric,
                    float(alert.value) if alert.value else None,
                    float(alert.threshold) if alert.threshold else None,
                )

        if not is_valid:
            self.stats["trades_rejected"] += 1
            if self.trade_logger:
                self.trade_logger.log_rejection(
                    rec.market_id,
                    alerts[0].message if alerts else "Risk validation failed"
                )
            return

        # Check if manual approval required
        if rec.recommended_size > self.config.manual_approval_threshold and not self.config.dry_run:
            self.pending_trades.append(rec)
            logger.info(
                "trade_pending_approval",
                market=rec.market_id,
                size=float(rec.recommended_size),
            )
            if self.notifications:
                await self.notifications.send(
                    f"Trade requires approval:\n{self.kelly.explain_bet(rec)}" if self.kelly else "Trade requires approval",
                    level="WARNING",
                    title="Manual Approval Required"
                )
            return

        # Execute trade
        await self._execute_trade(rec)

    async def _execute_trade(self, rec: BetRecommendation):
        """Execute a trade"""
        if not self.polymarket or not self.risk_manager:
            return

        market = self.markets_cache.get(rec.market_id)
        if not market:
            logger.warning("market_not_found", market_id=rec.market_id)
            return

        # Get token ID based on direction
        token_id = market.yes_token_id if rec.direction.value == "YES" else market.no_token_id
        if not token_id:
            logger.warning("token_not_found", market_id=rec.market_id, direction=rec.direction.value)
            return

        # Determine price
        price = rec.market_probability if rec.direction.value == "YES" else (Decimal("1") - rec.market_probability)

        # Calculate shares
        shares = rec.recommended_size / price

        logger.info(
            "executing_trade",
            market=rec.market_id,
            direction=rec.direction.value,
            size=float(rec.recommended_size),
            price=float(price),
            shares=float(shares),
            dry_run=self.config.dry_run,
        )

        # Place order
        from .api.models import Side
        side = Side.BUY

        order = await self.polymarket.place_order(
            token_id=token_id,
            side=side,
            price=price,
            size=shares,
            dry_run=self.config.dry_run,
        )

        if order:
            self.stats["trades_executed"] += 1
            self.stats["total_volume"] += rec.recommended_size

            # Record trade in risk manager
            from .strategy.kelly import BetDirection
            self.risk_manager.record_trade(
                rec.market_id,
                rec.recommended_size,
                BetDirection(rec.direction.value),
            )

            # Log trade
            if self.trade_logger:
                self.trade_logger.log_trade(
                    rec.market_id,
                    rec.direction.value,
                    float(rec.recommended_size),
                    float(price),
                    dry_run=self.config.dry_run,
                )

            # Send notification
            if self.notifications:
                await self.notifications.send_trade_alert(
                    market=rec.market_question or rec.market_id,
                    direction=rec.direction.value,
                    size=float(rec.recommended_size),
                    price=float(price),
                    expected_ev=float(rec.expected_value),
                    dry_run=self.config.dry_run,
                )

            logger.info("trade_executed", order_id=order.order_id)

    async def _daily_reset(self):
        """Daily reset of counters"""
        if self.risk_manager:
            self.risk_manager.reset_daily_counters()
        logger.info("daily_counters_reset")

    # ==================== Manual Controls ====================

    async def approve_trade(self, market_id: str) -> bool:
        """Manually approve a pending trade"""
        for i, rec in enumerate(self.pending_trades):
            if rec.market_id == market_id:
                await self._execute_trade(rec)
                self.pending_trades.pop(i)
                return True
        return False

    async def reject_trade(self, market_id: str) -> bool:
        """Reject a pending trade"""
        for i, rec in enumerate(self.pending_trades):
            if rec.market_id == market_id:
                self.pending_trades.pop(i)
                logger.info("trade_rejected_manual", market=market_id)
                return True
        return False

    def get_status(self) -> Dict:
        """Get current bot status"""
        return {
            "is_running": self.is_running,
            "dry_run": self.config.dry_run,
            "stats": {k: float(v) if isinstance(v, Decimal) else v for k, v in self.stats.items()},
            "risk_status": self.risk_manager.get_status_report() if self.risk_manager else "N/A",
            "pending_trades": len(self.pending_trades),
            "cached_markets": len(self.markets_cache),
            "cached_signals": len(self.signals_cache),
        }

    async def analyze_market_manual(self, market_id: str) -> Optional[Dict]:
        """Manually analyze a specific market"""
        if not self.polymarket:
            return None

        market = await self.polymarket.get_market(market_id)
        if not market:
            return None

        signal = await self._analyze_market(market)
        if not signal:
            return None

        return {
            "market": {
                "id": market.condition_id,
                "question": market.question,
                "yes_price": float(market.yes_price),
                "volume_24h": float(market.volume_24h),
                "liquidity": float(market.liquidity),
            },
            "signal": signal.to_dict(),
            "recommendation": None,  # Would need to generate
        }

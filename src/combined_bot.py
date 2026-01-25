"""
Combined Polymarket Trading Bot
Runs both Sentiment and BTC Arbitrage strategies in parallel

Strategy allocation:
- Sentiment Bot: 60% of capital (slower, higher conviction trades)
- BTC Arbitrage: 40% of capital (faster, more frequent trades)
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional
import structlog

from config import get_settings
from .bot import PolymarketSentinelBot, BotConfig
from .btc_bot import BTCArbitrageBot, BTCBotConfig
from .utils import setup_logging, NotificationService

logger = structlog.get_logger()


@dataclass
class CombinedBotConfig:
    """Configuration for combined bot"""
    total_capital: Decimal = Decimal("1000")

    # Capital allocation
    sentiment_allocation: Decimal = Decimal("0.60")  # 60%
    btc_allocation: Decimal = Decimal("0.40")  # 40%

    # Mode
    dry_run: bool = True

    # Sentiment bot settings
    sentiment_poll_interval: int = 60  # seconds
    sentiment_max_markets: int = 20

    # BTC bot settings
    btc_poll_interval_ms: int = 1000  # 1 second
    btc_max_position: Decimal = Decimal("100")


class CombinedBot:
    """
    Runs both trading strategies in parallel

    - Sentiment strategy for longer-term, higher conviction trades
    - BTC arbitrage for quick latency exploitation
    """

    def __init__(self, config: Optional[CombinedBotConfig] = None):
        self.config = config or CombinedBotConfig()
        self.settings = get_settings()

        # Calculate capital allocation
        self.sentiment_capital = self.config.total_capital * self.config.sentiment_allocation
        self.btc_capital = self.config.total_capital * self.config.btc_allocation

        # Bots
        self.sentiment_bot: Optional[PolymarketSentinelBot] = None
        self.btc_bot: Optional[BTCArbitrageBot] = None
        self.notifications: Optional[NotificationService] = None

        # State
        self.is_running = False
        self.start_time: Optional[datetime] = None

    async def initialize(self):
        """Initialize both bots"""
        logger.info(
            "combined_bot_initializing",
            total_capital=float(self.config.total_capital),
            sentiment_capital=float(self.sentiment_capital),
            btc_capital=float(self.btc_capital),
        )

        setup_logging(level=self.settings.bot.log_level)

        # Initialize sentiment bot
        sentiment_config = BotConfig(
            poll_interval=self.config.sentiment_poll_interval,
            dry_run=self.config.dry_run,
            max_markets_to_analyze=self.config.sentiment_max_markets,
        )
        self.sentiment_bot = PolymarketSentinelBot(config=sentiment_config)

        # Override capital in risk manager after init
        # (will be done in start())

        # Initialize BTC bot
        btc_config = BTCBotConfig(
            dry_run=self.config.dry_run,
            max_capital=self.btc_capital,
            max_position_size=min(self.config.btc_max_position, self.btc_capital * Decimal("0.20")),
            poll_interval_ms=self.config.btc_poll_interval_ms,
        )
        self.btc_bot = BTCArbitrageBot(config=btc_config)

        # Notifications
        self.notifications = NotificationService(
            telegram_token=self.settings.notifications.telegram_bot_token,
            telegram_chat_id=self.settings.notifications.telegram_chat_id,
            discord_webhook=self.settings.notifications.discord_webhook_url,
        )

        logger.info("combined_bot_initialized")

    async def start(self):
        """Start both bots"""
        if self.is_running:
            return

        await self.initialize()

        self.is_running = True
        self.start_time = datetime.utcnow()

        logger.info(
            "combined_bot_starting",
            dry_run=self.config.dry_run,
        )

        # Send startup notification
        if self.notifications:
            await self.notifications.send(
                f"Combined Bot Started\n\n"
                f"Mode: {'DRY RUN' if self.config.dry_run else 'LIVE'}\n"
                f"Total Capital: ${self.config.total_capital:,.2f}\n\n"
                f"Sentiment Strategy: ${self.sentiment_capital:,.2f} (60%)\n"
                f"BTC Arbitrage: ${self.btc_capital:,.2f} (40%)",
                level="INFO",
                title="Polymarket Combined Bot"
            )

        # Start both bots concurrently
        await asyncio.gather(
            self._run_sentiment_bot(),
            self._run_btc_bot(),
            self._run_status_reporter(),
        )

    async def _run_sentiment_bot(self):
        """Run sentiment bot"""
        try:
            if self.sentiment_bot:
                await self.sentiment_bot.start()

                while self.is_running and self.sentiment_bot.is_running:
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error("sentiment_bot_error", error=str(e))

    async def _run_btc_bot(self):
        """Run BTC arbitrage bot"""
        try:
            if self.btc_bot:
                await self.btc_bot.start()

                while self.is_running and self.btc_bot.is_running:
                    await asyncio.sleep(1)

        except Exception as e:
            logger.error("btc_bot_error", error=str(e))

    async def _run_status_reporter(self):
        """Periodic status reporting"""
        while self.is_running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes

                if not self.is_running:
                    break

                status = self.get_combined_status()
                logger.info("combined_status", **status)

            except Exception as e:
                logger.warning("status_reporter_error", error=str(e))

    async def stop(self):
        """Stop both bots"""
        logger.info("combined_bot_stopping")
        self.is_running = False

        # Stop both bots
        if self.sentiment_bot:
            await self.sentiment_bot.stop()
        if self.btc_bot:
            await self.btc_bot.stop()

        # Final status
        status = self.get_combined_status()

        if self.notifications:
            await self.notifications.send(
                f"Combined Bot Stopped\n\n"
                f"Runtime: {status.get('runtime_hours', 0):.1f} hours\n"
                f"Sentiment Trades: {status.get('sentiment_trades', 0)}\n"
                f"BTC Trades: {status.get('btc_trades', 0)}\n"
                f"Total Trades: {status.get('total_trades', 0)}",
                level="INFO",
                title="Bot Stopped"
            )

        logger.info("combined_bot_stopped", **status)

    def get_combined_status(self) -> dict:
        """Get combined status from both bots"""
        sentiment_status = self.sentiment_bot.get_status() if self.sentiment_bot else {}
        btc_status = self.btc_bot.get_status() if self.btc_bot else {}

        runtime = 0
        if self.start_time:
            runtime = (datetime.utcnow() - self.start_time).total_seconds() / 3600

        sentiment_trades = sentiment_status.get("stats", {}).get("trades_executed", 0)
        btc_trades = btc_status.get("strategy", {}).get("trades_executed", 0)

        return {
            "is_running": self.is_running,
            "dry_run": self.config.dry_run,
            "runtime_hours": runtime,
            "total_capital": float(self.config.total_capital),
            "sentiment_trades": sentiment_trades,
            "btc_trades": btc_trades,
            "total_trades": sentiment_trades + btc_trades,
            "sentiment_status": sentiment_status,
            "btc_status": btc_status,
        }


async def run_combined(
    dry_run: bool = True,
    capital: float = 1000,
    sentiment_pct: float = 0.60,
):
    """
    Run the combined bot

    Args:
        dry_run: Whether to run in dry-run mode
        capital: Total capital in USDC
        sentiment_pct: Percentage allocated to sentiment strategy
    """
    config = CombinedBotConfig(
        total_capital=Decimal(str(capital)),
        sentiment_allocation=Decimal(str(sentiment_pct)),
        btc_allocation=Decimal(str(1 - sentiment_pct)),
        dry_run=dry_run,
    )

    bot = CombinedBot(config=config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    import sys

    dry_run = "--live" not in sys.argv
    asyncio.run(run_combined(dry_run=dry_run))

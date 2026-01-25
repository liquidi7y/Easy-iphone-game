"""
BTC Arbitrage Bot
High-frequency latency arbitrage on Polymarket BTC markets

This bot operates differently from the sentiment bot:
- Much faster polling (every 1-2 seconds)
- No sentiment or whale analysis
- Pure price arbitrage based on exchange/Polymarket latency
- Shorter position holding times (seconds to minutes)
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
import structlog

from config import get_settings
from .api import PolymarketClient
from .api.models import Side
from .strategies import BTCArbitrageStrategy, BTCArbitrageConfig
from .strategies.base import TradeOpportunity
from .risk import RiskManager, RiskLimits
from .utils import setup_logging, NotificationService
from .utils.logging import TradeLogger

logger = structlog.get_logger()


@dataclass
class BTCBotConfig:
    """Configuration for BTC arbitrage bot"""
    # Polling - FAST for arbitrage
    poll_interval_ms: int = 1000  # 1 second
    position_check_interval_ms: int = 500  # 0.5 seconds

    # Risk
    max_capital: Decimal = Decimal("1000")
    max_concurrent_positions: int = 3
    max_position_size: Decimal = Decimal("200")

    # Execution
    dry_run: bool = True
    use_market_orders: bool = True  # For speed


class BTCArbitrageBot:
    """
    High-frequency BTC arbitrage bot

    Runs a tight loop monitoring:
    1. Binance BTC price (via WebSocket)
    2. Polymarket BTC markets
    3. Entry/exit opportunities
    """

    def __init__(self, config: Optional[BTCBotConfig] = None):
        self.config = config or BTCBotConfig()
        self.settings = get_settings()

        # Components
        self.polymarket: Optional[PolymarketClient] = None
        self.strategy: Optional[BTCArbitrageStrategy] = None
        self.risk_manager: Optional[RiskManager] = None
        self.notifications: Optional[NotificationService] = None
        self.trade_logger: Optional[TradeLogger] = None

        # State
        self.is_running = False
        self._main_task: Optional[asyncio.Task] = None
        self._position_task: Optional[asyncio.Task] = None

        # Active positions
        self.positions: Dict[str, dict] = {}

    async def initialize(self):
        """Initialize bot components"""
        logger.info("btc_bot_initializing")

        setup_logging(level=self.settings.bot.log_level)

        # Polymarket client
        self.polymarket = PolymarketClient(
            api_key=self.settings.polymarket.api_key,
            api_secret=self.settings.polymarket.api_secret.get_secret_value(),
            api_passphrase=self.settings.polymarket.api_passphrase.get_secret_value(),
            private_key=self.settings.wallet.private_key.get_secret_value(),
            wallet_address=self.settings.wallet.address,
        )
        await self.polymarket.connect()

        # Strategy
        strategy_config = BTCArbitrageConfig(
            max_position_size=self.config.max_position_size,
            position_size_pct=float(self.config.max_position_size / self.config.max_capital),
        )
        self.strategy = BTCArbitrageStrategy(
            config=strategy_config,
            polymarket_client=self.polymarket,
            bankroll=self.config.max_capital,
        )
        await self.strategy.initialize()

        # Risk manager (simplified for speed)
        self.risk_manager = RiskManager(
            limits=RiskLimits(
                max_position_size=self.config.max_position_size,
                max_concurrent_positions=self.config.max_concurrent_positions,
                min_time_between_trades=1,  # Allow rapid trading
            ),
            initial_capital=self.config.max_capital,
        )

        # Notifications
        self.notifications = NotificationService(
            telegram_token=self.settings.notifications.telegram_bot_token,
            telegram_chat_id=self.settings.notifications.telegram_chat_id,
            discord_webhook=self.settings.notifications.discord_webhook_url,
        )

        self.trade_logger = TradeLogger(log_file="btc_trades.log")

        logger.info("btc_bot_initialized")

    async def start(self):
        """Start the bot"""
        if self.is_running:
            return

        await self.initialize()
        self.is_running = True

        logger.info(
            "btc_bot_starting",
            dry_run=self.config.dry_run,
            poll_interval_ms=self.config.poll_interval_ms,
        )

        # Start main loop and position monitor
        self._main_task = asyncio.create_task(self._main_loop())
        self._position_task = asyncio.create_task(self._position_monitor_loop())

        if self.notifications:
            await self.notifications.send(
                f"BTC Arbitrage Bot started\n"
                f"Mode: {'DRY RUN' if self.config.dry_run else 'LIVE'}\n"
                f"Capital: ${self.config.max_capital}",
                level="INFO",
                title="BTC Bot Started"
            )

    async def stop(self):
        """Stop the bot"""
        logger.info("btc_bot_stopping")
        self.is_running = False

        if self._main_task:
            self._main_task.cancel()
        if self._position_task:
            self._position_task.cancel()

        if self.strategy:
            await self.strategy.shutdown()
        if self.polymarket:
            await self.polymarket.close()

        # Log final stats
        if self.strategy:
            stats = self.strategy.get_stats()
            logger.info("btc_bot_final_stats", **stats)

            if self.notifications:
                await self.notifications.send(
                    f"Final P&L: ${stats['total_profit']:+,.2f}\n"
                    f"Win Rate: {stats['win_rate']:.1%}\n"
                    f"Trades: {stats['trades_executed']}",
                    level="INFO",
                    title="BTC Bot Stopped"
                )

    async def _main_loop(self):
        """Main trading loop - runs every second"""
        while self.is_running:
            try:
                loop_start = datetime.utcnow()

                # Find opportunities
                opportunities = await self.strategy.find_opportunities()

                # Process each opportunity
                for opp in opportunities:
                    await self._process_opportunity(opp)

                # Calculate sleep time to maintain interval
                elapsed_ms = (datetime.utcnow() - loop_start).total_seconds() * 1000
                sleep_ms = max(0, self.config.poll_interval_ms - elapsed_ms)
                await asyncio.sleep(sleep_ms / 1000)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("main_loop_error", error=str(e))
                await asyncio.sleep(1)

    async def _position_monitor_loop(self):
        """Monitor positions for exit signals"""
        while self.is_running:
            try:
                # Check each position
                for market_id in list(self.positions.keys()):
                    await self._check_position_exit(market_id)

                await asyncio.sleep(self.config.position_check_interval_ms / 1000)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("position_monitor_error", error=str(e))
                await asyncio.sleep(0.5)

    async def _process_opportunity(self, opp: TradeOpportunity):
        """Process a trading opportunity"""
        # Check if already in this market
        if opp.market_id in self.positions:
            return

        # Check concurrent position limit
        if len(self.positions) >= self.config.max_concurrent_positions:
            return

        # Validate opportunity
        if not self.strategy.validate_opportunity(opp):
            return

        logger.info(
            "executing_btc_opportunity",
            market=opp.market_id[:20],
            direction=opp.direction,
            entry=float(opp.entry_price),
            size=float(opp.size),
        )

        # Execute trade
        await self._execute_entry(opp)

    async def _execute_entry(self, opp: TradeOpportunity):
        """Execute entry trade"""
        if self.config.dry_run:
            logger.info(
                "dry_run_entry",
                market=opp.market_id,
                direction=opp.direction,
                price=float(opp.entry_price),
                size=float(opp.size),
            )

            # Record in positions anyway for simulation
            self.positions[opp.market_id] = {
                "entry_price": opp.entry_price,
                "size": opp.size,
                "direction": opp.direction,
                "token_id": opp.token_id,
                "entry_time": datetime.utcnow(),
                "expected_exit": opp.expected_exit_price,
            }

            self.strategy.record_trade(opp.market_id, opp.entry_price, opp.size)

            if self.trade_logger:
                self.trade_logger.log_trade(
                    opp.market_id,
                    opp.direction,
                    float(opp.size),
                    float(opp.entry_price),
                    dry_run=True,
                )
            return

        # Live execution
        try:
            # Calculate shares
            shares = opp.size / opp.entry_price

            order = await self.polymarket.place_order(
                token_id=opp.token_id,
                side=Side.BUY,
                price=opp.entry_price,
                size=shares,
                dry_run=False,
            )

            if order:
                self.positions[opp.market_id] = {
                    "entry_price": opp.entry_price,
                    "size": opp.size,
                    "direction": opp.direction,
                    "token_id": opp.token_id,
                    "entry_time": datetime.utcnow(),
                    "order_id": order.order_id,
                    "expected_exit": opp.expected_exit_price,
                }

                self.strategy.record_trade(opp.market_id, opp.entry_price, opp.size)

                if self.trade_logger:
                    self.trade_logger.log_trade(
                        opp.market_id,
                        opp.direction,
                        float(opp.size),
                        float(opp.entry_price),
                        dry_run=False,
                    )

                if self.notifications:
                    await self.notifications.send_trade_alert(
                        market=opp.market_id[:30],
                        direction=opp.direction,
                        size=float(opp.size),
                        price=float(opp.entry_price),
                        expected_ev=float(opp.expected_profit * opp.size),
                        dry_run=False,
                    )

        except Exception as e:
            logger.error("entry_execution_error", error=str(e))

    async def _check_position_exit(self, market_id: str):
        """Check if position should be exited"""
        if market_id not in self.positions:
            return

        position = self.positions[market_id]

        # Get current price
        try:
            market = await self.polymarket.get_market(market_id)
            if not market:
                return

            if position["direction"] == "YES":
                current_price = market.yes_price
            else:
                current_price = market.no_price

        except Exception as e:
            logger.warning("price_fetch_error", market=market_id, error=str(e))
            return

        # Check exit conditions
        should_exit = await self.strategy.should_exit_position(
            market_id=market_id,
            entry_price=position["entry_price"],
            current_price=current_price,
        )

        if should_exit:
            await self._execute_exit(market_id, current_price)

    async def _execute_exit(self, market_id: str, exit_price: Decimal):
        """Execute exit trade"""
        if market_id not in self.positions:
            return

        position = self.positions[market_id]

        logger.info(
            "executing_exit",
            market=market_id[:20],
            entry=float(position["entry_price"]),
            exit=float(exit_price),
        )

        if self.config.dry_run:
            # Simulate exit
            self.strategy.close_position(market_id, exit_price)
            del self.positions[market_id]

            profit = (exit_price - position["entry_price"]) * position["size"]
            logger.info(
                "dry_run_exit",
                market=market_id,
                profit=float(profit),
            )
            return

        # Live exit
        try:
            # Calculate shares to sell
            shares = position["size"] / position["entry_price"]

            order = await self.polymarket.place_order(
                token_id=position["token_id"],
                side=Side.SELL,
                price=exit_price,
                size=shares,
                dry_run=False,
            )

            if order:
                self.strategy.close_position(market_id, exit_price)
                del self.positions[market_id]

                profit = (exit_price - position["entry_price"]) * position["size"]

                if self.notifications:
                    await self.notifications.send(
                        f"Position closed\nProfit: ${profit:+,.2f}",
                        level="INFO",
                        title="Exit Executed"
                    )

        except Exception as e:
            logger.error("exit_execution_error", error=str(e))

    def get_status(self) -> Dict:
        """Get current bot status"""
        strategy_stats = self.strategy.get_stats() if self.strategy else {}

        return {
            "is_running": self.is_running,
            "dry_run": self.config.dry_run,
            "positions": len(self.positions),
            "strategy": strategy_stats,
        }


async def run_btc_bot(dry_run: bool = True, capital: float = 1000):
    """
    Run the BTC arbitrage bot

    Args:
        dry_run: Whether to run in dry-run mode
        capital: Starting capital in USDC
    """
    config = BTCBotConfig(
        dry_run=dry_run,
        max_capital=Decimal(str(capital)),
    )

    bot = BTCArbitrageBot(config=config)

    try:
        await bot.start()

        # Run until interrupted
        while bot.is_running:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


if __name__ == "__main__":
    import sys

    dry_run = "--live" not in sys.argv
    asyncio.run(run_btc_bot(dry_run=dry_run))

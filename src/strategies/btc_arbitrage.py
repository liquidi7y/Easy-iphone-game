"""
BTC Latency Arbitrage Strategy

Exploits the delay between real-time BTC price movements on exchanges
and Polymarket odds updating on BTC prediction markets.

How it works:
1. Monitor BTC price on Binance via WebSocket (real-time)
2. Detect significant price movements
3. Check Polymarket BTC markets for stale odds
4. Buy positions that reflect what has ALREADY happened
5. Exit when odds catch up (typically 30-60 seconds)

This is pure latency arbitrage - no prediction, just speed.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from enum import Enum
import statistics

import httpx
import structlog
from websockets import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .base import BaseStrategy, TradeOpportunity

logger = structlog.get_logger()


class PriceDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


@dataclass
class BTCArbitrageConfig:
    """Configuration for BTC arbitrage strategy"""
    # Price movement thresholds
    min_price_move_pct: float = 0.3  # Minimum 0.3% move to trigger
    max_price_move_pct: float = 5.0  # Ignore moves > 5% (likely data error)

    # Timing
    price_window_seconds: int = 60  # Look at last 60 seconds of prices
    stale_odds_threshold_seconds: int = 30  # Consider odds stale after 30s
    max_hold_seconds: int = 300  # Maximum time to hold position (5 min)

    # Position sizing
    min_odds_edge: float = 0.05  # Minimum 5 cent edge (e.g., buy at 0.28 expect 0.33+)
    max_position_size: Decimal = Decimal("500")  # Max $500 per trade
    position_size_pct: float = 0.02  # 2% of bankroll per trade

    # Risk
    stop_loss_pct: float = 0.15  # Exit if price moves 15% against us
    take_profit_pct: float = 0.50  # Take profit at 50% gain

    # Markets to monitor (Polymarket BTC prediction market patterns)
    btc_market_keywords: List[str] = field(default_factory=lambda: [
        "bitcoin",
        "btc",
        "$100k",
        "$100,000",
        "bitcoin price",
    ])


@dataclass
class PriceTick:
    """Single price tick from exchange"""
    price: Decimal
    timestamp: datetime
    volume: Decimal = Decimal("0")


@dataclass
class BTCMarket:
    """Polymarket BTC-related market"""
    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    current_yes_price: Decimal
    last_update: datetime
    target_price: Optional[Decimal] = None  # BTC price threshold if applicable
    direction: str = ""  # "above" or "below"


class BinancePriceFeed:
    """
    Real-time BTC price feed from Binance WebSocket

    Provides ultra-low latency price updates
    """

    def __init__(self):
        self.current_price: Optional[Decimal] = None
        self.price_history: List[PriceTick] = []
        self.max_history = 1000
        self._ws = None
        self._running = False
        self._last_update: Optional[datetime] = None

    async def connect(self):
        """Connect to Binance WebSocket"""
        self._running = True
        asyncio.create_task(self._run_websocket())

        # Also fetch initial price via REST
        await self._fetch_initial_price()

        logger.info("binance_feed_connected")

    async def disconnect(self):
        """Disconnect from WebSocket"""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _fetch_initial_price(self):
        """Fetch initial price via REST API"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": "BTCUSDT"}
                )
                if response.status_code == 200:
                    data = response.json()
                    self.current_price = Decimal(data["price"])
                    self._last_update = datetime.utcnow()
                    self.price_history.append(PriceTick(
                        price=self.current_price,
                        timestamp=self._last_update,
                    ))
        except Exception as e:
            logger.error("binance_rest_error", error=str(e))

    async def _run_websocket(self):
        """Run WebSocket connection with auto-reconnect"""
        ws_url = "wss://stream.binance.com:9443/ws/btcusdt@trade"

        while self._running:
            try:
                async with ws_connect(ws_url) as ws:
                    self._ws = ws
                    logger.info("binance_ws_connected")

                    async for message in ws:
                        if not self._running:
                            break

                        try:
                            data = json.loads(message)
                            price = Decimal(data["p"])
                            timestamp = datetime.utcfromtimestamp(data["T"] / 1000)

                            self.current_price = price
                            self._last_update = timestamp

                            # Add to history
                            self.price_history.append(PriceTick(
                                price=price,
                                timestamp=timestamp,
                                volume=Decimal(data.get("q", "0")),
                            ))

                            # Trim history
                            if len(self.price_history) > self.max_history:
                                self.price_history = self.price_history[-self.max_history:]

                        except (KeyError, json.JSONDecodeError) as e:
                            logger.warning("ws_parse_error", error=str(e))

            except ConnectionClosed:
                logger.warning("binance_ws_disconnected")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error("binance_ws_error", error=str(e))
                await asyncio.sleep(5)

    def get_price_change(self, seconds: int = 60) -> Tuple[Optional[Decimal], PriceDirection]:
        """
        Get price change over last N seconds

        Returns:
            Tuple of (percentage_change, direction)
        """
        if not self.price_history or not self.current_price:
            return None, PriceDirection.FLAT

        cutoff = datetime.utcnow() - timedelta(seconds=seconds)
        old_prices = [p for p in self.price_history if p.timestamp < cutoff]

        if not old_prices:
            return None, PriceDirection.FLAT

        old_price = old_prices[-1].price
        change_pct = ((self.current_price - old_price) / old_price) * 100

        if change_pct > Decimal("0.1"):
            direction = PriceDirection.UP
        elif change_pct < Decimal("-0.1"):
            direction = PriceDirection.DOWN
        else:
            direction = PriceDirection.FLAT

        return change_pct, direction

    def get_volatility(self, seconds: int = 300) -> Optional[float]:
        """Get price volatility (std dev of returns) over period"""
        cutoff = datetime.utcnow() - timedelta(seconds=seconds)
        prices = [p.price for p in self.price_history if p.timestamp >= cutoff]

        if len(prices) < 10:
            return None

        # Calculate returns
        returns = []
        for i in range(1, len(prices)):
            ret = float((prices[i] - prices[i-1]) / prices[i-1])
            returns.append(ret)

        if not returns:
            return None

        return statistics.stdev(returns) * 100  # As percentage


class BTCArbitrageStrategy(BaseStrategy):
    """
    BTC Latency Arbitrage Strategy

    Monitors BTC price on Binance and exploits delayed odds
    updates on Polymarket BTC prediction markets.
    """

    def __init__(
        self,
        config: Optional[BTCArbitrageConfig] = None,
        polymarket_client=None,
        bankroll: Decimal = Decimal("1000"),
    ):
        self.config = config or BTCArbitrageConfig()
        self.polymarket = polymarket_client
        self.bankroll = bankroll

        # Price feed
        self.price_feed = BinancePriceFeed()

        # Market tracking
        self.btc_markets: Dict[str, BTCMarket] = {}
        self._last_market_refresh: Optional[datetime] = None
        self._market_refresh_interval = 300  # 5 minutes

        # Position tracking
        self.active_positions: Dict[str, dict] = {}

        # Stats
        self.stats = {
            "opportunities_found": 0,
            "trades_executed": 0,
            "total_profit": Decimal("0"),
            "win_count": 0,
            "loss_count": 0,
        }

    @property
    def name(self) -> str:
        return "BTC Latency Arbitrage"

    @property
    def description(self) -> str:
        return "Exploits delay between Binance BTC prices and Polymarket odds"

    async def initialize(self):
        """Initialize strategy"""
        logger.info("btc_arbitrage_initializing")

        # Start price feed
        await self.price_feed.connect()

        # Load BTC markets
        await self._refresh_btc_markets()

        logger.info(
            "btc_arbitrage_initialized",
            markets_found=len(self.btc_markets)
        )

    async def shutdown(self):
        """Shutdown strategy"""
        await self.price_feed.disconnect()
        logger.info("btc_arbitrage_shutdown")

    async def _refresh_btc_markets(self):
        """Refresh list of BTC-related Polymarket markets"""
        if not self.polymarket:
            return

        # Check if refresh needed
        if self._last_market_refresh:
            elapsed = (datetime.utcnow() - self._last_market_refresh).total_seconds()
            if elapsed < self._market_refresh_interval:
                return

        try:
            # Search for BTC markets
            all_markets = await self.polymarket.get_markets(active_only=True, limit=200)

            self.btc_markets.clear()

            for market in all_markets:
                question_lower = market.question.lower()

                # Check if BTC-related
                is_btc = any(kw in question_lower for kw in self.config.btc_market_keywords)

                if is_btc:
                    # Try to extract price threshold
                    target_price = self._extract_price_threshold(market.question)
                    direction = self._extract_direction(market.question)

                    btc_market = BTCMarket(
                        market_id=market.condition_id,
                        question=market.question,
                        yes_token_id=market.yes_token_id or "",
                        no_token_id=market.no_token_id or "",
                        current_yes_price=market.yes_price,
                        last_update=datetime.utcnow(),
                        target_price=target_price,
                        direction=direction,
                    )

                    self.btc_markets[market.condition_id] = btc_market

            self._last_market_refresh = datetime.utcnow()

            logger.info("btc_markets_refreshed", count=len(self.btc_markets))

        except Exception as e:
            logger.error("market_refresh_error", error=str(e))

    def _extract_price_threshold(self, question: str) -> Optional[Decimal]:
        """Extract BTC price threshold from market question"""
        import re

        # Patterns like "$100,000", "$100k", "100000"
        patterns = [
            r'\$?([\d,]+)k',  # $100k
            r'\$?([\d,]+),000',  # $100,000
            r'\$?([\d,]+)000(?!\d)',  # 100000
        ]

        question_clean = question.lower().replace(",", "")

        for pattern in patterns:
            match = re.search(pattern, question_clean)
            if match:
                try:
                    num = match.group(1).replace(",", "")
                    if "k" in question_clean[match.end()-1:match.end()+1]:
                        return Decimal(num) * 1000
                    return Decimal(num)
                except (ValueError, IndexError):
                    pass

        return None

    def _extract_direction(self, question: str) -> str:
        """Extract direction (above/below/reach) from question"""
        question_lower = question.lower()

        if any(w in question_lower for w in ["above", "over", "exceed", "reach", "hit"]):
            return "above"
        elif any(w in question_lower for w in ["below", "under", "drop", "fall"]):
            return "below"

        return "above"  # Default assumption

    async def find_opportunities(self) -> List[TradeOpportunity]:
        """
        Scan for arbitrage opportunities

        Looks for markets where:
        1. BTC just made a significant move on Binance
        2. Polymarket odds haven't updated yet
        3. The move makes the outcome more/less likely
        """
        opportunities = []

        # Get current BTC state
        if not self.price_feed.current_price:
            return opportunities

        btc_price = self.price_feed.current_price
        price_change, direction = self.price_feed.get_price_change(
            seconds=self.config.price_window_seconds
        )

        if price_change is None:
            return opportunities

        # Check if move is significant enough
        abs_change = abs(float(price_change))
        if abs_change < self.config.min_price_move_pct:
            return opportunities

        if abs_change > self.config.max_price_move_pct:
            logger.warning("price_move_too_large", change=abs_change)
            return opportunities

        logger.info(
            "significant_btc_move",
            price=float(btc_price),
            change_pct=float(price_change),
            direction=direction.value,
        )

        # Refresh markets if needed
        await self._refresh_btc_markets()

        # Check each BTC market for opportunity
        for market_id, market in self.btc_markets.items():
            opp = await self._check_market_opportunity(
                market=market,
                btc_price=btc_price,
                price_direction=direction,
                price_change_pct=abs_change,
            )

            if opp:
                opportunities.append(opp)
                self.stats["opportunities_found"] += 1

        return opportunities

    async def _check_market_opportunity(
        self,
        market: BTCMarket,
        btc_price: Decimal,
        price_direction: PriceDirection,
        price_change_pct: float,
    ) -> Optional[TradeOpportunity]:
        """Check single market for arbitrage opportunity"""

        # Skip if no price threshold
        if not market.target_price:
            return None

        # Calculate what the "true" probability should be based on current price
        # This is simplified - in reality you'd want more sophisticated modeling
        distance_pct = float((btc_price - market.target_price) / market.target_price) * 100

        # Determine expected probability based on price position and direction
        if market.direction == "above":
            # Market asks "Will BTC be above $X?"
            if btc_price > market.target_price:
                # Already above - high probability
                expected_prob = Decimal("0.85") + Decimal(str(min(distance_pct / 20, 0.10)))
            else:
                # Below target
                if price_direction == PriceDirection.UP:
                    # Moving toward target
                    expected_prob = Decimal("0.50") + Decimal(str(min(price_change_pct / 10, 0.15)))
                else:
                    # Moving away
                    expected_prob = Decimal("0.30") - Decimal(str(min(abs(distance_pct) / 50, 0.15)))
        else:
            # Market asks "Will BTC be below $X?"
            if btc_price < market.target_price:
                expected_prob = Decimal("0.85") + Decimal(str(min(abs(distance_pct) / 20, 0.10)))
            else:
                if price_direction == PriceDirection.DOWN:
                    expected_prob = Decimal("0.50") + Decimal(str(min(price_change_pct / 10, 0.15)))
                else:
                    expected_prob = Decimal("0.30") - Decimal(str(min(distance_pct / 50, 0.15)))

        expected_prob = max(Decimal("0.05"), min(Decimal("0.95"), expected_prob))

        # Get current market price (may be stale)
        current_price = market.current_yes_price

        # Calculate edge
        edge = expected_prob - current_price

        # Determine trade direction
        if edge > Decimal(str(self.config.min_odds_edge)):
            # Buy YES - market is underpriced
            trade_direction = "YES"
            entry_price = current_price
            token_id = market.yes_token_id
        elif edge < -Decimal(str(self.config.min_odds_edge)):
            # Buy NO - market is overpriced (YES is too expensive)
            trade_direction = "NO"
            entry_price = Decimal("1") - current_price
            token_id = market.no_token_id
            edge = -edge  # Make positive for calculation
        else:
            # Edge too small
            return None

        if not token_id:
            return None

        # Calculate position size
        size = min(
            self.bankroll * Decimal(str(self.config.position_size_pct)),
            self.config.max_position_size,
        )

        # Calculate expected exit price
        expected_exit = entry_price + edge

        logger.info(
            "opportunity_found",
            market=market.question[:50],
            direction=trade_direction,
            entry=float(entry_price),
            expected_exit=float(expected_exit),
            edge=float(edge),
        )

        return TradeOpportunity(
            market_id=market.market_id,
            token_id=token_id,
            direction=trade_direction,
            entry_price=entry_price,
            expected_exit_price=expected_exit,
            size=size,
            confidence=Decimal(str(min(0.5 + price_change_pct / 10, 0.9))),
            reason=f"BTC {price_direction.value} {price_change_pct:.2f}%, odds stale",
            timestamp=datetime.utcnow(),
            strategy_name=self.name,
            metadata={
                "btc_price": float(btc_price),
                "target_price": float(market.target_price) if market.target_price else None,
                "price_change_pct": price_change_pct,
            }
        )

    async def should_exit_position(
        self,
        market_id: str,
        entry_price: Decimal,
        current_price: Decimal,
    ) -> bool:
        """Check if should exit position"""
        if market_id not in self.active_positions:
            return False

        position = self.active_positions[market_id]
        entry_time = position.get("entry_time", datetime.utcnow())
        elapsed = (datetime.utcnow() - entry_time).total_seconds()

        # Time-based exit
        if elapsed > self.config.max_hold_seconds:
            logger.info("exit_max_hold_time", market=market_id)
            return True

        # Calculate profit/loss
        if entry_price > 0:
            pnl_pct = float((current_price - entry_price) / entry_price)

            # Stop loss
            if pnl_pct < -self.config.stop_loss_pct:
                logger.info("exit_stop_loss", market=market_id, pnl=pnl_pct)
                return True

            # Take profit
            if pnl_pct > self.config.take_profit_pct:
                logger.info("exit_take_profit", market=market_id, pnl=pnl_pct)
                return True

        return False

    def record_trade(self, market_id: str, entry_price: Decimal, size: Decimal):
        """Record a new position"""
        self.active_positions[market_id] = {
            "entry_price": entry_price,
            "size": size,
            "entry_time": datetime.utcnow(),
        }
        self.stats["trades_executed"] += 1

    def close_position(self, market_id: str, exit_price: Decimal):
        """Close and record position result"""
        if market_id not in self.active_positions:
            return

        position = self.active_positions.pop(market_id)
        entry_price = position["entry_price"]
        size = position["size"]

        profit = (exit_price - entry_price) * size
        self.stats["total_profit"] += profit

        if profit > 0:
            self.stats["win_count"] += 1
        else:
            self.stats["loss_count"] += 1

        logger.info(
            "position_closed",
            market=market_id,
            profit=float(profit),
            total_profit=float(self.stats["total_profit"]),
        )

    def get_stats(self) -> Dict:
        """Get strategy statistics"""
        total_trades = self.stats["win_count"] + self.stats["loss_count"]
        win_rate = self.stats["win_count"] / total_trades if total_trades > 0 else 0

        return {
            "strategy": self.name,
            "opportunities_found": self.stats["opportunities_found"],
            "trades_executed": self.stats["trades_executed"],
            "total_profit": float(self.stats["total_profit"]),
            "win_count": self.stats["win_count"],
            "loss_count": self.stats["loss_count"],
            "win_rate": win_rate,
            "active_positions": len(self.active_positions),
            "btc_markets_tracked": len(self.btc_markets),
            "current_btc_price": float(self.price_feed.current_price) if self.price_feed.current_price else None,
        }

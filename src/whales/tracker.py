"""
Whale Wallet Tracker
Monitors top Polymarket traders on Polygon for trading signals
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set
from enum import Enum

import httpx
import structlog
from web3 import Web3, AsyncWeb3
from web3.providers import AsyncHTTPProvider
from cachetools import TTLCache

logger = structlog.get_logger()

# Polymarket contract addresses on Polygon
POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLYMARKET_NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Known whale wallets (curated list - these are public addresses)
# You can expand this list based on Polymarket leaderboard
KNOWN_WHALES = [
    "0x1234567890123456789012345678901234567890",  # Placeholder
]


class ActivityType(str, Enum):
    """Type of whale activity"""
    BUY = "BUY"
    SELL = "SELL"
    MINT = "MINT"  # Creating new position
    REDEEM = "REDEEM"  # Closing position


@dataclass
class WhaleWallet:
    """Information about a whale wallet"""
    address: str
    label: Optional[str] = None
    total_volume: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    win_rate: float = 0.0
    markets_traded: int = 0
    last_active: Optional[datetime] = None
    positions: Dict[str, Decimal] = field(default_factory=dict)  # market_id -> size
    reputation_score: float = 0.5  # 0-1 score of how good their predictions are

    @property
    def is_profitable(self) -> bool:
        return self.total_pnl > 0

    @property
    def trust_score(self) -> float:
        """
        Calculate how much to trust this whale's trades
        Based on historical performance
        """
        volume_factor = min(float(self.total_volume) / 100000, 1.0)  # Max at $100k
        pnl_factor = 0.5 + (0.5 if self.is_profitable else 0)
        win_factor = self.win_rate

        return (volume_factor * 0.3 + pnl_factor * 0.3 + win_factor * 0.4)


@dataclass
class WhaleActivity:
    """Single whale trade/activity"""
    tx_hash: str
    wallet: str
    activity_type: ActivityType
    market_id: str
    token_id: str
    outcome: str  # YES or NO
    size: Decimal
    price: Decimal
    timestamp: datetime
    block_number: int

    @property
    def value(self) -> Decimal:
        """USD value of the trade"""
        return self.size * self.price

    @property
    def is_large(self) -> bool:
        """Check if this is a large trade (>$1000)"""
        return self.value > 1000


@dataclass
class WhaleSignal:
    """Aggregated signal from whale activity"""
    market_id: str
    market_question: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Activity counts
    buy_count: int = 0
    sell_count: int = 0
    total_buy_volume: Decimal = Decimal("0")
    total_sell_volume: Decimal = Decimal("0")

    # Whale consensus
    bullish_whales: int = 0  # Whales with net long position
    bearish_whales: int = 0  # Whales with net short position

    # Top whale activity
    top_whale_direction: Optional[str] = None  # Direction of most profitable whale
    top_whale_size: Decimal = Decimal("0")

    # Signal strength
    signal_strength: float = 0.0  # -1 (strong sell) to 1 (strong buy)
    confidence: float = 0.0

    @property
    def net_flow(self) -> Decimal:
        """Net buy/sell flow"""
        return self.total_buy_volume - self.total_sell_volume

    @property
    def is_bullish(self) -> bool:
        return self.signal_strength > 0.1

    @property
    def is_bearish(self) -> bool:
        return self.signal_strength < -0.1

    @property
    def is_actionable(self) -> bool:
        """Check if signal is strong enough to act on"""
        return self.confidence >= 0.3 and abs(self.signal_strength) >= 0.2


class WhaleTracker:
    """
    Tracks whale wallet activity on Polymarket

    Features:
    - Monitor top 100 wallets
    - Track position changes
    - Generate trading signals based on whale consensus
    - Historical performance tracking
    """

    def __init__(
        self,
        rpc_url: str,
        backup_rpc_url: Optional[str] = None,
    ):
        self.rpc_url = rpc_url
        self.backup_rpc_url = backup_rpc_url

        self._w3: Optional[AsyncWeb3] = None
        self._http_client: Optional[httpx.AsyncClient] = None

        # Whale database
        self.whales: Dict[str, WhaleWallet] = {}
        self._whale_addresses: Set[str] = set()

        # Activity cache
        self._activity_cache: TTLCache = TTLCache(maxsize=10000, ttl=3600)
        self._signal_cache: TTLCache = TTLCache(maxsize=500, ttl=300)

        # Last processed block
        self._last_block: int = 0

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        """Initialize connections"""
        # Initialize Web3
        self._w3 = AsyncWeb3(AsyncHTTPProvider(self.rpc_url))

        # Initialize HTTP client for additional APIs
        self._http_client = httpx.AsyncClient(timeout=30.0)

        # Load whale addresses
        await self._load_whale_addresses()

        logger.info("whale_tracker_connected", whale_count=len(self._whale_addresses))

    async def close(self):
        """Close connections"""
        if self._http_client:
            await self._http_client.aclose()
        self._w3 = None
        logger.info("whale_tracker_closed")

    async def _load_whale_addresses(self):
        """
        Load top whale addresses

        Sources:
        - Polymarket leaderboard
        - Known profitable traders
        - High volume addresses from on-chain data
        """
        # Start with known whales
        for addr in KNOWN_WHALES:
            self._whale_addresses.add(addr.lower())

        # Try to fetch from Polymarket leaderboard API
        try:
            if self._http_client:
                response = await self._http_client.get(
                    "https://gamma-api.polymarket.com/leaderboard",
                    params={"limit": 100}
                )
                if response.status_code == 200:
                    data = response.json()
                    for entry in data.get("leaderboard", data) if isinstance(data, dict) else data:
                        if isinstance(entry, dict):
                            address = entry.get("address", entry.get("userAddress", ""))
                            if address and address.startswith("0x"):
                                self._whale_addresses.add(address.lower())

                                # Create whale profile
                                self.whales[address.lower()] = WhaleWallet(
                                    address=address.lower(),
                                    label=entry.get("name", entry.get("username")),
                                    total_volume=Decimal(str(entry.get("volume", 0))),
                                    total_pnl=Decimal(str(entry.get("pnl", entry.get("profit", 0)))),
                                    win_rate=float(entry.get("winRate", entry.get("win_rate", 0.5))),
                                    markets_traded=int(entry.get("marketsTraded", entry.get("markets", 0))),
                                )

        except Exception as e:
            logger.warning("leaderboard_fetch_error", error=str(e))

        # Ensure we have at least some addresses
        if len(self._whale_addresses) < 10:
            # Add some default high-activity addresses (these would be real in production)
            logger.warning("using_default_whale_list", count=len(self._whale_addresses))

    async def get_whale_positions(self, market_id: str) -> Dict[str, Dict]:
        """
        Get all whale positions in a specific market

        Returns:
            Dict mapping wallet address to position info
        """
        positions = {}

        # Query positions via Polymarket API
        try:
            if self._http_client:
                for address in list(self._whale_addresses)[:50]:  # Limit to avoid rate limits
                    response = await self._http_client.get(
                        f"https://gamma-api.polymarket.com/positions",
                        params={"user": address, "market": market_id}
                    )

                    if response.status_code == 200:
                        data = response.json()
                        if data:
                            positions[address] = {
                                "size": Decimal(str(data.get("size", 0))),
                                "outcome": data.get("outcome", ""),
                                "avg_price": Decimal(str(data.get("avgPrice", 0))),
                            }

                    await asyncio.sleep(0.1)  # Rate limit protection

        except Exception as e:
            logger.error("whale_positions_error", market_id=market_id, error=str(e))

        return positions

    async def get_recent_activity(
        self,
        market_id: Optional[str] = None,
        hours_back: int = 24,
        min_size: Decimal = Decimal("100"),
    ) -> List[WhaleActivity]:
        """
        Get recent whale trading activity

        Args:
            market_id: Filter to specific market (optional)
            hours_back: How far back to look
            min_size: Minimum trade size in USD

        Returns:
            List of WhaleActivity objects
        """
        activities = []

        # Query recent trades from Polymarket
        try:
            if self._http_client:
                for address in list(self._whale_addresses)[:30]:
                    params = {
                        "user": address,
                        "limit": 50,
                    }
                    if market_id:
                        params["market"] = market_id

                    response = await self._http_client.get(
                        "https://gamma-api.polymarket.com/trades",
                        params=params
                    )

                    if response.status_code == 200:
                        trades = response.json()
                        for trade in (trades if isinstance(trades, list) else trades.get("trades", [])):
                            try:
                                size = Decimal(str(trade.get("size", trade.get("amount", 0))))
                                price = Decimal(str(trade.get("price", 0)))

                                if size * price < min_size:
                                    continue

                                # Parse timestamp
                                ts_str = trade.get("timestamp", trade.get("createdAt", ""))
                                if ts_str:
                                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                else:
                                    timestamp = datetime.utcnow()

                                # Check if within time window
                                cutoff = datetime.utcnow() - timedelta(hours=hours_back)
                                if timestamp < cutoff:
                                    continue

                                side = trade.get("side", trade.get("type", "BUY")).upper()

                                activities.append(WhaleActivity(
                                    tx_hash=trade.get("txHash", trade.get("transactionHash", "")),
                                    wallet=address,
                                    activity_type=ActivityType.BUY if side == "BUY" else ActivityType.SELL,
                                    market_id=trade.get("market", trade.get("conditionId", "")),
                                    token_id=trade.get("assetId", trade.get("tokenId", "")),
                                    outcome=trade.get("outcome", ""),
                                    size=size,
                                    price=price,
                                    timestamp=timestamp,
                                    block_number=int(trade.get("blockNumber", 0)),
                                ))

                            except Exception as e:
                                logger.debug("trade_parse_error", error=str(e))

                    await asyncio.sleep(0.1)

        except Exception as e:
            logger.error("activity_fetch_error", error=str(e))

        # Sort by timestamp (most recent first)
        activities.sort(key=lambda x: x.timestamp, reverse=True)

        return activities

    async def analyze_market(self, market_id: str, market_question: str = "") -> WhaleSignal:
        """
        Analyze whale activity for a specific market

        Args:
            market_id: The market condition ID
            market_question: Market question text (for logging)

        Returns:
            WhaleSignal with aggregated analysis
        """
        # Check cache
        if market_id in self._signal_cache:
            return self._signal_cache[market_id]

        logger.info("analyzing_whale_activity", market_id=market_id)

        signal = WhaleSignal(
            market_id=market_id,
            market_question=market_question,
        )

        # Get recent activity
        activities = await self.get_recent_activity(market_id=market_id, hours_back=48)

        if not activities:
            logger.info("no_whale_activity", market_id=market_id)
            signal.confidence = 0.1
            self._signal_cache[market_id] = signal
            return signal

        # Aggregate activity
        whale_positions: Dict[str, Decimal] = {}  # address -> net position

        for activity in activities:
            # Track volumes
            if activity.activity_type == ActivityType.BUY:
                signal.buy_count += 1
                signal.total_buy_volume += activity.value
                whale_positions[activity.wallet] = whale_positions.get(
                    activity.wallet, Decimal("0")
                ) + activity.size
            else:
                signal.sell_count += 1
                signal.total_sell_volume += activity.value
                whale_positions[activity.wallet] = whale_positions.get(
                    activity.wallet, Decimal("0")
                ) - activity.size

        # Count bullish vs bearish whales
        for address, position in whale_positions.items():
            if position > 0:
                signal.bullish_whales += 1
            elif position < 0:
                signal.bearish_whales += 1

        # Find top whale activity
        if self.whales:
            # Sort by reputation/profit
            top_whales = sorted(
                [(addr, pos) for addr, pos in whale_positions.items() if addr in self.whales],
                key=lambda x: self.whales[x[0]].trust_score if x[0] in self.whales else 0,
                reverse=True
            )

            if top_whales:
                top_addr, top_pos = top_whales[0]
                signal.top_whale_direction = "BUY" if top_pos > 0 else "SELL"
                signal.top_whale_size = abs(top_pos)

        # Calculate signal strength
        total_whales = signal.bullish_whales + signal.bearish_whales
        if total_whales > 0:
            # Consensus-based signal
            consensus = (signal.bullish_whales - signal.bearish_whales) / total_whales

            # Volume-based signal
            total_volume = signal.total_buy_volume + signal.total_sell_volume
            if total_volume > 0:
                volume_signal = float(
                    (signal.total_buy_volume - signal.total_sell_volume) / total_volume
                )
            else:
                volume_signal = 0

            # Combined signal
            signal.signal_strength = (consensus * 0.6) + (volume_signal * 0.4)

            # Confidence based on sample size and agreement
            sample_confidence = min(total_whales / 10, 1.0)
            agreement_confidence = abs(consensus)
            signal.confidence = (sample_confidence * 0.5) + (agreement_confidence * 0.5)

        # Cache result
        self._signal_cache[market_id] = signal

        return signal

    async def get_whale_consensus(
        self,
        markets: List[str],
    ) -> Dict[str, WhaleSignal]:
        """
        Get whale consensus for multiple markets

        Args:
            markets: List of market IDs

        Returns:
            Dict mapping market_id to WhaleSignal
        """
        results = {}

        for market_id in markets:
            try:
                signal = await self.analyze_market(market_id)
                results[market_id] = signal
            except Exception as e:
                logger.error("consensus_error", market_id=market_id, error=str(e))

        return results

    def get_top_whales(self, limit: int = 20) -> List[WhaleWallet]:
        """Get top performing whales"""
        return sorted(
            self.whales.values(),
            key=lambda w: w.trust_score,
            reverse=True
        )[:limit]

    def explain_signal(self, signal: WhaleSignal) -> str:
        """Generate human-readable explanation of whale signal"""
        lines = [
            f"Market: {signal.market_question[:60]}..." if signal.market_question else f"Market: {signal.market_id}",
            f"Signal Strength: {signal.signal_strength:+.2f} ({'BULLISH' if signal.is_bullish else 'BEARISH' if signal.is_bearish else 'NEUTRAL'})",
            f"Confidence: {signal.confidence:.1%}",
            "",
            f"Whale Consensus: {signal.bullish_whales} bullish, {signal.bearish_whales} bearish",
            f"Buy Volume: ${signal.total_buy_volume:,.0f} ({signal.buy_count} trades)",
            f"Sell Volume: ${signal.total_sell_volume:,.0f} ({signal.sell_count} trades)",
            f"Net Flow: ${signal.net_flow:+,.0f}",
        ]

        if signal.top_whale_direction:
            lines.append(
                f"Top Whale: {signal.top_whale_direction} ${signal.top_whale_size:,.0f}"
            )

        if signal.is_actionable:
            lines.append(f"\n*** ACTIONABLE SIGNAL ***")

        return "\n".join(lines)

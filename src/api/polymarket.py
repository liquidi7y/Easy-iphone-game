"""
Polymarket CLOB API Client
Handles all interactions with Polymarket's Central Limit Order Book
"""

import asyncio
import hashlib
import hmac
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from .models import (
    Market,
    MarketData,
    MarketStatus,
    Order,
    OrderBook,
    OrderBookLevel,
    OrderStatus,
    Position,
    Side,
    TokenInfo,
    Trade,
)

logger = structlog.get_logger()

# Polymarket API endpoints
CLOB_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"


class PolymarketError(Exception):
    """Base exception for Polymarket API errors"""
    pass


class PolymarketClient:
    """
    Async client for Polymarket CLOB API

    Handles:
    - Market data fetching
    - Order placement and management
    - Position tracking
    - Order book data
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        private_key: str = "",
        wallet_address: str = "",
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.private_key = private_key
        self.wallet_address = wallet_address.lower() if wallet_address else ""

        self._client: Optional[httpx.AsyncClient] = None
        self._markets_cache: Dict[str, Market] = {}
        self._cache_timestamp: float = 0
        self._cache_ttl: float = 300  # 5 minutes

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        """Initialize HTTP client"""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "PolymarketSentinel/1.0"}
        )
        logger.info("polymarket_client_connected")

    async def close(self):
        """Close HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("polymarket_client_closed")

    def _get_client(self) -> httpx.AsyncClient:
        """Get HTTP client, raise if not connected"""
        if self._client is None:
            raise PolymarketError("Client not connected. Call connect() first.")
        return self._client

    def _sign_request(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """
        Sign API request for authenticated endpoints
        Uses HMAC-SHA256 signature
        """
        timestamp = str(int(time.time() * 1000))
        message = timestamp + method.upper() + path + body

        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return {
            "POLY-API-KEY": self.api_key,
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-PASSPHRASE": self.api_passphrase,
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def _request(
        self,
        method: str,
        url: str,
        authenticated: bool = False,
        **kwargs
    ) -> Any:
        """Make HTTP request with retry logic"""
        client = self._get_client()

        headers = kwargs.pop("headers", {})

        if authenticated:
            path = url.replace(CLOB_API_BASE, "")
            body = kwargs.get("json", "")
            if body:
                import json
                body = json.dumps(body)
            auth_headers = self._sign_request(method, path, body)
            headers.update(auth_headers)

        response = await client.request(method, url, headers=headers, **kwargs)

        if response.status_code >= 400:
            logger.error(
                "polymarket_api_error",
                status=response.status_code,
                body=response.text[:500]
            )
            raise PolymarketError(f"API error {response.status_code}: {response.text[:200]}")

        return response.json() if response.text else None

    # ==================== MARKET DATA ====================

    async def get_markets(
        self,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Market]:
        """
        Fetch all markets from Polymarket

        Args:
            active_only: Only return active markets
            limit: Number of markets to fetch
            offset: Pagination offset

        Returns:
            List of Market objects
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active_only).lower(),
        }

        # Use Gamma API for market discovery
        data = await self._request(
            "GET",
            f"{GAMMA_API_BASE}/markets",
            params=params
        )

        markets = []
        for item in data or []:
            try:
                market = self._parse_market(item)
                markets.append(market)
                self._markets_cache[market.condition_id] = market
            except Exception as e:
                logger.warning("market_parse_error", error=str(e), item=item.get("question", ""))

        self._cache_timestamp = time.time()
        return markets

    async def get_market(self, condition_id: str) -> Optional[Market]:
        """Get single market by condition ID"""
        # Check cache first
        if condition_id in self._markets_cache:
            if time.time() - self._cache_timestamp < self._cache_ttl:
                return self._markets_cache[condition_id]

        data = await self._request(
            "GET",
            f"{GAMMA_API_BASE}/markets/{condition_id}"
        )

        if data:
            market = self._parse_market(data)
            self._markets_cache[condition_id] = market
            return market
        return None

    async def search_markets(self, query: str, limit: int = 20) -> List[Market]:
        """Search markets by keyword"""
        data = await self._request(
            "GET",
            f"{GAMMA_API_BASE}/markets",
            params={"query": query, "limit": limit}
        )

        return [self._parse_market(item) for item in (data or [])]

    def _parse_market(self, data: Dict) -> Market:
        """Parse market data from API response"""
        tokens = []

        # Parse token information
        if "tokens" in data:
            for token in data["tokens"]:
                tokens.append(TokenInfo(
                    token_id=token.get("token_id", ""),
                    outcome=token.get("outcome", ""),
                    price=Decimal(str(token.get("price", 0.5))),
                    winner=token.get("winner"),
                ))
        elif "outcomePrices" in data:
            # Alternative format
            prices = data["outcomePrices"]
            if isinstance(prices, list) and len(prices) >= 2:
                tokens = [
                    TokenInfo(token_id="", outcome="Yes", price=Decimal(str(prices[0]))),
                    TokenInfo(token_id="", outcome="No", price=Decimal(str(prices[1]))),
                ]

        # Parse end date
        end_date = None
        if data.get("endDate"):
            try:
                end_date = datetime.fromisoformat(data["endDate"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        return Market(
            condition_id=data.get("conditionId", data.get("condition_id", "")),
            question_id=data.get("questionId", data.get("question_id", "")),
            question=data.get("question", ""),
            description=data.get("description"),
            market_slug=data.get("slug", data.get("market_slug", "")),
            status=MarketStatus(data.get("active", True) and "active" or "closed"),
            tokens=tokens,
            end_date=end_date,
            volume=Decimal(str(data.get("volume", 0))),
            volume_24h=Decimal(str(data.get("volume24hr", data.get("volume_24h", 0)))),
            liquidity=Decimal(str(data.get("liquidity", 0))),
        )

    # ==================== ORDER BOOK ====================

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Get order book for a specific token"""
        data = await self._request(
            "GET",
            f"{CLOB_API_BASE}/book",
            params={"token_id": token_id}
        )

        bids = [
            OrderBookLevel(
                price=Decimal(str(level.get("price", 0))),
                size=Decimal(str(level.get("size", 0)))
            )
            for level in (data.get("bids") or [])
        ]

        asks = [
            OrderBookLevel(
                price=Decimal(str(level.get("price", 0))),
                size=Decimal(str(level.get("size", 0)))
            )
            for level in (data.get("asks") or [])
        ]

        return OrderBook(
            token_id=token_id,
            bids=sorted(bids, key=lambda x: x.price, reverse=True),
            asks=sorted(asks, key=lambda x: x.price),
        )

    async def get_market_data(self, market: Market) -> MarketData:
        """Get complete market data including order books"""
        orderbook_yes = None
        orderbook_no = None

        if market.yes_token_id:
            orderbook_yes = await self.get_orderbook(market.yes_token_id)
        if market.no_token_id:
            orderbook_no = await self.get_orderbook(market.no_token_id)

        return MarketData(
            market=market,
            orderbook_yes=orderbook_yes,
            orderbook_no=orderbook_no,
        )

    # ==================== TRADING ====================

    async def get_positions(self) -> List[Position]:
        """Get all current positions"""
        if not self.wallet_address:
            return []

        data = await self._request(
            "GET",
            f"{CLOB_API_BASE}/positions",
            authenticated=True
        )

        positions = []
        for item in (data or []):
            positions.append(Position(
                market_id=item.get("market_id", item.get("conditionId", "")),
                token_id=item.get("token_id", ""),
                outcome=item.get("outcome", ""),
                size=Decimal(str(item.get("size", 0))),
                avg_price=Decimal(str(item.get("avgPrice", item.get("avg_price", 0)))),
                current_price=Decimal(str(item.get("currentPrice", item.get("price", 0)))),
                unrealized_pnl=Decimal(str(item.get("unrealizedPnl", 0))),
                realized_pnl=Decimal(str(item.get("realizedPnl", 0))),
            ))

        return positions

    async def get_orders(self, market_id: Optional[str] = None) -> List[Order]:
        """Get open orders"""
        params = {}
        if market_id:
            params["market"] = market_id

        data = await self._request(
            "GET",
            f"{CLOB_API_BASE}/orders",
            authenticated=True,
            params=params
        )

        orders = []
        for item in (data or []):
            orders.append(Order(
                order_id=item.get("id", item.get("order_id", "")),
                market_id=item.get("market", item.get("market_id", "")),
                token_id=item.get("asset_id", item.get("token_id", "")),
                side=Side(item.get("side", "BUY").upper()),
                price=Decimal(str(item.get("price", 0))),
                size=Decimal(str(item.get("original_size", item.get("size", 0)))),
                size_matched=Decimal(str(item.get("size_matched", 0))),
                status=OrderStatus(item.get("status", "LIVE").upper()),
            ))

        return orders

    async def place_order(
        self,
        token_id: str,
        side: Side,
        price: Decimal,
        size: Decimal,
        dry_run: bool = False,
    ) -> Optional[Order]:
        """
        Place a limit order

        Args:
            token_id: Token to trade
            side: BUY or SELL
            price: Limit price (0-1)
            size: Size in shares
            dry_run: If True, don't actually place the order

        Returns:
            Order object if successful
        """
        if dry_run:
            logger.info(
                "dry_run_order",
                token_id=token_id,
                side=side.value,
                price=str(price),
                size=str(size)
            )
            return Order(
                order_id=f"dry_run_{int(time.time())}",
                market_id="",
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.LIVE,
            )

        order_payload = {
            "tokenID": token_id,
            "side": side.value,
            "price": str(price),
            "size": str(size),
            "type": "GTC",  # Good til cancelled
        }

        data = await self._request(
            "POST",
            f"{CLOB_API_BASE}/order",
            authenticated=True,
            json=order_payload
        )

        if data and data.get("success"):
            order_data = data.get("order", data)
            return Order(
                order_id=order_data.get("id", order_data.get("orderID", "")),
                market_id=order_data.get("market", ""),
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status=OrderStatus.LIVE,
            )

        logger.error("order_placement_failed", response=data)
        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order"""
        data = await self._request(
            "DELETE",
            f"{CLOB_API_BASE}/order/{order_id}",
            authenticated=True
        )

        return data.get("success", False) if data else False

    async def cancel_all_orders(self, market_id: Optional[str] = None) -> int:
        """Cancel all open orders, optionally for a specific market"""
        params = {}
        if market_id:
            params["market"] = market_id

        data = await self._request(
            "DELETE",
            f"{CLOB_API_BASE}/orders",
            authenticated=True,
            params=params
        )

        return data.get("canceled", 0) if data else 0

    # ==================== ACCOUNT ====================

    async def get_balance(self) -> Decimal:
        """Get USDC balance"""
        data = await self._request(
            "GET",
            f"{CLOB_API_BASE}/balance",
            authenticated=True
        )

        return Decimal(str(data.get("balance", 0))) if data else Decimal("0")

    async def get_trade_history(
        self,
        limit: int = 100,
        market_id: Optional[str] = None
    ) -> List[Trade]:
        """Get trade history"""
        params = {"limit": limit}
        if market_id:
            params["market"] = market_id

        data = await self._request(
            "GET",
            f"{CLOB_API_BASE}/trades",
            authenticated=True,
            params=params
        )

        trades = []
        for item in (data or []):
            trades.append(Trade(
                trade_id=item.get("id", item.get("trade_id", "")),
                market_id=item.get("market", item.get("market_id", "")),
                token_id=item.get("asset_id", item.get("token_id", "")),
                side=Side(item.get("side", "BUY").upper()),
                price=Decimal(str(item.get("price", 0))),
                size=Decimal(str(item.get("size", 0))),
                timestamp=datetime.fromisoformat(
                    item.get("timestamp", item.get("created_at", datetime.utcnow().isoformat()))
                    .replace("Z", "+00:00")
                ),
                maker=item.get("maker", ""),
                taker=item.get("taker", ""),
            ))

        return trades

    # ==================== UTILITIES ====================

    async def get_trending_markets(self, limit: int = 20) -> List[Market]:
        """Get trending markets by volume"""
        markets = await self.get_markets(active_only=True, limit=100)

        # Sort by 24h volume
        trending = sorted(markets, key=lambda m: m.volume_24h, reverse=True)
        return trending[:limit]

    async def get_closing_soon(self, hours: int = 24, limit: int = 20) -> List[Market]:
        """Get markets closing within specified hours"""
        from datetime import timedelta

        markets = await self.get_markets(active_only=True, limit=200)
        now = datetime.utcnow()
        cutoff = now + timedelta(hours=hours)

        closing_soon = [
            m for m in markets
            if m.end_date and now < m.end_date < cutoff
        ]

        return sorted(closing_soon, key=lambda m: m.end_date)[:limit]

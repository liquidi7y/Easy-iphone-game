"""
Data models for Polymarket API responses
"""

from pydantic import BaseModel, Field
from typing import Optional, List
from decimal import Decimal
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    """Order side"""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Order status"""
    LIVE = "LIVE"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    MATCHED = "MATCHED"


class MarketStatus(str, Enum):
    """Market status"""
    ACTIVE = "active"
    CLOSED = "closed"
    RESOLVED = "resolved"


class TokenInfo(BaseModel):
    """Token information for a market outcome"""
    token_id: str
    outcome: str
    price: Decimal = Decimal("0")
    winner: Optional[bool] = None


class Market(BaseModel):
    """Polymarket market data"""
    condition_id: str
    question_id: str
    question: str
    description: Optional[str] = None
    market_slug: str = ""
    status: MarketStatus = MarketStatus.ACTIVE
    tokens: List[TokenInfo] = Field(default_factory=list)
    end_date: Optional[datetime] = None
    volume: Decimal = Decimal("0")
    volume_24h: Decimal = Decimal("0")
    liquidity: Decimal = Decimal("0")
    created_at: Optional[datetime] = None

    @property
    def yes_price(self) -> Decimal:
        """Get YES token price"""
        for token in self.tokens:
            if token.outcome.upper() == "YES":
                return token.price
        return Decimal("0.5")

    @property
    def no_price(self) -> Decimal:
        """Get NO token price"""
        for token in self.tokens:
            if token.outcome.upper() == "NO":
                return token.price
        return Decimal("0.5")

    @property
    def yes_token_id(self) -> Optional[str]:
        """Get YES token ID"""
        for token in self.tokens:
            if token.outcome.upper() == "YES":
                return token.token_id
        return None

    @property
    def no_token_id(self) -> Optional[str]:
        """Get NO token ID"""
        for token in self.tokens:
            if token.outcome.upper() == "NO":
                return token.token_id
        return None


class OrderBookLevel(BaseModel):
    """Single level in order book"""
    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    """Order book for a market"""
    token_id: str
    bids: List[OrderBookLevel] = Field(default_factory=list)
    asks: List[OrderBookLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get best bid price"""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get best ask price"""
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[Decimal]:
        """Get bid-ask spread"""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def mid_price(self) -> Optional[Decimal]:
        """Get mid price"""
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None


class Trade(BaseModel):
    """Trade execution data"""
    trade_id: str
    market_id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    timestamp: datetime
    maker: str
    taker: str


class Order(BaseModel):
    """Order data"""
    order_id: str
    market_id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal
    size_matched: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.LIVE
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expiration: Optional[datetime] = None

    @property
    def filled_percent(self) -> Decimal:
        """Get fill percentage"""
        if self.size == 0:
            return Decimal("0")
        return (self.size_matched / self.size) * 100


class Position(BaseModel):
    """User position in a market"""
    market_id: str
    token_id: str
    outcome: str
    size: Decimal
    avg_price: Decimal
    current_price: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")

    @property
    def value(self) -> Decimal:
        """Current position value"""
        return self.size * self.current_price

    @property
    def cost_basis(self) -> Decimal:
        """Original cost"""
        return self.size * self.avg_price


class MarketData(BaseModel):
    """Aggregated market data for analysis"""
    market: Market
    orderbook_yes: Optional[OrderBook] = None
    orderbook_no: Optional[OrderBook] = None
    recent_trades: List[Trade] = Field(default_factory=list)
    price_history: List[tuple] = Field(default_factory=list)  # (timestamp, price)

    @property
    def has_liquidity(self) -> bool:
        """Check if market has sufficient liquidity"""
        return self.market.liquidity > 1000  # $1000 minimum

    @property
    def is_tradeable(self) -> bool:
        """Check if market is tradeable"""
        return (
            self.market.status == MarketStatus.ACTIVE
            and self.has_liquidity
            and self.orderbook_yes is not None
        )

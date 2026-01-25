"""
Base Strategy Interface
All trading strategies must implement this interface
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional
from datetime import datetime


@dataclass
class TradeOpportunity:
    """Represents a trading opportunity identified by a strategy"""
    market_id: str
    token_id: str
    direction: str  # "YES" or "NO"
    entry_price: Decimal
    expected_exit_price: Decimal
    size: Decimal
    confidence: Decimal
    reason: str
    timestamp: datetime
    strategy_name: str
    metadata: dict = None

    @property
    def expected_profit(self) -> Decimal:
        """Expected profit per share"""
        return self.expected_exit_price - self.entry_price

    @property
    def expected_roi(self) -> Decimal:
        """Expected ROI percentage"""
        if self.entry_price == 0:
            return Decimal("0")
        return (self.expected_profit / self.entry_price) * 100


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies

    Implement this to create new strategies that can be
    plugged into the bot orchestrator.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Strategy description"""
        pass

    @abstractmethod
    async def initialize(self):
        """Initialize strategy resources"""
        pass

    @abstractmethod
    async def shutdown(self):
        """Clean up strategy resources"""
        pass

    @abstractmethod
    async def find_opportunities(self) -> List[TradeOpportunity]:
        """
        Scan for trading opportunities

        Returns:
            List of TradeOpportunity objects
        """
        pass

    @abstractmethod
    async def should_exit_position(
        self,
        market_id: str,
        entry_price: Decimal,
        current_price: Decimal,
    ) -> bool:
        """
        Check if we should exit a position

        Args:
            market_id: The market identifier
            entry_price: Price we entered at
            current_price: Current market price

        Returns:
            True if should exit
        """
        pass

    def validate_opportunity(self, opp: TradeOpportunity) -> bool:
        """
        Validate an opportunity before execution

        Override in subclass for strategy-specific validation
        """
        return (
            opp.confidence > Decimal("0.5") and
            opp.expected_profit > Decimal("0.01") and
            opp.size > Decimal("0")
        )

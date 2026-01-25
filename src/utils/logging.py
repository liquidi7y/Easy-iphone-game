"""
Logging configuration for Polymarket Sentinel
"""

import logging
import sys
from datetime import datetime
from typing import Optional

import structlog


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_file: Optional[str] = None,
) -> None:
    """
    Configure structured logging for the application

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        json_output: Output JSON format (for production)
        log_file: Optional file path for logging
    """
    # Set up standard logging
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper()),
        stream=sys.stdout,
    )

    # Add file handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(getattr(logging, level.upper()))
        logging.getLogger().addHandler(file_handler)

    # Configure processors
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_output:
        # JSON output for production
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Human-readable output for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            ),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class TradeLogger:
    """Specialized logger for trade activity"""

    def __init__(self, log_file: str = "trades.log"):
        self.log_file = log_file
        self._logger = structlog.get_logger("trades")

    def log_signal(self, market_id: str, direction: str, confidence: float, edge: float):
        """Log a trading signal"""
        self._logger.info(
            "signal_generated",
            market_id=market_id,
            direction=direction,
            confidence=confidence,
            edge=edge,
        )
        self._write_to_file(f"SIGNAL: {market_id} {direction} conf={confidence:.2%} edge={edge:.2%}")

    def log_trade(
        self,
        market_id: str,
        direction: str,
        size: float,
        price: float,
        dry_run: bool = False,
    ):
        """Log a trade execution"""
        trade_type = "DRY_RUN" if dry_run else "TRADE"
        self._logger.info(
            "trade_executed",
            market_id=market_id,
            direction=direction,
            size=size,
            price=price,
            dry_run=dry_run,
        )
        self._write_to_file(
            f"{trade_type}: {market_id} {direction} size=${size:.2f} price={price:.4f}"
        )

    def log_rejection(self, market_id: str, reason: str):
        """Log a rejected trade"""
        self._logger.info(
            "trade_rejected",
            market_id=market_id,
            reason=reason,
        )
        self._write_to_file(f"REJECTED: {market_id} - {reason}")

    def _write_to_file(self, message: str):
        """Append to trade log file"""
        try:
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_file, "a") as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass  # Don't fail on logging errors

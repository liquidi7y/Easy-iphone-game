"""
Polymarket Sentinel CLI
Command-line interface for running and managing the bot
"""

import asyncio
import sys
from decimal import Decimal
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
from rich import box

from .bot import PolymarketSentinelBot, BotConfig
from .btc_bot import BTCArbitrageBot, BTCBotConfig
from config import get_settings

app = typer.Typer(
    name="polysentinel",
    help="Polymarket Sentinel - AI-powered betting bot with sentiment analysis and whale tracking",
    add_completion=False,
)

# Sub-app for BTC strategy
btc_app = typer.Typer(help="BTC latency arbitrage strategy commands")
app.add_typer(btc_app, name="btc")

console = Console()


def print_banner():
    """Print startup banner"""
    banner = """
    ╔═══════════════════════════════════════════════════════════╗
    ║          POLYMARKET SENTINEL BOT v1.0.0                   ║
    ║   AI-Powered Trading with Sentiment & Whale Tracking      ║
    ╚═══════════════════════════════════════════════════════════╝
    """
    console.print(banner, style="bold cyan")


@app.command()
def run(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--live",
        help="Run in dry-run mode (no real trades)"
    ),
    interval: int = typer.Option(
        60,
        "--interval", "-i",
        help="Polling interval in seconds"
    ),
    max_markets: int = typer.Option(
        20,
        "--max-markets", "-m",
        help="Maximum markets to analyze per cycle"
    ),
    approval_threshold: float = typer.Option(
        50.0,
        "--approval-threshold", "-a",
        help="USD threshold for manual trade approval"
    ),
):
    """
    Run the Polymarket Sentinel bot

    Example:
        polysentinel run --dry-run --interval 120
        polysentinel run --live --approval-threshold 100
    """
    print_banner()

    if not dry_run:
        console.print("[bold red]WARNING: Running in LIVE mode - real trades will be executed![/bold red]")
        if not typer.confirm("Are you sure you want to continue?"):
            raise typer.Abort()

    config = BotConfig(
        poll_interval=interval,
        dry_run=dry_run,
        manual_approval_threshold=Decimal(str(approval_threshold)),
        max_markets_to_analyze=max_markets,
    )

    console.print(f"\n[green]Starting bot in {'DRY RUN' if dry_run else 'LIVE'} mode...[/green]")
    console.print(f"  Poll interval: {interval}s")
    console.print(f"  Max markets: {max_markets}")
    console.print(f"  Approval threshold: ${approval_threshold}")
    console.print()

    bot = PolymarketSentinelBot(config=config)

    async def run_bot():
        try:
            await bot.start()

            # Keep running until interrupted
            while bot.is_running:
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
        finally:
            await bot.stop()

    asyncio.run(run_bot())


@app.command()
def status():
    """Show current bot status and configuration"""
    print_banner()

    settings = get_settings()

    # Configuration table
    config_table = Table(title="Configuration", box=box.ROUNDED)
    config_table.add_column("Setting", style="cyan")
    config_table.add_column("Value", style="green")

    config_table.add_row("Dry Run", str(settings.bot.dry_run))
    config_table.add_row("Poll Interval", f"{settings.bot.poll_interval_seconds}s")
    config_table.add_row("Max Capital", f"${settings.risk.max_capital_usdc:,}")
    config_table.add_row("Max Bet %", f"{settings.risk.max_bet_percent:.1%}")
    config_table.add_row("Max Exposure %", f"{settings.risk.max_exposure_percent:.1%}")
    config_table.add_row("Max Drawdown %", f"{settings.risk.max_drawdown_percent:.1%}")
    config_table.add_row("Min Edge", f"{settings.risk.min_edge_threshold:.1%}")

    console.print(config_table)

    # API status
    api_table = Table(title="API Configuration", box=box.ROUNDED)
    api_table.add_column("Service", style="cyan")
    api_table.add_column("Status", style="green")

    api_table.add_row(
        "Polymarket",
        "[green]Configured[/green]" if settings.polymarket.api_key else "[red]Not configured[/red]"
    )
    api_table.add_row(
        "Twitter",
        "[green]Configured[/green]" if settings.twitter.bearer_token.get_secret_value() else "[yellow]Optional[/yellow]"
    )
    api_table.add_row(
        "Polygon RPC",
        "[green]Configured[/green]" if settings.blockchain.polygon_rpc_url else "[red]Not configured[/red]"
    )
    api_table.add_row(
        "Telegram",
        "[green]Configured[/green]" if settings.notifications.telegram_bot_token else "[dim]Not configured[/dim]"
    )
    api_table.add_row(
        "Discord",
        "[green]Configured[/green]" if settings.notifications.discord_webhook_url else "[dim]Not configured[/dim]"
    )

    console.print(api_table)


@app.command()
def analyze(
    market_id: str = typer.Argument(..., help="Market condition ID to analyze"),
):
    """
    Analyze a specific market

    Example:
        polysentinel analyze 0x1234...
    """
    print_banner()

    console.print(f"[cyan]Analyzing market: {market_id}[/cyan]\n")

    bot = PolymarketSentinelBot()

    async def run_analysis():
        try:
            await bot.initialize()

            result = await bot.analyze_market_manual(market_id)

            if not result:
                console.print("[red]Market not found or analysis failed[/red]")
                return

            # Market info
            market = result["market"]
            console.print(Panel(
                f"[bold]{market['question']}[/bold]\n\n"
                f"YES Price: {market['yes_price']:.1%}\n"
                f"24h Volume: ${market['volume_24h']:,.0f}\n"
                f"Liquidity: ${market['liquidity']:,.0f}",
                title="Market Info",
                border_style="cyan"
            ))

            # Signal info
            signal = result["signal"]
            signal_color = {
                "BULLISH": "green",
                "BEARISH": "red",
                "NEUTRAL": "yellow"
            }.get(signal["direction"], "white")

            console.print(Panel(
                f"Direction: [{signal_color}]{signal['direction']}[/{signal_color}]\n"
                f"Strength: {signal['strength']:+.2f}\n"
                f"Confidence: {signal['confidence']:.1%}\n"
                f"Edge: {signal['edge']:+.2%}\n"
                f"Actionable: {'Yes' if signal['is_actionable'] else 'No'}",
                title="Signal Analysis",
                border_style=signal_color
            ))

        finally:
            await bot.stop()

    asyncio.run(run_analysis())


@app.command()
def markets(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of markets to show"),
    sort_by: str = typer.Option("volume", "--sort", "-s", help="Sort by: volume, liquidity, price"),
):
    """
    List active Polymarket markets

    Example:
        polysentinel markets --limit 50 --sort volume
    """
    print_banner()

    from .api import PolymarketClient

    async def fetch_markets():
        async with PolymarketClient() as client:
            markets = await client.get_markets(active_only=True, limit=limit * 2)

            # Sort
            if sort_by == "volume":
                markets.sort(key=lambda m: m.volume_24h, reverse=True)
            elif sort_by == "liquidity":
                markets.sort(key=lambda m: m.liquidity, reverse=True)
            elif sort_by == "price":
                markets.sort(key=lambda m: abs(m.yes_price - Decimal("0.5")))

            markets = markets[:limit]

            table = Table(title=f"Active Markets (sorted by {sort_by})", box=box.ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("Question", style="cyan", max_width=50)
            table.add_column("YES", justify="right", style="green")
            table.add_column("24h Vol", justify="right")
            table.add_column("Liquidity", justify="right")

            for i, m in enumerate(markets, 1):
                table.add_row(
                    str(i),
                    m.question[:50] + "..." if len(m.question) > 50 else m.question,
                    f"{m.yes_price:.1%}",
                    f"${m.volume_24h:,.0f}",
                    f"${m.liquidity:,.0f}",
                )

            console.print(table)

    asyncio.run(fetch_markets())


@app.command()
def whales(
    limit: int = typer.Option(20, "--limit", "-l", help="Number of whales to show"),
):
    """
    Show top Polymarket whale wallets

    Example:
        polysentinel whales --limit 30
    """
    print_banner()

    from .whales import WhaleTracker
    settings = get_settings()

    async def fetch_whales():
        tracker = WhaleTracker(
            rpc_url=settings.blockchain.polygon_rpc_url,
        )
        await tracker.connect()

        try:
            top_whales = tracker.get_top_whales(limit=limit)

            if not top_whales:
                console.print("[yellow]No whale data available. Run the bot to collect data.[/yellow]")
                return

            table = Table(title="Top Polymarket Whales", box=box.ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("Address", style="cyan")
            table.add_column("Label", style="green")
            table.add_column("Volume", justify="right")
            table.add_column("PnL", justify="right")
            table.add_column("Win Rate", justify="right")
            table.add_column("Trust", justify="right")

            for i, whale in enumerate(top_whales, 1):
                pnl_style = "green" if whale.total_pnl >= 0 else "red"
                table.add_row(
                    str(i),
                    f"{whale.address[:8]}...{whale.address[-6:]}",
                    whale.label or "-",
                    f"${whale.total_volume:,.0f}",
                    f"[{pnl_style}]${whale.total_pnl:+,.0f}[/{pnl_style}]",
                    f"{whale.win_rate:.1%}",
                    f"{whale.trust_score:.2f}",
                )

            console.print(table)

        finally:
            await tracker.close()

    asyncio.run(fetch_whales())


@app.command()
def backtest(
    days: int = typer.Option(7, "--days", "-d", help="Number of days to backtest"),
    capital: float = typer.Option(1000.0, "--capital", "-c", help="Starting capital"),
):
    """
    Run a backtest simulation (requires historical data)

    Example:
        polysentinel backtest --days 30 --capital 5000
    """
    print_banner()

    console.print("[yellow]Backtesting feature coming soon![/yellow]")
    console.print("This will simulate bot performance using historical market data.")


@app.command()
def init():
    """
    Initialize configuration and create .env file

    Example:
        polysentinel init
    """
    print_banner()

    import shutil
    from pathlib import Path

    env_example = Path(".env.example")
    env_file = Path(".env")

    if env_file.exists():
        if not typer.confirm(".env already exists. Overwrite?"):
            raise typer.Abort()

    if env_example.exists():
        shutil.copy(env_example, env_file)
        console.print("[green]Created .env file from .env.example[/green]")
        console.print("\n[yellow]Please edit .env and add your API keys:[/yellow]")
        console.print("  1. Polymarket API credentials")
        console.print("  2. Wallet private key")
        console.print("  3. Twitter API bearer token")
        console.print("  4. Polygon RPC URL (Alchemy or Infura)")
    else:
        console.print("[red].env.example not found. Please create it first.[/red]")


@app.command()
def version():
    """Show version information"""
    from . import __version__
    console.print(f"Polymarket Sentinel v{__version__}")


# ============ BTC ARBITRAGE COMMANDS ============

def print_btc_banner():
    """Print BTC bot banner"""
    banner = """
    ╔═══════════════════════════════════════════════════════════╗
    ║          BTC LATENCY ARBITRAGE BOT                        ║
    ║   Exploiting Binance/Polymarket Price Delays              ║
    ╚═══════════════════════════════════════════════════════════╝
    """
    console.print(banner, style="bold yellow")


@btc_app.command("run")
def btc_run(
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--live",
        help="Run in dry-run mode (no real trades)"
    ),
    capital: float = typer.Option(
        1000.0,
        "--capital", "-c",
        help="Starting capital in USDC"
    ),
    max_position: float = typer.Option(
        200.0,
        "--max-position", "-m",
        help="Maximum position size per trade"
    ),
    poll_ms: int = typer.Option(
        1000,
        "--poll-ms", "-p",
        help="Polling interval in milliseconds"
    ),
):
    """
    Run the BTC latency arbitrage bot

    This bot exploits the delay between Binance BTC price updates
    and Polymarket odds adjustments. It monitors real-time BTC
    prices and buys positions when odds are stale.

    Example:
        polysentinel btc run --dry-run --capital 500
        polysentinel btc run --live --max-position 100
    """
    print_btc_banner()

    if not dry_run:
        console.print("[bold red]WARNING: Running in LIVE mode![/bold red]")
        console.print("[red]This strategy executes trades automatically based on price latency.[/red]")
        if not typer.confirm("Are you sure you want to continue?"):
            raise typer.Abort()

    config = BTCBotConfig(
        dry_run=dry_run,
        max_capital=Decimal(str(capital)),
        max_position_size=Decimal(str(max_position)),
        poll_interval_ms=poll_ms,
    )

    console.print(f"\n[yellow]Starting BTC Arbitrage Bot...[/yellow]")
    console.print(f"  Mode: {'DRY RUN' if dry_run else '[red]LIVE[/red]'}")
    console.print(f"  Capital: ${capital:,.2f}")
    console.print(f"  Max Position: ${max_position:,.2f}")
    console.print(f"  Poll Interval: {poll_ms}ms")
    console.print()

    bot = BTCArbitrageBot(config=config)

    async def run_bot():
        try:
            await bot.start()
            while bot.is_running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
        finally:
            await bot.stop()

    asyncio.run(run_bot())


@btc_app.command("status")
def btc_status():
    """
    Show BTC strategy status and statistics

    Example:
        polysentinel btc status
    """
    print_btc_banner()

    console.print("[cyan]BTC Arbitrage Strategy Configuration[/cyan]")
    console.print()

    table = Table(title="Strategy Parameters", box=box.ROUNDED)
    table.add_column("Parameter", style="cyan")
    table.add_column("Value", style="green")

    from .strategies import BTCArbitrageConfig
    config = BTCArbitrageConfig()

    table.add_row("Min Price Move", f"{config.min_price_move_pct}%")
    table.add_row("Max Price Move", f"{config.max_price_move_pct}%")
    table.add_row("Price Window", f"{config.price_window_seconds}s")
    table.add_row("Min Odds Edge", f"${config.min_odds_edge:.2f}")
    table.add_row("Max Hold Time", f"{config.max_hold_seconds}s")
    table.add_row("Stop Loss", f"{config.stop_loss_pct:.0%}")
    table.add_row("Take Profit", f"{config.take_profit_pct:.0%}")

    console.print(table)

    console.print("\n[yellow]How it works:[/yellow]")
    console.print("  1. Monitors BTC/USDT on Binance via WebSocket (real-time)")
    console.print("  2. Detects significant price movements (>0.3%)")
    console.print("  3. Checks Polymarket BTC markets for stale odds")
    console.print("  4. Buys positions reflecting what ALREADY happened")
    console.print("  5. Exits when odds catch up (30-60 seconds)")


@btc_app.command("markets")
def btc_markets():
    """
    List BTC-related Polymarket markets

    Example:
        polysentinel btc markets
    """
    print_btc_banner()

    from .api import PolymarketClient
    from .strategies import BTCArbitrageConfig

    config = BTCArbitrageConfig()

    async def fetch_btc_markets():
        async with PolymarketClient() as client:
            all_markets = await client.get_markets(active_only=True, limit=200)

            # Filter for BTC markets
            btc_markets = []
            for m in all_markets:
                question_lower = m.question.lower()
                if any(kw in question_lower for kw in config.btc_market_keywords):
                    btc_markets.append(m)

            if not btc_markets:
                console.print("[yellow]No BTC-related markets found[/yellow]")
                return

            table = Table(title="BTC-Related Markets", box=box.ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("Question", style="cyan", max_width=55)
            table.add_column("YES", justify="right", style="green")
            table.add_column("Volume", justify="right")

            for i, m in enumerate(btc_markets, 1):
                table.add_row(
                    str(i),
                    m.question[:55] + "..." if len(m.question) > 55 else m.question,
                    f"{m.yes_price:.1%}",
                    f"${m.volume_24h:,.0f}",
                )

            console.print(table)
            console.print(f"\n[dim]Found {len(btc_markets)} BTC-related markets[/dim]")

    asyncio.run(fetch_btc_markets())


@btc_app.command("price")
def btc_price():
    """
    Show current BTC price from Binance

    Example:
        polysentinel btc price
    """
    import httpx

    async def fetch_price():
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.binance.com/api/v3/ticker/24hr",
                params={"symbol": "BTCUSDT"}
            )
            if response.status_code == 200:
                data = response.json()
                price = float(data["lastPrice"])
                change_24h = float(data["priceChangePercent"])
                high_24h = float(data["highPrice"])
                low_24h = float(data["lowPrice"])
                volume = float(data["volume"])

                change_color = "green" if change_24h >= 0 else "red"

                console.print(Panel(
                    f"[bold]${price:,.2f}[/bold]\n\n"
                    f"24h Change: [{change_color}]{change_24h:+.2f}%[/{change_color}]\n"
                    f"24h High: ${high_24h:,.2f}\n"
                    f"24h Low: ${low_24h:,.2f}\n"
                    f"24h Volume: {volume:,.2f} BTC",
                    title="BTC/USDT (Binance)",
                    border_style="yellow"
                ))
            else:
                console.print("[red]Failed to fetch BTC price[/red]")

    asyncio.run(fetch_price())


def main():
    """Main entry point"""
    app()


if __name__ == "__main__":
    main()

# Polymarket Sentinel Bot

AI-powered Polymarket trading bot with Twitter sentiment analysis and whale wallet tracking.

## Features

- **Twitter Sentiment Analysis**: Real-time sentiment scoring using VADER NLP
- **Whale Wallet Tracking**: Monitor top 100 Polymarket traders
- **Kelly Criterion Betting**: Mathematically optimal position sizing
- **Risk Management**: Drawdown limits, exposure caps, daily loss limits
- **Multi-Platform**: Runs on macOS, Linux, and Docker

## Quick Start

### Prerequisites

- Python 3.10+
- Twitter Developer Account (for sentiment analysis)
- Polygon RPC endpoint (Alchemy or Infura recommended)
- Polymarket account with API access

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/polymarket-sentinel.git
cd polymarket-sentinel

# Install dependencies
pip install -e .

# Copy and configure environment
cp .env.example .env
nano .env  # Add your API keys
```

### Configuration

Edit `.env` with your credentials:

```bash
# Polymarket
POLYMARKET_API_KEY=your_key
POLYMARKET_API_SECRET=your_secret
POLYMARKET_API_PASSPHRASE=your_passphrase

# Wallet (for trading)
WALLET_PRIVATE_KEY=your_private_key
WALLET_ADDRESS=0x...

# Twitter (for sentiment)
TWITTER_BEARER_TOKEN=your_bearer_token

# Polygon RPC (for whale tracking)
POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/YOUR_KEY

# Risk Settings
MAX_CAPITAL_USDC=1000
MAX_BET_PERCENT=0.03
MAX_DRAWDOWN_PERCENT=0.15
```

### Running the Bot

```bash
# Dry run mode (recommended for testing)
python -m src.cli run --dry-run

# Live trading (use with caution!)
python -m src.cli run --live --approval-threshold 100

# View market opportunities
python -m src.cli markets --limit 30

# Analyze specific market
python -m src.cli analyze 0x1234...
```

## Docker Deployment

### Build and Run

```bash
# Build image
docker build -t polymarket-sentinel .

# Run with docker-compose (dry-run)
docker-compose up -d

# View logs
docker-compose logs -f

# Run in live mode
DRY_RUN=false docker-compose up -d
```

### Production Deployment

```bash
# On your Linux server
git clone https://github.com/yourusername/polymarket-sentinel.git
cd polymarket-sentinel

# Configure
cp .env.example .env
nano .env

# Start
docker-compose up -d

# Monitor
docker-compose logs -f polysentinel
```

## How It Works

### Signal Generation Pipeline

```
1. Market Discovery
   └── Fetch active markets from Polymarket
   └── Filter by volume, liquidity

2. Sentiment Analysis
   └── Extract keywords from market question
   └── Search Twitter for relevant tweets
   └── Score sentiment using VADER NLP
   └── Weight by author influence

3. Whale Tracking
   └── Monitor top 100 trader wallets
   └── Track their positions and trades
   └── Calculate whale consensus

4. Signal Aggregation
   └── Combine sentiment + whale signals
   └── Weight by confidence
   └── Generate probability estimate

5. Kelly Criterion Sizing
   └── Calculate optimal bet fraction
   └── Apply fractional Kelly (25%)
   └── Cap by risk limits

6. Risk Validation
   └── Check position limits
   └── Verify exposure caps
   └── Validate drawdown status

7. Trade Execution
   └── Place limit order
   └── Log and notify
```

### Risk Management

| Limit | Default | Description |
|-------|---------|-------------|
| Max Position | 10% | Max % of capital per trade |
| Max Exposure | 30% | Max total capital at risk |
| Max Drawdown | 15% | Halt trading if exceeded |
| Max Daily Loss | 5% | Daily stop-loss |
| Min Edge | 2% | Minimum edge required |
| Min Confidence | 30% | Minimum signal confidence |

### Kelly Criterion

The bot uses fractional Kelly (25%) for position sizing:

```
Kelly Fraction = (p * b - q) / b

Where:
- p = estimated probability of winning
- q = 1 - p (probability of losing)
- b = odds (payout ratio)
```

Using fractional Kelly reduces variance while maintaining positive expected value.

## CLI Commands

```bash
# Run bot
polysentinel run [--dry-run|--live] [--interval SECONDS]

# View status
polysentinel status

# List markets
polysentinel markets [--limit N] [--sort volume|liquidity]

# Analyze market
polysentinel analyze MARKET_ID

# View whales
polysentinel whales [--limit N]

# Initialize config
polysentinel init
```

## API Keys Setup

### Twitter Developer Account

1. Go to [developer.twitter.com](https://developer.twitter.com)
2. Create a project and app
3. Generate Bearer Token
4. Add to `.env` as `TWITTER_BEARER_TOKEN`

### Polygon RPC (Alchemy)

1. Go to [alchemy.com](https://alchemy.com)
2. Create app for Polygon Mainnet
3. Copy API key
4. Add to `.env` as `POLYGON_RPC_URL`

### Polymarket API

1. Create account at [polymarket.com](https://polymarket.com)
2. Generate API credentials in settings
3. Add to `.env`

## Architecture

```
polymarket-sentinel/
├── src/
│   ├── api/           # Polymarket API client
│   ├── sentiment/     # Twitter sentiment analysis
│   ├── whales/        # Whale wallet tracking
│   ├── strategy/      # Kelly criterion, signals
│   ├── risk/          # Risk management
│   ├── utils/         # Logging, notifications
│   ├── bot.py         # Main orchestrator
│   └── cli.py         # CLI interface
├── config/
│   └── settings.py    # Configuration management
├── tests/             # Test suite
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Safety Features

1. **Dry Run Mode**: Default mode, no real trades
2. **Manual Approval**: Large trades require confirmation
3. **Position Limits**: Hard caps on bet sizes
4. **Drawdown Halt**: Auto-stop on excessive losses
5. **Rate Limiting**: Respects API limits
6. **Logging**: Full audit trail of all decisions

## Notifications

Configure optional alerts:

```bash
# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Discord
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

## Disclaimer

**This software is for educational purposes only.**

- Trading prediction markets involves significant risk
- Past performance does not guarantee future results
- Never trade with money you cannot afford to lose
- The authors are not responsible for any financial losses

## License

MIT License - see LICENSE file for details.

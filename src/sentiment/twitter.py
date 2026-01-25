"""
Twitter/X Sentiment Analysis Module
Fetches and analyzes tweets related to Polymarket events
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from decimal import Decimal

import httpx
import structlog
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = structlog.get_logger()

# Twitter API v2 endpoints
TWITTER_API_BASE = "https://api.twitter.com/2"


@dataclass
class TweetData:
    """Parsed tweet data"""
    id: str
    text: str
    author_id: str
    author_username: str
    author_followers: int
    created_at: datetime
    retweet_count: int
    like_count: int
    reply_count: int
    quote_count: int
    is_verified: bool = False
    is_blue_verified: bool = False

    @property
    def engagement_score(self) -> int:
        """Calculate engagement score"""
        return (
            self.retweet_count * 3 +
            self.like_count +
            self.reply_count * 2 +
            self.quote_count * 2
        )

    @property
    def influence_weight(self) -> float:
        """
        Calculate influence weight based on followers and verification
        Used to weight sentiment scores
        """
        base = min(self.author_followers / 100000, 10)  # Cap at 10x
        if self.is_verified:
            base *= 1.5
        return max(base, 0.1)


@dataclass
class SentimentResult:
    """Result of sentiment analysis"""
    compound: float  # Overall sentiment (-1 to 1)
    positive: float
    negative: float
    neutral: float
    confidence: float  # How confident in the analysis
    weighted_score: float  # Influence-weighted score

    @property
    def is_bullish(self) -> bool:
        return self.compound > 0.05

    @property
    def is_bearish(self) -> bool:
        return self.compound < -0.05

    @property
    def signal_strength(self) -> str:
        """Get signal strength descriptor"""
        abs_score = abs(self.compound)
        if abs_score > 0.6:
            return "STRONG"
        elif abs_score > 0.3:
            return "MODERATE"
        elif abs_score > 0.1:
            return "WEAK"
        return "NEUTRAL"


class TwitterSentimentAnalyzer:
    """
    Analyzes Twitter sentiment for Polymarket market predictions

    Features:
    - Real-time tweet fetching
    - VADER sentiment analysis
    - Influence-weighted scoring
    - Keyword extraction and matching
    - Caching for rate limit management
    """

    def __init__(
        self,
        bearer_token: str,
        api_key: str = "",
        api_secret: str = "",
        access_token: str = "",
        access_secret: str = "",
    ):
        self.bearer_token = bearer_token
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_secret = access_secret

        # Initialize VADER sentiment analyzer
        self.vader = SentimentIntensityAnalyzer()

        # Cache for rate limiting (15 min TTL matching Twitter limits)
        self._cache = TTLCache(maxsize=1000, ttl=900)

        # HTTP client
        self._client: Optional[httpx.AsyncClient] = None

        # Influential accounts to track (can be extended)
        self.influential_accounts = {
            "polyaboronin",
            "polymarket",
            "dustinfaulkner",
            "NateSilver538",
            "pmtraders",
            "SpikeCohen",
            "ElonMusk",
            "realDonaldTrump",
            "POTUS",
            "JoeBiden",
            "KamalaHarris",
        }

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        """Initialize HTTP client"""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "PolymarketSentinel/1.0",
            }
        )
        logger.info("twitter_client_connected")

    async def close(self):
        """Close HTTP client"""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Client not connected")
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def _request(self, endpoint: str, params: Dict) -> Optional[Dict]:
        """Make Twitter API request"""
        client = self._get_client()

        # Check cache
        cache_key = f"{endpoint}:{str(sorted(params.items()))}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{TWITTER_API_BASE}{endpoint}"
        response = await client.get(url, params=params)

        if response.status_code == 429:
            # Rate limited - extract wait time
            reset_time = int(response.headers.get("x-rate-limit-reset", 0))
            wait_seconds = max(reset_time - int(datetime.utcnow().timestamp()), 60)
            logger.warning("twitter_rate_limited", wait_seconds=wait_seconds)
            await asyncio.sleep(min(wait_seconds, 300))  # Max 5 min wait
            raise Exception("Rate limited, retrying...")

        if response.status_code != 200:
            logger.error(
                "twitter_api_error",
                status=response.status_code,
                body=response.text[:500]
            )
            return None

        data = response.json()
        self._cache[cache_key] = data
        return data

    def _extract_keywords(self, market_question: str) -> List[str]:
        """
        Extract searchable keywords from market question

        Example: "Will Bitcoin reach $100k by end of 2024?" ->
                 ["Bitcoin", "$100k", "BTC", "crypto"]
        """
        # Clean and tokenize
        text = market_question.lower()

        # Common market topic mappings
        keyword_map = {
            "bitcoin": ["bitcoin", "btc", "$btc", "crypto"],
            "ethereum": ["ethereum", "eth", "$eth", "crypto"],
            "trump": ["trump", "donald trump", "maga", "gop"],
            "biden": ["biden", "joe biden", "potus", "democrat"],
            "harris": ["harris", "kamala", "kamala harris"],
            "election": ["election", "2024 election", "presidential", "vote"],
            "fed": ["fed", "federal reserve", "interest rate", "powell"],
            "inflation": ["inflation", "cpi", "prices"],
            "ukraine": ["ukraine", "zelensky", "russia", "putin"],
            "israel": ["israel", "gaza", "netanyahu", "hamas"],
            "openai": ["openai", "chatgpt", "sam altman", "gpt"],
            "spacex": ["spacex", "starship", "elon musk", "rocket"],
            "tesla": ["tesla", "tsla", "$tsla", "elon musk"],
            "super bowl": ["super bowl", "nfl", "chiefs", "football"],
            "oscar": ["oscars", "academy awards", "hollywood"],
        }

        keywords = []

        # Extract based on keyword map
        for key, related in keyword_map.items():
            if key in text:
                keywords.extend(related)

        # Extract proper nouns (capitalized words in original)
        words = market_question.split()
        for word in words:
            if word[0].isupper() and len(word) > 2:
                clean_word = re.sub(r'[^\w]', '', word)
                if clean_word and clean_word.lower() not in ["will", "the", "and", "for"]:
                    keywords.append(clean_word)

        # Extract numbers with context (prices, dates)
        numbers = re.findall(r'\$?[\d,]+[kKmMbB]?', market_question)
        keywords.extend(numbers)

        # Deduplicate while preserving order
        seen = set()
        unique_keywords = []
        for kw in keywords:
            if kw.lower() not in seen:
                seen.add(kw.lower())
                unique_keywords.append(kw)

        return unique_keywords[:10]  # Limit to 10 keywords

    async def search_tweets(
        self,
        query: str,
        max_results: int = 100,
        hours_back: int = 24,
    ) -> List[TweetData]:
        """
        Search for tweets matching query

        Args:
            query: Search query
            max_results: Maximum tweets to return (10-100)
            hours_back: How many hours back to search

        Returns:
            List of TweetData objects
        """
        start_time = (datetime.utcnow() - timedelta(hours=hours_back)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Build query with filters
        full_query = f"{query} -is:retweet lang:en"

        params = {
            "query": full_query,
            "max_results": min(max_results, 100),
            "start_time": start_time,
            "tweet.fields": "created_at,public_metrics,author_id",
            "user.fields": "username,public_metrics,verified,verified_type",
            "expansions": "author_id",
        }

        data = await self._request("/tweets/search/recent", params)

        if not data or "data" not in data:
            return []

        # Build user lookup
        users = {}
        if "includes" in data and "users" in data["includes"]:
            for user in data["includes"]["users"]:
                users[user["id"]] = user

        tweets = []
        for tweet in data["data"]:
            author = users.get(tweet["author_id"], {})
            metrics = tweet.get("public_metrics", {})
            author_metrics = author.get("public_metrics", {})

            tweets.append(TweetData(
                id=tweet["id"],
                text=tweet["text"],
                author_id=tweet["author_id"],
                author_username=author.get("username", "unknown"),
                author_followers=author_metrics.get("followers_count", 0),
                created_at=datetime.fromisoformat(
                    tweet["created_at"].replace("Z", "+00:00")
                ),
                retweet_count=metrics.get("retweet_count", 0),
                like_count=metrics.get("like_count", 0),
                reply_count=metrics.get("reply_count", 0),
                quote_count=metrics.get("quote_count", 0),
                is_verified=author.get("verified", False),
                is_blue_verified=author.get("verified_type") == "blue",
            ))

        return tweets

    async def get_influential_tweets(
        self,
        query: str,
        hours_back: int = 48,
    ) -> List[TweetData]:
        """Get tweets from influential accounts about a topic"""
        all_tweets = []

        for username in list(self.influential_accounts)[:5]:  # Limit to avoid rate limits
            try:
                user_query = f"from:{username} {query}"
                tweets = await self.search_tweets(user_query, max_results=10, hours_back=hours_back)
                all_tweets.extend(tweets)
                await asyncio.sleep(0.5)  # Rate limit protection
            except Exception as e:
                logger.warning("influential_tweet_fetch_error", username=username, error=str(e))

        return all_tweets

    def analyze_sentiment(self, text: str) -> Dict[str, float]:
        """
        Analyze sentiment of a single text using VADER

        Returns:
            Dict with compound, pos, neg, neu scores
        """
        # Clean text
        clean_text = re.sub(r'http\S+', '', text)  # Remove URLs
        clean_text = re.sub(r'@\w+', '', clean_text)  # Remove mentions
        clean_text = re.sub(r'#(\w+)', r'\1', clean_text)  # Keep hashtag text

        scores = self.vader.polarity_scores(clean_text)
        return scores

    async def analyze_market_sentiment(
        self,
        market_question: str,
        custom_keywords: Optional[List[str]] = None,
        hours_back: int = 24,
        min_tweets: int = 10,
    ) -> SentimentResult:
        """
        Analyze Twitter sentiment for a market question

        Args:
            market_question: The market question to analyze
            custom_keywords: Additional keywords to search
            hours_back: How far back to search
            min_tweets: Minimum tweets needed for valid analysis

        Returns:
            SentimentResult with aggregated scores
        """
        # Extract keywords from question
        keywords = self._extract_keywords(market_question)
        if custom_keywords:
            keywords.extend(custom_keywords)

        if not keywords:
            # Fallback to first few words
            keywords = market_question.split()[:3]

        # Build search query
        query = " OR ".join(keywords[:5])  # Limit query length

        logger.info(
            "analyzing_market_sentiment",
            market=market_question[:50],
            keywords=keywords[:5]
        )

        # Fetch tweets
        tweets = await self.search_tweets(query, max_results=100, hours_back=hours_back)

        # Also get influential tweets
        influential = await self.get_influential_tweets(query, hours_back=hours_back * 2)
        tweets.extend(influential)

        if len(tweets) < min_tweets:
            logger.warning(
                "insufficient_tweets",
                found=len(tweets),
                required=min_tweets
            )
            # Return neutral with low confidence
            return SentimentResult(
                compound=0.0,
                positive=0.33,
                negative=0.33,
                neutral=0.34,
                confidence=0.1,
                weighted_score=0.0,
            )

        # Analyze each tweet
        total_weight = 0.0
        weighted_compound = 0.0
        all_positive = 0.0
        all_negative = 0.0
        all_neutral = 0.0

        for tweet in tweets:
            sentiment = self.analyze_sentiment(tweet.text)
            weight = tweet.influence_weight

            weighted_compound += sentiment["compound"] * weight
            all_positive += sentiment["pos"]
            all_negative += sentiment["neg"]
            all_neutral += sentiment["neu"]
            total_weight += weight

        # Calculate averages
        n = len(tweets)
        avg_compound = weighted_compound / total_weight if total_weight > 0 else 0

        # Calculate confidence based on sample size and agreement
        sample_confidence = min(n / 50, 1.0)  # Max confidence at 50 tweets

        # Calculate agreement (low std dev = high agreement)
        compounds = [self.analyze_sentiment(t.text)["compound"] for t in tweets]
        if compounds:
            import numpy as np
            std_dev = np.std(compounds)
            agreement_confidence = max(0, 1 - std_dev)
        else:
            agreement_confidence = 0

        confidence = (sample_confidence * 0.6) + (agreement_confidence * 0.4)

        return SentimentResult(
            compound=avg_compound,
            positive=all_positive / n,
            negative=all_negative / n,
            neutral=all_neutral / n,
            confidence=confidence,
            weighted_score=avg_compound * confidence,
        )

    async def get_sentiment_trend(
        self,
        market_question: str,
        intervals: int = 4,
        hours_per_interval: int = 6,
    ) -> List[Tuple[datetime, SentimentResult]]:
        """
        Get sentiment trend over time

        Args:
            market_question: Market question
            intervals: Number of time intervals
            hours_per_interval: Hours per interval

        Returns:
            List of (timestamp, sentiment) tuples
        """
        trends = []
        keywords = self._extract_keywords(market_question)
        query = " OR ".join(keywords[:5])

        for i in range(intervals):
            hours_back = (i + 1) * hours_per_interval
            hours_start = i * hours_per_interval

            # This is simplified - Twitter API doesn't easily support windowed queries
            # In production, you'd want to store historical data
            try:
                tweets = await self.search_tweets(
                    query,
                    max_results=50,
                    hours_back=hours_back
                )

                # Filter to just this interval
                cutoff_end = datetime.utcnow() - timedelta(hours=hours_start)
                cutoff_start = datetime.utcnow() - timedelta(hours=hours_back)

                interval_tweets = [
                    t for t in tweets
                    if cutoff_start <= t.created_at <= cutoff_end
                ]

                if interval_tweets:
                    # Analyze this batch
                    total_compound = sum(
                        self.analyze_sentiment(t.text)["compound"]
                        for t in interval_tweets
                    )
                    avg_compound = total_compound / len(interval_tweets)

                    trends.append((
                        cutoff_start,
                        SentimentResult(
                            compound=avg_compound,
                            positive=0,
                            negative=0,
                            neutral=0,
                            confidence=min(len(interval_tweets) / 20, 1.0),
                            weighted_score=avg_compound,
                        )
                    ))

            except Exception as e:
                logger.warning("trend_interval_error", interval=i, error=str(e))

        return sorted(trends, key=lambda x: x[0])

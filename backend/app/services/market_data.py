"""Market Data Service for TokenIQ.

Fetches, validates, deduplicates, and monitors OHLCV candle streams.
Rejects stale or out-of-order data before strategy evaluation.
"""

import time
from typing import List, Optional, Tuple
import pandas as pd
from app.config import settings
from app.core.logger import logger
from app.core.state import runtime_state
from app.exchange.client import exchange_client
from app.exchange.models import Candle


class MarketDataService:
    """Processes, validates, and normalizes market data."""

    def __init__(self):
        self.instrument = settings.DEFAULT_INSTRUMENT
        self.timeframe = settings.DEFAULT_TIMEFRAME
        self.max_age_seconds = settings.MAX_DATA_AGE_SECONDS
        self._last_processed_candle_time: int = 0

    async def fetch_and_validate_candles(
        self,
        symbol: Optional[str] = None,
        resolution: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[bool, pd.DataFrame, str]:
        """Fetches historical candles, validates timestamps, and returns a sanitized DataFrame.

        Returns: (is_valid, dataframe, error_message)
        """
        sym = symbol or self.instrument
        res = resolution or self.timeframe

        try:
            candles: List[Candle] = await exchange_client.get_candles(
                symbol=sym,
                resolution=res,
                limit=limit,
            )
        except Exception as e:
            logger.error(f"Failed to fetch market data for {sym}: {e}")
            runtime_state.update_market_status(valid=False)
            return False, pd.DataFrame(), f"Fetch failed: {str(e)}"

        min_required = min(limit, 10)
        if not candles or len(candles) < min_required:
            msg = f"Insufficient candle history returned ({len(candles)} candles, minimum required: {min_required})"
            logger.warning(msg)
            runtime_state.update_market_status(valid=False)
            return False, pd.DataFrame(), msg

        # Convert to pandas DataFrame
        df = pd.DataFrame([c.model_dump() for c in candles])
        df = df.sort_values("time").reset_index(drop=True)

        # Validate monotonic timestamps
        if not df["time"].is_monotonic_increasing:
            df = df.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

        latest_candle = df.iloc[-1]
        latest_ts = int(latest_candle["time"])
        current_time = int(time.time())
        age = current_time - latest_ts

        # Stale data check
        # For live testnet, if the latest candle is older than max_age_seconds + 1 interval, reject
        interval_seconds = 300 if res == "5m" else 60
        threshold = self.max_age_seconds + interval_seconds

        if age > threshold and settings.USE_LIVE_TESTNET:
            msg = f"Market data is stale. Latest candle time: {latest_ts}, age: {age}s exceeds limit {threshold}s"
            logger.warning(msg)
            runtime_state.update_market_status(valid=False, price=float(latest_candle["close"]), candle_ts=latest_ts)
            return False, df, msg

        # Data is valid
        runtime_state.update_market_status(
            valid=True,
            price=float(latest_candle["close"]),
            candle_ts=latest_ts,
        )
        return True, df, "OK"

    def is_new_completed_candle(self, candle_time: int) -> bool:
        """Deduplication guard: returns True only if this is a newly closed candle."""
        if candle_time > self._last_processed_candle_time:
            self._last_processed_candle_time = candle_time
            return True
        return False


market_data_service = MarketDataService()

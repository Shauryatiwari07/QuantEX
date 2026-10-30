"""Delta Exchange Testnet API Client.

Implements official REST API endpoints for Delta Exchange Testnet, including
HMAC-SHA256 authenticated requests, product validation, ticker/candle streaming,
wallet balance and position checks, order submission, and order status polling.
"""

import asyncio
import json
import random
import time
from typing import Dict, Any, List, Optional, Tuple, Union
import httpx
from app.config import settings
from app.core.logger import logger
from app.core.exceptions import ExchangeConnectionError, AuthenticationError
from app.core.security import decrypt_secret
from app.exchange.auth import build_auth_headers, format_compact_payload
from app.exchange.models import (
    Candle,
    ProductInfo,
    ExchangeOrderPayload,
    ExchangeOrderResult,
    ExchangePosition,
    WalletBalance,
)


class DeltaExchangeClient:
    """Client for Delta Exchange Testnet."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        use_live: Optional[bool] = None,
    ):
        self.api_key = api_key if api_key is not None else settings.DELTA_API_KEY
        self.api_secret = api_secret if api_secret is not None else settings.DELTA_API_SECRET
        self.base_url = (base_url or settings.DELTA_BASE_URL).rstrip("/")
        self.use_live = use_live if use_live is not None else settings.USE_LIVE_TESTNET
        self.timeout = httpx.Timeout(10.0, connect=5.0)

        # Simulation fallback state (explicitly labeled for offline demos)
        self._sim_price = 65000.0
        self._sim_balance = 10000.0
        self._sim_orders: Dict[str, Dict[str, Any]] = {}
        self._sim_positions: Dict[str, Dict[str, Any]] = {
            settings.DEFAULT_INSTRUMENT: {
                "product_symbol": settings.DEFAULT_INSTRUMENT,
                "size": 0.0,
                "entry_price": 0.0,
                "mark_price": 65000.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            }
        }

    @classmethod
    def from_account_connection(cls, account) -> "DeltaExchangeClient":
        """Instantiates client using credentials from a decrypted AccountConnection."""
        api_key = decrypt_secret(account.api_key_encrypted)
        api_secret = decrypt_secret(account.api_secret_encrypted)
        return cls(
            api_key=api_key,
            api_secret=api_secret,
            base_url=account.base_url,
            use_live=True,  # Account connections explicitly use the configured live testnet
        )

    async def _send_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """Executes an authenticated HTTP request against Delta Exchange."""
        url = f"{self.base_url}{path}"
        query_str = ""
        if params:
            import urllib.parse
            query_str = urllib.parse.urlencode(params)

        payload_str = format_compact_payload(json_data) if json_data else ""

        headers = {}
        if auth_required:
            if not self.api_key or not self.api_secret:
                raise AuthenticationError("Delta Exchange API key or secret is not configured.")
            headers = build_auth_headers(
                api_key=self.api_key,
                api_secret=self.api_secret,
                method=method,
                path=path,
                query_string=query_str,
                payload=payload_str,
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    content=payload_str if payload_str else None,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                logger.error(f"Network error communicating with Delta Exchange ({url}): {exc}")
                raise ExchangeConnectionError(f"Connection failure: {str(exc)}")

            if response.status_code == 401:
                logger.error("Authentication rejected by Delta Exchange (HTTP 401)")
                raise AuthenticationError("Invalid, unauthorized, or expired Delta Exchange API credentials")
            elif response.status_code == 429:
                logger.warning("Delta Exchange rate limit reached (HTTP 429)")
                raise ExchangeConnectionError("Delta Exchange rate limit reached. Backing off.")
            elif response.status_code >= 500:
                logger.error(f"Delta Exchange server error: {response.status_code}")
                raise ExchangeConnectionError(f"Server error {response.status_code} from exchange")

            try:
                data = response.json()
            except Exception as e:
                raise ExchangeConnectionError(f"Invalid JSON returned from exchange: {e}")

            # Check Delta API error payload format
            if isinstance(data, dict) and not data.get("success", True) and "error" in data:
                err_msg = data.get("error", {}).get("message", "Exchange returned error")
                err_code = data.get("error", {}).get("code", "")
                if "auth" in err_msg.lower() or "signature" in err_msg.lower() or err_code == "AUTHENTICATION_FAILED":
                    raise AuthenticationError(f"Delta Exchange Authentication Error: {err_msg}")

            return data

    async def ping_public_connectivity(self) -> bool:
        """Verifies reachability of Delta Exchange public API."""
        try:
            res = await self._send_request("GET", "/v2/products", auth_required=False)
            return bool(res.get("success", False) or "result" in res)
        except Exception as e:
            logger.warning(f"Public API connectivity check failed: {e}")
            return False

    async def validate_credentials(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Tests private authentication against Delta Exchange by querying wallet balances."""
        if not self.api_key or not self.api_secret:
            return False, "Missing API Key or API Secret", {}

        urls_to_test = [self.base_url]
        if settings.DELTA_INDIA_BASE_URL not in urls_to_test:
            urls_to_test.append(settings.DELTA_INDIA_BASE_URL)
        if settings.DELTA_TESTNET_BASE_URL not in urls_to_test:
            urls_to_test.append(settings.DELTA_TESTNET_BASE_URL)

        last_error = ""
        for url in urls_to_test:
            original_url = self.base_url
            self.base_url = url
            try:
                res = await self._send_request("GET", "/v2/wallet/balances", auth_required=True)
                result = res.get("result", [])
                env_label = "Delta Exchange India (Live)" if "api.india" in url else "Delta Testnet"
                return True, f"Authentication successful on {env_label}", {
                    "base_url": url,
                    "environment": "mainnet" if "api.india" in url else "testnet",
                    "balances_count": len(result),
                    "balances": result,
                }
            except AuthenticationError as e:
                last_error = f"Authentication failed on Delta ({url}): {str(e)}"
            except ExchangeConnectionError as e:
                last_error = f"Exchange unreachable ({url}): {str(e)}"
            except Exception as e:
                last_error = f"Validation error ({url}): {str(e)}"
            finally:
                self.base_url = original_url

        return False, last_error or "Authentication failed against Delta Exchange. Check API Key, Secret, and IP Whitelist.", {}

    async def ping_connectivity(self) -> bool:
        """Checks overall connectivity (public and private if configured)."""
        if not self.use_live:
            return True
        return await self.ping_public_connectivity()

    async def get_products(self) -> List[ProductInfo]:
        """Retrieves list of active traded products on the exchange."""
        if not self.use_live:
            return [
                ProductInfo(
                    id=1,
                    symbol="BTCUSD",
                    contract_type="perpetual_futures",
                    contract_value=1.0,
                    tick_size=0.5,
                )
            ]

        res = await self._send_request("GET", "/v2/products", auth_required=False)
        products = []
        for p in res.get("result", []):
            products.append(
                ProductInfo(
                    id=int(p.get("id", 0)),
                    symbol=p.get("symbol", ""),
                    contract_type=p.get("contract_type", ""),
                    contract_value=float(p.get("contract_value", 1.0)),
                    tick_size=float(p.get("tick_size", 0.5)),
                )
            )
        return products

    async def validate_instrument(self, symbol: str) -> Tuple[bool, Optional[ProductInfo]]:
        """Validates if configured symbol is supported on the exchange."""
        products = await self.get_products()
        for p in products:
            if p.symbol.upper() == symbol.upper():
                return True, p
        return False, None

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Retrieves real-time 24hr ticker for an instrument from Delta Exchange."""
        try:
            res = await self._send_request("GET", f"/v2/tickers/{symbol}", auth_required=False)
            result = res.get("result", {})
            if result:
                return result
        except Exception as e:
            logger.warning(f"Error fetching ticker for {symbol} from Delta: {e}")

        return {
            "symbol": symbol,
            "mark_price": self._sim_price,
            "close": self._sim_price,
            "volume": 0.0,
            "error": "Exchange ticker fetch unavailable",
        }

    async def get_wallet_balances(self) -> List[WalletBalance]:
        """Retrieves authoritative user wallet balances."""
        if not self.use_live:
            return [
                WalletBalance(
                    asset="USDT",
                    balance=self._sim_balance,
                    available_balance=self._sim_balance,
                )
            ]

        res = await self._send_request("GET", "/v2/wallet/balances", auth_required=True)
        balances = []
        for item in res.get("result", []):
            balances.append(
                WalletBalance(
                    asset=item.get("asset_symbol", "USDT"),
                    balance=float(item.get("balance", 0.0)),
                    available_balance=float(item.get("available_balance", 0.0)),
                )
            )
        return balances

    async def get_candles(
        self,
        symbol: str = "BTCUSD",
        resolution: str = "5m",
        limit: int = 100,
    ) -> List[Candle]:
        """Retrieves real historical OHLCV candles from Delta Exchange."""
        end_time = int(time.time())
        res_map = {
            "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
            "1h": "60", "4h": "240", "1d": "D", "1D": "D",
            "1": "1", "3": "3", "5": "5", "15": "15", "30": "30",
            "60": "60", "240": "240", "D": "D"
        }
        res_code = res_map.get(str(resolution), "5")
        
        interval_secs = {
            "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
            "60": 3600, "240": 14400, "D": 86400
        }
        step_seconds = interval_secs.get(res_code, 300)
        start_time = end_time - (limit * step_seconds)

        # Primary official endpoint: /v2/chart/history
        chart_params = {
            "symbol": symbol,
            "resolution": res_code,
            "from": start_time,
            "to": end_time,
        }

        try:
            res = await self._send_request("GET", "/v2/chart/history", params=chart_params, auth_required=False)
            candles: List[Candle] = []
            result_data = res.get("result", {})
            if isinstance(result_data, dict) and "t" in result_data and result_data["t"]:
                t_list = result_data.get("t", [])
                o_list = result_data.get("o", [])
                h_list = result_data.get("h", [])
                l_list = result_data.get("l", [])
                c_list = result_data.get("c", [])
                v_list = result_data.get("v", [])
                
                for i in range(len(t_list)):
                    candles.append(
                        Candle(
                            time=int(t_list[i]),
                            open=float(o_list[i]),
                            high=float(h_list[i]),
                            low=float(l_list[i]),
                            close=float(c_list[i]),
                            volume=float(v_list[i]) if i < len(v_list) else 0.0,
                        )
                    )
            elif isinstance(result_data, list):
                for c in result_data:
                    if isinstance(c, dict):
                        candles.append(
                            Candle(
                                time=int(c.get("time", 0)),
                                open=float(c.get("open", 0.0)),
                                high=float(c.get("high", 0.0)),
                                low=float(c.get("low", 0.0)),
                                close=float(c.get("close", 0.0)),
                                volume=float(c.get("volume", 0.0)),
                            )
                        )
                    elif isinstance(c, list) and len(c) >= 6:
                        candles.append(
                            Candle(
                                time=int(c[0]),
                                open=float(c[1]),
                                high=float(c[2]),
                                low=float(c[3]),
                                close=float(c[4]),
                                volume=float(c[5]),
                            )
                        )
            if candles:
                candles.sort(key=lambda x: x.time)
                self._sim_price = candles[-1].close
                return candles
        except Exception as e:
            logger.warning(f"Failed to fetch live Delta candles for {symbol} on /v2/chart/history: {e}")

        # Fallback to Delta India public REST if client URL was testnet
        if self.base_url != settings.DELTA_INDIA_BASE_URL:
            try:
                url = f"{settings.DELTA_INDIA_BASE_URL}/v2/chart/history"
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, params=chart_params)
                    if resp.status_code == 200:
                        data = resp.json().get("result", {})
                        if isinstance(data, dict) and "t" in data and data["t"]:
                            candles = []
                            t_list = data["t"]
                            o_list = data["o"]
                            h_list = data["h"]
                            l_list = data["l"]
                            c_list = data["c"]
                            v_list = data["v"]
                            for i in range(len(t_list)):
                                candles.append(Candle(
                                    time=int(t_list[i]),
                                    open=float(o_list[i]),
                                    high=float(h_list[i]),
                                    low=float(l_list[i]),
                                    close=float(c_list[i]),
                                    volume=float(v_list[i]) if i < len(v_list) else 0.0,
                                ))
                            candles.sort(key=lambda x: x.time)
                            if candles:
                                self._sim_price = candles[-1].close
                                return candles
            except Exception as e2:
                logger.warning(f"Fallback candle fetch also failed: {e2}")

        return []

    async def place_order(self, payload: ExchangeOrderPayload) -> ExchangeOrderResult:
        """Submits an order to Delta Exchange."""
        if not self.use_live:
            order_id = f"sim_{int(time.time() * 1000)}"
            fill_price = self._sim_price
            actual_fill = fill_price + random.uniform(-0.5, 0.5)

            self._sim_orders[order_id] = {
                "order_id": order_id,
                "client_order_id": payload.client_order_id,
                "symbol": payload.product_symbol,
                "side": payload.side,
                "size": payload.size,
                "fill_price": actual_fill,
                "status": "filled",
            }

            pos = self._sim_positions.get(payload.product_symbol, {
                "product_symbol": payload.product_symbol,
                "size": 0.0,
                "entry_price": 0.0,
                "mark_price": actual_fill,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
            })

            curr_size = pos["size"]
            new_size = curr_size + payload.size if payload.side.lower() == "buy" else curr_size - payload.size
            pos["size"] = round(new_size, 4)
            pos["entry_price"] = actual_fill
            pos["mark_price"] = actual_fill
            self._sim_positions[payload.product_symbol] = pos

            return ExchangeOrderResult(
                success=True,
                order_id=order_id,
                client_order_id=payload.client_order_id,
                status="filled",
                fill_price=round(actual_fill, 2),
                size=payload.size,
                side=payload.side,
            )

        # Live Order Submission
        body = {
            "product_symbol": payload.product_symbol,
            "size": payload.size,
            "side": payload.side.lower(),
            "order_type": payload.order_type,
        }
        if payload.client_order_id:
            body["client_order_id"] = payload.client_order_id
        if payload.limit_price:
            body["limit_price"] = str(payload.limit_price)

        res = await self._send_request("POST", "/v2/orders", json_data=body, auth_required=True)
        result = res.get("result", {})
        status = result.get("state", "open")
        order_id = str(result.get("id", ""))
        avg_price = float(result.get("avg_fill_price", 0.0) or 0.0)

        return ExchangeOrderResult(
            success=bool(order_id),
            order_id=order_id,
            client_order_id=payload.client_order_id,
            status=status,
            fill_price=avg_price if avg_price > 0 else None,
            size=payload.size,
            side=payload.side,
            error_message=res.get("error", {}).get("message"),
        )

    async def get_order_status(self, order_id: str) -> Optional[ExchangeOrderResult]:
        """Polls status of a specific order for reconciliation."""
        if not self.use_live:
            sim = self._sim_orders.get(order_id)
            if not sim:
                return None
            return ExchangeOrderResult(
                success=True,
                order_id=order_id,
                client_order_id=sim.get("client_order_id"),
                status=sim.get("status", "filled"),
                fill_price=sim.get("fill_price"),
                size=sim.get("size", 0.0),
                side=sim.get("side", ""),
            )

        res = await self._send_request("GET", f"/v2/orders/{order_id}", auth_required=True)
        result = res.get("result", {})
        if not result:
            return None

        status = result.get("state", "open")
        avg_price = float(result.get("avg_fill_price", 0.0) or 0.0)
        return ExchangeOrderResult(
            success=True,
            order_id=str(result.get("id")),
            client_order_id=result.get("client_order_id"),
            status=status,
            fill_price=avg_price if avg_price > 0 else None,
            size=float(result.get("size", 0.0)),
            side=result.get("side", ""),
        )

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancels an open order."""
        if not self.use_live:
            if order_id in self._sim_orders:
                self._sim_orders[order_id]["status"] = "cancelled"
            return {"success": True, "order_id": order_id, "status": "cancelled"}

        res = await self._send_request("DELETE", f"/v2/orders/{order_id}", auth_required=True)
        return res

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Retrieves active open orders."""
        if not self.use_live:
            return [o for o in self._sim_orders.values() if o.get("status") in ("open", "pending")]

        res = await self._send_request("GET", "/v2/orders", params={"state": "open"}, auth_required=True)
        return res.get("result", [])

    async def get_positions(self) -> List[ExchangePosition]:
        """Retrieves authoritative positions from exchange."""
        if not self.use_live:
            res = []
            for k, pos in self._sim_positions.items():
                size = float(pos.get("size", 0.0))
                if abs(size) < 1e-6:
                    continue
                entry = float(pos.get("entry_price", 0.0))
                mark = float(self._sim_price or entry)
                if size > 0:
                    u_pnl = (mark - entry) * size
                else:
                    u_pnl = (entry - mark) * abs(size)
                res.append(
                    ExchangePosition(
                        product_symbol=pos["product_symbol"],
                        size=size,
                        entry_price=entry,
                        mark_price=mark,
                        unrealized_pnl=round(u_pnl, 2),
                        realized_pnl=float(pos.get("realized_pnl", 0.0)),
                    )
                )
            return res

        res = await self._send_request("GET", "/v2/positions", auth_required=True)
        positions = []
        for p in res.get("result", []):
            positions.append(
                ExchangePosition(
                    product_symbol=p.get("product_symbol", ""),
                    size=float(p.get("size", 0.0)),
                    entry_price=float(p.get("entry_price", 0.0)),
                    mark_price=float(p.get("mark_price", 0.0)),
                    unrealized_pnl=float(p.get("unrealized_pnl", 0.0)),
                    realized_pnl=float(p.get("realized_pnl", 0.0)),
                    liquidation_price=float(p.get("liquidation_price")) if p.get("liquidation_price") else None,
                )
            )
        return positions

    async def get_order_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves order history from Delta Exchange."""
        if not self.use_live:
            return list(self._sim_orders.values())
        try:
            res = await self._send_request("GET", "/v2/orders/history", params={"limit": limit}, auth_required=True)
            return res.get("result", [])
        except Exception as e:
            logger.warning(f"Error fetching order history from Delta: {e}")
            return []

    async def get_fills(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Retrieves trade execution fills and realized P&L from Delta Exchange."""
        if not self.use_live:
            return []
        try:
            res = await self._send_request("GET", "/v2/fills", params={"limit": limit}, auth_required=True)
            return res.get("result", [])
        except Exception as e:
            logger.warning(f"Error fetching fills from Delta: {e}")
            return []


# Default global client (uses environment credentials if provided)
exchange_client = DeltaExchangeClient()

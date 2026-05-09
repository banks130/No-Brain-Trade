import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Callable, Optional, Awaitable
import aiohttp
import websockets
import json
from datetime import datetime
from config import (PUMP_PORTAL_WS, SPIKE_THRESHOLD_PCT, MCAP_MIN_SOL,
                    MCAP_MAX_SOL, SCAN_INTERVAL_SEC)
from utils import logger

@dataclass
class TokenState:
    mint: str
    name: str = "?"
    symbol: str = "?"
    initial_mcap: float = 0.0
    peak_mcap: float = 0.0
    current_mcap: float = 0.0
    unique_wallets: set = field(default_factory=set)
    buy_count: int = 0
    sell_count: int = 0
    total_buy_sol: float = 0.0
    total_sell_sol: float = 0.0
    first_seen: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    alerted_spike: bool = False
    alerted_signal: bool = False

    @property
    def spike_pct(self) -> float:
        if self.initial_mcap <= 0:
            return 0.0
        return (self.peak_mcap - self.initial_mcap) / self.initial_mcap * 100

    @property
    def buy_ratio(self) -> float:
        total = self.buy_count + self.sell_count
        return self.buy_count / total if total > 0 else 0.0

    @property
    def net_sol_flow(self) -> float:
        return self.total_buy_sol - self.total_sell_sol

    @property
    def unique_wallet_count(self) -> int:
        return len(self.unique_wallets)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.first_seen

class TrendingDetector:
    def __init__(self):
        self._tokens: Dict[str, TokenState] = {}
        self._ws = None
        self._spike_callbacks: List[Callable[[TokenState], Awaitable[None]]] = []
        self._signal_callbacks: List[Callable[[TokenState], Awaitable[None]]] = []
        self._session = aiohttp.ClientSession()
        # For No Brain Score history tracking
        self._price_history: Dict[str, List[float]] = {}
        self._volume_history: Dict[str, List[float]] = {}

    async def connect(self):
        while True:
            try:
                async with websockets.connect(PUMP_PORTAL_WS, ping_interval=20,
                                              ping_timeout=10) as ws:
                    self._ws = ws
                    logger.info("Connected to PumpPortal WebSocket")
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    self._reconnect_delay = 1
                    async for raw in ws:
                        await self._handle_message(raw)
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in {self._reconnect_delay}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _handle_message(self, raw: str):
        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "new_token":
            mint = msg.get("mint")
            if mint and mint not in self._tokens:
                self._tokens[mint] = TokenState(
                    mint=mint,
                    name=msg.get("name", "?"),
                    symbol=msg.get("symbol", "?"),
                    initial_mcap=float(msg.get("marketCapSol", 0)),
                )
                self._tokens[mint].peak_mcap = self._tokens[mint].initial_mcap
                self._tokens[mint].current_mcap = self._tokens[mint].initial_mcap
                if self._ws:
                    await self._ws.send(json.dumps({
                        "method": "subscribeTokenTrade",
                        "keys": [mint],
                    }))

        elif msg_type in ("token_trade", "trade"):
            mint = msg.get("mint", "")
            if not mint or mint not in self._tokens:
                return
            token = self._tokens[mint]
            token.last_update = time.time()

            sol_amount = float(msg.get("solAmount", 0))
            is_buy = msg.get("isBuy", True)
            trader = msg.get("traderPublicKey", "")
            mcap = float(msg.get("marketCapSol", token.current_mcap))

            token.current_mcap = mcap
            if mcap > token.peak_mcap:
                token.peak_mcap = mcap
            if trader:
                token.unique_wallets.add(trader)
            if is_buy:
                token.buy_count += 1
                token.total_buy_sol += sol_amount
            else:
                token.sell_count += 1
                token.total_sell_sol += sol_amount

            # Spike detection
            spike = token.spike_pct
            if (not token.alerted_spike and spike >= SPIKE_THRESHOLD_PCT and
                MCAP_MIN_SOL <= token.current_mcap <= MCAP_MAX_SOL):
                token.alerted_spike = True
                for cb in self._spike_callbacks:
                    await cb(token)

            # No Brain Score evaluation (simplified; full scoring would require more history)
            # Here we trigger a strong signal if spike is extremely high and buy ratio >0.7
            score = self._quick_score(token)
            if (not token.alerted_signal and score >= 85 and
                MCAP_MIN_SOL <= token.current_mcap <= MCAP_MAX_SOL):
                token.alerted_signal = True
                for cb in self._signal_callbacks:
                    await cb(token)

    def _quick_score(self, token: TokenState) -> int:
        """Rapid No Brain Score proxy. Replace with full calculation if needed."""
        if token.spike_pct < SPIKE_THRESHOLD_PCT:
            return 0
        score = 0
        score += min((token.spike_pct - SPIKE_THRESHOLD_PCT) * 0.5, 25)  # spike bonus
        score += min(token.buy_ratio * 20, 20)                     # buy ratio
        score += min(token.unique_wallet_count / 5 * 15, 15)       # holder growth
        return min(score, 100)

    def on_spike(self, cb):
        self._spike_callbacks.append(cb)

    def on_strong_signal(self, cb):
        self._signal_callbacks.append(cb)

    def get_spiked_tokens(self) -> List[TokenState]:
        return [t for t in self._tokens.values()
                if t.spike_pct >= SPIKE_THRESHOLD_PCT and
                MCAP_MIN_SOL <= t.current_mcap <= MCAP_MAX_SOL]

    def get_active_tokens(self) -> List[TokenState]:
        cutoff = time.time() - 300
        return [t for t in self._tokens.values() if t.last_update > cutoff]

    async def get_token_price(self, mint: str) -> Optional[float]:
        """Fetch current market cap in SOL from pump.fun API."""
        try:
            async with self._session.get(f"https://frontend-api.pump.fun/coins/{mint}") as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
        except:
            return None

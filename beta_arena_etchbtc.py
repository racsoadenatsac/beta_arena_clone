#!/usr/bin/env python3
"""
Alpha Arena Competition Bot - ETH Watermark Accumulation Strategy
Goal: Accumulate more ETH through strategic EUR positioning
Starting Capital: 87.00 ETH
Uses: Live Kraken API, cycle indicators, Grok AI decision making

Strategy:
- Trade only ETH and EUR with STRICT ETH watermark enforcement
- ETH Watermark: STRICT - must beat on every EUR→ETH re-entry (cycle-based improvement)
- EUR Watermark: Tracked for reference only (Grok decides exits based on market conditions)
- Dynamic ETH requirements based on BTC price levels (market cycle indicator)
- Strategic EUR exits/entries based on cycle position, trends, RSI
- Stop Loss: €2,000 max loss when in EUR - emergency buy back to ETH
- ETH is our home base, EUR is temporary for ladder trading
- Newsletter Integration: Paste newsletters into newsletter.txt for Grok AI context
"""

import requests
import time
import json
import sys
import os
import numpy as np
import subprocess
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict
import sqlite3
import hashlib
from collections import deque
import pytz
import base64
import hmac
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ==============================================================================
# CONFIGURATION
# ==============================================================================

@dataclass
class Config:
    """Competition Configuration - ETH/EUR Cycle-Aware Strategy"""
    initial_eth: float = 87.00
    llm_provider: str = "grok"
    bot_name: str = ""
    
    # Trading assets (ETH and EUR only)
    tradeable_assets: List[str] = None
    
    # Cycle-based watermark requirements for ETH
    min_improvement_accumulation: float = 0.00001  # 0.001% - Bear market
    min_improvement_early_bull: float = 0.0001     # 0.01% - Recovery
    min_improvement_mid_bull: float = 0.001        # 0.1% - Bull market
    min_improvement_late_bull: float = 0.0015      # 0.15% - Late cycle (reduced from 0.5%)
    min_improvement_euphoria: float = 0.01         # 1% - Top signals
    
    # Fees (Kraken taker fees - market orders)
    fee_rate: float = 0.0026  # 0.26% taker fee
    
    # EUR strategy
    eur_reentry_improvement: float = 0.001  # Need 0.1% improvement for re-entry
    
    # Market cycle thresholds (using BTC as market indicator)
    realized_price_btc: float = 56200  # Average cost basis
    ma_200w_btc: float = 55000        # 200-week moving average
    mining_cost: float = 60000        # Production cost
    etf_cost_basis: float = 83000     # ETF average entry
    mstr_cost_basis: float = 39000    # MicroStrategy cost basis
    
    # RSI thresholds
    overbought_rsi_threshold: float = 70
    oversold_rsi_threshold: float = 30
    extreme_overbought_rsi: float = 75
    extreme_oversold_rsi: float = 25
    
    # Volatility thresholds
    high_volatility_threshold: float = 0.02  # 2% movement
    extreme_volatility_threshold: float = 0.04  # 4% movement
    
    # Timing
    loop_interval_seconds: int = 60  # Adjusted dynamically
    
    # Competition
    target_return: float = 1.0  # 100% return goal
    
    # API Keys
    grok_api_key: str = os.getenv("GROK_API_KEY", "")
    kalshi_api_key: str = os.getenv("KALSHI_API_KEY", "")
    kalshi_private_key: str = os.getenv("KALSHI_PRIVATE_KEY", "")
    
    # Notifications
    enable_imessage: bool = True
    imessage_recipient: str = "oscar.castaneda@gmail.com"
    
    def __post_init__(self):
        if self.tradeable_assets is None:
            self.tradeable_assets = ["ETH", "EUR"]
        if not self.bot_name:
            self.bot_name = "ETH-EUR-CYCLE"

# ==============================================================================
# CYCLE POSITION ANALYZER (Using BTC as market indicator)
# ==============================================================================

class CyclePositionAnalyzer:
    """Analyze market cycle position using BTC as indicator"""
    
    def __init__(self, config: Config):
        self.config = config
        
    def get_cycle_phase(self, btc_price: float) -> str:
        """Determine cycle phase based on BTC price levels"""
        
        if btc_price < self.config.mining_cost:
            return "accumulation"
        elif btc_price < self.config.realized_price_btc * 1.1:
            return "early_recovery"
        elif btc_price < self.config.etf_cost_basis * 0.9:
            return "mid_bull"
        elif btc_price < self.config.etf_cost_basis * 1.1:
            return "late_bull"
        else:
            return "euphoria"
    
    def get_minimum_improvement(self, btc_price: float, market_info: Dict) -> float:
        """Get minimum ETH watermark improvement based on cycle"""
        
        phase = self.get_cycle_phase(btc_price)
        
        requirements = {
            "accumulation": self.config.min_improvement_accumulation,
            "early_recovery": self.config.min_improvement_early_bull,
            "mid_bull": self.config.min_improvement_mid_bull,
            "late_bull": self.config.min_improvement_late_bull,
            "euphoria": self.config.min_improvement_euphoria
        }
        
        min_improvement = requirements.get(phase, self.config.min_improvement_mid_bull)
        
        # Adjust for market hours
        if market_info.get("is_nyse_open"):
            min_improvement *= 0.5
        elif market_info.get("is_major_market_open"):
            min_improvement *= 0.75
        
        return min_improvement
    
    def should_exit_to_eur(self, btc_price: float, eth_price: float, 
                          profit_pct: float, eth_rsi: Optional[float]) -> Tuple[bool, str]:
        """Determine if we should exit ETH to EUR"""
        
        signals = []
        phase = self.get_cycle_phase(btc_price)

        # Signal 1: Late cycle with decent profit
        if phase in ["late_bull", "euphoria"] and profit_pct > 5:
            signals.append(f"Late cycle profit ({profit_pct:.1f}%)")

        # Signal 2: ETH extremely overbought
        if eth_rsi and eth_rsi > self.config.extreme_overbought_rsi:
            signals.append(f"ETH overbought (RSI {eth_rsi:.0f}%)")
        
        # Signal 3: BTC above major resistance (market top signal)
        if btc_price > self.config.etf_cost_basis:
            signals.append(f"BTC above ETF resistance")
        
        # Signal 4: BTC far above realized price
        if btc_price > self.config.realized_price_btc * 1.5:
            signals.append(f"BTC 50%+ above realized")
        
        # Need 2+ signals for exit
        should_exit = len(signals) >= 2
        reasoning = ", ".join(signals) if signals else ""
        
        return should_exit, reasoning
    
    def should_reenter_eth(self, btc_price: float, eth_price: float,
                          eth_rsi: Optional[float]) -> Tuple[bool, str]:
        """Determine if we should re-enter ETH from EUR"""
        
        signals = []
        phase = self.get_cycle_phase(btc_price)
        
        # Check support levels and conditions
        if phase == "accumulation":
            signals.append(f"Accumulation phase (BTC < ${self.config.mining_cost:,})")
        
        if btc_price < self.config.realized_price_btc * 1.05:
            signals.append(f"Near BTC realized price")
        
        if eth_rsi and eth_rsi < self.config.extreme_oversold_rsi:
            signals.append(f"ETH oversold (RSI {eth_rsi:.0f})")
        
        # Need at least 1 signal for re-entry
        if len(signals) >= 1:
            return True, ", ".join(signals)
        
        return False, ""

# ==============================================================================
# MARKET HOURS DETECTOR (Keep unchanged)
# ==============================================================================

class MarketHoursDetector:
    """Detect if major stock markets are open"""
    
    def __init__(self):
        self.markets = {
            "NYSE": {
                "timezone": pytz.timezone("America/New_York"),
                "open_time": (9, 30),
                "close_time": (16, 0),
                "days": [0, 1, 2, 3, 4],
                "importance": 3
            },
            "NASDAQ": {
                "timezone": pytz.timezone("America/New_York"),
                "open_time": (9, 30),
                "close_time": (16, 0),
                "days": [0, 1, 2, 3, 4],
                "importance": 3
            },
            "LSE": {
                "timezone": pytz.timezone("Europe/London"),
                "open_time": (8, 0),
                "close_time": (16, 30),
                "days": [0, 1, 2, 3, 4],
                "importance": 2
            }
        }
    
    def is_market_open(self, market_name: str) -> bool:
        """Check if a specific market is currently open"""
        if market_name not in self.markets:
            return False
        
        market = self.markets[market_name]
        tz = market["timezone"]
        now_local = datetime.now(tz)
        
        if now_local.weekday() not in market["days"]:
            return False
        
        open_hour, open_min = market["open_time"]
        close_hour, close_min = market["close_time"]
        
        current_time = now_local.hour * 60 + now_local.minute
        open_time = open_hour * 60 + open_min
        close_time = close_hour * 60 + close_min
        
        return open_time <= current_time < close_time
    
    def get_open_markets(self) -> List[str]:
        """Get list of currently open markets"""
        return [name for name in self.markets if self.is_market_open(name)]
    
    def get_market_session_info(self) -> Dict:
        """Get market session information"""
        open_markets = self.get_open_markets()
        
        return {
            "open_markets": open_markets,
            "is_nyse_open": "NYSE" in open_markets,
            "is_major_market_open": len(open_markets) > 0
        }

# ==============================================================================
# IMESSAGE NOTIFIER (Keep unchanged)
# ==============================================================================

class IMessageNotifier:
    """Send iMessage notifications"""
    
    def __init__(self, recipient: str, bot_name: str):
        self.recipient = recipient
        self.bot_name = bot_name
        self.enabled = sys.platform == "darwin"
        
        if not self.enabled:
            print(f"   ⚠️ iMessage notifications not available (requires macOS)")
    
    def send_trade_notification(self, trade_info: Dict) -> bool:
        """Send trade notification"""
        if not self.enabled:
            return False
        
        try:
            message = self._format_trade_message(trade_info)
            return self._send_message(message)
        except Exception as e:
            print(f"      ⚠️ iMessage error: {e}")
            return False
    
    def send_market_alert(self, alert_type: str, details: str) -> bool:
        """Send market alert"""
        if not self.enabled:
            return False
        
        message = f"🔔 {self.bot_name} ALERT\n\n{details}"
        return self._send_message(message)
    
    def _send_message(self, message: str) -> bool:
        """Send message via AppleScript"""
        try:
            escaped_message = message.replace('\\', '\\\\').replace('"', '\\"')
            
            applescript = f'''
            tell application "Messages"
                set targetService to 1st account whose service type = iMessage
                set targetBuddy to participant "{self.recipient}" of targetService
                send "{escaped_message}" to targetBuddy
            end tell
            '''
            
            process = subprocess.run(
                ['osascript', '-e', applescript],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            return process.returncode == 0
        except Exception:
            return False
    
    def _format_trade_message(self, trade_info: Dict) -> str:
        """Format trade message"""
        from_asset = trade_info.get("from_asset", "?")
        to_asset = trade_info.get("to_asset", "?")
        trade_type = trade_info.get("type", "unknown")
        value = trade_info.get("value", 0)
        fee = trade_info.get("fee", 0)
        new_qty = trade_info.get("new_quantity", 0)
        new_price = trade_info.get("new_price", 0)
        reasoning = trade_info.get("reasoning", "")
        eth_price = trade_info.get("eth_price", 0)

        message = f"🔄 TRADE EXECUTED: {from_asset} → {to_asset}\n"
        message += f"Type: {trade_type}\n"
        message += f"ETH @TT: €{eth_price:,.2f}\n"
        message += f"Value: €{value:,.2f} | Fee: €{fee:.2f}\n"

        # Always show both positions
        if to_asset == "EUR":
            # Just sold ETH for EUR - show EUR and equivalent ETH value
            equivalent_eth = new_qty / eth_price if eth_price > 0 else 0
            message += f"New Position EUR: €{new_qty:,.2f}\n"
            message += f"New Position ETH: {equivalent_eth:.6f}\n"
        else:
            # Just bought ETH with EUR - show ETH and equivalent EUR value
            equivalent_eur = new_qty * new_price
            message += f"New Position EUR: €{equivalent_eur:,.2f}\n"
            message += f"New Position ETH: {new_qty:.6f}\n"

        message += f"Reason: {reasoning}"

        return message

# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class Watermark:
    """Track ETH watermark (strict) and EUR watermark (strict) - dual ladder strategy"""
    eth_quantity: float = 0.0
    eth_achieved_at: Optional[str] = None
    eur_quantity: float = 0.0
    eur_achieved_at: Optional[str] = None

    def update_eth(self, quantity: float) -> bool:
        """Update ETH watermark if new quantity is higher (STRICT - enforced before trade)"""
        if quantity > self.eth_quantity:
            self.eth_quantity = quantity
            self.eth_achieved_at = datetime.now().isoformat()
            return True
        return False

    def update_eur(self, quantity: float) -> bool:
        """Update EUR watermark if new quantity is higher (STRICT - enforced before trade)"""
        if quantity > self.eur_quantity:
            self.eur_quantity = quantity
            self.eur_achieved_at = datetime.now().isoformat()
            return True
        return False

    def update(self, quantity: float) -> bool:
        """Backward compatibility - update ETH watermark"""
        return self.update_eth(quantity)

    def get(self) -> float:
        """Get current ETH watermark"""
        return self.eth_quantity

    def get_eur(self) -> float:
        """Get current EUR watermark (reference only)"""
        return self.eur_quantity

@dataclass
class Position:
    """Current position"""
    symbol: str
    quantity: float
    entry_price: float
    entry_time: str
    
    def current_value(self, price: float) -> float:
        if self.symbol == "EUR":
            return self.quantity
        return self.quantity * price

@dataclass
class MarketData:
    """Market data"""
    symbol: str
    price: float
    bid: float
    ask: float
    change_24h: float
    momentum_5m: float
    momentum_1h: float
    volatility: float
    trend: str
    rsi_estimate: Optional[float] = None

@dataclass
class TradeOpportunity:
    """Trading opportunity"""
    type: str
    from_asset: str
    to_asset: str
    expected_return: float
    confidence: float
    reasoning: str
    market_conditions: Dict
    expected_quantity: Optional[float] = None  # Expected quantity of to_asset (e.g., ETH amount for EUR→ETH)

# ==============================================================================
# KALSHI PROVIDER (ETH Price Predictions)
# ==============================================================================

class KalshiProvider:
    """Fetch ETH price predictions from Kalshi prediction markets with RSA authentication"""

    def __init__(self, api_key: str = "", private_key_pem: str = ""):
        self.base_url = "https://api.elections.kalshi.com"
        self.api_version = "v2"
        self.api_key = api_key
        self.cache = {}
        self.cache_duration = 60  # Cache predictions for 1 minute

        # Parse RSA private key if provided
        self.private_key = None
        if private_key_pem:
            try:
                self.private_key = serialization.load_pem_private_key(
                    private_key_pem.encode('utf-8'),
                    password=None,
                    backend=default_backend()
                )
                print(f"✅ Kalshi: Private key loaded successfully")
            except Exception as e:
                print(f"⚠️ Kalshi: Failed to parse private key: {e}")
        else:
            print(f"⚠️ Kalshi: No private key provided")

    def _create_signature(self, timestamp: str, method: str, path: str) -> Optional[str]:
        """Create RSA signature for Kalshi API request"""
        if not self.private_key:
            return None

        try:
            # Create message to sign: timestamp + method + path
            message = f"{timestamp}{method}{path}"

            # Sign with RSA private key
            signature = self.private_key.sign(
                message.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256()
            )

            # Base64 encode the signature
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            print(f"⚠️ Kalshi: Failed to create signature: {e}")
            return None

    def get_eth_prediction(self) -> Optional[Dict]:
        """
        Get Kalshi prediction for ETH price movement.
        Searches for any active ETH prediction markets.

        Returns:
            Dict with 'probability' (0-100), 'market_title', 'last_updated', 'available'
            None if unable to fetch
        """
        try:
            # Check cache first
            cache_key = "eth_prediction"
            if cache_key in self.cache:
                cached_data, cached_time = self.cache[cache_key]
                if time.time() - cached_time < self.cache_duration:
                    return cached_data

            # Search for ETH markets on Kalshi with RSA authentication
            path = f"/trade-api/{self.api_version}/markets"
            params = {
                "limit": 100,
                "status": "open"
            }

            # Build query string for signature
            query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            path_with_query = f"{path}?{query_string}"
            search_url = f"{self.base_url}{path_with_query}"

            # Create RSA signature authentication headers
            headers = {}
            if self.api_key and self.private_key:
                # Generate timestamp in milliseconds
                timestamp = str(int(time.time() * 1000))

                # Create signature with full path including query string
                signature = self._create_signature(timestamp, "GET", path_with_query)

                if signature:
                    headers["KALSHI-ACCESS-KEY"] = self.api_key
                    headers["KALSHI-ACCESS-SIGNATURE"] = signature
                    headers["KALSHI-ACCESS-TIMESTAMP"] = timestamp
                    print(f"🔐 Kalshi: Auth headers created (timestamp: {timestamp}, path: {path_with_query})")
                else:
                    print(f"⚠️ Kalshi: Signature creation failed")
            elif not self.api_key:
                print(f"⚠️ Kalshi: No API key available")
            elif not self.private_key:
                print(f"⚠️ Kalshi: No private key available")

            response = requests.get(search_url, headers=headers, timeout=5)
            if response.status_code != 200:
                try:
                    error_detail = response.json()
                    print(f"⚠️ Kalshi API error {response.status_code}: {error_detail}")
                except:
                    print(f"⚠️ Kalshi API error {response.status_code}: {response.text[:200]}")
                return self._neutral_prediction(f"Kalshi API HTTP {response.status_code}")

            try:
                data = response.json()
            except Exception as e:
                return self._neutral_prediction(f"Kalshi JSON parse error: {str(e)}")

            markets = data.get("markets", [])

            # If no markets returned, might be API structure issue
            if not isinstance(markets, list):
                return self._neutral_prediction(f"Kalshi unexpected response format")

            # Search for ETH markets with priority ordering
            eth_market = None
            market_priority = None

            for market in markets:
                ticker = market.get("ticker", "").lower()
                title = market.get("title", "").lower()
                subtitle = market.get("subtitle", "").lower()

                # Combine all text fields for searching
                search_text = f"{ticker} {title} {subtitle}"

                # Skip non-ETH markets
                if "eth" not in search_text and "ethereum" not in search_text:
                    continue

                # Skip expired or closed markets
                if market.get("status") != "open":
                    continue

                # Priority 1: Daily predictions
                if any(word in search_text for word in ["day", "daily", "today"]):
                    eth_market = market
                    market_priority = "daily"
                    break

                # Priority 2: Weekly predictions
                if not eth_market and any(word in search_text for word in ["week", "weekly"]):
                    eth_market = market
                    market_priority = "weekly"
                    continue

                # Priority 3: Price target predictions
                if not eth_market and any(word in search_text for word in ["above", "below", "higher", "lower", "reach"]):
                    eth_market = market
                    market_priority = "price_target"

            if not eth_market:
                return self._neutral_prediction("No active ETH markets on Kalshi")

            # Get the probability from the market
            # Kalshi uses "yes_bid" and "yes_ask" for probabilities
            yes_bid = eth_market.get("yes_bid")
            yes_ask = eth_market.get("yes_ask")

            # Calculate bullish probability (average of bid/ask if both available)
            if yes_bid is not None and yes_ask is not None:
                # Kalshi prices are in cents (0-100)
                bullish_probability = (yes_bid + yes_ask) / 2
            elif yes_bid is not None:
                bullish_probability = yes_bid
            elif yes_ask is not None:
                bullish_probability = yes_ask
            else:
                # Try last price
                last_price = eth_market.get("last_price")
                bullish_probability = last_price if last_price is not None else 50.0

            result = {
                "probability": bullish_probability,
                "market_title": eth_market.get("title", "ETH Prediction"),
                "market_type": market_priority,
                "ticker": eth_market.get("ticker", ""),
                "last_updated": datetime.now().isoformat(),
                "available": True
            }

            # Cache the result
            self.cache[cache_key] = (result, time.time())

            return result

        except Exception as e:
            # Return neutral prediction on error
            return self._neutral_prediction(f"Kalshi error: {str(e)}")

    def _neutral_prediction(self, reason: str) -> Dict:
        """Return a neutral 50% prediction with reason"""
        return {
            "probability": 50.0,
            "market_title": reason,
            "market_type": "none",
            "ticker": "",
            "last_updated": datetime.now().isoformat(),
            "available": False
        }

# ==============================================================================
# KRAKEN MARKET PROVIDER (Simplified for ETH and BTC tracking)
# ==============================================================================

class KrakenMarketProvider:
    """Market data provider"""

    def __init__(self):
        self.base_url = "https://api.kraken.com/0/public"
        # Increase history size for 6 hours of 1-minute data
        self.price_history = {
            "ETH": deque(maxlen=360),  # 6 hours * 60 minutes
            "BTC": deque(maxlen=360)   # Track BTC for cycle analysis
        }
        self.last_prices = {"ETH": None, "BTC": None}
        self.five_min_history = {
            "ETH": deque(maxlen=5),
            "BTC": deque(maxlen=5)
        }
        self.history_loaded = False
    
    def _estimate_rsi(self, prices: deque) -> Optional[float]:
        """Estimate RSI"""
        if len(prices) < 14:
            return None
        
        gains = []
        losses = []
        
        for i in range(1, min(14, len(prices))):
            diff = prices[i] - prices[i-1]
            if diff > 0:
                gains.append(diff)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(diff))
        
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        
        if avg_loss == 0:
            return 100
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def load_historical_data(self, hours: int = 6):
        """Load historical OHLC data from Kraken to build price history"""
        print(f"\n📊 Loading {hours}h historical market data from Kraken...")

        pairs = {
            "ETH": "ETHEUR",
            "BTC": "XBTEUR"
        }

        for asset, pair in pairs.items():
            try:
                # Kraken OHLC endpoint - interval 1 = 1 minute
                response = requests.get(
                    f"{self.base_url}/OHLC",
                    params={
                        "pair": pair,
                        "interval": 1  # 1-minute candles
                    },
                    timeout=30
                )
                response.raise_for_status()
                data = response.json()

                if data.get("error") and len(data["error"]) > 0:
                    print(f"   ⚠️ Kraken error for {asset}: {data['error']}")
                    continue

                result = data.get("result", {})

                # Get the OHLC data (key varies: XETHZEUR, XXBTZEUR, etc.)
                ohlc_data = None
                for key in result.keys():
                    if key.startswith("X") or key == pair:
                        ohlc_data = result[key]
                        break

                if not ohlc_data:
                    print(f"   ⚠️ No OHLC data found for {asset}")
                    continue

                # OHLC format: [time, open, high, low, close, vwap, volume, count]
                # We want closing prices from the last N hours
                max_candles = hours * 60  # hours * 60 minutes
                candles = ohlc_data[-max_candles:] if len(ohlc_data) > max_candles else ohlc_data

                # Extract closing prices and add to history
                for candle in candles:
                    close_price = float(candle[4])  # Index 4 is close price
                    self.price_history[asset].append(close_price)

                print(f"   ✅ {asset}: Loaded {len(candles)} candles (last price: €{close_price:,.2f})")
                self.last_prices[asset] = close_price

            except Exception as e:
                print(f"   ⚠️ Error loading {asset} history: {e}")
                continue

        self.history_loaded = True
        print(f"   📈 Historical data loaded successfully\n")

    def fetch(self) -> Dict[str, MarketData]:
        """Get market data"""
        try:
            response = requests.get(
                f"{self.base_url}/Ticker",
                params={"pair": "XBTEUR,ETHEUR"},
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            if data.get("error"):
                return self._fallback_data()
            
            result = data.get("result", {})
            market = {}
            
            # EUR is always stable
            market["EUR"] = MarketData(
                symbol="EUR",
                price=1.0,
                bid=1.0,
                ask=1.0,
                change_24h=0,
                momentum_5m=0,
                momentum_1h=0,
                volatility=0,
                trend="neutral",
                rsi_estimate=50
            )
            
            # Process ETH
            if "XETHZEUR" in result:
                eth_data = result["XETHZEUR"]
                eth_price = float(eth_data["c"][0])
                eth_bid = float(eth_data["b"][0])
                eth_ask = float(eth_data["a"][0])
                eth_open = float(eth_data["o"])
                eth_high = float(eth_data["h"][1])
                eth_low = float(eth_data["l"][1])
                
                eth_change = ((eth_price - eth_open) / eth_open) * 100
                eth_volatility = ((eth_high - eth_low) / eth_price) * 100
                
                self.price_history["ETH"].append(eth_price)
                self.five_min_history["ETH"].append(eth_price)
                
                eth_momentum_5m = 0
                if len(self.five_min_history["ETH"]) >= 2:
                    eth_momentum_5m = ((eth_price - self.five_min_history["ETH"][0]) / 
                                      self.five_min_history["ETH"][0]) * 100
                
                eth_momentum_1h = 0
                if len(self.price_history["ETH"]) >= 10:
                    old_price = self.price_history["ETH"][-10]
                    eth_momentum_1h = ((eth_price - old_price) / old_price) * 100
                
                eth_rsi = self._estimate_rsi(self.price_history["ETH"])
                
                if eth_momentum_5m > 0.5:
                    eth_trend = "bullish"
                elif eth_momentum_5m < -0.5:
                    eth_trend = "bearish"
                else:
                    eth_trend = "neutral"
                
                market["ETH"] = MarketData(
                    symbol="ETH",
                    price=eth_price,
                    bid=eth_bid,
                    ask=eth_ask,
                    change_24h=eth_change,
                    momentum_5m=eth_momentum_5m,
                    momentum_1h=eth_momentum_1h,
                    volatility=eth_volatility,
                    trend=eth_trend,
                    rsi_estimate=eth_rsi
                )
                
                self.last_prices["ETH"] = eth_price
            
            # Process BTC (for cycle analysis only)
            if "XXBTZEUR" in result:
                btc_data = result["XXBTZEUR"]
                btc_price = float(btc_data["c"][0])
                btc_bid = float(btc_data["b"][0])
                btc_ask = float(btc_data["a"][0])
                
                self.price_history["BTC"].append(btc_price)
                self.five_min_history["BTC"].append(btc_price)
                
                btc_momentum_5m = 0
                if len(self.five_min_history["BTC"]) >= 2:
                    btc_momentum_5m = ((btc_price - self.five_min_history["BTC"][0]) / 
                                      self.five_min_history["BTC"][0]) * 100
                
                market["BTC"] = MarketData(
                    symbol="BTC",
                    price=btc_price,
                    bid=btc_bid,
                    ask=btc_ask,
                    change_24h=0,
                    momentum_5m=btc_momentum_5m,
                    momentum_1h=0,
                    volatility=0,
                    trend="neutral",
                    rsi_estimate=None
                )
                
                self.last_prices["BTC"] = btc_price
            
            return market
            
        except Exception as e:
            print(f"   ⚠️ Error fetching Kraken data: {e}")
            return self._fallback_data()
    
    def _fallback_data(self) -> Dict[str, MarketData]:
        """Fallback data"""
        return {
            "EUR": MarketData(
                symbol="EUR", price=1.0, bid=1.0, ask=1.0,
                change_24h=0, momentum_5m=0, momentum_1h=0,
                volatility=0, trend="neutral", rsi_estimate=50
            ),
            "ETH": MarketData(
                symbol="ETH", 
                price=self.last_prices.get("ETH", 3200.0),
                bid=self.last_prices.get("ETH", 3200.0) * 0.999,
                ask=self.last_prices.get("ETH", 3200.0) * 1.001,
                change_24h=0, momentum_5m=0, momentum_1h=0,
                volatility=0.2, trend="neutral", rsi_estimate=50
            ),
            "BTC": MarketData(
                symbol="BTC",
                price=self.last_prices.get("BTC", 92000.0),
                bid=self.last_prices.get("BTC", 92000.0) * 0.999,
                ask=self.last_prices.get("BTC", 92000.0) * 1.001,
                change_24h=0, momentum_5m=0, momentum_1h=0,
                volatility=0.2, trend="neutral", rsi_estimate=50
            )
        }

# ==============================================================================
# ETH/EUR OPPORTUNITY DETECTOR
# ==============================================================================

class ETHEUROpportunityDetector:
    """Detect ETH/EUR trading opportunities"""

    def __init__(self, config: Config):
        self.config = config
        self.cycle_analyzer = CyclePositionAnalyzer(config)
        self.eur_exit_price = None
        self.market_provider = None  # Will be set by bot

        # Midpoint exit window tracking (1 hour to choose best exit after exceeding midpoint)
        self.midpoint_exceeded_at: Optional[datetime] = None  # When price first exceeded 6h midpoint

    def _analyze_historical_trend(self, market: Dict) -> Tuple[str, str, Dict]:
        """Analyze historical price data to determine trend direction and strength

        Returns:
            trend_direction: "upcycle", "downcycle", or "sideways"
            trend_strength: "strong", "moderate", or "weak"
            details: Dict with analysis details
        """
        if not self.market_provider:
            return "sideways", "weak", {}

        price_history = self.market_provider.price_history.get("ETH", deque())

        if len(price_history) < 60:
            # Not enough data
            return "sideways", "weak", {"reason": "insufficient_data"}

        prices = list(price_history)
        current_price = prices[-1]

        # Calculate price changes over different timeframes
        price_1h_ago = prices[-60] if len(prices) >= 60 else prices[0]
        price_3h_ago = prices[-180] if len(prices) >= 180 else prices[0]
        price_6h_ago = prices[0]

        change_1h = ((current_price - price_1h_ago) / price_1h_ago) * 100
        change_3h = ((current_price - price_3h_ago) / price_3h_ago) * 100
        change_6h = ((current_price - price_6h_ago) / price_6h_ago) * 100

        # Calculate RSI trajectory (is RSI rising or falling?)
        rsi_current = market["ETH"].rsi_estimate

        # Look at recent price momentum (last 30 minutes vs previous 30 minutes)
        recent_30m = prices[-30:] if len(prices) >= 30 else prices
        previous_30m = prices[-60:-30] if len(prices) >= 60 else prices[:len(prices)//2]

        recent_avg = sum(recent_30m) / len(recent_30m) if recent_30m else current_price
        previous_avg = sum(previous_30m) / len(previous_30m) if previous_30m else current_price

        momentum_30m = ((recent_avg - previous_avg) / previous_avg) * 100

        # Determine trend direction
        # Upcycle: Multiple timeframes showing upward movement
        # Downcycle: Multiple timeframes showing downward movement
        upward_signals = sum([change_1h > 0.5, change_3h > 1.0, change_6h > 1.5, momentum_30m > 0.3])
        downward_signals = sum([change_1h < -0.5, change_3h < -1.0, change_6h < -1.5, momentum_30m < -0.3])

        # STALL DETECTION: If 1h momentum is near zero, the trend has stalled regardless of longer timeframes
        # This prevents lag from 3h/6h periods that still reflect earlier rally
        # Critical for exit signals when holding ETH with good profits
        if -0.5 <= change_1h <= 0.5 and (change_3h > 0 or change_6h > 0):
            # 1h momentum stalled but longer timeframes still positive = uptrend has ended
            trend_direction = "sideways"
        elif upward_signals >= 3:
            trend_direction = "upcycle"
        elif downward_signals >= 3:
            trend_direction = "downcycle"
        elif upward_signals >= 2:
            trend_direction = "upcycle"
        elif downward_signals >= 2:
            trend_direction = "downcycle"
        else:
            trend_direction = "sideways"

        # Determine trend strength based on magnitude of changes
        max_change = max(abs(change_1h), abs(change_3h), abs(change_6h))

        if max_change > 3.0:
            trend_strength = "strong"
        elif max_change > 1.5:
            trend_strength = "moderate"
        else:
            trend_strength = "weak"

        details = {
            "change_1h": change_1h,
            "change_3h": change_3h,
            "change_6h": change_6h,
            "momentum_30m": momentum_30m,
            "rsi": rsi_current,
            "upward_signals": upward_signals,
            "downward_signals": downward_signals,
            "data_points": len(prices)
        }

        return trend_direction, trend_strength, details

    def detect_opportunities(self, state: Dict, market_info: Dict) -> List[TradeOpportunity]:
        """Detect trading opportunities"""
        opportunities = []

        current_asset = state["current_position"]
        market = state["market"]
        watermark = state["watermark"]
        eur_watermark = state.get("eur_watermark", 0)
        portfolio_value = state["portfolio_value"]
        profit_pct = state.get("profit_pct", 0)
        btc_price = market["BTC"].price
        eth_price = market["ETH"].price
        eur_entry_value = state.get("eur_entry_value")
        stop_loss_threshold = state.get("stop_loss_threshold", 5000.0)
        eth_6h_high = state.get("eth_6h_high")
        eth_6h_low = state.get("eth_6h_low")

        # Get cycle-adjusted minimum improvement
        min_improvement = self.cycle_analyzer.get_minimum_improvement(btc_price, market_info)
        cycle_phase = self.cycle_analyzer.get_cycle_phase(btc_price)

        print(f"      📊 Cycle: {cycle_phase} | Min improvement: {min_improvement*100:.3f}%")
        if eur_watermark > 0:
            print(f"      💶 EUR Watermark: €{eur_watermark:,.2f} (reference only)")

        if current_asset == "EUR":
            # Look for ETH re-entry opportunities (including stop loss check)
            reentry_ops = self._detect_eth_reentry(
                market, watermark, portfolio_value, min_improvement,
                eur_entry_value, stop_loss_threshold
            )
            opportunities.extend(reentry_ops)

        elif current_asset == "ETH":
            # Check for EUR exit signals
            exit_ops = self._detect_eur_exit(
                market, profit_pct, btc_price, eth_price, portfolio_value, watermark, eur_watermark,
                min_improvement, market_info, eth_6h_high, eth_6h_low
            )
            opportunities.extend(exit_ops)
            
            # Check if we can improve ETH watermark (shouldn't happen but safety check)
            watermark_ops = self._detect_watermark_improvement(
                market, watermark, portfolio_value, min_improvement, cycle_phase
            )
            opportunities.extend(watermark_ops)
        
        # Sort by expected return
        opportunities.sort(key=lambda x: x.expected_return, reverse=True)
        
        return opportunities
    
    def _detect_eur_exit(self, market: Dict, profit_pct: float,
                        btc_price: float, eth_price: float, portfolio_value: float,
                        eth_watermark: float, eur_watermark: float, min_improvement: float,
                        market_info: Dict, eth_6h_high: Optional[float], eth_6h_low: Optional[float]) -> List[TradeOpportunity]:
        """Detect opportunities to exit to EUR (STRICT EUR watermark enforcement)

        Only returns opportunities if EUR watermark can be beaten by min_eur_improvement (0.1%).
        If watermark cannot be beaten, returns empty list (no trade opportunity).
        """
        opportunities = []

        eth_data = market["ETH"]
        eth_bid_price = market["ETH"].bid  # Price we'd get selling ETH
        eth_ask_price = market["ETH"].ask  # Price we'd pay buying back ETH

        # Calculate current ETH quantity from portfolio value
        current_eth_qty = portfolio_value / eth_bid_price

        # Calculate what EUR value we'd get after exit
        expected_eur = portfolio_value * (1 - self.config.fee_rate)

        # ========================================================================
        # ETH PROTECTION STRATEGY:
        # - Exits (ETH→EUR): Grok decides (no hard block)
        # - Re-entries (EUR→ETH): MUST get > eth_watermark
        # This guarantees ETH grows over complete cycles while allowing flexible exits
        # ========================================================================

        # ========================================================================
        # SPECIAL CASE: Initial exit from starting position (EUR watermark = 0)
        # ========================================================================
        # ALWAYS consult Grok for the initial exit decision - let AI decide based on full context
        markets_closed = not market_info.get("is_major_market_open", False)
        market_hours_note = " (markets closed)" if markets_closed else ""

        if eur_watermark == 0:
            # Analyze historical trend for context
            trend_direction, trend_strength, trend_details = self._analyze_historical_trend(market)

            # Build reasoning based on trend analysis
            change_1h = trend_details.get("change_1h", 0)
            change_3h = trend_details.get("change_3h", 0)
            change_6h = trend_details.get("change_6h", 0)
            momentum_30m = trend_details.get("momentum_30m", 0)

            trend_summary = f"1h: {change_1h:+.2f}%, 3h: {change_3h:+.2f}%, 6h: {change_6h:+.2f}%"

            # Initial exit: Create opportunity based on trend analysis, let Grok decide
            # Signal to Grok if profit exceeds round-trip fee (0.52% = 2x 0.26%)
            roundtrip_threshold = 0.52  # Need to beat both entry and exit fees
            exceeds_threshold = profit_pct > roundtrip_threshold

            print(f"      🔍 Initial Exit Analysis: {trend_direction.upper()} ({trend_strength}) - Profit: {profit_pct:+.2f}%{market_hours_note}")
            print(f"         Price changes: {trend_summary}")
            if exceeds_threshold:
                print(f"         ✅ Profit ({profit_pct:+.2f}%) exceeds threshold ({roundtrip_threshold:.2f}%) - acceptable exit")
            else:
                print(f"         ⚠️ Profit ({profit_pct:+.2f}%) below threshold ({roundtrip_threshold:.2f}%) - marginal")
            print(f"         Creating opportunity for Grok to evaluate market conditions")

            # Create opportunity for Grok to decide
            confidence = 0.5  # Neutral - let Grok decide

            # Adjust confidence hints based on conditions
            if trend_direction == "downcycle":
                confidence = 0.75
                hint = "Downcycle detected"
            elif profit_pct > 1.0:
                confidence = 0.7
                hint = f"Good profit opportunity ({profit_pct:.2f}%)"
            elif exceeds_threshold:
                # Profit exceeds round-trip threshold - signal this is acceptable, but let Grok decide
                hint = f"Profit {profit_pct:+.2f}% exceeds threshold {roundtrip_threshold:.2f}% - acceptable exit, your decision"
            else:
                hint = f"Current profit: {profit_pct:+.2f}%"

            opportunities.append(TradeOpportunity(
                type="initial_exit",
                from_asset="ETH",
                to_asset="EUR",
                expected_return=profit_pct / 100,
                confidence=confidence,
                reasoning=f"Initial exit decision needed: {hint}, {trend_direction} trend ({trend_strength}){market_hours_note}. Trend: {trend_summary}",
                market_conditions={
                    "trend": trend_direction,
                    "trend_strength": trend_strength,
                    "profit_pct": profit_pct,
                    "expected_eur": expected_eur,
                    "markets_closed": markets_closed,
                    **trend_details
                }
            ))
            print(f"         Consulting Grok for decision...")

            return opportunities

        # ========================================================================
        # ALWAYS consult Grok when holding ETH - let AI decide based on full context
        # ========================================================================

        # Analyze historical trend for context
        trend_direction, trend_strength, trend_details = self._analyze_historical_trend(market)

        # Build reasoning based on trend analysis
        change_1h = trend_details.get("change_1h", 0)
        change_3h = trend_details.get("change_3h", 0)
        change_6h = trend_details.get("change_6h", 0)

        trend_summary = f"1h: {change_1h:+.2f}%, 3h: {change_3h:+.2f}%, 6h: {change_6h:+.2f}%"

        # ========================================================================
        # PRICE FILTER: Only exit at or above 6h midpoint
        # ========================================================================
        # After entering ETH, wait for price to reach halfway between 6h high/low
        # Once exceeded, Grok has 1 hour to choose best exit
        minutes_since_midpoint_exceeded = None
        if eth_6h_high is not None and eth_6h_low is not None:
            midpoint_6h = (eth_6h_high + eth_6h_low) / 2
            price_vs_midpoint = ((eth_price - midpoint_6h) / midpoint_6h) * 100

            if eth_price < midpoint_6h:
                # Price below midpoint - reset timer and wait
                if self.midpoint_exceeded_at is not None:
                    print(f"      🔻 Price dropped below midpoint - resetting exit window")
                self.midpoint_exceeded_at = None

                print(f"      🔍 Hold/Exit Analysis: {trend_direction.upper()} ({trend_strength}) - Profit: {profit_pct:+.2f}%{market_hours_note}")
                print(f"         💰 Price: €{eth_price:,.2f} | 6h range: €{eth_6h_low:,.2f} - €{eth_6h_high:,.2f}")
                print(f"         ⏸️ Below midpoint €{midpoint_6h:,.2f} ({price_vs_midpoint:+.2f}%)")
                print(f"         Waiting for price to reach or exceed midpoint before creating exit opportunity")
                return opportunities  # Return empty - no opportunity yet

            # Price at or above midpoint - start/continue exit window
            now = datetime.now()
            if self.midpoint_exceeded_at is None:
                self.midpoint_exceeded_at = now
                print(f"      🎯 Price exceeded midpoint! Starting 1-hour exit window")

            minutes_since_midpoint_exceeded = (now - self.midpoint_exceeded_at).total_seconds() / 60
            minutes_remaining = 60 - minutes_since_midpoint_exceeded

            print(f"      💰 Price: €{eth_price:,.2f} ✓ At/above midpoint €{midpoint_6h:,.2f} ({price_vs_midpoint:+.2f}%)")
            print(f"      6h range: €{eth_6h_low:,.2f} - €{eth_6h_high:,.2f}")
            print(f"      ⏱️ Exit window: {minutes_since_midpoint_exceeded:.0f}m elapsed, {minutes_remaining:.0f}m remaining (1h limit)")

        # ========================================================================
        # EUR WATERMARK - Track for reference only (NOT enforced)
        # ========================================================================
        # ETH watermark is the only strict requirement - EUR is reference only
        # Grok decides EUR exits based on market conditions

        eur_quality_note = ""
        improvement_pct = 0

        if eur_watermark > 0:
            # Calculate improvement for informational purposes only
            eur_ratio = expected_eur / eur_watermark
            improvement_pct = (eur_ratio - 1) * 100
            eur_quality_note = f" EUR: {improvement_pct:+.2f}% vs last exit"

        # Check cycle-based exit signals (for context/hints, not gates)
        _, exit_signals = self.cycle_analyzer.should_exit_to_eur(
            btc_price, eth_price, profit_pct, eth_data.rsi_estimate
        )

        # Build hints based on conditions
        hints = []
        confidence = 0.5  # Neutral - let Grok decide

        # EUR info (reference only, not a requirement)
        if eur_watermark > 0 and improvement_pct != 0:
            hints.append(f"EUR {improvement_pct:+.2f}% vs last exit")

        # Trend hints
        if trend_direction == "downcycle":
            hints.append("Downcycle detected")
            confidence = min(confidence + 0.1, 0.8)
        elif trend_direction == "upcycle":
            hints.append("Upcycle detected")
            confidence = max(confidence - 0.1, 0.2)

        # RSI hints
        rsi = eth_data.rsi_estimate
        if rsi:
            if rsi > 75:
                hints.append(f"Overbought RSI {rsi:.0f}")
                confidence = min(confidence + 0.15, 0.8)
            elif rsi < 25:
                hints.append(f"Oversold RSI {rsi:.0f}")
                confidence = max(confidence - 0.15, 0.2)

        # Profit hints - check against 0.26% threshold (cover this trade's fee)
        fee_threshold = 0.26  # Cover this transaction's fee
        if profit_pct > 2.0:
            hints.append(f"Good profit {profit_pct:+.2f}%")
            confidence = min(confidence + 0.1, 0.8)
        elif profit_pct > fee_threshold:
            # Exceeds fee - signal to Grok this covers the exit fee
            hints.append(f"Profit {profit_pct:+.2f}% covers exit fee {fee_threshold:.2f}%")
            confidence = min(confidence + 0.05, 0.7)
        elif profit_pct < -1.0:
            hints.append(f"Loss {profit_pct:+.2f}%")

        # Exit signals from cycle analyzer
        if exit_signals:
            hints.append(f"Exit signals: {exit_signals}")
            confidence = min(confidence + 0.1, 0.8)

        hint_text = ", ".join(hints) if hints else "No strong signals"

        # Calculate position within 6h range for Grok context
        eth_6h_position = "unknown"
        price_to_high_pct = 0
        if eth_6h_high is not None and eth_6h_low is not None:
            range_size = eth_6h_high - eth_6h_low
            if range_size > 0:
                position_in_range = (eth_price - eth_6h_low) / range_size * 100
                price_to_high_pct = ((eth_6h_high - eth_price) / eth_price) * 100
                if position_in_range >= 80:
                    eth_6h_position = "near_high"
                elif position_in_range >= 50:
                    eth_6h_position = "upper_half"
                elif position_in_range >= 20:
                    eth_6h_position = "lower_half"
                else:
                    eth_6h_position = "near_low"

        opportunities.append(TradeOpportunity(
            type="hold_or_exit",
            from_asset="ETH",
            to_asset="EUR",
            expected_return=profit_pct / 100,
            confidence=confidence,
            reasoning=f"Hold/Exit decision: {hint_text}.{eur_quality_note}{market_hours_note} Trend: {trend_summary}",
            market_conditions={
                "trend": trend_direction,
                "trend_strength": trend_strength,
                "profit_pct": profit_pct,
                "expected_eur": expected_eur,
                "can_beat_eur_watermark": True,  # Always true if we got here
                "eur_improvement_pct": improvement_pct if eur_watermark > 0 else 0,
                "rsi": rsi,
                "markets_closed": markets_closed,
                "exit_signals": exit_signals,
                "eth_6h_high": eth_6h_high,
                "eth_6h_low": eth_6h_low,
                "eth_6h_position": eth_6h_position,
                "price_to_high_pct": price_to_high_pct,
                "minutes_since_midpoint_exceeded": minutes_since_midpoint_exceeded,
                **trend_details
            }
        ))
        print(f"      🔍 Hold/Exit Analysis: {trend_direction.upper()} ({trend_strength}) - Profit: {profit_pct:+.2f}%{market_hours_note}")
        # Show fee coverage status
        if profit_pct > fee_threshold:
            print(f"         ✅ Profit ({profit_pct:+.2f}%) covers exit fee ({fee_threshold:.2f}%)")
        else:
            print(f"         ⚠️ Profit ({profit_pct:+.2f}%) below fee ({fee_threshold:.2f}%)")
        print(f"         Price changes: {trend_summary}")
        rsi_text = f"{rsi:.0f}" if rsi else "N/A"
        if eur_watermark > 0:
            eur_wm_status = f"✅ CAN beat (+{improvement_pct:.2f}%)"
        else:
            eur_wm_status = "First exit"
        print(f"         RSI: {rsi_text} | EUR Watermark: {eur_wm_status}")
        print(f"         Consulting Grok for decision...")

        return opportunities
    
    def _detect_eth_reentry(self, market: Dict, watermark: float,
                           portfolio_value: float, min_improvement: float,
                           eur_entry_value: Optional[float] = None,
                           stop_loss_threshold: float = 2000.0) -> List[TradeOpportunity]:
        """Detect ETH re-entry opportunities from EUR - ALWAYS consult Grok"""
        opportunities = []

        btc_price = market["BTC"].price
        eth_price = market["ETH"].price
        eth_rsi = market["ETH"].rsi_estimate

        # ========================================================================
        # CRITICAL: STOP LOSS CHECK - Highest priority
        # ========================================================================
        if eur_entry_value is not None:
            current_loss = eur_entry_value - portfolio_value
            if current_loss >= stop_loss_threshold:
                # EMERGENCY: Stop loss triggered - buy back ETH immediately
                value_after_fee = portfolio_value * (1 - self.config.fee_rate)
                expected_qty = value_after_fee / market["ETH"].ask

                print(f"      🚨 STOP LOSS TRIGGERED: Loss €{current_loss:,.2f} >= €{stop_loss_threshold:,.2f}")
                print(f"         Emergency buy back to ETH (safe position)")

                opportunities.append(TradeOpportunity(
                    type="stop_loss",
                    from_asset="EUR",
                    to_asset="ETH",
                    expected_return=-current_loss / eur_entry_value,  # Negative return (loss)
                    confidence=0.99,  # Very high confidence - this is emergency
                    reasoning=f"STOP LOSS: Portfolio down €{current_loss:,.2f} from EUR entry. Buying back ETH as safe position (ETH is our home base)",
                    market_conditions={
                        "stop_loss_triggered": True,
                        "loss_eur": current_loss,
                        "eur_entry_value": eur_entry_value,
                        "current_value": portfolio_value,
                        "expected_eth_qty": expected_qty,
                        "eth_price": eth_price
                    },
                    expected_quantity=expected_qty
                ))

                # Return immediately - stop loss takes absolute priority
                return opportunities

        # Check if we can beat watermark
        value_after_fee = portfolio_value * (1 - self.config.fee_rate)
        expected_qty = value_after_fee / market["ETH"].ask

        if watermark == 0:
            # First ETH position
            opportunities.append(TradeOpportunity(
                type="initial_entry",
                from_asset="EUR",
                to_asset="ETH",
                expected_return=0.10,
                confidence=0.9,
                reasoning="First ETH position",
                market_conditions={"cycle_phase": self.cycle_analyzer.get_cycle_phase(btc_price)},
                expected_quantity=expected_qty
            ))
        else:
            # ========================================================================
            # EMERGENCY RE-ENTRY EXCEPTION - Extremely oversold conditions
            # ========================================================================
            # Check for emergency re-entry conditions that bypass watermark
            # This prevents getting stuck in EUR during extreme dips
            emergency_reentry = False
            emergency_reason = ""

            if eth_rsi and eth_rsi < 20:
                # RSI extremely oversold + check for buy signals
                _, reentry_signals = self.cycle_analyzer.should_reenter_eth(
                    btc_price, eth_price, eth_rsi
                )

                # Calculate loss percentage from watermark (using expected_qty after single fee)
                watermark_deficit = (1 - expected_qty/watermark) * 100

                # Allow emergency re-entry if:
                # 1. RSI < 20 (extremely oversold)
                # 2. Have buy signals OR deficit < 5%
                if reentry_signals or watermark_deficit < 5.0:
                    emergency_reentry = True
                    emergency_reason = f"EMERGENCY: RSI {eth_rsi:.0f} extremely oversold"
                    if reentry_signals:
                        emergency_reason += f", {reentry_signals}"

                    print(f"      🚨 Emergency Re-entry Override Activated")
                    print(f"         RSI: {eth_rsi:.0f} (extremely oversold)")
                    print(f"         Will get: {expected_qty:.6f} ETH (below watermark {watermark:.6f})")
                    print(f"         Reason: Prevent being stuck in EUR during extreme dip")

                    opportunities.append(TradeOpportunity(
                        type="emergency_entry",
                        from_asset="EUR",
                        to_asset="ETH",
                        expected_return=(expected_qty / watermark) - 1,
                        confidence=0.85,
                        reasoning=emergency_reason,
                        market_conditions={
                            "emergency": True,
                            "rsi": eth_rsi,
                            "expected_qty": expected_qty,
                            "watermark": watermark,
                            "deficit_pct": watermark_deficit
                        },
                        expected_quantity=expected_qty
                    ))

                    return opportunities

            # ========================================================================
            # STRICT ETH WATERMARK REQUIREMENT - Must beat watermark to enter
            # ========================================================================
            # Check if expected ETH (after paying entry fee) beats watermark
            # Fee is already deducted in expected_qty, don't deduct twice!
            required_qty = watermark * (1 + min_improvement)
            can_beat_watermark = expected_qty > required_qty
            improvement = (expected_qty / watermark) - 1

            if not can_beat_watermark:
                # CANNOT beat ETH watermark - do NOT create opportunity
                deficit = required_qty - expected_qty
                deficit_pct = (deficit / watermark) * 100

                print(f"      🔍 Hold/Enter Analysis: Checking ETH entry opportunity")
                print(f"         ❌ CANNOT beat ETH watermark (after fees): need {required_qty:.6f} ETH, can get {expected_qty:.6f} ETH")
                print(f"         Short by {deficit:.6f} ETH ({deficit_pct:.2f}%)")
                print(f"         ⏸️ HOLDING EUR until better entry price")
                return opportunities  # Return empty - no trade opportunity

            # CAN beat watermark - proceed with analyzing opportunity
            # Analyze trend
            trend_direction, trend_strength, trend_details = self._analyze_historical_trend(market)
            change_1h = trend_details.get("change_1h", 0)
            change_3h = trend_details.get("change_3h", 0)
            change_6h = trend_details.get("change_6h", 0)
            trend_summary = f"1h: {change_1h:+.2f}%, 3h: {change_3h:+.2f}%, 6h: {change_6h:+.2f}%"

            # Check cycle-based re-entry signals (for context)
            _, reentry_signals = self.cycle_analyzer.should_reenter_eth(
                btc_price, eth_price, eth_rsi
            )

            # Build hints
            hints = []
            confidence = 0.5  # Neutral - let Grok decide

            # Watermark is now enforced - if we got here, we CAN beat it
            hints.append(f"Can beat watermark by {improvement*100:.2f}%")
            confidence = 0.7

            # Trend hints
            if trend_direction == "downcycle":
                hints.append("Downcycle - price may drop more")
                confidence = max(confidence - 0.1, 0.2)
            elif trend_direction == "upcycle":
                hints.append("Upcycle - may miss opportunity")
                confidence = min(confidence + 0.1, 0.8)

            # RSI hints
            if eth_rsi:
                if eth_rsi < 25:
                    hints.append(f"Oversold RSI {eth_rsi:.0f} - good entry")
                    confidence = min(confidence + 0.15, 0.85)
                elif eth_rsi > 75:
                    hints.append(f"Overbought RSI {eth_rsi:.0f} - wait for pullback")
                    confidence = max(confidence - 0.1, 0.2)

            # Re-entry signals
            if reentry_signals:
                hints.append(f"Entry signals: {reentry_signals}")
                confidence = min(confidence + 0.1, 0.85)

            hint_text = ", ".join(hints) if hints else "No strong signals"

            opportunities.append(TradeOpportunity(
                type="hold_or_enter",
                from_asset="EUR",
                to_asset="ETH",
                expected_return=improvement,
                confidence=confidence,
                reasoning=f"Hold/Enter decision: {hint_text}. Trend: {trend_summary}",
                market_conditions={
                    "trend": trend_direction,
                    "trend_strength": trend_strength,
                    "improvement": improvement,
                    "can_beat_watermark": True,  # Always true if we got here
                    "expected_qty": expected_qty,
                    "required_qty": required_qty,
                    "rsi": eth_rsi,
                    "entry_signals": reentry_signals,
                    **trend_details
                },
                expected_quantity=expected_qty
            ))

            rsi_text = f"{eth_rsi:.0f}" if eth_rsi else "N/A"
            watermark_status = f"✅ CAN beat (+{improvement*100:.2f}%)"
            print(f"      🔍 Hold/Enter Analysis: {trend_direction.upper()} ({trend_strength})")
            print(f"         RSI: {rsi_text} | Watermark: {watermark_status}")
            print(f"         Consulting Grok for decision...")

        return opportunities
    
    def _detect_watermark_improvement(self, market: Dict, watermark: float,
                                     portfolio_value: float, min_improvement: float,
                                     cycle_phase: str) -> List[TradeOpportunity]:
        """Check if current ETH position beats watermark (shouldn't happen)"""
        opportunities = []

        # This is a safety check - we shouldn't be in ETH without beating watermark
        current_eth = portfolio_value / market["ETH"].bid

        # Only trigger if we're SIGNIFICANTLY below watermark (>0.5% below)
        # Small deviations (<0.5%) are normal due to bid/ask spread and price fluctuations
        watermark_ratio = current_eth / watermark if watermark > 0 else 1.0

        if watermark_ratio < 0.995:  # More than 0.5% below watermark
            # We're significantly below watermark - something went wrong
            deficit_pct = (1 - watermark_ratio) * 100
            opportunities.append(TradeOpportunity(
                type="safety_exit",
                from_asset="ETH",
                to_asset="EUR",
                expected_return=0.01,
                confidence=0.95,
                reasoning=f"Significantly below watermark ({deficit_pct:.1f}% deficit) - exit and wait for better entry",
                market_conditions={"cycle_phase": cycle_phase, "watermark_deficit": deficit_pct}
            ))

        return opportunities

# ==============================================================================
# GROK TRADER (Simplified for ETH/EUR)
# ==============================================================================

class GrokTrader:
    """Grok AI trader for ETH/EUR decisions"""

    def __init__(self, api_key: str, config: Config, get_recent_trades_fn=None, get_newsletters_fn=None):
        self.api_key = api_key
        self.config = config
        self.cycle_analyzer = CyclePositionAnalyzer(config)
        self.base_url = "https://api.x.ai/v1"
        self.get_recent_trades = get_recent_trades_fn
        self.get_newsletters = get_newsletters_fn
    
    def evaluate_opportunities(self, opportunities: List[TradeOpportunity],
                              state: Dict, market_info: Dict) -> Optional[TradeOpportunity]:
        """Select best opportunity"""

        if not opportunities:
            return None

        # Get recent trade history
        recent_trades = []
        last_trade_time = None
        minutes_since_last_trade = None

        if self.get_recent_trades:
            recent_trades = self.get_recent_trades(5)  # Get last 5 trades

            if recent_trades:
                # Calculate time since last trade
                last_trade_time_str = recent_trades[0]["timestamp"]
                last_trade_time = datetime.fromisoformat(last_trade_time_str)
                now = datetime.now()
                minutes_since_last_trade = (now - last_trade_time).total_seconds() / 60

                # CRITICAL: Minimum time buffer between trades (30 minutes)
                # Prevents overtrading and allows trends to develop
                if minutes_since_last_trade < 30.0:
                    print(f"      ⏸️ TRADE COOLDOWN: Last trade {minutes_since_last_trade:.1f} min ago (min: 30 min)")
                    print(f"         Last trade: {recent_trades[0]['from_asset']}→{recent_trades[0]['to_asset']}")

                    # Show what opportunities were blocked
                    if opportunities:
                        blocked_types = [opp.type for opp in opportunities[:3]]
                        print(f"         Blocked: {', '.join(blocked_types)}")

                    return None

        if not self.api_key:
            # Fallback: only execute entry opportunities without API key
            # For hold_or_exit decisions, default to HOLD (return None)
            for opp in opportunities:
                if opp.type in ["initial_exit", "cycle_entry"]:
                    return opp
            # For hold_or_exit, return None to HOLD
            return None

        btc_price = state["market"]["BTC"].price
        cycle_phase = self.cycle_analyzer.get_cycle_phase(btc_price)

        if minutes_since_last_trade:
            print(f"      ⏰ Last trade: {minutes_since_last_trade:.1f} minutes ago")

        print(f"      🌐 Calling Grok (Cycle: {cycle_phase})...")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        context = {
            "current_position": state["current_position"],
            "portfolio_value": state["portfolio_value"],
            "profit_pct": state.get("profit_pct", 0),
            "eth_watermark": state.get("watermark", 0),
            "eur_watermark": state.get("eur_watermark", 0),
            "cycle_phase": cycle_phase,
            "btc_price": btc_price,
            "eth_price": state["market"]["ETH"].price,
            "minutes_since_last_trade": minutes_since_last_trade,
            "open_markets": market_info.get("open_markets", []),
            "is_nyse_open": market_info.get("is_nyse_open", False),
            "is_major_market_open": market_info.get("is_major_market_open", False),
            "recent_trades": [
                {
                    "timestamp": trade["timestamp"],
                    "from_asset": trade["from_asset"],
                    "to_asset": trade["to_asset"],
                    "trade_type": trade["trade_type"],
                    "improvement": trade["improvement"],
                    "reasoning": trade["reasoning"]
                }
                for trade in recent_trades
            ] if recent_trades else [],
            "opportunities": [
                {
                    "index": i,
                    "type": opp.type,
                    "from": opp.from_asset,
                    "to": opp.to_asset,
                    "expected_return": opp.expected_return,
                    "confidence": opp.confidence,
                    "reasoning": opp.reasoning
                }
                for i, opp in enumerate(opportunities[:3])
            ]
        }
        
        # Format trade history for prompt
        trade_history_text = "No recent trades"
        if recent_trades:
            trade_history_text = "\n".join([
                f"   - {trade['timestamp']}: {trade['from_asset']}→{trade['to_asset']} "
                f"({trade['trade_type']}, {trade['reasoning']})"
                for trade in recent_trades[:3]
            ])

        time_since_text = f"{minutes_since_last_trade:.1f} minutes ago" if minutes_since_last_trade else "No trades yet"

        eur_watermark = state.get("eur_watermark", 0)
        eur_watermark_text = f"EUR WATERMARK: €{eur_watermark:,.2f} (STRICT - must beat on every exit)" if eur_watermark > 0 else "EUR WATERMARK: Not set yet"

        # Get recent newsletters
        newsletters = []
        newsletter_text = "No newsletters this week"
        if self.get_newsletters:
            newsletters = self.get_newsletters(7)  # Get last 7 days
            if newsletters:
                newsletter_parts = []
                for nl in newsletters[:2]:  # Limit to 2 most recent
                    week = nl.get("week_of", "Unknown")
                    summary = nl.get("summary", "")
                    content_preview = nl.get("content", "")[:500]  # First 500 chars

                    if summary:
                        newsletter_parts.append(f"   {week}: {summary}\n   Preview: {content_preview}...")
                    else:
                        newsletter_parts.append(f"   {week}:\n   {content_preview}...")

                newsletter_text = "\n\n".join(newsletter_parts)

        # Market hours status
        open_markets = market_info.get("open_markets", [])
        if market_info.get("is_nyse_open"):
            market_status_text = "MARKET HOURS: NYSE/NASDAQ open (9:30am-4pm ET) - High volume period"
        elif open_markets:
            market_status_text = f"MARKET HOURS: {', '.join(open_markets)} open - Moderate activity"
        else:
            market_status_text = "MARKET HOURS: All major markets closed - Lower volume, consider price moves carefully"

        system_prompt = f"""You are Grok, the PRIMARY decision maker for ETH/EUR trading. You are consulted on EVERY iteration.

CURRENT CYCLE: {cycle_phase}
BTC PRICE: €{btc_price:,.0f} (market indicator)
ETH WATERMARK: {state.get("watermark", 0):.6f} (STRICT - must beat on every entry)
{eur_watermark_text}
{market_status_text}

TRADE HISTORY (last {len(recent_trades)} trades):
{trade_history_text}

TIME SINCE LAST TRADE: {time_since_text}

CRYPTO NEWSLETTERS (Recent insights from trusted sources):
{newsletter_text}

IMPORTANT: Consider newsletter insights about market trends, sentiment, on-chain metrics, and macro factors in your decision-making. These are expert analyses that may highlight risks or opportunities not visible in price action alone.

YOUR ROLE:
You are the SOLE decision maker. The system provides hints and context, but YOU decide whether to:
- HOLD: Set should_trade=false (keep current position)
- EXIT/ENTER: Set should_trade=true and select the opportunity

OPPORTUNITY TYPES:
- "stop_loss": EMERGENCY - Portfolio loss >= €2,000 when holding EUR. ALWAYS execute immediately to buy back ETH (our safe home base).
- "emergency_entry": EMERGENCY - RSI <20 extremely oversold. Bypasses watermark to avoid being stuck in EUR during extreme dips. STRONGLY consider executing.
- "hold_or_exit": You're holding ETH - decide whether to exit to EUR or continue holding
- "hold_or_enter": You're holding EUR - decide whether to enter ETH or continue waiting
- "initial_exit": First exit from starting ETH position
- "cycle_entry": Re-entering ETH from EUR (must beat watermark)

DECISION GUIDELINES:
1. Consider the full context: trend direction, RSI, profit/loss, market hours, watermark status
2. Be patient - frequent trading costs fees (0.26% each way)
3. CRITICAL: BOTH watermarks are STRICTLY ENFORCED - you will ONLY see opportunities that can beat watermarks

WHEN HOLDING ETH (hold_or_exit):
4. If you see an opportunity, EUR watermark is ALREADY beaten (system enforces this)
5. FEE THRESHOLD: 0.26% covers this trade's fee. Profit must exceed this to avoid loss.
6. 6H WINDOW STRATEGY: Price is at/above 6h midpoint (system enforces). This prevents eager/premature selling after buying ETH at good prices. Ideal is to sell near 6h HIGH, but midpoint is practical heuristic.
7. WAVE-RIDING STRATEGY - PRIMARY EXIT SIGNAL: Check "trend" in market conditions. The trend classification (UPTREND/SIDEWAYS/DOWNTREND) is your PRIMARY exit signal.
   - UPTREND: HOLD - ride the wave even if >60 minutes, never exit into strength
   - SIDEWAYS: EXIT NOW if profit >0.26% - momentum lost, take profits. SIDEWAYS means the rally is OVER.
   - DOWNTREND: EXIT NOW if profit >0.26% - tide has turned, exit immediately
8. CRITICAL: SIDEWAYS IS NOT UPTREND. If trend shows SIDEWAYS, the wave has ended. Exit to lock in profits.
9. 1-HOUR EXIT WINDOW: Check "minutes_since_midpoint_exceeded". Provides urgency for SIDEWAYS/DOWNTREND:
   - >50 minutes + SIDEWAYS + profit >0.26% = MANDATORY EXIT
   - Time limit is REMOVED only during UPTREND (any strength)
10. EXIT if: profit > 0.26% AND (DOWNTREND OR SIDEWAYS OR RSI >75 OR profit >2% OR [>50min + SIDEWAYS])
11. HOLD if: profit < 0.26% - doesn't cover exit fee
12. HOLD if: trend = "UPTREND" (any strength) - ride the wave regardless of time
13. MANDATORY EXIT: If SIDEWAYS + profit >1% + >50min window = EXIT IMMEDIATELY. This is excellent profit with no upward momentum.

WHEN HOLDING EUR (hold_or_enter):
14. STOP LOSS OVERRIDE: If opportunity type is "stop_loss", ALWAYS execute immediately - no exceptions. This means we've lost €2,000+ and must return to our safe ETH position.
15. EMERGENCY RE-ENTRY: If opportunity type is "emergency_entry", STRONGLY consider executing - RSI <20 is extremely oversold and we need to avoid being stuck in EUR.
16. If you see a regular opportunity, ETH watermark is ALREADY beaten (system enforces this)
17. 6H WINDOW STRATEGY: Ideal is to buy near 6h LOW to maximize ETH accumulation. Wait for dips toward low end of range.
18. ENTER if: oversold RSI <30 + good ETH improvement, or strong upcycle trend starting from near 6h low
19. HOLD if: RSI is overbought (>75) even if can beat watermark - pullback likely
20. HOLD if: trend is downcycle but price not yet near 6h low - better entry coming
21. Consider: Will ETH price drop more toward 6h low, giving us even better entry? Patience yields more ETH.

CONSTRAINTS:
- 5-minute minimum between trades (enforced by system)
- ETH entries MUST beat ETH watermark by 0.1-0.15%+ (cycle-adjusted, STRICTLY ENFORCED)
- EUR exits MUST beat EUR watermark by 0.1%+ (STRICTLY ENFORCED)
- STOP LOSS: €2,000 maximum loss when holding EUR - triggers emergency buy back to ETH (bypasses watermark)
- EMERGENCY RE-ENTRY: RSI <20 triggers emergency re-entry to avoid EUR trap (bypasses watermark if deficit <5%)
- GOAL: Accumulate more ETH over time while preserving EUR value (ladder up on BOTH sides)

Respond with JSON:
{{
    "selected_index": 0-2 or null to HOLD,
    "reasoning": "your analysis and why you chose to hold or trade",
    "strategy": "aggressive/moderate/conservative",
    "should_trade": true/false
}}"""
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, indent=2)}
        ]
        
        data = {
            "model": "grok-3",
            "messages": messages,
            "temperature": 0.4,
            "response_format": {"type": "json_object"}
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=data,
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                content = result["choices"][0]["message"]["content"]
                decision = json.loads(content)

                # Check if Grok recommends trading
                should_trade = decision.get("should_trade", True)

                if not should_trade:
                    print(f"         ⏸️ Grok: HOLD - {decision.get('reasoning', 'No trade recommended')}")
                    return None

                selected_idx = decision.get("selected_index")
                if selected_idx is not None and 0 <= selected_idx < len(opportunities):
                    selected = opportunities[selected_idx]
                    print(f"         ✅ Grok: {selected.from_asset}→{selected.to_asset}")
                    print(f"         Strategy: {decision.get('strategy', 'unknown')}")
                    return selected
            
        except Exception as e:
            print(f"      ❌ Grok API Error: {e}")
        
        return opportunities[0] if opportunities else None

# ==============================================================================
# MAIN BOT
# ==============================================================================

class ETHEURBot:
    """ETH/EUR cycle-aware trading bot"""
    
    def __init__(self, config: Config):
        self.config = config
        self.current_position: Optional[Position] = None
        self.watermark = Watermark()
        self.initial_value = 0

        # Tracking
        self.total_trades = 0
        self.successful_trades = 0
        self.total_fees = 0

        # Stop loss tracking
        self.eur_entry_value: Optional[float] = None  # EUR value when we first exit to EUR
        self.stop_loss_threshold = 2000.0  # Maximum acceptable loss in EUR (reduced from €5,000)

        # Exit patience tracking
        self.exit_signal_first_seen: Optional[str] = None  # Timestamp of first exit signal
        self.exit_signal_count: int = 0  # How many iterations we've seen exit signal
        self.last_eth_price_at_signal: Optional[float] = None  # ETH price when signal first appeared

        # EUR→ETH consensus tracking (gather Grok opinions over 5 minutes)
        self.eth_entry_decisions: List[Dict] = []  # Store Grok decisions for EUR→ETH
        self.eth_entry_consensus_start: Optional[str] = None  # When we started collecting

        # 6-hour price tracking for ETH exit strategy
        self.eth_price_history: List[Tuple[datetime, float]] = []  # (timestamp, price) tuples for 6h window

        # Database - must be initialized before creating AI trader
        self.db_name = f"eth_eur_{config.bot_name.lower()}.db"
        self._init_db()

        # Components
        self.market_hours = MarketHoursDetector()
        self.market = KrakenMarketProvider()
        self.kalshi = KalshiProvider(config.kalshi_api_key, config.kalshi_private_key)
        self.opportunity_detector = ETHEUROpportunityDetector(config)

        # Connect market provider to opportunity detector for trend analysis
        self.opportunity_detector.market_provider = self.market

        # Pass get_recent_trades and get_newsletters methods to GrokTrader for context
        self.ai = GrokTrader(
            config.grok_api_key,
            config,
            get_recent_trades_fn=self.get_recent_trades,
            get_newsletters_fn=self.get_recent_newsletters
        )
        self.imessage = IMessageNotifier(config.imessage_recipient, config.bot_name) if config.enable_imessage else None

        self._print_header()
    
    def _print_header(self):
        """Print startup header"""
        print(f"""
╔══════════════════════════════════════════════════════════════════════════╗
║           ALPHA ARENA - ETH WATERMARK ACCUMULATION TRADER                ║
╠══════════════════════════════════════════════════════════════════════════╣
║  Strategy: STRICT ETH Watermark | Grok-Driven EUR Exits                 ║
║  Starting: {self.config.initial_eth} ETH | Goal: Accumulate More ETH                        ║
║  ETH Watermark: STRICT (cycle-based) | EUR Watermark: Reference Only    ║
║  Stop Loss: €2,000 max loss | Cycle Indicators: BTC Price Levels        ║
║  AI: Grok + Newsletter Context | Notifications: iMessage                 ║
║  📰 To add newsletter: Create newsletter.txt file during operation       ║
╚══════════════════════════════════════════════════════════════════════════╝""")
    
    def _init_db(self):
        """Initialize database"""
        self.conn = sqlite3.connect(self.db_name)
        self.cursor = self.conn.cursor()
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                from_asset TEXT,
                to_asset TEXT,
                amount REAL,
                fee REAL,
                new_quantity REAL,
                improvement REAL,
                trade_type TEXT,
                cycle_phase TEXT,
                reasoning TEXT
            )
        """)
        
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                portfolio_value REAL,
                profit_pct REAL,
                current_asset TEXT,
                eth_watermark REAL,
                cycle_phase TEXT,
                btc_price REAL,
                eth_price REAL
            )
        """)

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS newsletters (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                content TEXT,
                week_of TEXT,
                summary TEXT
            )
        """)

        self.conn.commit()

    def store_newsletter(self, content: str, week_of: str = None, summary: str = None):
        """Store newsletter content in database"""
        if not week_of:
            week_of = datetime.now().strftime("%Y-W%U")  # Year-Week format

        self.cursor.execute("""
            INSERT INTO newsletters (timestamp, content, week_of, summary)
            VALUES (?, ?, ?, ?)
        """, (datetime.now().isoformat(), content, week_of, summary))
        self.conn.commit()

        print(f"\n   📰 Newsletter stored for {week_of}")
        if summary:
            print(f"      Summary: {summary}")

    def get_recent_newsletters(self, days: int = 7) -> List[Dict]:
        """Get newsletters from the last N days"""
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()

        self.cursor.execute("""
            SELECT timestamp, content, week_of, summary
            FROM newsletters
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
        """, (cutoff_date,))

        rows = self.cursor.fetchall()
        newsletters = []
        for row in rows:
            newsletters.append({
                "timestamp": row[0],
                "content": row[1],
                "week_of": row[2],
                "summary": row[3]
            })

        return newsletters

    def check_for_newsletter_input(self):
        """Check if user wants to input a newsletter"""
        newsletter_file = "newsletter.txt"

        # Check if newsletter file exists
        if os.path.exists(newsletter_file):
            try:
                with open(newsletter_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()

                if content:
                    # Ask for optional summary
                    summary = input("\n   📰 Newsletter found! Optional summary (press Enter to skip): ").strip()
                    if not summary:
                        summary = None

                    self.store_newsletter(content, summary=summary)

                # Delete the file after reading
                os.remove(newsletter_file)

                return True
            except Exception as e:
                print(f"   ⚠️ Error reading newsletter file: {e}")
                return False

        return False

    def initialize(self):
        """Initialize with ETH"""
        # Load 6 hours of historical data from Kraken for better analysis
        if not self.market.history_loaded:
            self.market.load_historical_data(hours=6)

        # Populate eth_price_history with historical data (estimate timestamps)
        # Assume 1-minute intervals working backwards from now
        now = datetime.now()
        eth_history = list(self.market.price_history["ETH"])
        for i, price in enumerate(eth_history):
            # Calculate timestamp: oldest data is furthest back
            minutes_ago = len(eth_history) - i - 1
            timestamp = now - timedelta(minutes=minutes_ago)
            self.eth_price_history.append((timestamp, price))

        print("\n🔄 Fetching current market prices...")
        market = self.market.fetch()

        eth_price = market["ETH"].price
        btc_price = market["BTC"].price
        eth_rsi = market["ETH"].rsi_estimate

        self.current_position = Position(
            symbol="ETH",
            quantity=self.config.initial_eth,
            entry_price=eth_price,
            entry_time=datetime.now().isoformat()
        )

        # Set initial watermarks
        self.watermark.update(self.config.initial_eth)  # ETH watermark
        self.initial_value = self.config.initial_eth * eth_price
        self.watermark.update_eur(self.initial_value)  # EUR watermark = initial EUR value

        cycle_analyzer = CyclePositionAnalyzer(self.config)
        cycle_phase = cycle_analyzer.get_cycle_phase(btc_price)

        print(f"\n🚀 Bot Initialized")
        print(f"   Starting: {self.config.initial_eth} ETH @ €{eth_price:,.2f}")
        print(f"   Portfolio: €{self.initial_value:,.2f}")
        print(f"   Cycle Phase: {cycle_phase}")
        print(f"   BTC Price: €{btc_price:,.2f}")
        if eth_rsi:
            rsi_status = "Overbought" if eth_rsi > 70 else "Oversold" if eth_rsi < 30 else "Neutral"
            print(f"   ETH RSI: ~{eth_rsi:.0f} ({rsi_status})")
        print(f"   Price History: {len(self.market.price_history['ETH'])} data points loaded")
    
    def calculate_portfolio_value(self, market: Dict[str, MarketData]) -> float:
        """Calculate current portfolio value"""
        if not self.current_position:
            return 0

        if self.current_position.symbol == "EUR":
            return self.current_position.quantity
        else:  # ETH
            return self.current_position.quantity * market["ETH"].price

    def get_recent_trades(self, limit: int = 10) -> List[Dict]:
        """Get recent trades from database"""
        self.cursor.execute("""
            SELECT timestamp, from_asset, to_asset, amount, fee, new_quantity,
                   improvement, trade_type, cycle_phase, reasoning
            FROM trades
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

        rows = self.cursor.fetchall()
        trades = []
        for row in rows:
            trades.append({
                "timestamp": row[0],
                "from_asset": row[1],
                "to_asset": row[2],
                "amount": row[3],
                "fee": row[4],
                "new_quantity": row[5],
                "improvement": row[6],
                "trade_type": row[7],
                "cycle_phase": row[8],
                "reasoning": row[9]
            })

        # Return in chronological order (most recent first)
        return trades

    def confirm_trade(self, opportunity: TradeOpportunity, market: Dict[str, MarketData]) -> bool:
        """Prompt user to confirm trade execution with 30-second timeout"""
        import select

        from_asset = opportunity.from_asset
        to_asset = opportunity.to_asset

        # Calculate trade details
        old_value = self.calculate_portfolio_value(market)
        fee = old_value * self.config.fee_rate

        if to_asset == "EUR":
            new_qty = old_value * (1 - self.config.fee_rate)
            eth_price = market["ETH"].price
            equivalent_eth = new_qty / eth_price if eth_price > 0 else 0
            print(f"\n╔══════════════════════════════════════════════════════════════╗")
            print(f"║  TRADE CONFIRMATION: {from_asset} → {to_asset}")
            print(f"╠══════════════════════════════════════════════════════════════╣")
            print(f"║  Type: {opportunity.type}")
            print(f"║  Value: €{old_value:,.2f} | Fee: €{fee:.2f}")
            print(f"║  New Position EUR: €{new_qty:,.2f} (EUR)")
            print(f"║  New Position ETH: {equivalent_eth:.6f} (ETH)")
            print(f"║  Reason: {opportunity.reasoning}")
            print(f"╚══════════════════════════════════════════════════════════════╝")
        else:  # ETH
            new_price = market["ETH"].ask
            new_qty = (old_value * (1 - self.config.fee_rate)) / new_price
            equivalent_eur = new_qty * new_price
            print(f"\n╔══════════════════════════════════════════════════════════════╗")
            print(f"║  TRADE CONFIRMATION: {from_asset} → {to_asset}")
            print(f"╠══════════════════════════════════════════════════════════════╣")
            print(f"║  Type: {opportunity.type}")
            print(f"║  Value: €{old_value:,.2f} | Fee: €{fee:.2f}")
            print(f"║  New Position EUR: €{equivalent_eur:,.2f} (EUR)")
            print(f"║  New Position ETH: {new_qty:.6f} (ETH)")
            print(f"║  Reason: {opportunity.reasoning}")
            print(f"╚══════════════════════════════════════════════════════════════╝")

        # Prompt for confirmation with 30-second timeout
        try:
            print("\n   Execute this trade? (y/n) [30s timeout]: ", end='', flush=True)

            # Wait for input with timeout
            ready, _, _ = select.select([sys.stdin], [], [], 30.0)

            if ready:
                response = sys.stdin.readline().strip().lower()
                if response == 'y':
                    return True
                else:
                    print("   Trade cancelled")
                    return False
            else:
                # Timeout - no response
                print("timeout - continuing without trade")
                return False

        except (EOFError, KeyboardInterrupt):
            print("\n   Trade cancelled by user")
            return False
        except Exception as e:
            # If select is not available (e.g., Windows), fall back to simple input
            print(f"\n   (Timeout not available on this system)")
            try:
                response = input("   Execute this trade? (y/n): ").strip().lower()
                return response == 'y'
            except (EOFError, KeyboardInterrupt):
                print("\n   Trade cancelled by user")
                return False

    def execute_trade(self, opportunity: TradeOpportunity, market: Dict[str, MarketData]) -> bool:
        """Execute trade"""
        if not self.current_position or not opportunity:
            return False
        
        from_asset = opportunity.from_asset
        to_asset = opportunity.to_asset
        
        old_value = self.calculate_portfolio_value(market)
        fee = old_value * self.config.fee_rate
        
        if to_asset == "EUR":
            new_qty = old_value * (1 - self.config.fee_rate)
            new_price = 1.0
        else:  # ETH
            new_price = market["ETH"].ask
            new_qty = (old_value * (1 - self.config.fee_rate)) / new_price

            # CRITICAL: STRICT watermark enforcement - NO exceptions (not even for Grok)
            if self.watermark.get() > 0 and new_qty <= self.watermark.get():
                print(f"\n   ❌ TRADE REJECTED: Would get {new_qty:.6f} ETH, below watermark {self.watermark.get():.6f}")
                print(f"      Watermark strategy is STRICT - no overrides allowed")
                return False
        
        # Update position
        self.current_position = Position(
            symbol=to_asset,
            quantity=new_qty,
            entry_price=new_price,
            entry_time=datetime.now().isoformat()
        )
        
        # Update watermarks
        improvement = 0
        watermark_updated = False
        eur_watermark_updated = False

        if to_asset == "ETH":
            # Update ETH watermark (strict enforcement)
            old_watermark = self.watermark.get()
            watermark_updated = self.watermark.update_eth(new_qty)
            if watermark_updated:
                self.successful_trades += 1
                improvement = (new_qty / old_watermark - 1) if old_watermark > 0 else 1.0
            # Reset EUR entry value when we buy back ETH
            self.eur_entry_value = None
        elif to_asset == "EUR":
            # Update EUR watermark (reference only, not enforced)
            eur_watermark_updated = self.watermark.update_eur(new_qty)
            # Track EUR entry value for stop loss calculation
            self.eur_entry_value = new_qty
            # Reset exit patience tracking after successful EUR exit
            self.exit_signal_first_seen = None
            self.exit_signal_count = 0
            self.last_eth_price_at_signal = None
            # Reset midpoint exit window timer in opportunity detector
            self.opportunity_detector.midpoint_exceeded_at = None

        self.total_trades += 1
        self.total_fees += fee
        
        # Log trade
        btc_price = market["BTC"].price
        cycle_analyzer = CyclePositionAnalyzer(self.config)
        cycle_phase = cycle_analyzer.get_cycle_phase(btc_price)
        
        self.cursor.execute("""
            INSERT INTO trades (timestamp, from_asset, to_asset, amount, fee, 
                              new_quantity, improvement, trade_type, cycle_phase, reasoning)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), from_asset, to_asset, old_value, fee,
              new_qty, improvement, opportunity.type, cycle_phase, opportunity.reasoning))
        self.conn.commit()
        
        # Print trade info
        is_stop_loss = opportunity.type == "stop_loss"
        trade_icon = "🚨" if is_stop_loss else "🔄"
        trade_label = "STOP LOSS EXECUTED" if is_stop_loss else "TRADE EXECUTED"

        print(f"\n   {trade_icon} {trade_label}: {from_asset} → {to_asset}")
        print(f"      Type: {opportunity.type}")
        print(f"      Value: €{old_value:,.2f} | Fee: €{fee:.2f}")

        # Get current ETH price for equivalence calculation
        eth_price = market["ETH"].price

        # Always show both positions in equivalence
        if to_asset == "EUR":
            # Just sold ETH for EUR - show EUR and equivalent ETH value
            equivalent_eth = new_qty / eth_price if eth_price > 0 else 0
            print(f"      New Position EUR: €{new_qty:,.2f} (EUR)")
            print(f"      New Position ETH: {equivalent_eth:.6f} (ETH)")
            if eur_watermark_updated:
                print(f"      💶 NEW EUR WATERMARK: €{new_qty:,.2f}")
            elif self.watermark.get_eur() > 0:
                eur_ratio = new_qty / self.watermark.get_eur()
                if eur_ratio >= 0.98:
                    print(f"      💶 EUR: {eur_ratio*100:.1f}% of watermark (€{self.watermark.get_eur():,.2f})")
                else:
                    print(f"      ⚠️ EUR: {eur_ratio*100:.1f}% of watermark (€{self.watermark.get_eur():,.2f})")
        else:
            # Just bought ETH with EUR - show ETH and equivalent EUR value
            equivalent_eur = new_qty * new_price
            print(f"      New Position EUR: €{equivalent_eur:,.2f} (EUR)")
            print(f"      New Position ETH: {new_qty:.6f} (ETH)")
            if watermark_updated:
                print(f"      🏔️ NEW ETH WATERMARK: {new_qty:.6f} ETH (+{improvement*100:.3f}%)")
            elif is_stop_loss:
                # Stop loss emergency - may be below watermark
                if self.watermark.get() > 0:
                    deficit = (1 - new_qty / self.watermark.get()) * 100
                    print(f"      🚨 STOP LOSS: {new_qty:.6f} ETH ({deficit:.2f}% below watermark - emergency safety trade)")
                else:
                    print(f"      🚨 STOP LOSS: Emergency return to ETH (safe position)")

        print(f"      Reason: {opportunity.reasoning}")
        
        # Send notification
        if self.imessage:
            trade_info = {
                "from_asset": from_asset,
                "to_asset": to_asset,
                "type": opportunity.type,
                "value": old_value,
                "fee": fee,
                "new_quantity": new_qty,
                "new_price": new_price,
                "reasoning": opportunity.reasoning,
                "eth_price": market["ETH"].price
            }
            self.imessage.send_trade_notification(trade_info)
        
        return True
    
    def run_iteration(self):
        """Run one trading iteration"""

        # Check for newsletter input
        self.check_for_newsletter_input()

        market = self.market.fetch()
        market_info = self.market_hours.get_market_session_info()
        
        portfolio_value = self.calculate_portfolio_value(market)
        profit = portfolio_value - self.initial_value
        profit_pct = (profit / self.initial_value) * 100
        
        btc_price = market["BTC"].price
        eth_price = market["ETH"].price
        cycle_analyzer = CyclePositionAnalyzer(self.config)
        cycle_phase = cycle_analyzer.get_cycle_phase(btc_price)
        min_improvement = cycle_analyzer.get_minimum_improvement(btc_price, market_info)

        # Track 6-hour high/low for ETH exit strategy
        now = datetime.now()
        self.eth_price_history.append((now, eth_price))

        # Keep only last 6 hours of data
        six_hours_ago = now - timedelta(hours=6)
        self.eth_price_history = [(ts, price) for ts, price in self.eth_price_history if ts > six_hours_ago]

        # Calculate high/low from 6-hour window
        if self.eth_price_history:
            prices = [price for _, price in self.eth_price_history]
            eth_6h_high = max(prices)
            eth_6h_low = min(prices)
        else:
            eth_6h_high = eth_price
            eth_6h_low = eth_price
        
        print(f"\n{'='*70}")
        print(f"🏔️ ETH/EUR TRADER - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*70}")
        
        print(f"\n📊 CYCLE STATUS:")
        print(f"   Phase: {cycle_phase}")
        print(f"   BTC Price: €{btc_price:,.2f}")
        print(f"   Required ETH Improvement: {min_improvement*100:.3f}%")
        
        print(f"\n💰 PERFORMANCE:")
        print(f"   Portfolio: €{portfolio_value:,.2f}")
        print(f"   Profit: €{profit:+,.2f} ({profit_pct:+.2f}%)")
        print(f"   Trades: {self.total_trades} (Successful: {self.successful_trades})")
        
        print(f"\n📍 POSITION: {self.current_position.symbol}")
        if self.current_position.symbol == "EUR":
            print(f"   €{self.current_position.quantity:,.2f}")
            # Show stop loss status
            if self.eur_entry_value is not None:
                current_loss = self.eur_entry_value - portfolio_value
                loss_pct = (current_loss / self.eur_entry_value) * 100 if self.eur_entry_value > 0 else 0
                remaining_buffer = self.stop_loss_threshold - current_loss

                if current_loss >= self.stop_loss_threshold:
                    print(f"   🚨 STOP LOSS TRIGGERED: -€{current_loss:,.2f} ({loss_pct:.2f}%)")
                elif remaining_buffer < 1000:
                    print(f"   ⚠️ Stop Loss Warning: -€{current_loss:,.2f} ({loss_pct:.2f}%) | Buffer: €{remaining_buffer:,.2f}")
                else:
                    print(f"   🛡️ Stop Loss: -€{current_loss:,.2f} ({loss_pct:.2f}%) | Safe: €{remaining_buffer:,.2f} buffer")
        else:
            print(f"   {self.current_position.quantity:.6f} ETH @ €{eth_price:,.2f}")
        
        print(f"\n🏔️ WATERMARKS:")
        print(f"   ETH: {self.watermark.get():.6f} ETH (STRICT: must beat by {min_improvement*100:.3f}%+)")
        if self.watermark.get_eur() > 0:
            print(f"   EUR: €{self.watermark.get_eur():,.2f} (reference only)")

        # Show newsletter status
        newsletters = self.get_recent_newsletters(7)
        if newsletters:
            print(f"\n📰 NEWSLETTERS:")
            for nl in newsletters[:2]:  # Show up to 2 most recent
                week = nl.get("week_of", "Unknown")
                summary = nl.get("summary", "")
                timestamp = nl.get("timestamp", "")
                if summary:
                    print(f"   {week}: {summary}")
                else:
                    # Show first 100 chars if no summary
                    preview = nl.get("content", "")[:100]
                    print(f"   {week}: {preview}...")
        else:
            print(f"\n📰 NEWSLETTERS: None (create newsletter.txt to add)")

        print(f"\n📈 MARKET:")
        eth_data = market["ETH"]
        print(f"   ETH: €{eth_price:,.2f}")
        print(f"      Momentum: 5m {eth_data.momentum_5m:+.2f}% | 1h {eth_data.momentum_1h:+.2f}%")
        if eth_data.rsi_estimate:
            rsi_status = "Overbought" if eth_data.rsi_estimate > 70 else "Oversold" if eth_data.rsi_estimate < 30 else "Neutral"
            print(f"      RSI: ~{eth_data.rsi_estimate:.0f} ({rsi_status})")
        
        # Detect opportunities
        state = {
            "current_position": self.current_position.symbol,
            "portfolio_value": portfolio_value,
            "profit_pct": profit_pct,
            "watermark": self.watermark.get(),
            "eur_watermark": self.watermark.get_eur(),
            "eur_entry_value": self.eur_entry_value,
            "stop_loss_threshold": self.stop_loss_threshold,
            "market": market,
            "eth_6h_high": eth_6h_high,
            "eth_6h_low": eth_6h_low
        }
        
        opportunities = self.opportunity_detector.detect_opportunities(state, market_info)
        
        if opportunities:
            print(f"\n🎯 OPPORTUNITIES: {len(opportunities)}")
            for i, opp in enumerate(opportunities[:2]):
                print(f"   {i+1}. {opp.from_asset}→{opp.to_asset}: {opp.type}")
                print(f"      Return: {opp.expected_return*100:.2f}% | Confidence: {opp.confidence:.0%}")
                if opp.expected_quantity is not None and opp.to_asset == "ETH":
                    print(f"      Expected ETH: {opp.expected_quantity:.6f} ETH")
                print(f"      {opp.reasoning}")
        else:
            print(f"\n⏸️ No opportunities")
        
        # AI evaluation
        if opportunities:
            print(f"\n🤖 Grok Analysis...")
            selected = self.ai.evaluate_opportunities(opportunities, state, market_info)

            if selected:
                print(f"\n   ✅ SIGNAL: {selected.from_asset}→{selected.to_asset}")
                print(f"      Type: {selected.type}")
                print(f"      Reasoning: {selected.reasoning}")

                # EUR→ETH entries: Collect opinions over 5 minutes for consensus
                if selected.from_asset == "EUR" and selected.to_asset == "ETH":
                    now = datetime.now()

                    # Start consensus tracking if not already started
                    if self.eth_entry_consensus_start is None:
                        self.eth_entry_consensus_start = now.isoformat()
                        self.eth_entry_decisions = []
                        print(f"\n   📊 Starting EUR→ETH consensus tracking (5 minutes)...")

                    # Record Grok's decision
                    self.eth_entry_decisions.append({
                        "timestamp": now.isoformat(),
                        "decision": "BUY",  # Grok approved
                        "reasoning": selected.reasoning,
                        "expected_qty": selected.expected_quantity,
                        "confidence": selected.confidence
                    })

                    # Check if 5 minutes have elapsed
                    start_time = datetime.fromisoformat(self.eth_entry_consensus_start)
                    elapsed_minutes = (now - start_time).total_seconds() / 60

                    # Calculate current consensus status
                    total_checks = len(self.eth_entry_decisions)
                    buy_votes = len([d for d in self.eth_entry_decisions if d["decision"] == "BUY"])
                    buy_pct = (buy_votes / total_checks * 100) if total_checks > 0 else 0

                    if elapsed_minutes >= 5.0:
                        # Analyze final consensus
                        print(f"\n   📊 EUR→ETH CONSENSUS COMPLETE:")
                        print(f"      Duration: {elapsed_minutes:.1f} minutes")
                        print(f"      Grok checks: {total_checks}")
                        print(f"      Buy signals: {buy_votes}/{total_checks} ({buy_pct:.0f}%)")

                        # Execute if strong consensus (>60% buy)
                        if buy_pct >= 60:
                            print(f"      ✅ CONSENSUS: BUY ({buy_pct:.0f}% agreement)")
                            print(f"\n   🤖 Executing consensus decision...")

                            # Execute trade autonomously
                            self.execute_trade(selected, market)
                        else:
                            print(f"      ⏸️ CONSENSUS: HOLD ({buy_pct:.0f}% agreement - need 60%+)")

                        # Reset consensus tracking
                        self.eth_entry_consensus_start = None
                        self.eth_entry_decisions = []
                    else:
                        remaining = 5.0 - elapsed_minutes
                        print(f"      📊 Collecting opinions... ({total_checks} checks, {remaining:.1f}min remaining)")
                        print(f"      Current agreement: {buy_votes}/{total_checks} ({buy_pct:.0f}%)")

                # ETH→EUR exits: Execute immediately (no consensus needed)
                else:
                    print(f"\n   🤖 Executing Grok's decision...")

                    # Execute trade autonomously
                    self.execute_trade(selected, market)
            else:
                print(f"   ⏸️ HOLD")

                # PREDICTION MARKET OVERRIDE: Check if we should force exit despite Grok saying HOLD
                # Only for ETH→EUR exits (not EUR→ETH entries)
                if (self.current_position and self.current_position.symbol == "ETH" and
                    opportunities and opportunities[0].to_asset == "EUR"):

                    # Get the exit opportunity
                    exit_opp = opportunities[0]
                    market_conditions = exit_opp.market_conditions

                    # Check if we have trend information
                    trend = market_conditions.get("trend", "").lower()
                    # trend_details are spread directly into market_conditions via **trend_details
                    change_3h = market_conditions.get("change_3h", 0)
                    change_6h = market_conditions.get("change_6h", 0)

                    # Detect "downturn within upcycle": trend is SIDEWAYS/DOWNTREND but 3h/6h still positive
                    is_downturn_in_upcycle = (
                        trend in ["sideways", "downcycle"] and
                        (change_3h > 0 or change_6h > 0)  # Longer timeframes still positive
                    )

                    if is_downturn_in_upcycle:
                        # Check prediction markets (Kalshi) and RSI
                        print(f"\n   📊 Override Check (Kalshi + RSI)...")
                        print(f"      Detected downturn within upcycle (trend: {trend}, 3h: {change_3h:+.2f}%, 6h: {change_6h:+.2f}%)")

                        override_triggered = False
                        override_reason = ""

                        # Check Kalshi prediction market
                        kalshi_prediction = self.kalshi.get_eth_prediction()

                        if kalshi_prediction:
                            prob = kalshi_prediction["probability"]
                            market_type = kalshi_prediction.get("market_type", "unknown")
                            ticker = kalshi_prediction.get("ticker", "")
                            print(f"      Kalshi ({market_type}): {prob:.1f}% bullish")
                            print(f"      Market: {kalshi_prediction['market_title']}")
                            if ticker:
                                print(f"      Ticker: {ticker}")

                            # High bullish sentiment during downturn suggests local peak
                            if prob > 60 and kalshi_prediction.get("available", False):
                                override_triggered = True
                                override_reason = f"Kalshi {prob:.1f}% bullish despite downturn = local peak"
                            else:
                                print(f"      Kalshi: {prob:.1f}% (need >60% for override)")

                        # RSI-based fallback: If Kalshi unavailable or didn't trigger, check RSI
                        if not override_triggered:
                            rsi = market_conditions.get("rsi")
                            profit_pct = market_conditions.get("profit_pct", 0)

                            if rsi and rsi > 70:
                                # SIDEWAYS + Overbought RSI + at a loss = strong exit signal
                                print(f"      RSI: {rsi:.0f} (Overbought >70)")
                                if profit_pct < 0:
                                    override_triggered = True
                                    override_reason = f"SIDEWAYS + Overbought RSI {rsi:.0f} + Loss {profit_pct:.2f}%"
                                    print(f"      ⚠️ RSI Override: Overbought + losing position in sideways market")
                                else:
                                    print(f"      RSI suggests caution but profit is positive ({profit_pct:+.2f}%)")
                            else:
                                rsi_text = f"{rsi:.0f}" if rsi else "N/A"
                                print(f"      RSI: {rsi_text} (need >70 for override)")

                        # Execute override if triggered
                        if override_triggered:
                            print(f"\n   ⚠️ OVERRIDE TRIGGERED: Force exit")
                            print(f"      Reasoning: {override_reason}")
                            print(f"      Action: SELL to prevent further losses / lock in gains")

                            # Override Grok's HOLD decision
                            selected = exit_opp
                            print(f"\n   🤖 Executing override decision...")
                            self.execute_trade(selected, market)
                            return  # Exit early to prevent duplicate processing
                        else:
                            print(f"      No override triggered - continuing to HOLD")

                # If we're tracking consensus but Grok says hold, record it
                if self.current_position and self.current_position.symbol == "EUR":
                    if self.eth_entry_consensus_start is not None:
                        now = datetime.now()
                        self.eth_entry_decisions.append({
                            "timestamp": now.isoformat(),
                            "decision": "HOLD",
                            "reasoning": "Grok recommended hold",
                            "expected_qty": None,
                            "confidence": 0
                        })

                        # Check if 5 minutes elapsed
                        start_time = datetime.fromisoformat(self.eth_entry_consensus_start)
                        elapsed_minutes = (now - start_time).total_seconds() / 60

                        # Calculate current consensus status
                        total_checks = len(self.eth_entry_decisions)
                        buy_votes = len([d for d in self.eth_entry_decisions if d["decision"] == "BUY"])
                        buy_pct = (buy_votes / total_checks * 100) if total_checks > 0 else 0

                        if elapsed_minutes >= 5.0:
                            # Final consensus: HOLD
                            print(f"\n   📊 EUR→ETH CONSENSUS COMPLETE:")
                            print(f"      Duration: {elapsed_minutes:.1f} minutes")
                            print(f"      Grok checks: {total_checks}")
                            print(f"      Buy signals: {buy_votes}/{total_checks} ({buy_pct:.0f}%)")
                            print(f"      ⏸️ CONSENSUS: HOLD ({buy_pct:.0f}% agreement - need 60%+)")

                            # Reset consensus tracking
                            self.eth_entry_consensus_start = None
                            self.eth_entry_decisions = []
                        else:
                            remaining = 5.0 - elapsed_minutes
                            print(f"      📊 Consensus tracking: {total_checks} checks, {remaining:.1f}min remaining")
                            print(f"      Current agreement: {buy_votes}/{total_checks} ({buy_pct:.0f}%)")
        
        # Log performance
        self.cursor.execute("""
            INSERT INTO performance (timestamp, portfolio_value, profit_pct, current_asset,
                                   eth_watermark, cycle_phase, btc_price, eth_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), portfolio_value, profit_pct, 
              self.current_position.symbol, self.watermark.get(),
              cycle_phase, btc_price, eth_price))
        self.conn.commit()
    
    def run(self):
        """Main loop"""
        self.initialize()

        while True:
            try:
                market_info = self.market_hours.get_market_session_info()

                # Fixed interval - always check every 30 seconds
                interval = 30

                self.run_iteration()

                # Show market status in next check message
                market_status = ""
                if market_info["is_nyse_open"]:
                    market_status = " (NYSE open)"
                elif market_info["is_major_market_open"]:
                    market_status = f" ({', '.join(market_info['open_markets'])} open)"
                else:
                    market_status = " (markets closed)"

                print(f"\n⏰ Next check in {interval}s{market_status}...")
                time.sleep(interval)
                
            except KeyboardInterrupt:
                print(f"\n\n{'='*70}")
                print(f"🏁 FINAL RESULTS")
                
                market = self.market.fetch()
                final_value = self.calculate_portfolio_value(market)
                final_profit = final_value - self.initial_value
                final_profit_pct = (final_profit / self.initial_value) * 100
                
                print(f"\n💰 SUMMARY:")
                print(f"   Initial: €{self.initial_value:,.2f}")
                print(f"   Final: €{final_value:,.2f}")
                print(f"   PROFIT: €{final_profit:+,.2f} ({final_profit_pct:+.2f}%)")
                
                if final_profit_pct >= 100:
                    print(f"\n   🎉 ACHIEVED 2X RETURN! 🎉")
                
                print(f"\n   ETH Watermark: {self.watermark.get():.6f}")
                print(f"   Total Trades: {self.total_trades}")
                
                break
                
            except Exception as e:
                print(f"Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(60)

# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    config = Config(
        llm_provider="grok",
        bot_name="ETH-EUR-CYCLE",
        initial_eth=87.00,
        enable_imessage=True,
        imessage_recipient="oscar.castaneda@gmail.com"
    )
    
    bot = ETHEURBot(config)
    bot.run()
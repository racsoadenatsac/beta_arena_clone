#!/usr/bin/env python3
"""
Alpha Arena Competition Bot - ETH/EUR Cycle-Aware Watermark Strategy
Goal: Beat ETH watermark with cycle-adjusted requirements
Starting Capital: 87.00 ETH
Uses: Live Kraken API, cycle indicators, strategic EUR positioning

Strategy:
- Trade only ETH (with watermark) and EUR (no watermark)
- Dynamic watermark requirements based on BTC price levels (as market indicator)
- Strategic EUR exits/entries based on cycle position
- STRICT watermark enforcement for ETH
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
    min_improvement_late_bull: float = 0.005       # 0.5% - Late cycle
    min_improvement_euphoria: float = 0.01         # 1% - Top signals
    
    # Fees (Kraken)
    fee_rate: float = 0.0026  # 0.26%
    
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
        
        # Signal 1: Late cycle with significant profit
        if phase in ["late_bull", "euphoria"] and profit_pct > 30:
            signals.append(f"Late cycle profit ({profit_pct:.1f}%)")
        
        # Signal 2: ETH extremely overbought
        if eth_rsi and eth_rsi > self.config.extreme_overbought_rsi:
            signals.append(f"ETH overbought (RSI {eth_rsi:.0f})")
        
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
        
        message = f"🔄 TRADE EXECUTED: {from_asset} → {to_asset}\n"
        message += f"      Type: {trade_type}\n"
        message += f"      Value: €{value:,.2f} | Fee: €{fee:.2f}\n"
        
        if to_asset == "EUR":
            message += f"      New Position: €{new_qty:,.2f}\n"
        else:
            message += f"      New Position: {new_qty:.6f} {to_asset} @ €{new_price:,.2f}\n"
        
        message += f"      Reason: {reasoning}"
        
        return message

# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class Watermark:
    """Track ETH watermark only"""
    eth_quantity: float = 0.0
    eth_achieved_at: Optional[str] = None
    
    def update(self, quantity: float) -> bool:
        """Update watermark if new quantity is higher"""
        if quantity > self.eth_quantity:
            self.eth_quantity = quantity
            self.eth_achieved_at = datetime.now().isoformat()
            return True
        return False
    
    def get(self) -> float:
        """Get current ETH watermark"""
        return self.eth_quantity

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

# ==============================================================================
# KRAKEN MARKET PROVIDER (Simplified for ETH and BTC tracking)
# ==============================================================================

class KrakenMarketProvider:
    """Market data provider"""
    
    def __init__(self):
        self.base_url = "https://api.kraken.com/0/public"
        self.price_history = {
            "ETH": deque(maxlen=60),
            "BTC": deque(maxlen=60)  # Track BTC for cycle analysis
        }
        self.last_prices = {"ETH": None, "BTC": None}
        self.five_min_history = {
            "ETH": deque(maxlen=5),
            "BTC": deque(maxlen=5)
        }
    
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
    
    def detect_opportunities(self, state: Dict, market_info: Dict) -> List[TradeOpportunity]:
        """Detect trading opportunities"""
        opportunities = []
        
        current_asset = state["current_position"]
        market = state["market"]
        watermark = state["watermark"]
        portfolio_value = state["portfolio_value"]
        profit_pct = state.get("profit_pct", 0)
        btc_price = market["BTC"].price
        eth_price = market["ETH"].price
        
        # Get cycle-adjusted minimum improvement
        min_improvement = self.cycle_analyzer.get_minimum_improvement(btc_price, market_info)
        cycle_phase = self.cycle_analyzer.get_cycle_phase(btc_price)
        
        print(f"      📊 Cycle: {cycle_phase} | Min improvement: {min_improvement*100:.3f}%")
        
        if current_asset == "EUR":
            # Look for ETH re-entry opportunities
            reentry_ops = self._detect_eth_reentry(
                market, watermark, portfolio_value, min_improvement
            )
            opportunities.extend(reentry_ops)
        
        elif current_asset == "ETH":
            # Check for EUR exit signals
            exit_ops = self._detect_eur_exit(
                market, profit_pct, btc_price, eth_price
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
                        btc_price: float, eth_price: float) -> List[TradeOpportunity]:
        """Detect opportunities to exit to EUR"""
        opportunities = []
        
        eth_data = market["ETH"]
        
        # Check cycle-based exit signals
        should_exit, reasoning = self.cycle_analyzer.should_exit_to_eur(
            btc_price, eth_price, profit_pct, eth_data.rsi_estimate
        )
        
        if should_exit:
            opportunities.append(TradeOpportunity(
                type="cycle_exit",
                from_asset="ETH",
                to_asset="EUR",
                expected_return=0.05,
                confidence=0.8,
                reasoning=f"Cycle exit: {reasoning}",
                market_conditions={
                    "cycle_phase": self.cycle_analyzer.get_cycle_phase(btc_price),
                    "profit_pct": profit_pct
                }
            ))
        
        return opportunities
    
    def _detect_eth_reentry(self, market: Dict, watermark: float,
                           portfolio_value: float, min_improvement: float) -> List[TradeOpportunity]:
        """Detect ETH re-entry opportunities from EUR"""
        opportunities = []
        
        btc_price = market["BTC"].price
        eth_price = market["ETH"].price
        eth_rsi = market["ETH"].rsi_estimate
        
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
                market_conditions={"cycle_phase": self.cycle_analyzer.get_cycle_phase(btc_price)}
            ))
        else:
            # Need to beat watermark
            improvement = (expected_qty / watermark) - 1
            required_qty = watermark * (1 + min_improvement)
            
            if expected_qty > required_qty:
                # Check cycle-based re-entry signals
                should_reenter, reentry_reasoning = self.cycle_analyzer.should_reenter_eth(
                    btc_price, eth_price, eth_rsi
                )
                
                if should_reenter or improvement > min_improvement * 2:
                    opportunities.append(TradeOpportunity(
                        type="cycle_entry",
                        from_asset="EUR",
                        to_asset="ETH",
                        expected_return=improvement,
                        confidence=0.85,
                        reasoning=f"Re-entry: {reentry_reasoning}, beats watermark by {improvement*100:.3f}%",
                        market_conditions={
                            "cycle_phase": self.cycle_analyzer.get_cycle_phase(btc_price),
                            "improvement": improvement
                        }
                    ))
        
        return opportunities
    
    def _detect_watermark_improvement(self, market: Dict, watermark: float,
                                     portfolio_value: float, min_improvement: float,
                                     cycle_phase: str) -> List[TradeOpportunity]:
        """Check if current ETH position beats watermark (shouldn't happen)"""
        opportunities = []
        
        # This is a safety check - we shouldn't be in ETH without beating watermark
        current_eth = portfolio_value / market["ETH"].bid
        
        if current_eth <= watermark:
            # We're below watermark - should exit to EUR and wait
            opportunities.append(TradeOpportunity(
                type="safety_exit",
                from_asset="ETH",
                to_asset="EUR",
                expected_return=0.01,
                confidence=0.95,
                reasoning=f"Below watermark - exit and wait for better entry",
                market_conditions={"cycle_phase": cycle_phase}
            ))
        
        return opportunities

# ==============================================================================
# GROK TRADER (Simplified for ETH/EUR)
# ==============================================================================

class GrokTrader:
    """Grok AI trader for ETH/EUR decisions"""

    def __init__(self, api_key: str, config: Config, get_recent_trades_fn=None):
        self.api_key = api_key
        self.config = config
        self.cycle_analyzer = CyclePositionAnalyzer(config)
        self.base_url = "https://api.x.ai/v1"
        self.get_recent_trades = get_recent_trades_fn
    
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

                # CRITICAL: Minimum time buffer between trades (5 minutes)
                # Only allow trades within 5 minutes if it's a critical safety exit
                if minutes_since_last_trade < 5.0:
                    # Check if any opportunity is a critical safety trade
                    is_critical = any(opp.type == "safety_exit" for opp in opportunities)

                    if not is_critical:
                        print(f"      ⏸️ TRADE COOLDOWN: Last trade {minutes_since_last_trade:.1f} min ago (min: 5 min)")
                        print(f"         Last trade: {recent_trades[0]['from_asset']}→{recent_trades[0]['to_asset']}")
                        return None

        if not self.api_key:
            # Fallback: prioritize cycle signals
            for opp in opportunities:
                if opp.type in ["cycle_exit", "cycle_entry"]:
                    return opp
            return opportunities[0]

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
            "cycle_phase": cycle_phase,
            "btc_price": btc_price,
            "eth_price": state["market"]["ETH"].price,
            "minutes_since_last_trade": minutes_since_last_trade,
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

        system_prompt = f"""You are Grok, trading ETH/EUR with cycle awareness.

CURRENT CYCLE: {cycle_phase}
BTC PRICE: €{btc_price:,.0f} (market indicator)
ETH WATERMARK: {state.get("watermark", 0):.6f}

TRADE HISTORY (last {len(recent_trades)} trades):
{trade_history_text}

TIME SINCE LAST TRADE: {time_since_text}

CRITICAL RULES:
1. NEVER make contradictory trades within 5 minutes unless market conditions have dramatically changed
2. If we just entered ETH, DO NOT immediately exit unless there's a critical risk (>3% drop, extreme overbought)
3. If we just exited to EUR, DO NOT immediately re-enter unless RSI shows extreme oversold (<20) AND significant price improvement
4. Consider the reasoning of recent trades - don't repeat failed strategies
5. Respect the watermark system - every ETH entry must beat the previous watermark

STRATEGY:
- Only trade ETH (with watermark) and EUR (no watermark)
- Accumulation phase: AGGRESSIVE (accept small improvements)
- Late bull/Euphoria: CONSERVATIVE (require large improvements)
- Exit to EUR when multiple top signals present
- Re-enter ETH when cycle bottoms and beats watermark

Respond with JSON:
{{
    "selected_index": 0-2 or null to HOLD,
    "reasoning": "why this trade fits the cycle and doesn't contradict recent trades",
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

        # Database - must be initialized before creating AI trader
        self.db_name = f"eth_eur_{config.bot_name.lower()}.db"
        self._init_db()

        # Components
        self.market_hours = MarketHoursDetector()
        self.market = KrakenMarketProvider()
        self.opportunity_detector = ETHEUROpportunityDetector(config)

        # Pass get_recent_trades method to GrokTrader for context
        self.ai = GrokTrader(config.grok_api_key, config, get_recent_trades_fn=self.get_recent_trades)
        self.imessage = IMessageNotifier(config.imessage_recipient, config.bot_name) if config.enable_imessage else None

        self._print_header()
    
    def _print_header(self):
        """Print startup header"""
        print(f"""
╔══════════════════════════════════════════════════════════════════════════╗
║               ALPHA ARENA - ETH/EUR CYCLE-AWARE TRADER                   ║
╠══════════════════════════════════════════════════════════════════════════╣
║  Strategy: ETH Watermark + EUR Strategic Positioning                     ║
║  Starting: {self.config.initial_eth} ETH | Target: 2x Return                         ║
║  Cycle Indicators: BTC Price Levels                                      ║
║  AI: Grok | Notifications: iMessage                                      ║
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
        
        self.conn.commit()
    
    def initialize(self):
        """Initialize with ETH"""
        print("\n🔄 Fetching initial market prices...")
        market = self.market.fetch()
        
        eth_price = market["ETH"].price
        btc_price = market["BTC"].price
        
        self.current_position = Position(
            symbol="ETH",
            quantity=self.config.initial_eth,
            entry_price=eth_price,
            entry_time=datetime.now().isoformat()
        )
        
        self.watermark.update(self.config.initial_eth)
        self.initial_value = self.config.initial_eth * eth_price
        
        cycle_analyzer = CyclePositionAnalyzer(self.config)
        cycle_phase = cycle_analyzer.get_cycle_phase(btc_price)
        
        print(f"\n🚀 Bot Initialized")
        print(f"   Starting: {self.config.initial_eth} ETH @ €{eth_price:,.2f}")
        print(f"   Portfolio: €{self.initial_value:,.2f}")
        print(f"   Cycle Phase: {cycle_phase}")
        print(f"   BTC Price: €{btc_price:,.2f}")
    
    def calculate_portfolio_value(self, market: Dict[str, MarketData]) -> float:
        """Calculate current portfolio value"""
        if not self.current_position:
            return 0

        if self.current_position.symbol == "EUR":
            return self.current_position.quantity
        else:  # ETH
            return self.current_position.quantity * market["ETH"].bid

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
            
            # CRITICAL: Never accept position below watermark
            if self.watermark.get() > 0 and new_qty <= self.watermark.get():
                print(f"\n   ❌ TRADE REJECTED: Would get {new_qty:.6f} ETH, below watermark {self.watermark.get():.6f}")
                return False
        
        # Update position
        self.current_position = Position(
            symbol=to_asset,
            quantity=new_qty,
            entry_price=new_price,
            entry_time=datetime.now().isoformat()
        )
        
        # Update watermark if ETH
        improvement = 0
        watermark_updated = False
        if to_asset == "ETH":
            old_watermark = self.watermark.get()
            watermark_updated = self.watermark.update(new_qty)
            if watermark_updated:
                self.successful_trades += 1
                improvement = (new_qty / old_watermark - 1) if old_watermark > 0 else 1.0
        
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
        print(f"\n   🔄 TRADE EXECUTED: {from_asset} → {to_asset}")
        print(f"      Type: {opportunity.type}")
        print(f"      Value: €{old_value:,.2f} | Fee: €{fee:.2f}")
        
        if to_asset == "EUR":
            print(f"      New Position: €{new_qty:,.2f}")
        else:
            print(f"      New Position: {new_qty:.6f} ETH @ €{new_price:,.2f}")
            if watermark_updated:
                print(f"      🏔️ NEW WATERMARK: {new_qty:.6f} ETH (+{improvement*100:.3f}%)")
        
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
                "reasoning": opportunity.reasoning
            }
            self.imessage.send_trade_notification(trade_info)
        
        return True
    
    def run_iteration(self):
        """Run one trading iteration"""
        
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
        else:
            print(f"   {self.current_position.quantity:.6f} ETH @ €{eth_price:,.2f}")
        
        print(f"\n🏔️ ETH WATERMARK: {self.watermark.get():.6f}")
        
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
            "market": market
        }
        
        opportunities = self.opportunity_detector.detect_opportunities(state, market_info)
        
        if opportunities:
            print(f"\n🎯 OPPORTUNITIES: {len(opportunities)}")
            for i, opp in enumerate(opportunities[:2]):
                print(f"   {i+1}. {opp.from_asset}→{opp.to_asset}: {opp.type}")
                print(f"      Return: {opp.expected_return*100:.2f}% | Confidence: {opp.confidence:.0%}")
                print(f"      {opp.reasoning}")
        else:
            print(f"\n⏸️ No opportunities")
        
        # AI evaluation
        if opportunities:
            print(f"\n🤖 Grok Analysis...")
            selected = self.ai.evaluate_opportunities(opportunities, state, market_info)
            
            if selected:
                print(f"\n   ✅ SIGNAL: {selected.from_asset}→{selected.to_asset}")
                self.execute_trade(selected, market)
            else:
                print(f"   ⏸️ HOLD")
        
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
                
                # Dynamic interval
                if market_info["is_nyse_open"]:
                    interval = 30
                elif market_info["is_major_market_open"]:
                    interval = 300
                else:
                    interval = 600
                
                self.run_iteration()
                
                print(f"\n⏰ Next check in {interval}s...")
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
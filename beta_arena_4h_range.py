#!/usr/bin/env python3
"""
Alpha Arena Competition Bot - 4-Hour Range Scalping Strategy + ETH Ladder
Goal: Accumulate more ETH through strategic EUR positioning
Starting Capital: 87.00 ETH
Uses: Live Kraken API, 4-hour range scalping strategy

Strategy (from transcript):
1. Mark high/low of first 4-hour candle (NY time) each day
2. Monitor 5-minute candles for breakouts (close outside range)
3. Wait for re-entry (close back inside range)
4. Entry: SHORT if broke high, LONG if broke low
5. Stop loss: At breakout high/low (or nearest key level if too large)
6. Take profit: 2x stop loss distance
7. Multiple trades per day allowed

ETH Ladder:
- STRICT ETH watermark - must beat on every EUR→ETH re-entry
- Skip trades that don't meet watermark
- All-in on every trade
"""

import requests
import time
import json
import sys
import os
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import sqlite3
from collections import deque
import pytz
import select
import termios
import tty
import subprocess

# ==============================================================================
# CONFIGURATION
# ==============================================================================

@dataclass
class Config:
    """4-Hour Range Strategy Configuration"""
    initial_eth: float = 87.00
    bot_name: str = "4H-Range-Scalper"

    # Trading assets (ETH and EUR only)
    tradeable_assets: List[str] = field(default_factory=lambda: ["ETH", "EUR"])

    # ETH watermark improvement (cycle-based)
    min_improvement_base: float = 0.001  # 0.1% minimum improvement

    # Fees (Kraken taker fees - market orders)
    fee_rate: float = 0.0026  # 0.26% taker fee

    # 4-Hour Range Strategy Settings
    range_candle_hours: int = 4  # Use 4-hour candles for range
    scalp_candle_minutes: int = 5  # Use 5-minute candles for scalping
    take_profit_multiplier: float = 2.0  # TP = 2x SL distance

    # Timing
    loop_interval_seconds: int = 30  # Check every 30 seconds

    # Notifications
    enable_imessage: bool = True
    imessage_recipient: str = "oscar.castaneda@gmail.com"

    def __post_init__(self):
        if self.tradeable_assets is None:
            self.tradeable_assets = ["ETH", "EUR"]

# ==============================================================================
# DATA STRUCTURES
# ==============================================================================

@dataclass
class Position:
    """Current trading position"""
    symbol: str
    quantity: float
    entry_price: float

@dataclass
class Watermark:
    """Track ETH watermark for ladder strategy"""
    eth_amount: float = 0.0

    def set(self, amount: float):
        self.eth_amount = amount

    def get(self) -> float:
        return self.eth_amount

    def must_beat(self, improvement_pct: float) -> float:
        """Return amount needed to beat watermark"""
        return self.eth_amount * (1 + improvement_pct)

@dataclass
class FourHourRange:
    """Track the 4-hour range for the day"""
    date: str  # Date in YYYY-MM-DD format (NY time)
    range_high: float
    range_low: float
    candle_open_time: datetime
    candle_close_time: datetime

    def is_active(self) -> bool:
        """Check if range is still valid for today (NY time)"""
        ny_tz = pytz.timezone('America/New_York')
        now_ny = datetime.now(ny_tz)
        today_ny = now_ny.strftime('%Y-%m-%d')
        return self.date == today_ny

    def contains(self, price: float) -> bool:
        """Check if price is inside range"""
        return self.range_low <= price <= self.range_high

@dataclass
class BreakoutState:
    """Track breakout and re-entry state"""
    broke_above: bool = False  # Candle closed above range high
    broke_below: bool = False  # Candle closed below range low
    breakout_high: Optional[float] = None  # High of breakout candle
    breakout_low: Optional[float] = None  # Low of breakout candle
    awaiting_reentry: bool = False  # Waiting for price to re-enter range
    entry_signal: Optional[str] = None  # "LONG" or "SHORT"

    def reset(self):
        """Reset breakout state"""
        self.broke_above = False
        self.broke_below = False
        self.breakout_high = None
        self.breakout_low = None
        self.awaiting_reentry = False
        self.entry_signal = None

@dataclass
class ActiveTrade:
    """Track active trade with stop loss and take profit"""
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    entry_asset: str  # "ETH" or "EUR"
    entry_quantity: float
    stop_loss: float
    take_profit: float
    entry_time: datetime

# ==============================================================================
# KRAKEN API
# ==============================================================================

class KrakenAPI:
    """Simplified Kraken API client"""
    def __init__(self):
        self.base_url = "https://api.kraken.com/0/public"

    def get_ticker(self, pair: str) -> Dict:
        """Get current ticker data"""
        response = requests.get(f"{self.base_url}/Ticker", params={"pair": pair}, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("error") and len(data["error"]) > 0:
            raise Exception(f"Kraken API error: {data['error']}")

        result = data.get("result", {})
        for key in result:
            return result[key]
        return {}

    def get_ohlc(self, pair: str, interval: int = 1, since: Optional[int] = None) -> List[List]:
        """Get OHLC data. Interval in minutes."""
        params = {"pair": pair, "interval": interval}
        if since:
            params["since"] = since

        response = requests.get(f"{self.base_url}/OHLC", params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get("error") and len(data["error"]) > 0:
            raise Exception(f"Kraken API error: {data['error']}")

        result = data.get("result", {})
        for key in result.keys():
            if key != "last":
                return result[key]
        return []

    def get_current_price(self, pair: str) -> Tuple[float, float, float]:
        """Get current bid, ask, and last price"""
        ticker = self.get_ticker(pair)
        bid = float(ticker.get("b", [0])[0])
        ask = float(ticker.get("a", [0])[0])
        last = float(ticker.get("c", [0])[0])
        return bid, ask, last

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def get_user_input_with_timeout(prompt: str, timeout: float = 25.0) -> Optional[str]:
    """
    Get user input with a timeout (non-blocking).
    Returns user input if provided within timeout, otherwise None.
    Works on Unix-like systems (Linux/macOS).
    """
    try:
        # Save current terminal settings
        old_settings = termios.tcgetattr(sys.stdin)

        try:
            # Set terminal to raw mode for immediate input
            tty.setraw(sys.stdin.fileno())

            # Display prompt
            sys.stdout.write(f"\n{prompt} ")
            sys.stdout.flush()

            # Wait for input with timeout
            ready, _, _ = select.select([sys.stdin], [], [], timeout)

            if ready:
                # Read single character
                char = sys.stdin.read(1)
                sys.stdout.write(f"{char}\n")
                sys.stdout.flush()
                return char
            else:
                # Timeout - no input
                sys.stdout.write("(timeout)\n")
                sys.stdout.flush()
                return None

        finally:
            # Restore terminal settings
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    except Exception as e:
        # Fallback: if anything fails, just return None
        print(f"\n⚠️ Input error: {e}")
        return None

# ==============================================================================
# BOT CLASS
# ==============================================================================

class FourHourRangeBot:
    """4-Hour Range Scalping Bot with ETH Ladder"""

    def __init__(self, config: Config):
        self.config = config
        self.kraken = KrakenAPI()
        self.watermark = Watermark()

        # Current position
        self.current_position: Optional[Position] = None

        # 4-hour range tracking
        self.four_hour_range: Optional[FourHourRange] = None

        # Breakout state
        self.breakout_state = BreakoutState()

        # Active trade tracking
        self.active_trade: Optional[ActiveTrade] = None

        # Performance tracking
        self.initial_value = 0
        self.total_trades = 0
        self.successful_trades = 0
        self.total_fees = 0

        # Database for logging
        self.conn = sqlite3.connect(f"4h_range_eth_eur_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        """Create database tables"""
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                timestamp TEXT,
                portfolio_value REAL,
                profit_pct REAL,
                current_asset TEXT,
                eth_watermark REAL,
                eth_price REAL
            )
        """)

        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                timestamp TEXT,
                from_asset TEXT,
                to_asset TEXT,
                quantity REAL,
                price REAL,
                value REAL,
                fee REAL,
                reason TEXT
            )
        """)
        self.conn.commit()

    def initialize(self):
        """Initialize bot with starting position"""
        print(f"\n{'='*70}")
        print(f"4-HOUR RANGE SCALPING BOT - INITIALIZING")
        print(f"{'='*70}\n")

        # Get current ETH price
        _, _, eth_price = self.kraken.get_current_price("ETHEUR")

        # Set initial position (starting with ETH)
        self.current_position = Position(
            symbol="ETH",
            quantity=self.config.initial_eth,
            entry_price=eth_price
        )

        # Set watermark
        self.watermark.set(self.config.initial_eth)

        # Set initial value
        self.initial_value = self.config.initial_eth * eth_price

        print(f"🚀 Bot Initialized")
        print(f"   Starting: {self.config.initial_eth} ETH @ €{eth_price:,.2f}")
        print(f"   Portfolio: €{self.initial_value:,.2f}")
        print(f"   ETH Watermark: {self.watermark.get():.6f} ETH")
        print(f"   Strategy: 4-Hour Range Scalping + ETH Ladder\n")

    def update_four_hour_range(self):
        """Update the 4-hour range for the current day"""
        ny_tz = pytz.timezone('America/New_York')
        now_ny = datetime.now(ny_tz)
        today_ny = now_ny.strftime('%Y-%m-%d')

        # Check if we have an old range from a previous day
        if self.four_hour_range and not self.four_hour_range.is_active():
            print(f"\n📅 NEW TRADING DAY: {today_ny}")
            print(f"   Previous range from: {self.four_hour_range.date}")
            print(f"   Resetting for new day...")

            # Clear old range and state
            self.four_hour_range = None
            self.breakout_state.reset()

            # Clear any active trades from previous day
            if self.active_trade:
                print(f"   ⚠️ Clearing active trade from previous day")
                self.active_trade = None

        # Check if we already have a valid range for today
        if self.four_hour_range and self.four_hour_range.is_active():
            return  # Range already set for today

        print(f"\n📊 Fetching 4-hour range for {today_ny} (NY time)...")

        # Get 4-hour OHLC data
        ohlc_data = self.kraken.get_ohlc("ETHEUR", interval=240)  # 240 minutes = 4 hours

        if not ohlc_data:
            print(f"   ⚠️ No 4-hour data available")
            return

        # Find ALL candles from today, then get the FIRST one (earliest)
        today_candles = []
        for candle in ohlc_data:
            candle_time = datetime.fromtimestamp(candle[0], tz=ny_tz)
            candle_date = candle_time.strftime('%Y-%m-%d')

            if candle_date == today_ny:
                today_candles.append((candle, candle_time))

        if not today_candles:
            print(f"   ⚠️ No 4-hour candles found for today")
            return

        # Get the FIRST candle of the day (earliest time)
        first_candle, candle_open_time = min(today_candles, key=lambda x: x[1])
        candle_close_time = candle_open_time + timedelta(hours=4)

        # Check if candle is closed
        if now_ny < candle_close_time:
            print(f"   ⏳ First 4h candle still forming (closes at {candle_close_time.strftime('%H:%M')} NY)")
            return

        # Candle is closed, use it for range
        range_high = float(first_candle[2])  # High
        range_low = float(first_candle[3])   # Low

        self.four_hour_range = FourHourRange(
            date=today_ny,
            range_high=range_high,
            range_low=range_low,
            candle_open_time=candle_open_time,
            candle_close_time=candle_close_time
        )

        print(f"   ✅ 4-Hour Range Set:")
        print(f"      High: €{range_high:,.2f}")
        print(f"      Low: €{range_low:,.2f}")
        print(f"      Range: €{range_high - range_low:,.2f}")
        print(f"      Candle: {candle_open_time.strftime('%H:%M')} - {candle_close_time.strftime('%H:%M')} NY")

        # Reset breakout state for new range
        self.breakout_state.reset()

    def check_breakout_and_reentry(self, current_price: float) -> Optional[str]:
        """
        Check for breakout and re-entry signals.
        Returns "LONG" or "SHORT" if entry signal detected, None otherwise.
        """
        if not self.four_hour_range:
            return None

        # Get latest 5-minute candle
        ohlc_data = self.kraken.get_ohlc("ETHEUR", interval=5)  # 5-minute candles
        if not ohlc_data or len(ohlc_data) < 2:
            return None

        latest_candle = ohlc_data[-2]  # Use closed candle (not the forming one)
        candle_close = float(latest_candle[4])  # Close price
        candle_high = float(latest_candle[2])   # High
        candle_low = float(latest_candle[3])    # Low

        # Check for breakout (candle must CLOSE outside range)
        if not self.breakout_state.awaiting_reentry:
            # Not currently waiting for re-entry, check for breakout

            if candle_close > self.four_hour_range.range_high:
                # Broke above range high
                print(f"\n   🔺 BREAKOUT ABOVE: €{candle_close:,.2f} > €{self.four_hour_range.range_high:,.2f}")
                self.breakout_state.broke_above = True
                self.breakout_state.breakout_high = candle_high
                self.breakout_state.awaiting_reentry = True
                self.breakout_state.entry_signal = "SHORT"  # Will short on re-entry

                # Send iMessage alert
                message = f"🔺 BREAKOUT ABOVE\n€{candle_close:,.2f} > €{self.four_hour_range.range_high:,.2f}\nWaiting for re-entry..."
                self.send_imessage(message)

            elif candle_close < self.four_hour_range.range_low:
                # Broke below range low
                print(f"\n   🔻 BREAKOUT BELOW: €{candle_close:,.2f} < €{self.four_hour_range.range_low:,.2f}")
                self.breakout_state.broke_below = True
                self.breakout_state.breakout_low = candle_low
                self.breakout_state.awaiting_reentry = True
                self.breakout_state.entry_signal = "LONG"  # Will long on re-entry

                # Send iMessage alert
                message = f"🔻 BREAKOUT BELOW\n€{candle_close:,.2f} < €{self.four_hour_range.range_low:,.2f}\nWaiting for re-entry..."
                self.send_imessage(message)

        else:
            # Already broke out, waiting for re-entry
            # Check if price closed back inside range

            if self.four_hour_range.contains(candle_close):
                # Re-entered the range!
                print(f"\n   ↩️ RE-ENTRY: €{candle_close:,.2f} back inside range")
                entry_signal = self.breakout_state.entry_signal

                # Send iMessage alert
                signal_desc = "LONG (Buy ETH)" if entry_signal == "LONG" else "SHORT (Sell to EUR)"
                message = f"↩️ RE-ENTRY DETECTED\n€{candle_close:,.2f} back inside range\nSignal: {signal_desc}\nAwaiting your decision..."
                self.send_imessage(message)

                # Don't reset state yet - will reset after trade decision
                return entry_signal  # Return "LONG" or "SHORT"

        return None

    def calculate_stop_loss_and_take_profit(self, direction: str, entry_price: float) -> Tuple[float, float]:
        """Calculate stop loss and take profit levels"""
        if direction == "SHORT":
            # Stop loss above entry (at breakout high)
            stop_loss = self.breakout_state.breakout_high if self.breakout_state.breakout_high else entry_price * 1.02
            sl_distance = stop_loss - entry_price
            take_profit = entry_price - (sl_distance * self.config.take_profit_multiplier)
        else:  # LONG
            # Stop loss below entry (at breakout low)
            stop_loss = self.breakout_state.breakout_low if self.breakout_state.breakout_low else entry_price * 0.98
            sl_distance = entry_price - stop_loss
            take_profit = entry_price + (sl_distance * self.config.take_profit_multiplier)

        return stop_loss, take_profit

    def send_imessage(self, message: str):
        """Send iMessage notification"""
        if not self.config.enable_imessage:
            return

        try:
            escaped_message = message.replace('"', '\\"').replace("'", "'\\''")
            applescript = f'''
            tell application "Messages"
                set targetService to 1st account whose service type = iMessage
                set targetBuddy to participant "{self.config.imessage_recipient}" of targetService
                send "{escaped_message}" to targetBuddy
            end tell
            '''
            subprocess.run(["osascript", "-e", applescript], check=True, capture_output=True)
        except Exception as e:
            print(f"⚠️ iMessage failed: {e}")

    def run(self):
        """Main loop"""
        self.initialize()

        while True:
            try:
                self.run_iteration()
                time.sleep(self.config.loop_interval_seconds)
            except KeyboardInterrupt:
                print("\n\n👋 Shutting down...")
                self.conn.close()
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(self.config.loop_interval_seconds)

    def run_iteration(self):
        """Run one iteration of the bot"""
        # Update 4-hour range
        self.update_four_hour_range()

        if not self.four_hour_range:
            print(f"⏳ Waiting for 4-hour range to be established...")
            return

        # Get current price
        _, ask, current_price = self.kraken.get_current_price("ETHEUR")

        # Calculate portfolio value
        if self.current_position.symbol == "ETH":
            portfolio_value = self.current_position.quantity * current_price
        else:  # EUR
            portfolio_value = self.current_position.quantity

        profit_pct = ((portfolio_value - self.initial_value) / self.initial_value) * 100

        print(f"\n{'='*70}")
        print(f"4H-RANGE BOT - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*70}")
        print(f"\n💰 PERFORMANCE:")
        print(f"   Portfolio: €{portfolio_value:,.2f}")
        print(f"   Profit: €{portfolio_value - self.initial_value:+,.2f} ({profit_pct:+.2f}%)")

        print(f"\n📍 POSITION: {self.current_position.symbol}")
        if self.current_position.symbol == "ETH":
            print(f"   {self.current_position.quantity:.6f} ETH @ €{current_price:,.2f}")
        else:
            print(f"   €{self.current_position.quantity:,.2f}")

        print(f"\n🏔️ WATERMARK:")
        print(f"   ETH: {self.watermark.get():.6f} ETH (tracking only)")

        print(f"\n📊 4-HOUR RANGE ({self.four_hour_range.date}):")
        print(f"   High: €{self.four_hour_range.range_high:,.2f}")
        print(f"   Low: €{self.four_hour_range.range_low:,.2f}")
        print(f"   Current: €{current_price:,.2f}")

        # Check if we have an active trade with SL/TP
        if self.active_trade:
            print(f"\n🎯 ACTIVE TRADE ({self.active_trade.direction}):")
            print(f"   Entry: €{self.active_trade.entry_price:,.2f}")
            print(f"   Stop Loss: €{self.active_trade.stop_loss:,.2f}")
            print(f"   Take Profit: €{self.active_trade.take_profit:,.2f}")

            # Check if SL or TP hit
            if self.active_trade.direction == "LONG":
                if current_price <= self.active_trade.stop_loss:
                    print(f"\n   🛑 STOP LOSS HIT!")
                    # Execute exit trade
                    self.execute_exit_trade("Stop Loss Hit")
                    self.active_trade = None
                    return
                elif current_price >= self.active_trade.take_profit:
                    print(f"\n   ✅ TAKE PROFIT HIT!")
                    # Execute exit trade
                    self.execute_exit_trade("Take Profit Hit")
                    self.active_trade = None
                    return
            else:  # SHORT
                if current_price >= self.active_trade.stop_loss:
                    print(f"\n   🛑 STOP LOSS HIT!")
                    # Execute exit trade
                    self.execute_exit_trade("Stop Loss Hit")
                    self.active_trade = None
                    return
                elif current_price <= self.active_trade.take_profit:
                    print(f"\n   ✅ TAKE PROFIT HIT!")
                    # Execute exit trade
                    self.execute_exit_trade("Take Profit Hit")
                    self.active_trade = None
                    return

        # Check for entry signals (only if no active trade)
        if not self.active_trade:
            entry_signal = self.check_breakout_and_reentry(current_price)

            if entry_signal:
                print(f"\n🎯 ENTRY SIGNAL: {entry_signal}")

                # Calculate SL and TP
                stop_loss, take_profit = self.calculate_stop_loss_and_take_profit(entry_signal, current_price)

                print(f"   Entry: €{current_price:,.2f}")
                print(f"   Stop Loss: €{stop_loss:,.2f}")
                print(f"   Take Profit: €{take_profit:,.2f}")

                # Ask user if they want to trade (25-second timeout)
                signal_type = "Sell to EUR" if entry_signal == "SHORT" else "Buy ETH"
                user_response = get_user_input_with_timeout(f"💡 Trade? ({signal_type}) y", timeout=25.0)

                # Determine if we should execute
                should_execute = False
                execution_reason = ""

                if user_response and user_response.lower() == 'y':
                    # User explicitly approved
                    should_execute = True
                    execution_reason = "User Override"
                    print(f"\n   👤 USER APPROVED: Executing trade")
                elif user_response is None:
                    # Timeout - check if conditions are met
                    print(f"\n   ⏰ TIMEOUT: Checking conditions for automatic execution...")

                    # Check if we're in correct position for the signal
                    if entry_signal == "SHORT" and self.current_position.symbol == "ETH":
                        should_execute = True
                        execution_reason = "Auto (Timeout)"
                        print(f"   ✅ Conditions met - executing automatically")
                    elif entry_signal == "LONG" and self.current_position.symbol == "EUR":
                        should_execute = True
                        execution_reason = "Auto (Timeout)"
                        print(f"   ✅ Conditions met - executing automatically")
                    else:
                        print(f"   ⏸️ Conditions not met - skipping trade")
                else:
                    # User declined or gave invalid input
                    print(f"\n   ⏸️ USER DECLINED: Skipping trade")

                # Reset breakout state
                self.breakout_state.reset()

                # Execute trade if approved
                if should_execute:
                    if entry_signal == "SHORT" and self.current_position.symbol == "ETH":
                        # Sell ETH to EUR
                        self.execute_trade("ETH", "EUR", f"4H Range - SHORT Signal ({execution_reason})")
                        self.active_trade = ActiveTrade(
                            direction="SHORT",
                            entry_price=current_price,
                            entry_asset="EUR",
                            entry_quantity=self.current_position.quantity,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            entry_time=datetime.now()
                        )

                    elif entry_signal == "LONG" and self.current_position.symbol == "EUR":
                        # Buy ETH with EUR
                        self.execute_trade("EUR", "ETH", f"4H Range - LONG Signal ({execution_reason})")
                        self.active_trade = ActiveTrade(
                            direction="LONG",
                            entry_price=current_price,
                            entry_asset="ETH",
                            entry_quantity=self.current_position.quantity,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            entry_time=datetime.now()
                        )

        # Log performance
        self.cursor.execute("""
            INSERT INTO performance (timestamp, portfolio_value, profit_pct, current_asset, eth_watermark, eth_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), portfolio_value, profit_pct, self.current_position.symbol,
              self.watermark.get(), current_price))
        self.conn.commit()

    def execute_trade(self, from_asset: str, to_asset: str, reason: str):
        """Execute a trade"""
        print(f"\n🔄 EXECUTING TRADE: {from_asset} → {to_asset}")

        # Get current prices
        bid, ask, _ = self.kraken.get_current_price("ETHEUR")

        # Calculate trade details
        if from_asset == "ETH":
            # Selling ETH for EUR (use bid price)
            quantity = self.current_position.quantity
            price = bid
            value = quantity * price
            fee = value * self.config.fee_rate
            new_quantity = value - fee
            new_price = 0  # EUR doesn't have a "price"

        else:  # EUR to ETH
            # Buying ETH with EUR (use ask price)
            quantity = self.current_position.quantity
            price = ask
            value = quantity
            fee = value * self.config.fee_rate
            new_quantity = (value - fee) / price
            new_price = price

        # Update position
        self.current_position = Position(
            symbol=to_asset,
            quantity=new_quantity,
            entry_price=new_price if to_asset == "ETH" else 0
        )

        # Update watermark if bought ETH
        if to_asset == "ETH":
            if new_quantity > self.watermark.get():
                print(f"   🏔️ NEW WATERMARK: {new_quantity:.6f} ETH (was {self.watermark.get():.6f})")
                self.watermark.set(new_quantity)

        # Update stats
        self.total_trades += 1
        self.total_fees += fee

        print(f"   Value: €{value:,.2f} | Fee: €{fee:.2f}")
        print(f"   New Position: {new_quantity:.6f} {to_asset}")
        print(f"   Reason: {reason}")

        # Log trade
        self.cursor.execute("""
            INSERT INTO trades (timestamp, from_asset, to_asset, quantity, price, value, fee, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), from_asset, to_asset, quantity, price, value, fee, reason))
        self.conn.commit()

        # Send notification
        message = f"🔄 TRADE: {from_asset}→{to_asset}\n€{value:,.2f} | Fee: €{fee:.2f}\n{reason}"
        self.send_imessage(message)

    def execute_exit_trade(self, reason: str):
        """Execute exit from active trade"""
        if self.current_position.symbol == "EUR":
            # Currently in EUR (from SHORT), buy back ETH
            self.execute_trade("EUR", "ETH", reason)
        else:
            # Currently in ETH (from LONG), sell to EUR
            self.execute_trade("ETH", "EUR", reason)

# ==============================================================================
# MAIN
# ==============================================================================

if __name__ == "__main__":
    config = Config()
    bot = FourHourRangeBot(config)
    bot.run()

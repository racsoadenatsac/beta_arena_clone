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
import logging

# ==============================================================================
# LOGGING SETUP
# ==============================================================================

# Create log filename with timestamp
log_filename = f"trades-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}.log"

# Configure logging to both file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

def log(message: str):
    """Log message to both file and console"""
    logger.info(message)

# Log startup message
log(f"{'='*70}")
log(f"4-HOUR RANGE SCALPING BOT - LOG FILE CREATED")
log(f"Log file: {log_filename}")
log(f"{'='*70}")

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
        log(f"\n⚠️ Input error: {e}")
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
        log(f"\n{'='*70}")
        log(f"4-HOUR RANGE SCALPING BOT - INITIALIZING")
        log(f"{'='*70}\n")

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

        log(f"🚀 Bot Initialized")
        log(f"   Starting: {self.config.initial_eth} ETH @ €{eth_price:,.2f}")
        log(f"   Portfolio: €{self.initial_value:,.2f}")
        log(f"   ETH Watermark: {self.watermark.get():.6f} ETH")
        log(f"   Strategy: 4-Hour Range Scalping + ETH Ladder\n")

    def update_four_hour_range(self):
        """Update the 4-hour range for the current day"""
        ny_tz = pytz.timezone('America/New_York')
        now_ny = datetime.now(ny_tz)
        today_ny = now_ny.strftime('%Y-%m-%d')

        # Check if we have an old range from a previous day
        if self.four_hour_range and not self.four_hour_range.is_active():
            log(f"\n📅 NEW TRADING DAY: {today_ny}")
            log(f"   Previous range from: {self.four_hour_range.date}")
            log(f"   Resetting for new day...")

            # Clear old range and state
            self.four_hour_range = None
            self.breakout_state.reset()

            # Clear any active trades from previous day
            if self.active_trade:
                log(f"   ⚠️ Clearing active trade from previous day")
                self.active_trade = None

        # Check if we already have a valid range for today
        if self.four_hour_range and self.four_hour_range.is_active():
            return  # Range already set for today

        log(f"\n📊 Fetching 4-hour range for {today_ny} (NY time)...")

        # Get 4-hour OHLC data
        ohlc_data = self.kraken.get_ohlc("ETHEUR", interval=240)  # 240 minutes = 4 hours

        if not ohlc_data:
            log(f"   ⚠️ No 4-hour data available")
            return

        # Find ALL candles from today, then get the FIRST one (earliest)
        today_candles = []
        for candle in ohlc_data:
            candle_time = datetime.fromtimestamp(candle[0], tz=ny_tz)
            candle_date = candle_time.strftime('%Y-%m-%d')

            if candle_date == today_ny:
                today_candles.append((candle, candle_time))

        if not today_candles:
            log(f"   ⚠️ No 4-hour candles found for today")
            return

        # Get the FIRST candle of the day (earliest time)
        first_candle, candle_open_time = min(today_candles, key=lambda x: x[1])
        candle_close_time = candle_open_time + timedelta(hours=4)

        # Check if candle is closed
        if now_ny < candle_close_time:
            log(f"   ⏳ First 4h candle still forming (closes at {candle_close_time.strftime('%H:%M')} NY)")
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

        log(f"   ✅ 4-Hour Range Set:")
        log(f"      High: €{range_high:,.2f}")
        log(f"      Low: €{range_low:,.2f}")
        log(f"      Range: €{range_high - range_low:,.2f}")
        log(f"      Candle: {candle_open_time.strftime('%H:%M')} - {candle_close_time.strftime('%H:%M')} NY")

        # Reset breakout state for new range
        self.breakout_state.reset()

    def calculate_expected_profit(self, entry_price: float, sl_price: float, signal: str, current_balance: float = None) -> dict:
        """
        Calculate expected profit for a trade including Kraken fees.

        Args:
            entry_price: Expected entry price
            sl_price: Stop loss price
            signal: "LONG" or "SHORT"
            current_balance: Current balance (ETH for SHORT, EUR for LONG)

        Returns:
            dict with tp_price, profit_pct, profit_eur (if balance provided)
        """
        # Calculate SL distance
        if signal == "SHORT":
            sl_distance = sl_price - entry_price  # SL is above entry
        else:  # LONG
            sl_distance = entry_price - sl_price  # SL is below entry

        # Calculate TP (2x SL distance + entry fee adjustment)
        # TP is adjusted so that after paying entry fee, net profit = 2x SL distance
        target_profit = self.config.take_profit_multiplier * sl_distance
        adjusted_profit = target_profit / (1 - self.config.fee_rate)  # Adjust for entry fee

        if signal == "SHORT":
            tp_price = entry_price - adjusted_profit
        else:  # LONG
            tp_price = entry_price + adjusted_profit

        # Calculate actual net profit percentage (after entry fee)
        price_movement_pct = abs(tp_price - entry_price) / entry_price * 100
        entry_fee_pct = self.config.fee_rate * 100  # 0.26%
        net_profit_pct = price_movement_pct - entry_fee_pct

        result = {
            'tp_price': tp_price,
            'profit_pct': net_profit_pct,
            'sl_price': sl_price,
            'sl_distance': sl_distance
        }

        # Calculate EUR profit if balance provided
        if current_balance is not None:
            if signal == "SHORT":
                # Starting with ETH, selling to EUR
                # EUR received after entry fee
                eur_after_entry = current_balance * entry_price * (1 - self.config.fee_rate)
                # Value if we had held ETH until TP
                value_if_held = current_balance * tp_price
                # Profit = EUR we have - value if we held
                profit_eur = eur_after_entry - value_if_held
            else:  # LONG
                # Starting with EUR, buying ETH
                # ETH received after entry fee
                eth_after_entry = (current_balance / entry_price) * (1 - self.config.fee_rate)
                # Value at TP
                eur_value_at_tp = eth_after_entry * tp_price
                # Profit = value at TP - EUR we started with
                profit_eur = eur_value_at_tp - current_balance

            result['profit_eur'] = profit_eur

        return result

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
                log(f"\n   🔺 BREAKOUT ABOVE: €{candle_close:,.2f} > €{self.four_hour_range.range_high:,.2f}")
                log(f"      Current ETH price: €{current_price:,.2f}")
                log(f"      Re-entry target: Price closes back below €{self.four_hour_range.range_high:,.2f}")

                # Calculate expected profit
                current_balance = self.current_position.quantity if self.current_position.symbol == "ETH" else None
                profit_calc = self.calculate_expected_profit(
                    entry_price=self.four_hour_range.range_high,
                    sl_price=candle_high,
                    signal="SHORT",
                    current_balance=current_balance
                )

                # Only show target if profitable after entry fee
                log(f"      Stop Loss: €{candle_high:,.2f}")
                if profit_calc['profit_pct'] > 0:
                    if 'profit_eur' in profit_calc:
                        log(f"      Take Profit: €{profit_calc['tp_price']:,.2f} (+{profit_calc['profit_pct']:.2f}% after entry fee) = €{profit_calc['profit_eur']:,.2f} profit")
                    else:
                        log(f"      Take Profit: €{profit_calc['tp_price']:,.2f} (+{profit_calc['profit_pct']:.2f}% after entry fee)")
                else:
                    log(f"      ⚠️ No profitable target (entry fee exceeds potential profit)")

                self.breakout_state.broke_above = True
                self.breakout_state.breakout_high = candle_high
                self.breakout_state.awaiting_reentry = True
                self.breakout_state.entry_signal = "SHORT"  # Will short on re-entry

                # Send iMessage alert
                message = f"🔺 BREAKOUT ABOVE\n€{candle_close:,.2f} > €{self.four_hour_range.range_high:,.2f}\nCurrent: €{current_price:,.2f}\nRe-entry: Below €{self.four_hour_range.range_high:,.2f}"

                if profit_calc['profit_pct'] > 0:
                    profit_text = f"+{profit_calc['profit_pct']:.2f}%"
                    if 'profit_eur' in profit_calc:
                        profit_text += f" (€{profit_calc['profit_eur']:,.2f})"
                    message += f"\nStop Loss: €{candle_high:,.2f}"
                    message += f"\nTake Profit: €{profit_calc['tp_price']:,.2f} ({profit_text})"
                else:
                    message += f"\n⚠️ No profitable target after entry fee"

                message += f"\nSignal: SHORT when re-entry occurs"
                self.send_imessage(message)

            elif candle_close < self.four_hour_range.range_low:
                # Broke below range low
                log(f"\n   🔻 BREAKOUT BELOW: €{candle_close:,.2f} < €{self.four_hour_range.range_low:,.2f}")
                log(f"      Current ETH price: €{current_price:,.2f}")
                log(f"      Re-entry target: Price closes back above €{self.four_hour_range.range_low:,.2f}")

                # Calculate expected profit
                current_balance = self.current_position.quantity if self.current_position.symbol == "EUR" else None
                profit_calc = self.calculate_expected_profit(
                    entry_price=self.four_hour_range.range_low,
                    sl_price=candle_low,
                    signal="LONG",
                    current_balance=current_balance
                )

                # Only show target if profitable after entry fee
                log(f"      Stop Loss: €{candle_low:,.2f}")
                if profit_calc['profit_pct'] > 0:
                    if 'profit_eur' in profit_calc:
                        log(f"      Take Profit: €{profit_calc['tp_price']:,.2f} (+{profit_calc['profit_pct']:.2f}% after entry fee) = €{profit_calc['profit_eur']:,.2f} profit")
                    else:
                        log(f"      Take Profit: €{profit_calc['tp_price']:,.2f} (+{profit_calc['profit_pct']:.2f}% after entry fee)")
                else:
                    log(f"      ⚠️ No profitable target (entry fee exceeds potential profit)")

                self.breakout_state.broke_below = True
                self.breakout_state.breakout_low = candle_low
                self.breakout_state.awaiting_reentry = True
                self.breakout_state.entry_signal = "LONG"  # Will long on re-entry

                # Send iMessage alert
                message = f"🔻 BREAKOUT BELOW\n€{candle_close:,.2f} < €{self.four_hour_range.range_low:,.2f}\nCurrent: €{current_price:,.2f}\nRe-entry: Above €{self.four_hour_range.range_low:,.2f}"

                if profit_calc['profit_pct'] > 0:
                    profit_text = f"+{profit_calc['profit_pct']:.2f}%"
                    if 'profit_eur' in profit_calc:
                        profit_text += f" (€{profit_calc['profit_eur']:,.2f})"
                    message += f"\nStop Loss: €{candle_low:,.2f}"
                    message += f"\nTake Profit: €{profit_calc['tp_price']:,.2f} ({profit_text})"
                else:
                    message += f"\n⚠️ No profitable target after entry fee"

                message += f"\nSignal: LONG when re-entry occurs"
                self.send_imessage(message)

        else:
            # Already broke out, waiting for re-entry
            # Check if price closed back inside range

            if self.four_hour_range.contains(candle_close):
                # Re-entered the range!
                log(f"\n   ↩️ RE-ENTRY: €{candle_close:,.2f} back inside range")
                entry_signal = self.breakout_state.entry_signal

                # Calculate expected SL and TP for the re-entry
                if entry_signal == "SHORT":
                    expected_sl = self.breakout_state.breakout_high
                    sl_distance = expected_sl - candle_close
                else:  # LONG
                    expected_sl = self.breakout_state.breakout_low
                    sl_distance = candle_close - expected_sl

                # Calculate TP with fee adjustment
                target_profit = self.config.take_profit_multiplier * sl_distance
                adjusted_profit = target_profit / (1 - self.config.fee_rate)

                if entry_signal == "SHORT":
                    expected_tp = candle_close - adjusted_profit
                else:  # LONG
                    expected_tp = candle_close + adjusted_profit

                # Send iMessage alert with TP
                signal_desc = "LONG (Buy ETH)" if entry_signal == "LONG" else "SHORT (Sell to EUR)"
                message = f"↩️ RE-ENTRY DETECTED\n€{candle_close:,.2f} back inside range\nSignal: {signal_desc}\nStop Loss: €{expected_sl:,.2f}\nTake Profit: €{expected_tp:,.2f}\nAwaiting your decision..."
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
            log(f"⚠️ iMessage failed: {e}")

    def run(self):
        """Main loop"""
        self.initialize()

        while True:
            try:
                self.run_iteration()
                time.sleep(self.config.loop_interval_seconds)
            except KeyboardInterrupt:
                log("\n\n👋 Shutting down...")
                self.conn.close()
                break
            except Exception as e:
                log(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(self.config.loop_interval_seconds)

    def run_iteration(self):
        """Run one iteration of the bot"""
        # Update 4-hour range
        self.update_four_hour_range()

        if not self.four_hour_range:
            log(f"⏳ Waiting for 4-hour range to be established...")
            return

        # Get current price
        _, ask, current_price = self.kraken.get_current_price("ETHEUR")

        # Calculate portfolio value
        if self.current_position.symbol == "ETH":
            portfolio_value = self.current_position.quantity * current_price
        else:  # EUR
            portfolio_value = self.current_position.quantity

        profit_pct = ((portfolio_value - self.initial_value) / self.initial_value) * 100

        log(f"\n{'='*70}")
        log(f"4H-RANGE BOT - {datetime.now().strftime('%H:%M:%S')}")
        log(f"{'='*70}")
        log(f"\n💰 PERFORMANCE:")
        log(f"   Portfolio: €{portfolio_value:,.2f}")
        log(f"   Profit: €{portfolio_value - self.initial_value:+,.2f} ({profit_pct:+.2f}%)")

        log(f"\n📍 POSITION: {self.current_position.symbol}")
        if self.current_position.symbol == "ETH":
            log(f"   {self.current_position.quantity:.6f} ETH @ €{current_price:,.2f}")
        else:
            log(f"   €{self.current_position.quantity:,.2f}")

        log(f"\n🏔️ WATERMARK:")
        log(f"   ETH: {self.watermark.get():.6f} ETH (tracking only)")

        log(f"\n📊 4-HOUR RANGE ({self.four_hour_range.date}):")
        log(f"   High: €{self.four_hour_range.range_high:,.2f}")
        log(f"   Low: €{self.four_hour_range.range_low:,.2f}")
        log(f"   Current: €{current_price:,.2f}")

        # Show target price if we have one
        if self.active_trade:
            # Active trade - calculate fees and targets
            entry_value = self.current_position.quantity if self.current_position.symbol == "EUR" else (self.current_position.quantity * self.active_trade.entry_price)
            fee_eur = entry_value * self.config.fee_rate

            # Calculate target before fees (raw 2x SL)
            sl_distance = abs(self.active_trade.stop_loss - self.active_trade.entry_price)
            target_profit_raw = self.config.take_profit_multiplier * sl_distance

            if self.active_trade.direction == "SHORT":
                target_bf = self.active_trade.entry_price - target_profit_raw
            else:  # LONG
                target_bf = self.active_trade.entry_price + target_profit_raw

            log(f"   Fees: €{fee_eur:,.2f}")
            log(f"   Target BF: €{target_bf:,.2f}")
            log(f"   Target AF: €{self.active_trade.take_profit:,.2f}")

        elif self.breakout_state.awaiting_reentry:
            # Breakout occurred, awaiting re-entry - calculate expected TP
            if self.breakout_state.entry_signal == "SHORT":
                expected_entry = self.four_hour_range.range_high
                expected_sl = self.breakout_state.breakout_high
                sl_distance = expected_sl - expected_entry
            else:  # LONG
                expected_entry = self.four_hour_range.range_low
                expected_sl = self.breakout_state.breakout_low
                sl_distance = expected_entry - expected_sl

            # Calculate fees based on current position
            if self.current_position.symbol == "EUR":
                entry_value = self.current_position.quantity
            else:
                entry_value = self.current_position.quantity * expected_entry
            fee_eur = entry_value * self.config.fee_rate

            # Target before fees (raw 2x SL)
            target_profit_raw = self.config.take_profit_multiplier * sl_distance
            if self.breakout_state.entry_signal == "SHORT":
                target_bf = expected_entry - target_profit_raw
            else:  # LONG
                target_bf = expected_entry + target_profit_raw

            # Target after fees (adjusted for entry fee)
            adjusted_profit = target_profit_raw / (1 - self.config.fee_rate)
            if self.breakout_state.entry_signal == "SHORT":
                target_af = expected_entry - adjusted_profit
            else:  # LONG
                target_af = expected_entry + adjusted_profit

            log(f"   Fees: €{fee_eur:,.2f}")
            log(f"   Target BF: €{target_bf:,.2f}")
            log(f"   Target AF: €{target_af:,.2f}")

        # Check if we have an active trade with SL/TP
        if self.active_trade:
            log(f"\n🎯 ACTIVE TRADE ({self.active_trade.direction}):")
            log(f"   Entry: €{self.active_trade.entry_price:,.2f}")
            log(f"   Stop Loss: €{self.active_trade.stop_loss:,.2f}")
            log(f"   Take Profit: €{self.active_trade.take_profit:,.2f}")

            # Calculate expected profit at TP (after entry fee already paid)
            price_move_pct = abs(self.active_trade.take_profit - self.active_trade.entry_price) / self.active_trade.entry_price * 100
            entry_fee_pct = self.config.fee_rate * 100
            net_profit_pct = price_move_pct - entry_fee_pct
            log(f"   Expected profit at TP: +{net_profit_pct:.2f}% after entry fee")

            # Check if SL or TP hit
            if self.active_trade.direction == "LONG":
                if current_price <= self.active_trade.stop_loss:
                    log(f"\n   🛑 STOP LOSS HIT!")
                    log(f"   Current: €{current_price:,.2f}")
                    log(f"   Stop Loss: €{self.active_trade.stop_loss:,.2f}")

                    # Ask user if they want to exit
                    user_response = get_user_input_with_timeout(f"💡 Exit trade? (Sell to EUR) y", timeout=25.0)

                    should_exit = False
                    if user_response and user_response.lower() == 'y':
                        should_exit = True
                        log(f"\n   👤 USER APPROVED: Exiting trade")
                    elif user_response is None:
                        log(f"\n   ⏰ TIMEOUT: Auto-exiting at Stop Loss")
                        should_exit = True
                    else:
                        log(f"\n   ⏸️ USER DECLINED: Keeping position")

                    if should_exit:
                        self.execute_exit_trade("Stop Loss Hit")
                        self.active_trade = None
                    return

                elif current_price >= self.active_trade.take_profit:
                    log(f"\n   ✅ TAKE PROFIT HIT!")
                    log(f"   Current: €{current_price:,.2f}")
                    log(f"   Take Profit: €{self.active_trade.take_profit:,.2f}")

                    # Ask user if they want to exit
                    user_response = get_user_input_with_timeout(f"💡 Exit trade? (Sell to EUR) y", timeout=25.0)

                    should_exit = False
                    if user_response and user_response.lower() == 'y':
                        should_exit = True
                        log(f"\n   👤 USER APPROVED: Exiting trade")
                    elif user_response is None:
                        log(f"\n   ⏰ TIMEOUT: Auto-exiting at Take Profit")
                        should_exit = True
                    else:
                        log(f"\n   ⏸️ USER DECLINED: Keeping position")

                    if should_exit:
                        self.execute_exit_trade("Take Profit Hit")
                        self.active_trade = None
                    return

            else:  # SHORT
                if current_price >= self.active_trade.stop_loss:
                    log(f"\n   🛑 STOP LOSS HIT!")
                    log(f"   Current: €{current_price:,.2f}")
                    log(f"   Stop Loss: €{self.active_trade.stop_loss:,.2f}")

                    # Ask user if they want to exit
                    user_response = get_user_input_with_timeout(f"💡 Exit trade? (Buy back ETH) y", timeout=25.0)

                    should_exit = False
                    if user_response and user_response.lower() == 'y':
                        should_exit = True
                        log(f"\n   👤 USER APPROVED: Exiting trade")
                    elif user_response is None:
                        log(f"\n   ⏰ TIMEOUT: Auto-exiting at Stop Loss")
                        should_exit = True
                    else:
                        log(f"\n   ⏸️ USER DECLINED: Keeping position")

                    if should_exit:
                        self.execute_exit_trade("Stop Loss Hit")
                        self.active_trade = None
                    return

                elif current_price <= self.active_trade.take_profit:
                    log(f"\n   ✅ TAKE PROFIT HIT!")
                    log(f"   Current: €{current_price:,.2f}")
                    log(f"   Take Profit: €{self.active_trade.take_profit:,.2f}")

                    # Ask user if they want to exit
                    user_response = get_user_input_with_timeout(f"💡 Exit trade? (Buy back ETH) y", timeout=25.0)

                    should_exit = False
                    if user_response and user_response.lower() == 'y':
                        should_exit = True
                        log(f"\n   👤 USER APPROVED: Exiting trade")
                    elif user_response is None:
                        log(f"\n   ⏰ TIMEOUT: Auto-exiting at Take Profit")
                        should_exit = True
                    else:
                        log(f"\n   ⏸️ USER DECLINED: Keeping position")

                    if should_exit:
                        self.execute_exit_trade("Take Profit Hit")
                        self.active_trade = None
                    return

        # Check for entry signals (only if no active trade)
        if not self.active_trade:
            entry_signal = self.check_breakout_and_reentry(current_price)

            if entry_signal:
                log(f"\n🎯 ENTRY SIGNAL: {entry_signal}")

                # Calculate SL and TP
                stop_loss, take_profit = self.calculate_stop_loss_and_take_profit(entry_signal, current_price)

                log(f"   Entry: €{current_price:,.2f}")
                log(f"   Stop Loss: €{stop_loss:,.2f}")
                log(f"   Take Profit: €{take_profit:,.2f}")

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
                    log(f"\n   👤 USER APPROVED: Executing trade")
                elif user_response is None:
                    # Timeout - check if conditions are met
                    log(f"\n   ⏰ TIMEOUT: Checking conditions for automatic execution...")

                    # Check if we're in correct position for the signal
                    if entry_signal == "SHORT" and self.current_position.symbol == "ETH":
                        should_execute = True
                        execution_reason = "Auto (Timeout)"
                        log(f"   ✅ Conditions met - executing automatically")
                    elif entry_signal == "LONG" and self.current_position.symbol == "EUR":
                        should_execute = True
                        execution_reason = "Auto (Timeout)"
                        log(f"   ✅ Conditions met - executing automatically")
                    else:
                        log(f"   ⏸️ Conditions not met - skipping trade")
                else:
                    # User declined or gave invalid input
                    log(f"\n   ⏸️ USER DECLINED: Skipping trade")

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
                    else:
                        # Wrong position for the signal
                        required_pos = "EUR" if entry_signal == "LONG" else "ETH"
                        log(f"\n   ⚠️ Cannot execute {entry_signal} signal - requires {required_pos} position")
                        log(f"   Current position: {self.current_position.symbol}")
                        log(f"   Trade skipped")

        # Log performance
        self.cursor.execute("""
            INSERT INTO performance (timestamp, portfolio_value, profit_pct, current_asset, eth_watermark, eth_price)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(), portfolio_value, profit_pct, self.current_position.symbol,
              self.watermark.get(), current_price))
        self.conn.commit()

    def execute_trade(self, from_asset: str, to_asset: str, reason: str):
        """Execute a trade"""
        log(f"\n🔄 EXECUTING TRADE: {from_asset} → {to_asset}")

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
                log(f"   🏔️ NEW WATERMARK: {new_quantity:.6f} ETH (was {self.watermark.get():.6f})")
                self.watermark.set(new_quantity)

        # Update stats
        self.total_trades += 1
        self.total_fees += fee

        log(f"   Value: €{value:,.2f} | Fee: €{fee:.2f}")
        log(f"   New Position: {new_quantity:.6f} {to_asset}")
        log(f"   Reason: {reason}")

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

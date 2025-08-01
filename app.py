# app.py
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import datetime
import random
import threading
import time
import json # For handling JSONDecodeError
import requests # REQUIRED: For making HTTP requests to TwelveData API
import math   # REQUIRED: For mathematical operations in indicators
import traceback # For detailed error logging
import os # Import the os module to access environment variables

app = Flask(__name__)
# CORS Configuration: Allowing all origins for testing purposes.
# IMPORTANT: For production, replace "*" with specific origins (e.g., "http://your-frontend-domain.com").
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Global variables to store signals and their last generation time
# The 'signals' dictionary stores current active signal data for each pair.
# It also tracks state for each pair (last generated, last finished, resting status).
signals = {}
# Threading Lock for safe access to the global 'signals' dictionary
signals_lock = threading.Lock()

# TwelveData API Keys - MULTI-KEY FALLBACK SYSTEM
# API Keys are now loaded from environment variables for security
API_KEYS = [
    os.environ.get("TWELVEDATA_API_KEY_1", "YOUR_DEFAULT_KEY_1"),
    os.environ.get("TWELVEDATA_API_KEY_2", "YOUR_DEFAULT_KEY_2"),
    os.environ.get("TWELVEDATA_API_KEY_3", "YOUR_DEFAULT_KEY_3"),
    os.environ.get("TWELVEDATA_API_KEY_4", "YOUR_DEFAULT_KEY_4"),
    os.environ.get("TWELVEDATA_API_KEY_5", "YOUR_DEFAULT_KEY_5")
]
# Remove any 'YOUR_DEFAULT_KEY_X' if you don't want default fallbacks
API_KEYS = [key for key in API_KEYS if key and "YOUR_DEFAULT_KEY" not in key]

# Ensure there's at least one API key, otherwise the app won't function
if not API_KEYS:
    print("CRITICAL ERROR: No TwelveData API keys found in environment variables. Please set TWELVEDATA_API_KEY_1 to 5.")
    # You might want to raise an exception or exit here in a real production app
    # For now, we'll proceed, but API calls will fail.

TWELVEDATA_BASE_URL = 'https://api.twelvedata.com'

# Configuration for Flask (adjust host/port if running locally)
FLASK_HOST = '0.0.0.0' # Listen on all available interfaces
FLASK_PORT = 5000

# Global cache for TwelveData API responses
TWELVEDATA_CACHE = {}
CACHE_DURATION_SECONDS = 60 # Cache data for 60 seconds

# List of currency pairs to monitor - UPDATED TO USER'S SPECIFIC LIST
CURRENCY_PAIRS = [
    "AUD/USD", "EUR/JPY", "EUR/USD", "GBP/JPY", "GBP/USD", "NZD/USD",
    "USD/BDT", "USD/BRL", "USD/CAD", "USD/CHF", "USD/JPY", "USD/ZAR"
]

# Signal generation parameters - ADJUSTED FOR FASTER SIGNALS AND NEW REQUIREMENTS
SIGNAL_INTERVAL_MINUTES = 1 # How often to check for new signals (e.g., every 1 minute)
RESTING_PERIOD_MINUTES = 2 # RESTING PERIOD RE-ENABLED: Bot rests for 2 minutes after 5 signals
COOLDOWN_AFTER_RESULT_SECONDS = 30 # How long to display a WIN/LOSS result before clearing the card (e.g., 30 seconds)
MAX_ACTIVE_SIGNALS = 4 # Maximum number of "WAITING" signals at any given time


# --- Helper Functions for Indicators ---

def calculate_ema(prices, period):
    """Calculates Exponential Moving Average (EMA)."""
    if not prices or len(prices) < period:
        return []
    ema_values = []
    smoothing_factor = 2 / (period + 1)
    if period > 0:
        sma = sum(prices[:period]) / period
    else:
        return []
    ema_values.append(sma)
    for i in range(period, len(prices)):
        ema = (prices[i] * smoothing_factor) + (ema_values[-1] * (1 - smoothing_factor))
        ema_values.append(ema)
    return ema_values

def calculate_rsi(prices, period):
    """Calculates Relative Strength Index (RSI)."""
    if not prices or len(prices) < period + 1:
        return []
    
    gains = [0.0] * (len(prices) - 1)
    losses = [0.0] * (len(prices) - 1)

    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains[i-1] = change
        else:
            losses[i-1] = abs(change)

    rsi_values = []
    
    if len(gains) < period:
        return []

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        rs = float('inf') if avg_gain > 0 else 0.0
    else:
        rs = avg_gain / avg_loss
        
    rsi = 100 - (100 / (1 + rs))
    rsi_values.append(rsi)

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        
        if avg_loss == 0:
            rs = float('inf') if avg_gain > 0 else 0.0
        else:
            rs = avg_gain / avg_loss
            
        rsi = 100 - (100 / (1 + rs))
        rsi_values.append(rsi)
    
    return rsi_values

def calculate_macd(prices, fast_period, slow_period, signal_period):
    """Calculates Moving Average Convergence Divergence (MACD)."""
    if len(prices) < max(fast_period, slow_period) + signal_period:
        return [], [], []
    
    ema_fast = calculate_ema(prices, fast_period)
    ema_slow = calculate_ema(prices, slow_period)
    
    min_len_ema = min(len(ema_fast), len(ema_slow))
    if min_len_ema == 0: return [], [], []

    macd_line = [ema_fast[len(ema_fast) - min_len_ema + i] - ema_slow[len(ema_slow) - min_len_ema + i] for i in range(min_len_ema)]
    
    signal_line = calculate_ema(macd_line, signal_period)
    
    min_hist_len = min(len(macd_line), len(signal_line))
    if min_hist_len == 0: return macd_line, signal_line, []

    histogram = [macd_line[len(macd_line) - min_hist_len + i] - signal_line[len(signal_line) - min_hist_len + i] for i in range(min_hist_len)]
    
    return macd_line, signal_line, histogram

def calculate_bollinger_bands(prices, period, std_dev):
    """Calculates Bollinger Bands."""
    if not prices or len(prices) < period:
        return [], [], []
    
    upper_band = []
    middle_band = []
    lower_band = []
    
    for i in range(len(prices) - period + 1):
        window = prices[i:i+period]
        sma = sum(window) / period
        middle_band.append(sma)
        
        variance = sum([(x - sma) ** 2 for x in window]) / period
        current_std_dev = math.sqrt(variance)
        
        upper_band.append(sma + (current_std_dev * std_dev))
        lower_band.append(sma - (current_std_dev * std_dev))
        
    return upper_band, middle_band, lower_band

def calculate_average_volume(volumes, lookback_period=10):
    """Calculates the average volume over a lookback period."""
    if len(volumes) < lookback_period:
        return 0.0
    return sum(volumes[-lookback_period:]) / sum(volumes[-lookback_period:]) if sum(volumes[-lookback_period:]) > 0 else 0.0 # Changed to avoid division by zero

def get_candle_type(candle_data):
    """
    Determines if a candle was 'bullish_clean', 'bearish_clean', or 'neutral'.
    A 'clean' candle implies a strong body relative to its total range (small wicks).
    """
    open_price = float(candle_data['open'])
    close_price = float(candle_data['close'])
    high_price = float(candle_data['high'])
    low_price = float(candle_data['low'])

    body_size = abs(close_price - open_price)
    total_range = high_price - low_price

    if total_range == 0:
        return "neutral"

    body_ratio = body_size / total_range

    if close_price > open_price and body_ratio >= 0.6: # Strong full-body candle
        return "bullish_clean"
    elif close_price < open_price and body_ratio >= 0.6: # Strong full-body candle
        return "bearish_clean"
    else:
        return "neutral"

def get_heikin_ashi_candles(candles):
    """
    Converts regular OHLC candles to Heikin-Ashi candles.
    Returns a list of dictionaries with 'open', 'high', 'low', 'close', 'datetime'.
    """
    if not candles:
        return []

    ha_candles = []
    
    first_candle = candles[0]
    ha_close = (float(first_candle['open']) + float(first_candle['high']) + float(first_candle['low']) + float(first_candle['close'])) / 4
    ha_open = (float(first_candle['open']) + float(first_candle['close'])) / 2
    ha_high = max(float(first_candle['high']), ha_open, ha_close)
    ha_low = min(float(first_candle['low']), ha_open, ha_close)
    
    ha_candles.append({
        'datetime': first_candle['datetime'],
        'open': ha_open,
        'high': ha_high,
        'low': ha_low,
        'close': ha_close
    })

    for i in range(1, len(candles)):
        current_candle = candles[i]
        prev_ha_candle = ha_candles[-1]

        ha_close = (float(current_candle['open']) + float(current_candle['high']) + float(current_candle['low']) + float(current_candle['close'])) / 4
        ha_open = (prev_ha_candle['open'] + prev_ha_candle['close']) / 2
        ha_high = max(float(current_candle['high']), ha_open, ha_close)
        ha_low = min(float(current_candle['low']), ha_open, ha_close)

        ha_candles.append({
            'datetime': current_candle['datetime'],
            'open': ha_open,
            'high': ha_high,
            'low': ha_low,
            'close': ha_close
        })
    return ha_candles


# --- MK PRO STRATEGY LOGIC ---
def mk_pro_generate_signal(data, pair_symbol):
    """
    Optimized MK PRO STRATEGY to generate faster signals with less filtering.
    Adjusted thresholds to allow for more frequent, potentially lower confidence signals.
    """

    close_price = data['close_price']
    ema10 = data['ema10']
    ema30 = data['ema30']
    rsi = data['rsi']
    macd_line = data['macd_line']
    signal_line = data['signal_line']
    bb_upper = data['bb_upper']
    bb_lower = data['bb_lower']
    volume = data['volume']
    avg_volume = data['avg_volume']
    current_candle_open = data['current_candle_open']
    current_candle_high = data['current_candle_high']
    current_candle_low = data['current_candle_low']
    current_candle_close = data['current_candle_close']
    current_candle_body_percentage = data['current_candle_body_percentage']
    latest_histogram = data['latest_histogram']
    prev_histogram = data['prev_histogram']
    prev_prev_histogram = data['prev_prev_histogram']
    ha_current_candle = data['ha_current_candle']
    ha_prev_candle = data['ha_prev_candle']

    final_signal = "NONE"
    final_confidence = "LOW"
    reasons = []
    bull_conditions_met = 0
    bear_conditions_met = 0

    # User-defined CONFIDENCE_THRESHOLD - Adjusted for more signals
    CONFIDENCE_THRESHOLD_MEDIUM = 0.50 # Was 0.60
    CONFIDENCE_THRESHOLD_HIGH = 0.70 # For high confidence signals

    # === Optimized Filters (Reduced unnecessary filters) ===
    # Bollinger Band Width Filter: Adjusted threshold to be less restrictive (down to 0.05%)
    bb_width = bb_upper - bb_lower
    if close_price > 0 and (bb_width / close_price) * 100 < 0.05:
        reasons.append(f"CAUTION: Low BB Width ({(bb_width / close_price) * 100:.2f}%)")
        # Do NOT return here, continue processing. This is now a caution, not a blocker.

    # Candle Body Percentage Filter: Reduced threshold to 45%
    if current_candle_body_percentage < 45:
        reasons.append(f"FILTERED: Weak Candle Body ({current_candle_body_percentage:.2f}%)")
        print(f"DEBUG: {pair_symbol} - Signal filtered due to weak candle body ({current_candle_body_percentage:.2f}%).")
        return { "signal": "NONE", "confidence": "LOW", "reason": ", ".join(reasons), "reasons_list": reasons, "bull_conditions_met": 0, "bear_conditions_met": 0 }


    # === Bullish Conditions ===
    bull_reasons = []

    # 1. EMA10 > EMA30 for UP
    if ema10 > ema30:
        bull_conditions_met += 1
        bull_reasons.append("EMA10 > EMA30 (Bullish Trend)")

    # 2. MACD histogram 3 bar rising confirmation
    if latest_histogram is not None and prev_histogram is not None and prev_prev_histogram is not None and \
       latest_histogram > prev_histogram and prev_histogram > prev_prev_histogram and latest_histogram > 0:
        bull_conditions_met += 1
        bull_reasons.append("MACD Histogram Rising (3 bars) & Above 0")

    # 3. RSI > 63 as trend strength
    if rsi is not None and rsi > 63:
        bull_conditions_met += 1
        bull_reasons.append(f"RSI > 63 ({rsi:.2f}) (Strong Buy Pressure)")

    # 4. Volume spike condition (> 1.5x avg) - Prioritized
    if avg_volume > 0 and volume > 1.5 * avg_volume: # Added check for avg_volume > 0
        bull_conditions_met += 1
        bull_reasons.append(f"Volume Spike ({volume:.0f} > 1.5 * Avg {avg_volume:.0f})")

    # 5. Bollinger Band breakout (optional confirmation)
    if bb_upper is not None and current_candle_close > bb_upper and current_candle_body_percentage >= 45:
        bull_conditions_met += 1
        bull_reasons.append(f"BB Breakout UP (Price {current_candle_close:.4f} > BB Upper {bb_upper:.4f})")

    # Heikin Ashi clean confirmation (optional confirmation)
    if ha_current_candle and ha_prev_candle and \
       ha_current_candle['close'] > ha_current_candle['open'] and ha_current_candle['low'] == ha_current_candle['open'] and \
       ha_prev_candle['close'] > ha_prev_candle['open'] and ha_prev_candle['low'] == ha_prev_candle['open']:
        bull_conditions_met += 1
        bull_reasons.append("Heikin-Ashi: Last 2 candles clean green")
    
    # === Bearish Conditions ===
    bear_reasons = []

    # 1. EMA10 < EMA30 for DOWN
    if ema10 < ema30:
        bear_conditions_met += 1
        bear_reasons.append("EMA10 < EMA30 (Bearish Trend)")

    # 2. MACD histogram 3 bar falling confirmation
    if latest_histogram is not None and prev_histogram is not None and prev_prev_histogram is not None and \
       latest_histogram < prev_histogram and prev_histogram < prev_prev_histogram and latest_histogram < 0:
        bear_conditions_met += 1
        bear_reasons.append("MACD Histogram Falling (3 bars) & Below 0")

    # 3. RSI < 37 as trend strength
    if rsi is not None and rsi < 37:
        bear_conditions_met += 1
        bear_reasons.append(f"RSI < 37 ({rsi:.2f}) (Strong Sell Pressure)")

    # 4. Volume spike condition (> 1.5x avg) - Prioritized
    if avg_volume > 0 and volume > 1.5 * avg_volume: # Added check for avg_volume > 0
        bear_conditions_met += 1
        bear_reasons.append(f"Volume Spike ({volume:.0f} > 1.5 * Avg {avg_volume:.0f})")

    # 5. BB breakout (optional confirmation)
    if bb_lower is not None and current_candle_close < bb_lower and current_candle_body_percentage >= 45:
        bear_conditions_met += 1
        bear_reasons.append(f"BB Breakout DOWN (Price {current_candle_close:.4f} < BB Lower {bb_lower:.4f})")

    # Heikin Ashi clean confirmation (optional confirmation)
    if ha_current_candle and ha_prev_candle and \
       ha_current_candle['close'] < ha_current_candle['open'] and ha_current_candle['high'] == ha_current_candle['open'] and \
       ha_prev_candle['close'] < ha_prev_candle['open'] and ha_prev_candle['high'] == ha_prev_candle['open']:
        bear_conditions_met += 1
        bear_reasons.append("Heikin-Ashi: Last 2 candles clean red")

    # === Final Signal Decision ===
    # Goal: More frequent signals with medium to high confidence.
    # Prioritize EMA and MACD. If 2 or more conditions met, give signal.
    
    # Calculate overall confidence based on conditions met
    total_possible_conditions = 6 # EMA, MACD, Volume, RSI, BB, HA (all are conditions now, no hard filters except weak candle body)
    bull_confidence_score = bull_conditions_met / total_possible_conditions
    bear_confidence_score = bear_conditions_met / total_possible_conditions

    # "Focus on confidence stacking—1 or more confirmations = signal allowed." (Relaxed from 2)
    # If 1 or more conditions are met AND confidence threshold is met.
    if bull_conditions_met >= 1 and bull_confidence_score >= 0.15 and bear_conditions_met < 1: # Relaxed confidence score for LOW
        final_signal = "UP"
        if bull_confidence_score >= CONFIDENCE_THRESHOLD_HIGH:
            final_confidence = "HIGH"
        elif bull_confidence_score >= CONFIDENCE_THRESHOLD_MEDIUM:
            final_confidence = "MEDIUM"
        else:
            final_confidence = "LOW" # Explicitly set to LOW
        final_reasons = bull_reasons
    elif bear_conditions_met >= 1 and bear_confidence_score >= 0.15 and bull_conditions_met < 1: # Relaxed confidence score for LOW
        final_signal = "DOWN"
        if bear_confidence_score >= CONFIDENCE_THRESHOLD_HIGH:
            final_confidence = "HIGH"
        elif bear_confidence_score >= CONFIDENCE_THRESHOLD_MEDIUM:
            final_confidence = "MEDIUM"
        else:
            final_confidence = "LOW" # Explicitly set to LOW
        final_reasons = bear_reasons
    else:
        final_signal = "NONE"
        final_confidence = "LOW"
        reasons.append(f"SKIPPED: Bullish Confirmed: {bull_conditions_met}, Bearish Confirmed: {bear_conditions_met}. Not enough high-confidence conditions met or conflicting signals.")
        final_reasons = reasons # If skipped due to low confidence or conflict, use the general reasons list.

    # Add BB width caution to reasons if a signal is generated
    if final_signal != "NONE":
        bb_width_ratio = (bb_upper - bb_lower) / close_price * 100
        if bb_width_ratio < 0.05:
            final_reasons.insert(0, f"CAUTION: Low BB Width ({bb_width_ratio:.2f}%) - Potential for limited movement")
    
    return {
        "signal": final_signal,
        "confidence": final_confidence,
        "reason": " | ".join(final_reasons),
        "reasons_list": final_reasons,
        "bull_conditions_met": bull_conditions_met,
        "bear_conditions_met": bear_conditions_met
    }


# --- TwelveData API Integration ---
def fetch_twelvedata_candles(symbol, interval="1min", outputsize=250):
    """
    Fetches market candle data using multiple TwelveData API keys.
    If the first key fails (due to rate limit or error), the system auto-switches to the next key.
    """
    now = datetime.datetime.now() 
    with signals_lock: # Acquire lock before accessing TWELVEDATA_CACHE
        if symbol in TWELVEDATA_CACHE:
            cached_entry = TWELVEDATA_CACHE[symbol]
            if now - cached_entry['timestamp'] < datetime.timedelta(seconds=CACHE_DURATION_SECONDS): 
                print(f"Using cached data for {symbol} (fetched at {cached_entry['timestamp'].strftime('%H:%M:%S')})")
                return cached_entry['data']

    url = f"{TWELVEDATA_BASE_URL}/time_series"

    for key in API_KEYS:
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": key
        }

        try:
            print(f"🔄 Trying API Key: {key[:5]}... for {symbol}")
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            try:
                data = response.json()
            except json.JSONDecodeError:
                print(f"Error: Could not decode JSON from TwelveData for {symbol}. Response: {response.text[:200]}...")
                continue

            if 'values' in data:
                candles = []
                # Iterate in reverse to get oldest to newest, as indicators expect this order
                for item in reversed(data['values']): 
                    try:
                        open_price = float(item.get('open', 0.0) or 0.0)
                        high_price = float(item.get('high', 0.0) or 0.0)
                        low_price = float(item.get('low', 0.0) or 0.0)
                        close_price = float(item.get('close', 0.0) or 0.0)
                        volume = float(item.get('volume', 0.0) or 0.0)

                        candles.append({
                            'datetime': item.get('datetime', ''),
                            'open': open_price,
                            'high': high_price,
                            'low': low_price,
                            'close': close_price,
                            'volume': volume
                        })
                    except (ValueError, TypeError) as e:
                        print(f"Warning: Could not convert price or volume data for {symbol} at {item.get('datetime', 'N/A')}. Error: {e}. Skipping this candle.")
                        continue

                if candles:
                    with signals_lock: # Acquire lock before writing to TWELVEDATA_CACHE
                        TWELVEDATA_CACHE[symbol] = {'data': candles, 'timestamp': now}
                    print(f"✅ Success using API key: {key[:5]}... for {symbol}")
                    return candles
                else:
                    print(f"⚠️ No valid candle data found using key {key[:5]}... for {symbol}. Trying next key.")
                    continue

            elif 'message' in data:
                error_msg = data.get("message", "No message from API")
                print(f"❌ API Key {key[:5]}... returned error: {error_msg}. Trying next key.")
                continue

        except requests.exceptions.HTTPError as e:
            print(f"❌ HTTP Error for {symbol} with API Key {key[:5]}...: {e.response.status_code} - {e.response.text}. Trying next key.")
            continue
        except requests.exceptions.ConnectionError as e:
            print(f"❌ Connection Error for {symbol} with API Key {key[:5]}...: {e}. Check internet connection or API endpoint. Trying next key.")
            continue
        except requests.exceptions.Timeout:
            print(f"❌ Timeout Error for {symbol} with API Key {key[:5]}...: Request timed out. Trying next key.")
            continue
        except requests.exceptions.RequestException as e:
            print(f"❌ An unexpected Request Error occurred for {symbol} with API Key {key[:5]}...: {e}. Trying next key.")
            continue

    print(f"❌ All API keys failed for {symbol}. Cannot fetch candle data.")
    return None

# --- Main Signal Analysis Function (calls MK PRO STRATEGY) ---
def analyze_and_generate_signal(symbol, candles):
    """
    Prepares data and calls the MK PRO STRATEGY LOGIC to generate a trading signal.
    """
    # Minimum candles required for all indicators:
    # EMA200 needs 200 candles. MACD needs 26+9-1 = 34 candles. RSI needs 7+1=8 candles. BB needs 20 candles.
    # Heikin Ashi needs at least 2 for "last 2 HA candles".
    # So, minimum 200 candles are required for robust analysis.
    if not candles or len(candles) < 200: 
        print(f"DEBUG: Not enough candles ({len(candles)}) for {symbol}. At least 200 are needed for robust signal generation.")
        return {
            "pair": symbol,
            "signal_generated_at": "N/A",
            "entry_time": "N/A",
            "expiry_time": "N/A",
            "direction": "NONE",
            "confidence": "LOW",
            "result": "No Signal",
            "entry_price": None,
            "expiry_timestamp": 0,
            "reasons": ["Not enough historical data or indicator values missing."],
            "reason": "Not enough historical data or indicator values missing." # Added for consistency
        }

    close_prices = [float(c['close']) for c in candles]
    high_prices = [float(c['high']) for c in candles]
    low_prices = [float(c['low']) for c in candles]
    volumes = [float(c['volume']) for c in candles]

    current_candle = candles[-1]
    prev_candle_data = candles[-2] # Used for prev_candle_type
    
    # Calculate all indicators
    ema10_values = calculate_ema(close_prices, 10)
    ema30_values = calculate_ema(close_prices, 30)
    ema200_values = calculate_ema(close_prices, 200) # Still useful for overall trend context
    rsi_values = calculate_rsi(close_prices, 7) # RSI(7) as per prompt
    macd_line_values, signal_line_values, histogram_values = calculate_macd(close_prices, 12, 26, 9)
    bb_upper_values, bb_middle_values, bb_lower_values = calculate_bollinger_bands(close_prices, 20, 2)
    ha_candles = get_heikin_ashi_candles(candles)

    # Get latest values
    latest_ema10 = ema10_values[-1] if ema10_values else None
    latest_ema30 = ema30_values[-1] if ema30_values else None
    latest_rsi = rsi_values[-1] if rsi_values else None
    latest_macd_line = macd_line_values[-1] if macd_line_values else None
    latest_signal_line = signal_line_values[-1] if signal_line_values else None
    latest_bb_upper = bb_upper_values[-1] if bb_upper_values else None
    latest_bb_lower = bb_lower_values[-1] if bb_lower_values else None
    
    latest_histogram = histogram_values[-1] if len(histogram_values) >= 1 else None
    prev_histogram = histogram_values[-2] if len(histogram_values) >= 2 else None
    prev_prev_histogram = histogram_values[-3] if len(histogram_values) >= 3 else None

    ha_current_candle = ha_candles[-1] if ha_candles else None
    ha_prev_candle = ha_candles[-2] if len(ha_candles) >= 2 else None # Needed for "last 2 HA candles"

    current_volume = volumes[-1]
    avg_volume = calculate_average_volume(volumes, lookback_period=10)
    
    # Calculate candle body percentage for current candle
    current_candle_body_size = abs(current_candle['close'] - current_candle['open'])
    current_candle_total_range = current_candle['high'] - current_candle['low']
    current_candle_body_percentage = (current_candle_body_size / current_candle_total_range) * 100 if current_candle_total_range > 0 else 0

    # Check if all necessary latest indicator values are available
    if any(val is None for val in [latest_ema10, latest_ema30, latest_rsi, 
                                   latest_macd_line, latest_signal_line, 
                                   latest_bb_upper, latest_bb_lower,
                                   latest_histogram, prev_histogram, prev_prev_histogram,
                                   ha_current_candle, ha_prev_candle]): 
        print(f"DEBUG: Not all latest indicator values available for {symbol}. Skipping signal generation.")
        return {
            "pair": symbol,
            "signal_generated_at": datetime.datetime.now().strftime("%H:%M:%S"), 
            "entry_time": "N/A",
            "expiry_time": "N/A",
            "direction": "NONE",
            "confidence": "N/A",
            "result": "No Signal",
            "entry_price": None,
            "expiry_timestamp": 0,
            "reasons": ["Not enough historical data or indicator values missing."],
            "reason": "Not enough historical data or indicator values missing." # Added for consistency
        }

    strategy_data = {
        'close_price': current_candle['close'],
        'ema10': latest_ema10,
        'ema30': latest_ema30,
        'rsi': latest_rsi,
        'macd_line': latest_macd_line,
        'signal_line': latest_signal_line, # Corrected: Use latest_signal_line here
        'bb_upper': latest_bb_upper,
        'bb_lower': latest_bb_lower,
        'volume': current_volume,
        'avg_volume': avg_volume,
        'current_candle_open': current_candle['open'],
        'current_candle_high': current_candle['high'],
        'current_candle_low': current_candle['low'],
        'current_candle_close': current_candle['close'],
        'current_candle_body_percentage': current_candle_body_percentage,
        'latest_histogram': latest_histogram,
        'prev_histogram': prev_histogram,
        'prev_prev_histogram': prev_prev_histogram,
        'ha_current_candle': ha_current_candle,
        'ha_prev_candle': ha_prev_candle,
    }

    print(f"\n--- Analysis for {symbol} ({datetime.datetime.now().strftime('%H:%M:%S')}) ---") 
    print(f"  Close Price: {strategy_data['close_price']:.4f}")
    print(f"  EMA10: {strategy_data['ema10']:.4f}, EMA30: {strategy_data['ema30']:.4f}")
    print(f"  RSI: {strategy_data['rsi']:.2f}")
    print(f"  MACD Line: {strategy_data['macd_line']:.4f}, Signal Line: {strategy_data['signal_line']:.4f}") # Corrected line
    print(f"  Histogram: Current={strategy_data['latest_histogram']:.4f}, Prev={strategy_data['prev_histogram']:.4f}, PrevPrev={strategy_data['prev_prev_histogram']:.4f}")
    print(f"  BB Upper: {strategy_data['bb_upper']:.4f}, BB Lower: {strategy_data['bb_lower']:.4f}")
    print(f"  Current Volume: {strategy_data['volume']:.0f}, Avg Volume: {strategy_data['avg_volume']:.0f}")
    print(f"  Current Candle Body %: {strategy_data['current_candle_body_percentage']:.2f}%")
    print(f"  Heikin-Ashi (Current): O={ha_current_candle['open']:.4f}, H={ha_current_candle['high']:.4f}, L={ha_current_candle['low']:.4f}, C={ha_current_candle['close']:.4f}")
    print(f"  Heikin-Ashi (Prev): O={ha_prev_candle['open']:.4f}, H={ha_prev_candle['high']:.4f}, L={ha_prev_candle['low']:.4f}, C={ha_prev_candle['close']:.4f}")


    pro_signal_output = mk_pro_generate_signal(strategy_data, symbol)
    
    direction = pro_signal_output["signal"]
    confidence = pro_signal_output["confidence"]
    reason = pro_signal_output["reason"]
    reasons_list = pro_signal_output["reasons_list"] # Get the list of reasons

    print(f"  Bullish Conditions Met: {pro_signal_output.get('bull_conditions_met', 'N/A')}")
    print(f"  Bearish Conditions Met: {pro_signal_output.get('bear_conditions_met', 'N/A')}")
    print(f"  Reasons List: {reasons_list}")


    print(f"  MK PRO STRATEGY Decision: Signal={direction}, Confidence={confidence}, Reason={reason}")

    signal_data = {
        "pair": symbol,
        "signal_generated_at": "N/A",
        "entry_time": "N/A",
        "expiry_time": "N/A",
        "direction": direction,
        "confidence": confidence,
        "result": "No Signal" if direction == "NONE" else "⏳ WAITING",
        "entry_price": None,
        "expiry_timestamp": 0,
        "reasons": reasons_list, # Use the list of reasons here
        "reason": reason # Ensure the singular 'reason' key is also present
    }

    if direction != "NONE":
        signal_generation_time = datetime.datetime.now() 
        entry_time_dt = signal_generation_time + datetime.timedelta(minutes=1) # Entry 1 minute after signal generation
        expiry_time_dt = entry_time_dt + datetime.timedelta(minutes=1) # Expiry 1 minute after entry

        signal_data.update({
            "signal_generated_at": signal_generation_time.strftime("%H:%M:%S"),
            "entry_time": entry_time_dt.strftime("%H:%M:%S"),
            "entry_price": strategy_data['close_price'], # Entry price is the close of the candle analyzed
            "expiry_time": expiry_time_dt.strftime("%H:%M:%S"),
            "expiry_timestamp": expiry_time_dt.timestamp(),
        })
    
    return signal_data


# --- Background Signal Generation Loop ---
def signal_generation_loop():
    """
    Fetches data and generates signals periodically for each pair, respecting rest periods.
    This runs in a separate thread.
    """
    print("Signal generation loop starting...")
    for pair in CURRENCY_PAIRS:
        with signals_lock: # Acquire lock for initial signals dictionary setup
            signals[pair] = {
                "current_signal": {
                    "pair": pair,
                    "signal_generated_at": "N/A",
                    "entry_time": "N/A",
                    "expiry_time": "N/A",
                    "direction": "NONE",
                    "confidence": "N/A",
                    "result": "No Signal",
                    "entry_price": None,
                    "expiry_timestamp": 0,
                    "reasons": ["No signal generated yet."],
                    "reason": "No signal generated yet." # Initialize singular reason
                },
                "last_trade_finished_at": datetime.datetime.now() - datetime.timedelta(minutes=5), 
                "last_signal_generated_at": datetime.datetime.now() - datetime.timedelta(minutes=2), 
                "is_resting": False, # Initialize as False
                "rest_end_time": datetime.datetime.now(), # Initialize
                "result_display_end_time": datetime.datetime.now(),
                "consecutive_losses": 0, # New: Track consecutive losses for this pair
                "forced_wins_given": 0 # New: Track forced wins given for this pair
            }

    while True:
        try:
            # Re-fetch current time at the beginning of each loop iteration for accuracy
            current_time = datetime.datetime.now() 
            print(f"\n--- Signal Loop Iteration Start ({current_time.strftime('%H:%M:%S')}) ---")
            
            # --- Step 1: Check and update expired WAITING signals and clear old results ---
            for pair in CURRENCY_PAIRS:
                with signals_lock:
                    signal_data = signals[pair]["current_signal"]
                    pair_state = signals[pair] # Get the full state for the pair

                # Case 1: Signal was WAITING and has now expired
                if signal_data["result"] == "⏳ WAITING" and signal_data["expiry_timestamp"] > 0 and signal_data["expiry_timestamp"] <= current_time.timestamp():
                    print(f"Checking result for expired signal on {pair} (Expiry: {datetime.datetime.fromtimestamp(signal_data['expiry_timestamp']).strftime('%H:%M:%S')})...") 
                    
                    real_result = "UNKNOWN" # Store the actual calculated result
                    result = "UNKNOWN" # This will be the displayed result (potentially manipulated)
                    result_reason_detail = ""

                    # Fetch latest candles to determine result (need at least 1 candle after expiry)
                    # Fetch 2 candles: current (expiry) and previous to ensure we have the open/close of the expiry candle
                    latest_candles_for_result = fetch_twelvedata_candles(pair, outputsize=2) 
                    
                    if latest_candles_for_result and len(latest_candles_for_result) >= 1: # Only need the expiry candle itself
                        expiry_candle = latest_candles_for_result[-1] 
                        expiry_open = float(expiry_candle['open'])
                        expiry_close = float(expiry_candle['close'])
                        expiry_candle_datetime = expiry_candle['datetime']
                        entry_price_for_result = signal_data["entry_price"] # Get the stored entry price

                        original_reasons = signal_data.get('reasons', ['No specific reason provided for signal generation.'])
                        
                        print(f"  {pair} - Expiry Candle Open: {expiry_open:.4f}, Expiry Candle Close: {expiry_close:.4f} (at {expiry_candle_datetime}), Direction: {signal_data['direction']}, Entry Price: {entry_price_for_result:.4f}")

                        if entry_price_for_result is None:
                            real_result = "Data Error (Entry price missing for result check)"
                            result_reason_detail = "Entry price was not recorded for this signal."
                            print(f"Error: {result_reason_detail}")
                        else:
                            # Determine actual WIN/LOSS based on entry and expiry prices
                            if signal_data["direction"] == "UP":
                                if expiry_close > entry_price_for_result:
                                    real_result = "✅ WIN"
                                    result_reason_detail = f"Trade WIN: Expiry Close ({expiry_close:.4f}) > Entry Price ({entry_price_for_result:.4f})."
                                else:
                                    real_result = "❌ LOSS"
                                    result_reason_detail = f"Trade LOSS: Expiry Close ({expiry_close:.4f}) <= Entry Price ({entry_price_for_result:.4f})."
                            elif signal_data["direction"] == "DOWN":
                                if expiry_close < entry_price_for_result:
                                    real_result = "✅ WIN"
                                    result_reason_detail = f"Trade WIN: Expiry Close ({expiry_close:.4f}) < Entry Price ({entry_price_for_result:.4f})."
                                else:
                                    real_result = "❌ LOSS"
                                    result_reason_detail = f"Trade LOSS: Expiry Close ({expiry_close:.4f}) >= Entry Price ({entry_price_for_result:.4f})."
                            else:
                                real_result = "N/A (No Direction)"
                                result_reason_detail = "No direction for WAITING signal."
                            print(f"  {pair} - Actual Result: {real_result} - {result_reason_detail}")
                    else:
                        real_result = "Data Error (Could not fetch expiry candle for result)"
                        result_reason_detail = "Could not fetch enough candles to determine expiry result."
                        print(f"Error: Could not fetch enough candles for {pair} to determine expiry result.")

                    # --- Result Manipulation Logic ---
                    result = real_result # Start with the actual result

                    with signals_lock: # Acquire lock before modifying signals[pair]
                        if real_result == "❌ LOSS":
                            signals[pair]["consecutive_losses"] += 1
                            signals[pair]["forced_wins_given"] = 0 # Reset forced wins if a real loss occurs
                            print(f"  {pair} - Consecutive Losses: {signals[pair]['consecutive_losses']}")

                            # If 4 or more consecutive losses, force 2 wins
                            if signals[pair]["consecutive_losses"] >= 4 and signals[pair]["forced_wins_given"] < 2:
                                result = "✅ WIN" # Force it to be a WIN for display
                                signals[pair]["forced_wins_given"] += 1
                                # Do NOT reset consecutive_losses here, as it's still a "real" loss
                                result_reason_detail += " (FORCED WIN FOR DISPLAY - User Request)"
                                print(f"  {pair} - FORCED WIN for display. Forced Wins Given: {signals[pair]['forced_wins_given']}")
                        elif real_result == "✅ WIN":
                            signals[pair]["consecutive_losses"] = 0 # Reset consecutive losses on a real win
                            signals[pair]["forced_wins_given"] = 0 # Reset forced wins on a real win
                            print(f"  {pair} - Real WIN. Consecutive Losses Reset.")
                        else: # Data Error or N/A
                            signals[pair]["consecutive_losses"] = 0 # Reset on data error or no direction
                            signals[pair]["forced_wins_given"] = 0
                            print(f"  {pair} - Result N/A or Data Error. Consecutive Losses Reset.")

                        signals[pair]["current_signal"]["result"] = result # Update with potentially manipulated result
                        # Append the result reason to the existing reasons list
                        signals[pair]["current_signal"]["reasons"].append(f"Result: {result_reason_detail}")
                        signals[pair]["current_signal"]["reason"] = f"{signal_data['reason']} | Result Logic: {result_reason_detail}" # Update singular reason
                        signals[pair]["last_trade_finished_at"] = current_time
                        signals[pair]["result_display_end_time"] = current_time + datetime.timedelta(seconds=COOLDOWN_AFTER_RESULT_SECONDS) 

                        if signal_data["direction"] != "NONE":
                            signals[pair]["signals_given_count"] = signals[pair].get("signals_given_count", 0) + 1 # Initialize if not exists
                            print(f"{pair}: Signals given since last rest: {signals[pair]['signals_given_count']}")

                        print(f"  {pair} - Updated state after result: {signals[pair]['current_signal']['result']}, is_resting: {signals[pair]['is_resting']}")

                        if signals[pair].get("signals_given_count", 0) >= 5: # Check signals_given_count
                            signals[pair]["is_resting"] = True
                            signals[pair]["rest_end_time"] = current_time + datetime.timedelta(minutes=RESTING_PERIOD_MINUTES) 
                            signals[pair]["signals_given_count"] = 0
                            print(f"{pair} has given 5 signals. Entering {RESTING_PERIOD_MINUTES}-minute rest until {signals[pair]['rest_end_time'].strftime('%H:%M:%S')}.")
                        else:
                            print(f"Updated result for {pair}: {result}. Displaying for {COOLDOWN_AFTER_RESULT_SECONDS} seconds.")

                # Case 2: Signal was WIN/LOSS and its display cooldown has ended
                elif signal_data["result"] in ["✅ WIN", "❌ LOSS"] and current_time >= pair_state["result_display_end_time"]:
                    with signals_lock:
                        # Reset the signal for the pair to "No Signal" so new signals can be generated
                        signals[pair]["current_signal"] = {
                            "pair": pair,
                            "signal_generated_at": "N/A",
                            "entry_time": "N/A",
                            "expiry_time": "N/A",
                            "direction": "NONE",
                            "confidence": "N/A",
                            "result": "No Signal",
                            "entry_price": None,
                            "expiry_timestamp": 0,
                            "reasons": ["Previous trade result displayed. Waiting for new signal."],
                            "reason": "Previous trade result displayed. Waiting for new signal."
                        }
                        signals[pair]["last_signal_generated_at"] = current_time # Mark this time for eligibility check
                    print(f"Cleared displayed result for {pair}. Now 'No Signal' state.")

                # Case 3: Pair is resting and rest period has ended
                elif pair_state["is_resting"] and current_time >= pair_state["rest_end_time"]:
                    with signals_lock:
                        signals[pair]["is_resting"] = False
                        print(f"{pair} rest period over. Now eligible for new signal generation.")
                        # Reset signal state for the pair
                        signals[pair]["current_signal"] = {
                            "pair": pair,
                            "signal_generated_at": "N/A",
                            "entry_time": "N/A",
                            "expiry_time": "N/A",
                            "direction": "NONE",
                            "confidence": "N/A",
                            "result": "No Signal",
                            "entry_price": None,
                            "expiry_timestamp": 0,
                            "reasons": ["Rest period ended. Waiting for new signal."],
                            "reason": "Rest period ended. Waiting for new signal."
                        }
                        signals[pair]["last_signal_generated_at"] = current_time
                        signals[pair]["result_display_end_time"] = current_time # Clear result display cooldown
                        signals[pair]["consecutive_losses"] = 0
                        signals[pair]["forced_wins_given"] = 0
                        print(f"  {pair} - Updated state after rest: {signals[pair]['current_signal']['result']}, is_resting: {signals[pair]['is_resting']}")


            # --- Step 2: Identify eligible pairs for new signal generation ---
            potential_signals_for_this_iteration = []
            with signals_lock: # Acquire lock before reading signals for active_waiting_signals_count
                active_waiting_signals_count = sum(1 for p_data in signals.values() if p_data["current_signal"]["result"] == "⏳ WAITING")
            print(f"Current active WAITING signals count: {active_waiting_signals_count} (Max: {MAX_ACTIVE_SIGNALS})")


            for pair in CURRENCY_PAIRS:
                with signals_lock: # Acquire lock before accessing pair's signal data
                    pair_signals_data = signals[pair] # Get a reference to the pair's data
                
                print(f"Checking eligibility for {pair}:")
                print(f"  is_resting: {pair_signals_data['is_resting']}")
                print(f"  current_signal.result: {pair_signals_data['current_signal']['result']}")
                print(f"  result_display_end_time: {pair_signals_data['result_display_end_time'].strftime('%H:%M:%S')}")
                print(f"  current_time: {current_time.strftime('%H:%M:%S')}")
                print(f"  last_signal_generated_at: {pair_signals_data['last_signal_generated_at'].strftime('%H:%M:%S')}")

                # Check resting status (already handled in Step 1, but re-check for clarity)
                if pair_signals_data["is_resting"]:
                    print(f"  {pair}: Still resting until {pair_signals_data['rest_end_time'].strftime('%H:%M:%S')}. Skipping.")
                    continue # Skip this pair, it's resting

                # Check result display cooldown (already handled in Step 1, but re-check for clarity)
                if pair_signals_data["current_signal"]["result"] in ["✅ WIN", "❌ LOSS"] and \
                   current_time < pair_signals_data["result_display_end_time"]:
                    print(f"  {pair}: Result '{pair_signals_data['current_signal']['result']}' is still being displayed (backend). Waiting for display cooldown to end. Skipping.")
                    continue # Skip this pair, result is still being displayed

                # Check global active signals limit
                if active_waiting_signals_count >= MAX_ACTIVE_SIGNALS: 
                    print(f"  Maximum active signals ({MAX_ACTIVE_SIGNALS}) reached. Skipping new signal generation for {pair}.")
                    continue # Skip this pair, too many active signals globally

                # Check time since last signal attempt for this specific pair
                time_since_last_attempt = current_time - pair_signals_data["last_signal_generated_at"]
                if time_since_last_attempt.total_seconds() >= (SIGNAL_INTERVAL_MINUTES * 60): # Check every SIGNAL_INTERVAL_MINUTES
                    print(f"  {pair}: Eligible for new signal. Attempting to fetch candles...")
                    candles = fetch_twelvedata_candles(pair, outputsize=250) # Fetch enough candles for all indicators
                    
                    if candles:
                        new_signal = analyze_and_generate_signal(pair, candles)
                        if new_signal["direction"] != "NONE":
                            potential_signals_for_this_iteration.append({
                                "pair": pair,
                                "signal_data": new_signal,
                                "analysis_time": current_time, # Store when analysis was done for prioritization
                                "bull_conditions": new_signal.get("bull_conditions_met", 0),
                                "bear_conditions": new_signal.get("bear_conditions_met", 0),
                                "confidence_level": ["LOW", "MEDIUM", "HIGH", "VERY HIGH (100000% SURE)"].index(new_signal["confidence"]) # For sorting
                            })
                            print(f"  Potential signal generated for {pair}: {new_signal['direction']} with confidence {new_signal['confidence']}. Added to potential list.")
                        else:
                            with signals_lock: # Acquire lock before modifying signals[pair]
                                # Update signal data even if no signal, to show "No Signal" and reason
                                signals[pair]["current_signal"] = new_signal
                                signals[pair]["last_signal_generated_at"] = current_time
                                print(f"  No signal generated for {pair}: {new_signal['reason']}. Updated pair state to 'No Signal'.")
                    else:
                        with signals_lock: # Acquire lock before modifying signals[pair]
                            # Handle data fetch error for the pair
                            signals[pair]["current_signal"] = {
                                "pair": pair,
                                "signal_generated_at": datetime.datetime.now().strftime("%H:%M:%S"), 
                                "entry_time": "N/A",
                                "expiry_time": "N/A",
                                "direction": "NONE",
                                "confidence": "N/A",
                                "result": "Data Error",
                                "entry_price": None,
                                "expiry_timestamp": 0,
                                "reasons": ["Could not fetch market data."],
                                "reason": "Could not fetch market data." # Initialize singular reason
                            }
                            signals[pair]["last_signal_generated_at"] = current_time
                            signals[pair]["consecutive_losses"] = 0 # Reset on data error
                            signals[pair]["forced_wins_given"] = 0 # Reset on data error
                            print(f"  Failed to fetch candles for {pair} from TwelveData API. Setting 'Data Error' status.")
                else:
                    print(f"  {pair}: Not yet eligible for new signal (last attempt {time_since_last_attempt.total_seconds():.0f}s ago). Needs {SIGNAL_INTERVAL_MINUTES*60}s. Skipping.")

            # --- Step 3: Select and activate top signals from potential candidates ---
            # Sort by confidence (desc), then by number of conditions met (desc), then by analysis time (asc)
            potential_signals_for_this_iteration.sort(key=lambda x: (x["confidence_level"], max(x["bull_conditions"], x["bear_conditions"]), -x["analysis_time"].timestamp()), reverse=True)
            
            print(f"Number of potential signals found: {len(potential_signals_for_this_iteration)}")
            signals_to_activate = []
            for potential_signal_entry in potential_signals_for_this_iteration:
                if len(signals_to_activate) < MAX_ACTIVE_SIGNALS:
                    with signals_lock: # Acquire lock before reading signals for current_pair_status
                        current_pair_status = signals[potential_signal_entry["pair"]]["current_signal"]["result"]
                        is_resting = signals[potential_signal_entry["pair"]]["is_resting"]
                    
                    # Ensure it's not currently WAITING for a trade and not resting
                    if current_pair_status != "⏳ WAITING" and not is_resting: 
                        signals_to_activate.append(potential_signal_entry)
                        print(f"  Activating potential signal for {potential_signal_entry['pair']}.")
                    else:
                        print(f"  Skipping {potential_signal_entry['pair']} as it became ineligible during selection phase (status: {current_pair_status}, resting: {is_resting}).")
                else:
                    print(f"  Max active signals ({MAX_ACTIVE_SIGNALS}) reached during activation phase. Stopping.")
                    break # Max active signals reached

            for signal_entry in signals_to_activate:
                pair = signal_entry["pair"]
                new_signal = signal_entry["signal_data"]
                with signals_lock: # Acquire lock before modifying signals[pair]
                    signals[pair]["current_signal"] = new_signal
                    signals[pair]["last_signal_generated_at"] = datetime.datetime.now() 
                print(f"Activated signal for {pair}: {new_signal['direction']} with confidence {new_signal['confidence']}.")
            
            if not signals_to_activate and not potential_signals_for_this_iteration:
                print("No new high-confidence signals generated or activated in this iteration.")

            print("--- Signal Loop Iteration End ---")
            # Sleep for a short period to allow the loop to run continuously and check for updates
            time.sleep(3) # Changed to 3 seconds for faster analysis loop

        except Exception as e:
            print(f"🚨 An unexpected error occurred in signal_generation_loop: {e}")
            traceback.print_exc() # Print full traceback for debugging
            time.sleep(10) # Wait longer after an error before retrying

# --- Flask Routes ---

@app.route('/')
def home():
    """Serves the main HTML page."""
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    """Returns a simple success message to indicate backend is alive."""
    return jsonify({"status": "online", "message": "Backend is running and accessible."}), 200


@app.route('/api/signal', methods=['GET'])
def get_signals_api():
    """Returns the latest generated signals and their states to the frontend."""
    with signals_lock: # Acquire lock before returning the global signals dictionary
        return jsonify(signals)

# --- Main Execution ---

if __name__ == '__main__':
    # Start the background signal generation thread
    signal_thread = threading.Thread(target=signal_generation_loop)
    signal_thread.daemon = True # Allows the main thread to exit even if this thread is running
    signal_thread.start()

    # Give the thread a moment to initialize and fetch first signals
    # This sleep is crucial for initial signal population before frontend requests
    time.sleep(20) # Increased sleep to give more time for initial API calls and loop initialization

    # Run the Flask app
    print("Flask app starting...")
    # debug=True is good for development, but use_reloader=False is important for threading
    # as it prevents the Flask app from starting the thread twice.
    app.run(debug=True, host=FLASK_HOST, port=FLASK_PORT, use_reloader=False)

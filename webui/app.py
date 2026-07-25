import os
import pandas as pd
import numpy as np
import json
import plotly.graph_objects as go
import plotly.utils
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import sys
import warnings
import datetime
warnings.filterwarnings('ignore')

# Use the local proxy for Hugging Face/model downloads while keeping local APIs direct.
DOWNLOAD_PROXY = os.environ.get('MT5_DEMO_PROXY', 'http://127.0.0.1:7897')
os.environ.setdefault('HTTP_PROXY', DOWNLOAD_PROXY)
os.environ.setdefault('HTTPS_PROXY', DOWNLOAD_PROXY)
os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None
    MT5_AVAILABLE = False

# Add project root directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from model import Kronos, KronosTokenizer, KronosPredictor
    MODEL_AVAILABLE = True
except ImportError:
    MODEL_AVAILABLE = False
    print("Warning: Kronos model cannot be imported, will use simulated data for demonstration")

app = Flask(__name__)
CORS(app)

# Global variables to store models
tokenizer = None
model = None
predictor = None

MT5_TIMEFRAMES = {
    'M1': mt5.TIMEFRAME_M1 if MT5_AVAILABLE else None,
    'M5': mt5.TIMEFRAME_M5 if MT5_AVAILABLE else None,
    'M15': mt5.TIMEFRAME_M15 if MT5_AVAILABLE else None,
    'M30': mt5.TIMEFRAME_M30 if MT5_AVAILABLE else None,
    'H1': mt5.TIMEFRAME_H1 if MT5_AVAILABLE else None,
    'H4': mt5.TIMEFRAME_H4 if MT5_AVAILABLE else None,
    'D1': mt5.TIMEFRAME_D1 if MT5_AVAILABLE else None,
    'W1': mt5.TIMEFRAME_W1 if MT5_AVAILABLE else None,
}

MT5_TIMEFRAME_MINUTES = {
    'M1': 1, 'M5': 5, 'M15': 15, 'M30': 30,
    'H1': 60, 'H4': 240, 'D1': 1440, 'W1': 10080,
}


def ensure_mt5_connection():
    """Initialize the local MetaTrader 5 terminal connection."""
    if not MT5_AVAILABLE:
        return False, 'MetaTrader5 Python package is not installed'
    if mt5.terminal_info() is not None:
        return True, None
    if not mt5.initialize():
        code, message = mt5.last_error()
        return False, f'MT5 initialization failed ({code}): {message}'
    return True, None


def parse_utc_datetime(value, field_name):
    if not value:
        raise ValueError(f'{field_name} cannot be empty')
    parsed = pd.to_datetime(value, utc=True)
    return parsed.to_pydatetime()


def align_to_timeframe(value, timeframe):
    """Floor a UTC datetime to a valid boundary for the MT5 timeframe."""
    timestamp = pd.Timestamp(value).tz_convert('UTC')
    if timeframe == 'W1':
        timestamp = (timestamp - pd.Timedelta(days=timestamp.weekday())).normalize()
    elif timeframe == 'D1':
        timestamp = timestamp.normalize()
    else:
        timestamp = timestamp.floor(f'{MT5_TIMEFRAME_MINUTES[timeframe]}min')
    return timestamp.to_pydatetime()


def load_mt5_data(symbol, timeframe, date_from, date_to):
    """Fetch OHLCV bars from MT5 using a UTC date range."""
    connected, error = ensure_mt5_connection()
    if not connected:
        return None, error
    if not symbol:
        return None, 'Symbol cannot be empty'
    if timeframe not in MT5_TIMEFRAMES:
        return None, f'Unsupported timeframe: {timeframe}'

    try:
        start = align_to_timeframe(parse_utc_datetime(date_from, 'Start time'), timeframe)
        end = align_to_timeframe(parse_utc_datetime(date_to, 'End time'), timeframe)
    except (ValueError, TypeError) as exc:
        return None, str(exc)
    if start >= end:
        return None, 'Start time must be earlier than end time'

    if not mt5.symbol_select(symbol, True):
        code, message = mt5.last_error()
        return None, f'Unable to select symbol {symbol} ({code}): {message}'

    rates = mt5.copy_rates_range(symbol, MT5_TIMEFRAMES[timeframe], start, end)
    if rates is None:
        code, message = mt5.last_error()
        return None, f'Failed to fetch MT5 rates ({code}): {message}'
    if len(rates) == 0:
        return None, 'MT5 returned no bars for this symbol and time range'

    df = pd.DataFrame(rates)
    df['timestamps'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_localize(None)
    df['volume'] = pd.to_numeric(df['tick_volume'], errors='coerce').fillna(0)
    df = df[['timestamps', 'open', 'high', 'low', 'close', 'volume']]
    df = df.sort_values('timestamps').drop_duplicates('timestamps').dropna().reset_index(drop=True)
    return df, None


def load_request_data(data):
    if data.get('source', 'mt5') == 'mt5':
        return load_mt5_data(
            data.get('symbol'),
            data.get('timeframe'),
            data.get('date_from'),
            data.get('date_to'),
        )
    file_path = data.get('file_path')
    if not file_path:
        return None, 'File path cannot be empty'
    return load_data_file(file_path)

# Available model configurations
AVAILABLE_MODELS = {
    'kronos-mini': {
        'name': 'Kronos-mini',
        'model_id': 'NeoQuasar/Kronos-mini',
        'tokenizer_id': 'NeoQuasar/Kronos-Tokenizer-2k',
        'context_length': 2048,
        'params': '4.1M',
        'description': '轻量模型，适合快速预测'
    },
    'kronos-small': {
        'name': 'Kronos-small',
        'model_id': 'NeoQuasar/Kronos-small',
        'tokenizer_id': 'NeoQuasar/Kronos-Tokenizer-base',
        'context_length': 512,
        'params': '24.7M',
        'description': '小型模型，兼顾效果与速度'
    },
    'kronos-base': {
        'name': 'Kronos-base',
        'model_id': 'NeoQuasar/Kronos-base',
        'tokenizer_id': 'NeoQuasar/Kronos-Tokenizer-base',
        'context_length': 512,
        'params': '102.3M',
        'description': '基础模型，提供更高预测质量'
    }
}

def load_data_files():
    """Scan data directory and return available data files"""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    data_files = []
    
    if os.path.exists(data_dir):
        for file in os.listdir(data_dir):
            if file.endswith(('.csv', '.feather')):
                file_path = os.path.join(data_dir, file)
                file_size = os.path.getsize(file_path)
                data_files.append({
                    'name': file,
                    'path': file_path,
                    'size': f"{file_size / 1024:.1f} KB" if file_size < 1024*1024 else f"{file_size / (1024*1024):.1f} MB"
                })
    
    return data_files

def load_data_file(file_path):
    """Load data file"""
    try:
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        elif file_path.endswith('.feather'):
            df = pd.read_feather(file_path)
        else:
            return None, "Unsupported file format"
        
        # Check required columns
        required_cols = ['open', 'high', 'low', 'close']
        if not all(col in df.columns for col in required_cols):
            return None, f"Missing required columns: {required_cols}"
        
        # Process timestamp column
        if 'timestamps' in df.columns:
            df['timestamps'] = pd.to_datetime(df['timestamps'])
        elif 'timestamp' in df.columns:
            df['timestamps'] = pd.to_datetime(df['timestamp'])
        elif 'date' in df.columns:
            # If column name is 'date', rename it to 'timestamps'
            df['timestamps'] = pd.to_datetime(df['date'])
        else:
            # If no timestamp column exists, create one
            df['timestamps'] = pd.date_range(start='2024-01-01', periods=len(df), freq='1H')
        
        # Ensure numeric columns are numeric type
        for col in ['open', 'high', 'low', 'close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # Process volume column (optional)
        if 'volume' in df.columns:
            df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        
        # Process amount column (optional, but not used for prediction)
        if 'amount' in df.columns:
            df['amount'] = pd.to_numeric(df['amount'], errors='coerce')
        
        # Remove rows containing NaN values
        df = df.dropna()
        
        return df, None
        
    except Exception as e:
        return None, f"Failed to load file: {str(e)}"

def save_prediction_results(file_path, prediction_type, prediction_results, actual_data, input_data, prediction_params):
    """Save prediction results to file"""
    try:
        # Create prediction results directory
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prediction_results')
        os.makedirs(results_dir, exist_ok=True)
        
        # Generate filename
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'prediction_{timestamp}.json'
        filepath = os.path.join(results_dir, filename)
        
        # Prepare data for saving
        save_data = {
            'timestamp': datetime.datetime.now().isoformat(),
            'file_path': file_path,
            'prediction_type': prediction_type,
            'prediction_params': prediction_params,
            'input_data_summary': {
                'rows': len(input_data),
                'columns': list(input_data.columns),
                'price_range': {
                    'open': {'min': float(input_data['open'].min()), 'max': float(input_data['open'].max())},
                    'high': {'min': float(input_data['high'].min()), 'max': float(input_data['high'].max())},
                    'low': {'min': float(input_data['low'].min()), 'max': float(input_data['low'].max())},
                    'close': {'min': float(input_data['close'].min()), 'max': float(input_data['close'].max())}
                },
                'last_values': {
                    'open': float(input_data['open'].iloc[-1]),
                    'high': float(input_data['high'].iloc[-1]),
                    'low': float(input_data['low'].iloc[-1]),
                    'close': float(input_data['close'].iloc[-1])
                }
            },
            'prediction_results': prediction_results,
            'actual_data': actual_data,
            'analysis': {}
        }
        
        # If actual data exists, perform comparison analysis
        if actual_data and len(actual_data) > 0:
            # Calculate continuity analysis
            if len(prediction_results) > 0 and len(actual_data) > 0:
                last_pred = prediction_results[0]  # First prediction point
            first_actual = actual_data[0]      # First actual point
                
            save_data['analysis']['continuity'] = {
                    'last_prediction': {
                        'open': last_pred['open'],
                        'high': last_pred['high'],
                        'low': last_pred['low'],
                        'close': last_pred['close']
                    },
                    'first_actual': {
                        'open': first_actual['open'],
                        'high': first_actual['high'],
                        'low': first_actual['low'],
                        'close': first_actual['close']
                    },
                    'gaps': {
                        'open_gap': abs(last_pred['open'] - first_actual['open']),
                        'high_gap': abs(last_pred['high'] - first_actual['high']),
                        'low_gap': abs(last_pred['low'] - first_actual['low']),
                        'close_gap': abs(last_pred['close'] - first_actual['close'])
                    },
                    'gap_percentages': {
                        'open_gap_pct': (abs(last_pred['open'] - first_actual['open']) / first_actual['open']) * 100,
                        'high_gap_pct': (abs(last_pred['high'] - first_actual['high']) / first_actual['high']) * 100,
                        'low_gap_pct': (abs(last_pred['low'] - first_actual['low']) / first_actual['low']) * 100,
                        'close_gap_pct': (abs(last_pred['close'] - first_actual['close']) / first_actual['close']) * 100
                    }
                }
        
        # Save to file
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        
        print(f"Prediction results saved to: {filepath}")
        return filepath
        
    except Exception as e:
        print(f"Failed to save prediction results: {e}")
        return None

def create_prediction_chart(df, pred_df, lookback, pred_len, actual_df=None, historical_start_idx=0, prediction_timestamps=None):
    """Create prediction chart"""
    # Use specified historical data start position, not always from the beginning of df
    if historical_start_idx + lookback + pred_len <= len(df):
        # Display lookback historical points + pred_len prediction points starting from specified position
        historical_df = df.iloc[historical_start_idx:historical_start_idx+lookback]
        prediction_range = range(historical_start_idx+lookback, historical_start_idx+lookback+pred_len)
    else:
        # If data is insufficient, adjust to maximum available range
        available_lookback = min(lookback, len(df) - historical_start_idx)
        available_pred_len = min(pred_len, max(0, len(df) - historical_start_idx - available_lookback))
        historical_df = df.iloc[historical_start_idx:historical_start_idx+available_lookback]
        prediction_range = range(historical_start_idx+available_lookback, historical_start_idx+available_lookback+available_pred_len)
    
    # Create chart
    fig = go.Figure()
    
    # Add historical data (candlestick chart)
    fig.add_trace(go.Candlestick(
        x=historical_df['timestamps'] if 'timestamps' in historical_df.columns else historical_df.index,
        open=historical_df['open'],
        high=historical_df['high'],
        low=historical_df['low'],
        close=historical_df['close'],
        name='历史数据（400根）',
        increasing_fillcolor='#FFFFFF',
        increasing_line_color='#111111',
        decreasing_fillcolor='#111111',
        decreasing_line_color='#111111'
    ))
    
    # Add prediction data (candlestick chart)
    if pred_df is not None and len(pred_df) > 0:
        # Calculate prediction data timestamps - ensure continuity with historical data
        if 'timestamps' in df.columns and len(historical_df) > 0:
            # Start from the last timestamp of historical data, create prediction timestamps with the same time interval
            last_timestamp = historical_df['timestamps'].iloc[-1]
            time_diff = df['timestamps'].iloc[1] - df['timestamps'].iloc[0] if len(df) > 1 else pd.Timedelta(hours=1)
            
            pred_timestamps = prediction_timestamps if prediction_timestamps is not None else pd.date_range(
                start=last_timestamp + time_diff,
                periods=len(pred_df),
                freq=time_diff
            )
        else:
            # If no timestamps, use index
            pred_timestamps = range(len(historical_df), len(historical_df) + len(pred_df))
        
        fig.add_trace(go.Candlestick(
            x=pred_timestamps,
            open=pred_df['open'],
            high=pred_df['high'],
            low=pred_df['low'],
            close=pred_df['close'],
            name='预测数据（120根）',
            increasing_fillcolor='#E53935',
            increasing_line_color='#E53935',
            decreasing_fillcolor='#1B9E4B',
            decreasing_line_color='#1B9E4B'
        ))
    
    # Add actual data for comparison (if exists)
    if actual_df is not None and len(actual_df) > 0:
        # Actual data should be in the same time period as prediction data
        if 'timestamps' in df.columns:
            # Actual data should use the same timestamps as prediction data to ensure time alignment
            if 'pred_timestamps' in locals():
                actual_timestamps = pred_timestamps
            else:
                # If no prediction timestamps, calculate from the last timestamp of historical data
                if len(historical_df) > 0:
                    last_timestamp = historical_df['timestamps'].iloc[-1]
                    time_diff = df['timestamps'].iloc[1] - df['timestamps'].iloc[0] if len(df) > 1 else pd.Timedelta(hours=1)
                    actual_timestamps = pd.date_range(
                        start=last_timestamp + time_diff,
                        periods=len(actual_df),
                        freq=time_diff
                    )
                else:
                    actual_timestamps = range(len(historical_df), len(historical_df) + len(actual_df))
        else:
            actual_timestamps = range(len(historical_df), len(historical_df) + len(actual_df))
        
        fig.add_trace(go.Candlestick(
            x=actual_timestamps,
            open=actual_df['open'],
            high=actual_df['high'],
            low=actual_df['low'],
            close=actual_df['close'],
            name='实际数据（120根）',
            increasing_fillcolor='#FFFFFF',
            increasing_line_color='#111111',
            decreasing_fillcolor='#111111',
            decreasing_line_color='#111111'
        ))
    
    chart_title = (
        f'Kronos 未来K线预测：最新 {lookback} 根历史 + 未来 {pred_len} 根预测'
        if actual_df is None else
        'Kronos K线预测结果：400根历史 + 120根预测与实际对比'
    )

    # Update layout
    fig.update_layout(
        title=chart_title,
        xaxis_title='时间',
        yaxis_title='价格',
        template='plotly_white',
        height=650,
        showlegend=True,
        margin=dict(l=65, r=25, t=105, b=60),
        legend=dict(
            orientation='h',
            yanchor='bottom',
            y=1.02,
            xanchor='right',
            x=1
        )
    )
    
    # Ensure x-axis time continuity
    if 'timestamps' in historical_df.columns:
        # Get all timestamps and sort them
        all_timestamps = []
        if len(historical_df) > 0:
            all_timestamps.extend(historical_df['timestamps'])
        if 'pred_timestamps' in locals():
            all_timestamps.extend(pred_timestamps)
        if 'actual_timestamps' in locals():
            all_timestamps.extend(actual_timestamps)
        
        if all_timestamps:
            all_timestamps = sorted(all_timestamps)
            fig.update_xaxes(
                range=[all_timestamps[0], all_timestamps[-1]],
                rangeslider_visible=False,
                type='date'
            )
    
    return json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)


def generate_future_timestamps(last_timestamp, timeframe, count, symbol):
    """Generate future MT5 bar opens, skipping weekends for non-crypto symbols."""
    minutes = MT5_TIMEFRAME_MINUTES.get(timeframe)
    if not minutes:
        raise ValueError(f'不支持的时间周期：{timeframe}')
    delta = pd.Timedelta(minutes=minutes)
    current = pd.Timestamp(last_timestamp)
    timestamps = []
    is_crypto = any(token in (symbol or '').upper() for token in ('BTC', 'ETH', 'CRYPTO'))
    while len(timestamps) < count:
        current += delta
        if not is_crypto and current.weekday() >= 5:
            continue
        timestamps.append(current)
    return pd.DatetimeIndex(timestamps)


def predict_future(data, df, lookback, pred_len, temperature, top_p, sample_count):
    if not MODEL_AVAILABLE or predictor is None:
        return jsonify({'error': 'Kronos 模型尚未加载，请先加载模型'}), 400
    if len(df) < lookback:
        return jsonify({
            'error': f'行情数量不足：未来预测需要最新 {lookback} 根K线，当前只有 {len(df)} 根。',
            'rows': len(df),
            'required_rows': lookback,
        }), 400

    required_cols = ['open', 'high', 'low', 'close']
    if 'volume' in df.columns:
        required_cols.append('volume')
    historical_start_idx = len(df) - lookback
    x_df = df.iloc[historical_start_idx:][required_cols].reset_index(drop=True)
    x_timestamp = df.iloc[historical_start_idx:]['timestamps'].reset_index(drop=True)
    future_timestamps = generate_future_timestamps(
        x_timestamp.iloc[-1], data.get('timeframe'), pred_len, data.get('symbol')
    )
    y_timestamp = pd.Series(future_timestamps, name='timestamps')

    try:
        pred_df = predictor.predict(
            df=x_df,
            x_timestamp=x_timestamp,
            y_timestamp=y_timestamp,
            pred_len=pred_len,
            T=temperature,
            top_p=top_p,
            sample_count=sample_count
        )
    except Exception as exc:
        return jsonify({'error': f'Kronos 未来预测失败：{str(exc)}'}), 500

    prediction_results = []
    for i, (_, row) in enumerate(pred_df.iterrows()):
        prediction_results.append({
            'timestamp': future_timestamps[i].isoformat(),
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row['volume']) if 'volume' in row else 0,
            'amount': float(row['amount']) if 'amount' in row else 0
        })

    prediction_type = f'{data.get("symbol")} {data.get("timeframe")} 未来 {pred_len} 根K线预测'
    chart_json = create_prediction_chart(
        df, pred_df, lookback, pred_len,
        actual_df=None,
        historical_start_idx=historical_start_idx,
        prediction_timestamps=future_timestamps
    )
    save_prediction_results(
        file_path=f'MT5:{data.get("symbol")}:{data.get("timeframe")}',
        prediction_type=prediction_type,
        prediction_results=prediction_results,
        actual_data=[],
        input_data=x_df,
        prediction_params={
            'mode': 'future', 'lookback': lookback, 'pred_len': pred_len,
            'temperature': temperature, 'top_p': top_p, 'sample_count': sample_count,
            'symbol': data.get('symbol'), 'timeframe': data.get('timeframe')
        }
    )
    return jsonify({
        'success': True,
        'prediction_type': prediction_type,
        'chart': chart_json,
        'prediction_results': prediction_results,
        'actual_data': [],
        'has_comparison': False,
        'message': f'未来预测完成：已基于最新 {lookback} 根K线生成未来 {pred_len} 根预测K线'
    })

@app.route('/')
def index():
    """Home page"""
    return render_template('index.html')

@app.route('/api/data-files')
def get_data_files():
    """Get available data file list"""
    data_files = load_data_files()
    return jsonify(data_files)


@app.route('/api/mt5/status')
def mt5_status():
    connected, error = ensure_mt5_connection()
    if not connected:
        return jsonify({'available': MT5_AVAILABLE, 'connected': False, 'error': error}), 503
    terminal = mt5.terminal_info()
    account = mt5.account_info()
    return jsonify({
        'available': True,
        'connected': True,
        'terminal': terminal.name if terminal else None,
        'company': terminal.company if terminal else None,
        'account': account.login if account else None,
        'server': account.server if account else None,
        'timeframes': list(MT5_TIMEFRAMES.keys()),
    })


@app.route('/api/mt5/symbols')
def mt5_symbols():
    connected, error = ensure_mt5_connection()
    if not connected:
        return jsonify({'error': error}), 503
    symbols = mt5.symbols_get()
    if symbols is None:
        code, message = mt5.last_error()
        return jsonify({'error': f'Failed to read MT5 symbols ({code}): {message}'}), 500
    visible_only = request.args.get('visible', 'true').lower() != 'false'
    result = [item.name for item in symbols if not visible_only or item.visible]
    return jsonify({'symbols': sorted(result), 'count': len(result)})


@app.route('/api/mt5/symbol-info')
def mt5_symbol_info():
    connected, error = ensure_mt5_connection()
    if not connected:
        return jsonify({'error': error}), 503
    symbol = request.args.get('symbol', '').strip()
    if not symbol:
        return jsonify({'error': 'Symbol cannot be empty'}), 400
    info = mt5.symbol_info(symbol)
    if info is None:
        code, message = mt5.last_error()
        return jsonify({'error': f'Failed to read symbol information ({code}): {message}'}), 404
    tick = mt5.symbol_info_tick(symbol)
    trade_modes = {
        mt5.SYMBOL_TRADE_MODE_DISABLED: 'Disabled',
        mt5.SYMBOL_TRADE_MODE_LONGONLY: 'Long only',
        mt5.SYMBOL_TRADE_MODE_SHORTONLY: 'Short only',
        mt5.SYMBOL_TRADE_MODE_CLOSEONLY: 'Close only',
        mt5.SYMBOL_TRADE_MODE_FULL: 'Full access',
    }
    return jsonify({
        'symbol': symbol,
        'description': info.description,
        'currency_base': info.currency_base,
        'currency_profit': info.currency_profit,
        'digits': info.digits,
        'point': info.point,
        'bid': tick.bid if tick else info.bid,
        'ask': tick.ask if tick else info.ask,
        'spread': info.spread,
        'spread_float': info.spread_float,
        'contract_size': info.trade_contract_size,
        'volume_min': info.volume_min,
        'volume_max': info.volume_max,
        'volume_step': info.volume_step,
        'stops_level': info.trade_stops_level,
        'freeze_level': info.trade_freeze_level,
        'trade_mode': trade_modes.get(info.trade_mode, str(info.trade_mode)),
    })

@app.route('/api/load-data', methods=['POST'])
def load_data():
    """Load market data from MT5 (or a legacy local file)."""
    try:
        data = request.get_json()
        df, error = load_request_data(data)
        if error:
            return jsonify({'error': error}), 400
        
        # Detect data time frequency
        def detect_timeframe(df):
            if len(df) < 2:
                return "Unknown"
            
            time_diffs = []
            for i in range(1, min(10, len(df))):  # Check first 10 time differences
                diff = df['timestamps'].iloc[i] - df['timestamps'].iloc[i-1]
                time_diffs.append(diff)
            
            if not time_diffs:
                return "Unknown"
            
            # Calculate average time difference
            avg_diff = sum(time_diffs, pd.Timedelta(0)) / len(time_diffs)
            
            # Convert to readable format
            if avg_diff < pd.Timedelta(minutes=1):
                return f"{avg_diff.total_seconds():.0f} seconds"
            elif avg_diff < pd.Timedelta(hours=1):
                return f"{avg_diff.total_seconds() / 60:.0f} minutes"
            elif avg_diff < pd.Timedelta(days=1):
                return f"{avg_diff.total_seconds() / 3600:.0f} hours"
            else:
                return f"{avg_diff.days} days"
        
        # Return data information
        data_info = {
            'rows': len(df),
            'columns': list(df.columns),
            'start_date': df['timestamps'].min().isoformat() if 'timestamps' in df.columns else 'N/A',
            'end_date': df['timestamps'].max().isoformat() if 'timestamps' in df.columns else 'N/A',
            'price_range': {
                'min': float(df[['open', 'high', 'low', 'close']].min().min()),
                'max': float(df[['open', 'high', 'low', 'close']].max().max())
            },
            'prediction_columns': ['open', 'high', 'low', 'close'] + (['volume'] if 'volume' in df.columns else []),
            'timeframe': data.get('timeframe') or detect_timeframe(df),
            'symbol': data.get('symbol'),
            'source': data.get('source', 'mt5'),
            'required_rows': 400,
            'sufficient_for_prediction': len(df) >= 400
        }
        
        return jsonify({
            'success': True,
            'data_info': data_info,
            'message': (
                f'已从 {data.get("symbol", "数据源")} 加载 {len(df)} 根K线'
                if len(df) >= 400 else
                f'仅加载到 {len(df)} 根K线；未来预测至少需要 400 根，请扩大时间范围。'
            )
        })
        
    except Exception as e:
        return jsonify({'error': f'加载行情失败：{str(e)}'}), 500

@app.route('/api/predict', methods=['POST'])
def predict():
    """Perform prediction"""
    try:
        data = request.get_json()
        file_path = data.get('file_path')
        lookback = int(data.get('lookback', 400))
        pred_len = int(data.get('pred_len', 120))
        
        # Get prediction quality parameters
        temperature = float(data.get('temperature', 1.0))
        top_p = float(data.get('top_p', 0.9))
        sample_count = int(data.get('sample_count', 1))
        
        # Load the same MT5 range that was previewed in the UI.
        df, error = load_request_data(data)
        if error:
            return jsonify({'error': error}), 400

        if data.get('prediction_mode', 'future') == 'future':
            return predict_future(data, df, lookback, pred_len, temperature, top_p, sample_count)
        
        required_rows = lookback + pred_len
        if len(df) < required_rows:
            return jsonify({
                'error': (
                    f'行情数量不足：MT5 返回 {len(df)} 根K线，但预测需要 '
                    f'{lookback} 根历史 + {pred_len} 根对比 = {required_rows} 根。'
                    '请扩大所选时间范围。'
                ),
                'rows': len(df),
                'required_rows': required_rows,
            }), 400

        # Select the backtest window by bar index. Calendar-time percentages are
        # unreliable for MT5 data because weekends and trading pauses create gaps.
        requested_start_index = data.get('start_index')
        if requested_start_index is not None:
            try:
                start_index = int(requested_start_index)
            except (TypeError, ValueError):
                return jsonify({'error': 'K线窗口起始索引必须是整数'}), 400
            max_start_index = len(df) - required_rows
            if start_index < 0 or start_index > max_start_index:
                return jsonify({
                    'error': f'K线窗口起始索引无效，允许范围为 0 至 {max_start_index}',
                    'max_start_index': max_start_index,
                }), 400
            data['start_date'] = df.iloc[start_index]['timestamps'].isoformat()
        
        # Perform prediction
        if MODEL_AVAILABLE and predictor is not None:
            try:
                # Use real Kronos model
                # Only use necessary columns: OHLCV, excluding amount
                required_cols = ['open', 'high', 'low', 'close']
                if 'volume' in df.columns:
                    required_cols.append('volume')
                
                # Process time period selection
                start_date = data.get('start_date')
                
                if start_date:
                    # Custom time period - fix logic: use data within selected window
                    start_dt = pd.to_datetime(start_date)
                    
                    # Find data after start time
                    mask = df['timestamps'] >= start_dt
                    time_range_df = df[mask]
                    
                    # Ensure sufficient data: lookback + pred_len
                    if len(time_range_df) < lookback + pred_len:
                        return jsonify({'error': f'从 {start_dt.strftime("%Y-%m-%d %H:%M")} 开始的行情不足，需要 {lookback + pred_len} 根，当前只有 {len(time_range_df)} 根'}), 400
                    
                    # Use first lookback data points within selected window for prediction
                    x_df = time_range_df.iloc[:lookback][required_cols]
                    x_timestamp = time_range_df.iloc[:lookback]['timestamps']
                    
                    # Use last pred_len data points within selected window as actual values
                    y_timestamp = time_range_df.iloc[lookback:lookback+pred_len]['timestamps']
                    
                    # Calculate actual time period length
                    start_timestamp = time_range_df['timestamps'].iloc[0]
                    end_timestamp = time_range_df['timestamps'].iloc[lookback+pred_len-1]
                    time_span = end_timestamp - start_timestamp
                    
                    prediction_type = f"Kronos 模型预测（前 {lookback} 根作为历史输入，后 {pred_len} 根用于实际对比，跨度：{time_span}）"
                else:
                    # Use latest data
                    x_df = df.iloc[:lookback][required_cols]
                    x_timestamp = df.iloc[:lookback]['timestamps']
                    y_timestamp = df.iloc[lookback:lookback+pred_len]['timestamps']
                    prediction_type = "Kronos 模型预测（最新行情）"
                
                # Ensure timestamps are Series format, not DatetimeIndex, to avoid .dt attribute error in Kronos model
                if isinstance(x_timestamp, pd.DatetimeIndex):
                    x_timestamp = pd.Series(x_timestamp, name='timestamps')
                if isinstance(y_timestamp, pd.DatetimeIndex):
                    y_timestamp = pd.Series(y_timestamp, name='timestamps')
                
                pred_df = predictor.predict(
                    df=x_df,
                    x_timestamp=x_timestamp,
                    y_timestamp=y_timestamp,
                    pred_len=pred_len,
                    T=temperature,
                    top_p=top_p,
                    sample_count=sample_count
                )
                
            except Exception as e:
                return jsonify({'error': f'Kronos 模型预测失败：{str(e)}'}), 500
        else:
            return jsonify({'error': 'Kronos 模型尚未加载，请先加载模型'}), 400
        
        # Prepare actual data for comparison (if exists)
        actual_data = []
        actual_df = None
        
        if start_date:  # Custom time period
            # Fix logic: use data within selected window
            # Prediction uses first 400 data points within selected window
            # Actual data should be last 120 data points within selected window
            start_dt = pd.to_datetime(start_date)
            
            # Find data starting from start_date
            mask = df['timestamps'] >= start_dt
            time_range_df = df[mask]
            
            if len(time_range_df) >= lookback + pred_len:
                # Get last 120 data points within selected window as actual values
                actual_df = time_range_df.iloc[lookback:lookback+pred_len]
                
                for i, (_, row) in enumerate(actual_df.iterrows()):
                    actual_data.append({
                        'timestamp': row['timestamps'].isoformat(),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'volume': float(row['volume']) if 'volume' in row else 0,
                        'amount': float(row['amount']) if 'amount' in row else 0
                    })
        else:  # Latest data
            # Prediction uses first 400 data points
            # Actual data should be 120 data points after first 400 data points
            if len(df) >= lookback + pred_len:
                actual_df = df.iloc[lookback:lookback+pred_len]
                for i, (_, row) in enumerate(actual_df.iterrows()):
                    actual_data.append({
                        'timestamp': row['timestamps'].isoformat(),
                        'open': float(row['open']),
                        'high': float(row['high']),
                        'low': float(row['low']),
                        'close': float(row['close']),
                        'volume': float(row['volume']) if 'volume' in row else 0,
                        'amount': float(row['amount']) if 'amount' in row else 0
                    })
        
        # Create chart - pass historical data start position
        if start_date:
            # Custom time period: find starting position of historical data in original df
            start_dt = pd.to_datetime(start_date)
            mask = df['timestamps'] >= start_dt
            historical_start_idx = df[mask].index[0] if len(df[mask]) > 0 else 0
        else:
            # Latest data: start from beginning
            historical_start_idx = 0
        
        chart_json = create_prediction_chart(df, pred_df, lookback, pred_len, actual_df, historical_start_idx)
        
        # Prepare prediction result data - fix timestamp calculation logic
        if 'timestamps' in df.columns:
            if start_date:
                # Custom time period: use selected window data to calculate timestamps
                start_dt = pd.to_datetime(start_date)
                mask = df['timestamps'] >= start_dt
                time_range_df = df[mask]
                
                if len(time_range_df) >= lookback:
                    # Calculate prediction timestamps starting from last time point of selected window
                    last_timestamp = time_range_df['timestamps'].iloc[lookback-1]
                    time_diff = df['timestamps'].iloc[1] - df['timestamps'].iloc[0]
                    future_timestamps = pd.date_range(
                        start=last_timestamp + time_diff,
                        periods=pred_len,
                        freq=time_diff
                    )
                else:
                    future_timestamps = []
            else:
                # Latest data: calculate from last time point of entire data file
                last_timestamp = df['timestamps'].iloc[-1]
                time_diff = df['timestamps'].iloc[1] - df['timestamps'].iloc[0]
                future_timestamps = pd.date_range(
                    start=last_timestamp + time_diff,
                    periods=pred_len,
                    freq=time_diff
                )
        else:
            future_timestamps = range(len(df), len(df) + pred_len)
        
        prediction_results = []
        for i, (_, row) in enumerate(pred_df.iterrows()):
            prediction_results.append({
                'timestamp': future_timestamps[i].isoformat() if i < len(future_timestamps) else f"T{i}",
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': float(row['volume']) if 'volume' in row else 0,
                'amount': float(row['amount']) if 'amount' in row else 0
            })
        
        # Save prediction results to file
        try:
            save_prediction_results(
                file_path=file_path or f'MT5:{data.get("symbol")}:{data.get("timeframe")}',
                prediction_type=prediction_type,
                prediction_results=prediction_results,
                actual_data=actual_data,
                input_data=x_df,
                prediction_params={
                    'lookback': lookback,
                    'pred_len': pred_len,
                    'temperature': temperature,
                    'top_p': top_p,
                    'sample_count': sample_count,
                    'start_date': start_date if start_date else 'latest',
                    'start_index': requested_start_index,
                    'source': data.get('source', 'mt5'),
                    'symbol': data.get('symbol'),
                    'timeframe': data.get('timeframe'),
                    'date_from': data.get('date_from'),
                    'date_to': data.get('date_to')
                }
            )
        except Exception as e:
            print(f"Failed to save prediction results: {e}")
        
        return jsonify({
            'success': True,
            'prediction_type': prediction_type,
            'chart': chart_json,
            'prediction_results': prediction_results,
            'actual_data': actual_data,
            'has_comparison': len(actual_data) > 0,
            'message': f'预测完成，已生成 {pred_len} 根预测K线' + (f'，并包含 {len(actual_data)} 根实际K线用于对比' if len(actual_data) > 0 else '')
        })
        
    except Exception as e:
        return jsonify({'error': f'预测失败：{str(e)}'}), 500

@app.route('/api/load-model', methods=['POST'])
def load_model():
    """Load Kronos model"""
    global tokenizer, model, predictor
    
    try:
        if not MODEL_AVAILABLE:
            return jsonify({'error': 'Kronos 模型库不可用'}), 400
        
        data = request.get_json()
        model_key = data.get('model_key', 'kronos-small')
        device = data.get('device', 'cpu')
        
        if model_key not in AVAILABLE_MODELS:
            return jsonify({'error': f'不支持的模型：{model_key}'}), 400
        
        model_config = AVAILABLE_MODELS[model_key]
        
        # Load tokenizer and model
        tokenizer = KronosTokenizer.from_pretrained(model_config['tokenizer_id'])
        model = Kronos.from_pretrained(model_config['model_id'])
        
        # Create predictor
        predictor = KronosPredictor(model, tokenizer, device=device, max_context=model_config['context_length'])
        
        return jsonify({
            'success': True,
            'message': f'模型加载成功：{model_config["name"]}（{model_config["params"]}），设备 {device}',
            'model_info': {
                'name': model_config['name'],
                'params': model_config['params'],
                'context_length': model_config['context_length'],
                'description': model_config['description']
            }
        })
        
    except Exception as e:
        return jsonify({'error': f'模型加载失败：{str(e)}'}), 500

@app.route('/api/available-models')
def get_available_models():
    """Get available model list"""
    return jsonify({
        'models': AVAILABLE_MODELS,
        'model_available': MODEL_AVAILABLE
    })

@app.route('/api/model-status')
def get_model_status():
    """Get model status"""
    if MODEL_AVAILABLE:
        if predictor is not None:
            return jsonify({
                'available': True,
                'loaded': True,
                'message': 'Kronos model loaded and available',
                'current_model': {
                    'name': predictor.model.__class__.__name__,
                    'device': str(next(predictor.model.parameters()).device)
                }
            })
        else:
            return jsonify({
                'available': True,
                'loaded': False,
                'message': 'Kronos model available but not loaded'
            })
    else:
        return jsonify({
            'available': False,
            'loaded': False,
            'message': 'Kronos model library not available, please install related dependencies'
        })

if __name__ == '__main__':
    print("Starting Kronos Web UI...")
    print(f"Model availability: {MODEL_AVAILABLE}")
    if MODEL_AVAILABLE:
        print("Tip: You can load Kronos model through /api/load-model endpoint")
    else:
        print("Tip: Will use simulated data for demonstration")
    
    app.run(debug=True, host='0.0.0.0', port=7070)

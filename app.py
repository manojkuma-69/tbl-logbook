from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import json
import base64
import pandas as pd
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'change-this-secret-key-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trading_journal_v3.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db = SQLAlchemy(app)

# LOT VALUES for P&L calculation
LOT_VALUES = {
    'Nifty': 50,
    'Bank Nifty': 15,
    'Sensex': 20
}

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    # Trade Identification
    date = db.Column(db.Date, nullable=False)
    entry_time = db.Column(db.String(10), nullable=False)
    index = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(5), nullable=False)
    strike = db.Column(db.Integer, nullable=False)
    
    # Execution Data
    entry_premium = db.Column(db.Float, nullable=False)
    exit_premium = db.Column(db.Float, nullable=False)
    lot_size = db.Column(db.Integer, default=1)
    initial_sl_premium = db.Column(db.Float, nullable=False)
    exit_time = db.Column(db.String(10), nullable=False)
    
    # Auto-calculated
    initial_risk_points = db.Column(db.Float, nullable=False)
    points_captured = db.Column(db.Float, nullable=False)
    pnl_rupees = db.Column(db.Float, nullable=False)
    rr_achieved = db.Column(db.Float, nullable=False)
    result = db.Column(db.String(10), nullable=False)
    
    # Trade Management
    hit_1to1 = db.Column(db.Boolean, default=False)
    sl_moved_to_entry = db.Column(db.Boolean, default=False)
    hit_1to2 = db.Column(db.Boolean, default=False)
    sl_moved_to_1r = db.Column(db.Boolean, default=False)
    hit_1to3 = db.Column(db.Boolean, default=False)
    booked_at_1to3 = db.Column(db.Boolean, default=False)
    
    # Exit Details
    exit_reason = db.Column(db.String(100))
    
    # Re-entry tracking
    is_reentry = db.Column(db.Boolean, default=False)
    linked_trade_id = db.Column(db.Integer, nullable=True)
    
    # Post-trade Review
    followed_all_rules = db.Column(db.Boolean, default=True)
    emotion_before = db.Column(db.String(50))
    emotion_during = db.Column(db.String(50))
    emotion_after = db.Column(db.String(50))
    mistakes = db.Column(db.String(200))
    lesson_learned = db.Column(db.Text)
    
    # Chart
    chart_image = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class BacktestData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_trades = db.Column(db.Integer)
    win_rate = db.Column(db.Float)
    avg_rr = db.Column(db.Float)
    total_points = db.Column(db.Float)
    expectancy = db.Column(db.Float)
    reentry_count = db.Column(db.Integer)
    reentry_wins = db.Column(db.Integer)
    reentry_win_rate = db.Column(db.Float)

class WeeklyReview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    week_start = db.Column(db.Date, nullable=False)
    best_trade_reason = db.Column(db.Text)
    worst_trade_reason = db.Column(db.Text)
    main_mistake = db.Column(db.Text)
    next_week_focus = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    max_trades_per_day = db.Column(db.Integer, default=2)
    custom_strategies = db.Column(db.Text)

# NEW V4.0 MODELS
class MissedTrade(db.Model):
    """Tracks trades you saw but correctly avoided (good discipline)"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time_noticed = db.Column(db.String(10))
    reason_avoided = db.Column(db.Text, nullable=False)
    setup_type = db.Column(db.String(50))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class RevengeTrade(db.Model):
    """Simplified logging for revenge trades (bad discipline)"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(10))
    amount_pnl = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer)
    index = db.Column(db.String(20))
    notes = db.Column(db.Text)
    discipline_score = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DisciplineStreak(db.Model):
    """Tracks consecutive days of good discipline"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    current_streak = db.Column(db.Integer, default=0)
    longest_streak = db.Column(db.Integer, default=0)
    last_update = db.Column(db.Date)
    streak_broken_count = db.Column(db.Integer, default=0)
    last_broken_date = db.Column(db.Date)

# Helper Functions
def calculate_pnl(entry_premium, exit_premium, direction, lot_size, index):
    if direction == 'CE':
        points = (exit_premium - entry_premium)
    else:
        points = (exit_premium - entry_premium)
    lot_value = LOT_VALUES.get(index, 50)
    pnl_rupees = points * lot_size * lot_value
    return points, pnl_rupees

def calculate_rr(entry, exit, sl, direction):
    if direction == 'CE':
        risk = abs(entry - sl)
        reward = abs(exit - entry)
    else:
        risk = abs(entry - sl)
        reward = abs(exit - entry)
    if risk == 0:
        return 0
    return round(reward / risk, 2)

def update_discipline_streak(user_id, good_discipline=True):
    """Update or create discipline streak"""
    streak = DisciplineStreak.query.filter_by(user_id=user_id).first()
    today = datetime.now().date()
    
    if not streak:
        streak = DisciplineStreak(
            user_id=user_id,
            current_streak=1 if good_discipline else 0,
            longest_streak=1 if good_discipline else 0,
            last_update=today,
            streak_broken_count=0 if good_discipline else 1,
            last_broken_date=today if not good_discipline else None
        )
        db.session.add(streak)
        return
    
    if streak.last_update != today:
        if good_discipline:
            streak.current_streak += 1
            if streak.current_streak > streak.longest_streak:
                streak.longest_streak = streak.current_streak
        else:
            streak.streak_broken_count += 1
            streak.current_streak = 0
            streak.last_broken_date = today
        streak.last_update = today

def get_best_trades(user_id):
    """Get best trades by different metrics"""
    trades = Trade.query.filter_by(user_id=user_id).all()
    if not trades:
        return None
    return {
        'rupees': max(trades, key=lambda t: t.pnl_rupees),
        'rr': max(trades, key=lambda t: t.rr_achieved),
        'points': max(trades, key=lambda t: t.points_captured)
    }

def get_emotion_patterns(user_id):
    """Analyze which emotions lead to better results"""
    trades = Trade.query.filter_by(user_id=user_id).all()
    if len(trades) < 10:
        return None
    
    emotion_stats = {}
    for trade in trades:
        emotion = trade.emotion_before
        if emotion not in emotion_stats:
            emotion_stats[emotion] = {'wins': 0, 'total': 0, 'total_pnl': 0}
        emotion_stats[emotion]['total'] += 1
        emotion_stats[emotion]['total_pnl'] += trade.pnl_rupees
        if trade.result == 'Win':
            emotion_stats[emotion]['wins'] += 1
    
    for emotion in emotion_stats:
        stats = emotion_stats[emotion]
        stats['win_rate'] = round((stats['wins'] / stats['total']) * 100, 1)
        stats['avg_pnl'] = round(stats['total_pnl'] / stats['total'], 2)
    
    return emotion_stats

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'error')
        return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    
    # Today's stats
    today_trades = Trade.query.filter_by(user_id=user_id, date=today).all()
    today_points = sum(t.points_captured for t in today_trades)
    today_pnl = sum(t.pnl_rupees for t in today_trades)
    
    # Week stats
    week_trades = Trade.query.filter(Trade.user_id==user_id, Trade.date>=week_start).all()
    week_points = sum(t.points_captured for t in week_trades)
    week_pnl = sum(t.pnl_rupees for t in week_trades)
    
    # Month stats
    month_trades = Trade.query.filter(Trade.user_id==user_id, Trade.date>=month_start).all()
    month_points = sum(t.points_captured for t in month_trades)
    month_pnl = sum(t.pnl_rupees for t in month_trades)
    
    # Total live trades
    total_trades = Trade.query.filter_by(user_id=user_id).count()
    
    # Backtest data
    backtest = BacktestData.query.filter_by(user_id=user_id).first()
    
    # NEW V4.0 Features
    best_trades = get_best_trades(user_id)
    discipline_streak = DisciplineStreak.query.filter_by(user_id=user_id).first()
    emotion_patterns = get_emotion_patterns(user_id)
    missed_count = MissedTrade.query.filter_by(user_id=user_id).count()
    revenge_count = RevengeTrade.query.filter_by(user_id=user_id).count()
    
    stats = {
        'today_trades': len(today_trades),
        'today_points': round(today_points, 1),
        'today_pnl': round(today_pnl, 2),
        'week_points': round(week_points, 1),
        'week_pnl': round(week_pnl, 2),
        'month_points': round(month_points, 1),
        'month_pnl': round(month_pnl, 2),
        'total_trades': total_trades,
        'target_trades': 30
    }
    
    return render_template('dashboard.html', 
                         stats=stats, 
                         backtest=backtest,
                         best_trades=best_trades,
                         discipline_streak=discipline_streak,
                         emotion_patterns=emotion_patterns,
                         missed_count=missed_count,
                         revenge_count=revenge_count)

@app.route('/add_trade', methods=['GET', 'POST'])
def add_trade():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    
    if request.method == 'POST':
        # Get form data
        date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        entry_time = request.form.get('entry_time')
        index = request.form.get('index')
        direction = request.form.get('direction')
        strike = int(request.form.get('strike'))
        entry_premium = float(request.form.get('entry_premium'))
        exit_premium = float(request.form.get('exit_premium'))
        lot_size = 1
        initial_sl_premium = float(request.form.get('initial_sl_premium'))
        exit_time = request.form.get('exit_time')
        
        # Calculate metrics
        initial_risk_points = abs(entry_premium - initial_sl_premium)
        points_captured, pnl_rupees = calculate_pnl(entry_premium, exit_premium, direction, lot_size, index)
        rr_achieved = calculate_rr(entry_premium, exit_premium, initial_sl_premium, direction)
        
        # Determine result
        if points_captured > 0:
            result = 'Win'
        elif points_captured < 0:
            result = 'Loss'
        else:
            result = 'BE'
        
        # Trade management flags
        hit_1to1 = request.form.get('hit_1to1') == 'on'
        sl_moved_to_entry = request.form.get('sl_moved_to_entry') == 'on'
        hit_1to2 = request.form.get('hit_1to2') == 'on'
        sl_moved_to_1r = request.form.get('sl_moved_to_1r') == 'on'
        hit_1to3 = request.form.get('hit_1to3') == 'on'
        booked_at_1to3 = request.form.get('booked_at_1to3') == 'on'
        
        # Re-entry
        is_reentry = request.form.get('is_reentry') == 'on'
        linked_trade_id = request.form.get('linked_trade_id') if is_reentry else None
        
        # Post-trade
        followed_all_rules = request.form.get('followed_all_rules') == 'on'
        emotion_before = request.form.get('emotion_before')
        emotion_during = request.form.get('emotion_during')
        emotion_after = request.form.get('emotion_after')
        mistakes = request.form.get('mistakes', '')
        lesson_learned = request.form.get('lesson_learned')
        exit

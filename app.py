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
        exit_reason = request.form.get('exit_reason')
        
        # Chart
        chart_image = None
        if 'chart_file' in request.files:
            file = request.files['chart_file']
            if file and file.filename:
                chart_image = base64.b64encode(file.read()).decode('utf-8')
        
        # Create trade
        trade = Trade(
            user_id=user_id,
            date=date,
            entry_time=entry_time,
            index=index,
            direction=direction,
            strike=strike,
            entry_premium=entry_premium,
            exit_premium=exit_premium,
            lot_size=lot_size,
            initial_sl_premium=initial_sl_premium,
            exit_time=exit_time,
            initial_risk_points=initial_risk_points,
            points_captured=points_captured,
            pnl_rupees=pnl_rupees,
            rr_achieved=rr_achieved,
            result=result,
            hit_1to1=hit_1to1,
            sl_moved_to_entry=sl_moved_to_entry,
            hit_1to2=hit_1to2,
            sl_moved_to_1r=sl_moved_to_1r,
            hit_1to3=hit_1to3,
            booked_at_1to3=booked_at_1to3,
            exit_reason=exit_reason,
            is_reentry=is_reentry,
            linked_trade_id=linked_trade_id,
            followed_all_rules=followed_all_rules,
            emotion_before=emotion_before,
            emotion_during=emotion_during,
            emotion_after=emotion_after,
            mistakes=mistakes,
            lesson_learned=lesson_learned,
            chart_image=chart_image
        )
        
        db.session.add(trade)
        
        # Update discipline streak if rules followed
        if followed_all_rules:
            update_discipline_streak(user_id, good_discipline=True)
        else:
            update_discipline_streak(user_id, good_discipline=False)
        
        db.session.commit()
        
        flash('Trade logged successfully!', 'success')
        return redirect(url_for('dashboard'))
    
    # Check today's trade count
    today = datetime.now().date()
    today_count = Trade.query.filter_by(user_id=user_id, date=today).count()
    
    return render_template('add_trade.html', today_count=today_count)

@app.route('/trade_history')
def trade_history():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    trades = Trade.query.filter_by(user_id=user_id).order_by(Trade.date.desc(), Trade.entry_time.desc()).all()
    
    return render_template('trade_history.html', trades=trades)

@app.route('/trade/<int:trade_id>')
def trade_detail(trade_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id != session['user_id']:
        return redirect(url_for('dashboard'))
    
    # Get linked trade if re-entry
    linked_trade = None
    if trade.is_reentry and trade.linked_trade_id:
        linked_trade = Trade.query.get(trade.linked_trade_id)
    
    return render_template('trade_detail.html', trade=trade, linked_trade=linked_trade)

@app.route('/trade/<int:trade_id>/delete', methods=['POST'])
def delete_trade(trade_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    trade = Trade.query.get_or_404(trade_id)
    if trade.user_id != session['user_id']:
        return redirect(url_for('dashboard'))
    
    db.session.delete(trade)
    db.session.commit()
    
    flash('Trade deleted successfully', 'success')
    return redirect(url_for('trade_history'))

@app.route('/analytics')
def analytics():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    trades = Trade.query.filter_by(user_id=user_id).order_by(Trade.date).all()
    
    if not trades:
        return render_template('analytics.html', trades=[], stats={}, backtest=None)
    
    # Calculate live stats
    total_trades = len(trades)
    wins = len([t for t in trades if t.result == 'Win'])
    losses = len([t for t in trades if t.result == 'Loss'])
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    
    total_points = sum(t.points_captured for t in trades)
    expectancy = round(total_points / total_trades, 2) if total_trades > 0 else 0
    
    # R:R calculation
    winning_trades = [t for t in trades if t.result == 'Win']
    avg_rr = round(sum(t.rr_achieved for t in winning_trades) / len(winning_trades), 2) if winning_trades else 0
    
    # Re-entry stats
    reentry_trades = [t for t in trades if t.is_reentry]
    reentry_wins = len([t for t in reentry_trades if t.result == 'Win'])
    reentry_win_rate = round(reentry_wins / len(reentry_trades) * 100, 1) if reentry_trades else 0
    
    stats = {
        'total_trades': total_trades,
        'win_rate': win_rate,
        'avg_rr': avg_rr,
        'total_points': round(total_points, 1),
        'expectancy': expectancy,
        'reentry_count': len(reentry_trades),
        'reentry_win_rate': reentry_win_rate
    }
    
    # Get backtest data
    backtest = BacktestData.query.filter_by(user_id=user_id).first()
    
    # Prepare chart data
    cumulative_points = []
    running_total = 0
    for trade in trades:
        running_total += trade.points_captured
        cumulative_points.append({
            'date': trade.date.strftime('%Y-%m-%d'),
            'points': round(running_total, 1)
        })
    
    return render_template('analytics.html', trades=trades, stats=stats, backtest=backtest, 
                         cumulative_points=cumulative_points)

@app.route('/weekly_review', methods=['GET', 'POST'])
def weekly_review():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    
    if request.method == 'POST':
        review = WeeklyReview(
            user_id=user_id,
            week_start=week_start,
            best_trade_reason=request.form.get('best_trade_reason'),
            worst_trade_reason=request.form.get('worst_trade_reason'),
            main_mistake=request.form.get('main_mistake'),
            next_week_focus=request.form.get('next_week_focus')
        )
        db.session.add(review)
        db.session.commit()
        flash('Weekly review saved!', 'success')
        return redirect(url_for('dashboard'))
    
    # Calculate weekly stats
    week_trades = Trade.query.filter(Trade.user_id==user_id, Trade.date>=week_start).all()
    
    stats = {
        'total_trades': len(week_trades),
        'net_points': round(sum(t.points_captured for t in week_trades), 1),
        'win_rate': round(len([t for t in week_trades if t.result=='Win']) / len(week_trades) * 100, 1) if week_trades else 0
    }
    
    return render_template('weekly_review.html', stats=stats, week_trades=week_trades)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    user_settings = Settings.query.filter_by(user_id=user_id).first()
    
    if not user_settings:
        user_settings = Settings(user_id=user_id)
        db.session.add(user_settings)
        db.session.commit()
    
    if request.method == 'POST':
        user_settings.max_trades_per_day = int(request.form.get('max_trades_per_day', 2))
        db.session.commit()
        flash('Settings saved!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('settings.html', settings=user_settings)

# ========== NEW V4.0 ROUTES ==========

@app.route('/log_missed_trade', methods=['GET', 'POST'])
def log_missed_trade():
    """Log a trade you saw but correctly avoided"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        missed = MissedTrade(
            user_id=session['user_id'],
            date=datetime.strptime(request.form.get('date'), '%Y-%m-%d').date(),
            time_noticed=request.form.get('time_noticed'),
            reason_avoided=request.form.get('reason_avoided'),
            setup_type=request.form.get('setup_type'),
            notes=request.form.get('notes')
        )
        db.session.add(missed)
        update_discipline_streak(session['user_id'], good_discipline=True)
        db.session.commit()
        flash('✅ Good discipline! Missed trade logged.', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('log_missed_trade.html')

@app.route('/log_revenge_trade', methods=['GET', 'POST'])
def log_revenge_trade():
    """Quick entry for revenge trades"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        revenge = RevengeTrade(
            user_id=session['user_id'],
            date=datetime.strptime(request.form.get('date'), '%Y-%m-%d').date(),
            time=request.form.get('time'),
            amount_pnl=float(request.form.get('amount_pnl')),
            quantity=int(request.form.get('quantity')),
            index=request.form.get('index'),
            notes=request.form.get('notes'),
            discipline_score=1
        )
        db.session.add(revenge)
        update_discipline_streak(session['user_id'], good_discipline=False)
        db.session.commit()
        flash('⚠️ Revenge trade logged. Discipline score: 1/10', 'warning')
        return redirect(url_for('dashboard'))
    
    return render_template('log_revenge_trade.html')

@app.route('/export_trades')
def export_trades():
    """Export all trades to Excel"""
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    trades = Trade.query.filter_by(user_id=user_id).order_by(Trade.date).all()
    
    data = []
    for t in trades:
        data.append({
            'Date': t.date.strftime('%Y-%m-%d'),
            'Entry Time': t.entry_time,
            'Index': t.index,
            'Strike': t.strike,
            'Direction': t.direction,
            'Entry Premium': t.entry_premium,
            'Exit Premium': t.exit_premium,
            'Exit Time': t.exit_time,
            'Points Captured': t.points_captured,
            'P&L (₹)': t.pnl_rupees,
            'R:R': t.rr_achieved,
            'Result': t.result,
            'Emotion Before': t.emotion_before,
            'Emotion After': t.emotion_after,
            'Followed Rules': 'Yes' if t.followed_all_rules else 'No',
            'Mistakes': t.mistakes or '',
            'Lesson': t.lesson_learned or ''
        })
    
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='All Trades', index=False)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'trading_journal_{datetime.now().strftime("%Y%m%d")}.xlsx'
    )

def init_db():
    with app.app_context():
        db.create_all()
        
        # Create default user
        if not User.query.filter_by(email='trader@tbl.com').first():
            user = User(
                email='trader@tbl.com',
                password_hash=generate_password_hash('trader123')
            )
            db.session.add(user)
            db.session.commit()
            print("✓ Default user created: trader@tbl.com / trader123")
            
            # Add backtest data
            backtest = BacktestData(
                user_id=user.id,
                total_trades=18,
                win_rate=50.0,
                avg_rr=3.83,
                total_points=1593,
                expectancy=88.5,
                reentry_count=4,
                reentry_wins=3,
                reentry_win_rate=75.0
            )
            db.session.add(backtest)
            db.session.commit()
            print("✓ Backtest data loaded")

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)

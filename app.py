from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3
import os
import base64

app = Flask(__name__)
app.secret_key = 'change-this-secret-key-in-production'
import os
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///trading_journal_v3.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db = SQLAlchemy(app)

LOT_VALUES = {
    'Nifty': 65,
    'Bank Nifty': 30,
    'Sensex': 20
}

# ─────────────────────────────────────────────
# MODELS
# ─────────────────────────────────────────────

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

class Trade(db.Model):
    id                  = db.Column(db.Integer, primary_key=True)
    user_id             = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date                = db.Column(db.Date, nullable=False)
    entry_time          = db.Column(db.String(10), nullable=False)
    index               = db.Column(db.String(20), nullable=False)
    direction           = db.Column(db.String(5), nullable=False)
    strike              = db.Column(db.Integer, nullable=False)
    entry_premium       = db.Column(db.Float, nullable=False)
    exit_premium        = db.Column(db.Float, nullable=False)
    lot_size            = db.Column(db.Integer, default=1)
    initial_sl_premium  = db.Column(db.Float, nullable=False)
    exit_time           = db.Column(db.String(10), nullable=False)
    initial_risk_points = db.Column(db.Float, nullable=False)
    points_captured     = db.Column(db.Float, nullable=False)
    pnl_rupees          = db.Column(db.Float, nullable=False)
    rr_achieved         = db.Column(db.Float, nullable=False)
    result              = db.Column(db.String(10), nullable=False)
    hit_1to1            = db.Column(db.Boolean, default=False)
    sl_moved_to_entry   = db.Column(db.Boolean, default=False)
    hit_1to2            = db.Column(db.Boolean, default=False)
    sl_moved_to_1r      = db.Column(db.Boolean, default=False)
    hit_1to3            = db.Column(db.Boolean, default=False)
    booked_at_1to3      = db.Column(db.Boolean, default=False)
    exit_reason         = db.Column(db.String(100))
    is_reentry          = db.Column(db.Boolean, default=False)
    linked_trade_id     = db.Column(db.Integer, nullable=True)
    followed_all_rules  = db.Column(db.Boolean, default=True)
    emotion_before      = db.Column(db.String(50))
    emotion_during      = db.Column(db.String(50))
    emotion_after       = db.Column(db.String(50))
    mistakes            = db.Column(db.String(200))
    lesson_learned      = db.Column(db.Text)
    chart_image         = db.Column(db.Text)
    chart_url           = db.Column(db.String(500))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

class BacktestData(db.Model):
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    total_trades     = db.Column(db.Integer)
    win_rate         = db.Column(db.Float)
    avg_rr           = db.Column(db.Float)
    total_points     = db.Column(db.Float)
    expectancy       = db.Column(db.Float)
    reentry_count    = db.Column(db.Integer)
    reentry_wins     = db.Column(db.Integer)
    reentry_win_rate = db.Column(db.Float)

class WeeklyReview(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    week_start         = db.Column(db.Date, nullable=False)
    best_trade_reason  = db.Column(db.Text)
    worst_trade_reason = db.Column(db.Text)
    main_mistake       = db.Column(db.Text)
    next_week_focus    = db.Column(db.Text)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

class Settings(db.Model):
    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    max_trades_per_day = db.Column(db.Integer, default=2)
    custom_strategies  = db.Column(db.Text)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def calculate_pnl(entry_premium, exit_premium, direction, lot_size, index):
    points     = exit_premium - entry_premium
    lot_value  = LOT_VALUES.get(index, 50)
    pnl_rupees = points * lot_size * lot_value
    return points, pnl_rupees

def calculate_rr(entry, exit_p, sl):
    risk   = abs(entry - sl)
    reward = abs(exit_p - entry)
    if risk == 0:
        return 0
    return round(reward / risk, 2)

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def auto_migrate():
    """
    Safely adds any new columns to the existing database
    without deleting any data. Runs automatically on every startup.
    """
    possible_paths = [
        os.path.join(os.path.dirname(__file__), 'instance', 'trading_journal_v3.db'),
        os.path.join(os.path.dirname(__file__), 'trading_journal_v3.db'),
        'instance/trading_journal_v3.db',
        'trading_journal_v3.db',
    ]
    db_path = None
    for p in possible_paths:
        if os.path.exists(p):
            db_path = p
            break

    if not db_path:
        return  # No DB yet — create_all() will handle fresh creation

    try:
        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(trade)")
        existing = [row[1] for row in cursor.fetchall()]

        if 'chart_url' not in existing:
            cursor.execute("ALTER TABLE trade ADD COLUMN chart_url VARCHAR(500)")
            print("✓ Migration: added chart_url column to existing database")

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Migration info: {e}")

# ─────────────────────────────────────────────
# ROUTES — AUTH
# ─────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email')
        password = request.form.get('password')
        user     = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            return redirect(url_for('dashboard'))
        return render_template('login.html', error="Invalid email or password")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

# ─────────────────────────────────────────────
# ROUTES — DASHBOARD
# ─────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    user_id     = session['user_id']
    today       = datetime.now().date()
    week_start  = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    today_trades = Trade.query.filter_by(user_id=user_id, date=today).all()
    week_trades  = Trade.query.filter(Trade.user_id == user_id, Trade.date >= week_start).all()
    month_trades = Trade.query.filter(Trade.user_id == user_id, Trade.date >= month_start).all()
    total_trades = Trade.query.filter_by(user_id=user_id).count()
    backtest     = BacktestData.query.filter_by(user_id=user_id).first()

    stats = {
        'today_trades' : len(today_trades),
        'today_points' : round(sum(t.points_captured for t in today_trades), 1),
        'today_pnl'    : round(sum(t.pnl_rupees     for t in today_trades), 2),
        'week_points'  : round(sum(t.points_captured for t in week_trades),  1),
        'week_pnl'     : round(sum(t.pnl_rupees     for t in week_trades),  2),
        'month_points' : round(sum(t.points_captured for t in month_trades), 1),
        'month_pnl'    : round(sum(t.pnl_rupees     for t in month_trades), 2),
        'total_trades' : total_trades,
        'target_trades': 30,
    }
    return render_template('dashboard.html', stats=stats, backtest=backtest)

# ─────────────────────────────────────────────
# ROUTES — ADD TRADE
# ─────────────────────────────────────────────

@app.route('/add_trade', methods=['GET', 'POST'])
@login_required
def add_trade():
    user_id     = session['user_id']
    today       = datetime.now().date()
    today_count = Trade.query.filter_by(user_id=user_id, date=today).count()

    if request.method == 'POST':
        try:
            trade_date    = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
            entry_time    = request.form['entry_time']
            exit_time     = request.form['exit_time']
            index         = request.form['index']
            direction     = request.form['direction']
            strike        = int(request.form['strike'])
            entry_premium = float(request.form['entry_premium'])
            exit_premium  = float(request.form['exit_premium'])
            sl_premium    = float(request.form['initial_sl_premium'])
            lot_size      = 1

            points, pnl = calculate_pnl(entry_premium, exit_premium, direction, lot_size, index)
            risk_points = abs(entry_premium - sl_premium)
            rr          = calculate_rr(entry_premium, exit_premium, sl_premium)

            result = 'Win' if points > 0 else ('Loss' if points < 0 else 'BE')

            chart_image = None
            file = request.files.get('chart_file')
            if file and file.filename:
                chart_image = base64.b64encode(file.read()).decode('utf-8')

            chart_url = request.form.get('chart_url', '').strip()

            trade = Trade(
                user_id            = user_id,
                date               = trade_date,
                entry_time         = entry_time,
                exit_time          = exit_time,
                index              = index,
                direction          = direction,
                strike             = strike,
                entry_premium      = entry_premium,
                exit_premium       = exit_premium,
                lot_size           = lot_size,
                initial_sl_premium = sl_premium,
                initial_risk_points= risk_points,
                points_captured    = round(points, 2),
                pnl_rupees         = round(pnl, 2),
                rr_achieved        = rr,
                result             = result,
                hit_1to1           = 'hit_1to1'          in request.form,
                sl_moved_to_entry  = 'sl_moved_to_entry' in request.form,
                hit_1to2           = 'hit_1to2'          in request.form,
                sl_moved_to_1r     = 'sl_moved_to_1r'    in request.form,
                hit_1to3           = 'hit_1to3'          in request.form,
                booked_at_1to3     = 'booked_at_1to3'    in request.form,
                exit_reason        = request.form.get('exit_reason'),
                is_reentry         = 'is_reentry'        in request.form,
                linked_trade_id    = int(request.form['linked_trade_id']) if request.form.get('linked_trade_id') else None,
                followed_all_rules = 'followed_all_rules' in request.form,
                emotion_before     = request.form.get('emotion_before'),
                emotion_during     = request.form.get('emotion_during'),
                emotion_after      = request.form.get('emotion_after'),
                mistakes           = request.form.get('mistakes', ''),
                lesson_learned     = request.form.get('lesson_learned', ''),
                chart_image        = chart_image,
                chart_url          = chart_url,
            )

            db.session.add(trade)
            db.session.commit()
            flash('✅ Trade saved successfully!', 'success')
            return redirect(url_for('trade_history'))

        except Exception as e:
            db.session.rollback()
            flash(f'❌ Error saving trade: {str(e)}', 'error')

    return render_template('add_trade.html', today=today, today_count=today_count)

# ─────────────────────────────────────────────
# ROUTES — TRADE HISTORY
# ─────────────────────────────────────────────

@app.route('/trade_history')
@login_required
def trade_history():
    trades = Trade.query.filter_by(user_id=session['user_id']) \
                        .order_by(Trade.date.desc(), Trade.entry_time.desc()).all()
    return render_template('trade_history.html', trades=trades)

# ─────────────────────────────────────────────
# ROUTES — TRADE DETAIL
# ─────────────────────────────────────────────

@app.route('/trade/<int:trade_id>')
@login_required
def trade_detail(trade_id):
    trade        = Trade.query.filter_by(id=trade_id, user_id=session['user_id']).first_or_404()
    linked_trade = Trade.query.get(trade.linked_trade_id) if trade.linked_trade_id else None
    return render_template('trade_detail.html', trade=trade, linked_trade=linked_trade)

# ─────────────────────────────────────────────
# ROUTES — DELETE TRADE
# ─────────────────────────────────────────────

@app.route('/trade/<int:trade_id>/delete', methods=['POST'])
@login_required
def delete_trade(trade_id):
    trade = Trade.query.filter_by(id=trade_id, user_id=session['user_id']).first_or_404()
    db.session.delete(trade)
    db.session.commit()
    flash('🗑️ Trade deleted.', 'success')
    return redirect(url_for('trade_history'))

# ─────────────────────────────────────────────
# ROUTES — ANALYTICS
# ─────────────────────────────────────────────

@app.route('/analytics')
@login_required
def analytics():
    user_id  = session['user_id']
    trades   = Trade.query.filter_by(user_id=user_id).order_by(Trade.date).all()
    backtest = BacktestData.query.filter_by(user_id=user_id).first()

    if not trades:
        return render_template('analytics.html', trades=[], stats={},
                               backtest=backtest, cumulative_points=[])

    wins      = [t for t in trades if t.result == 'Win']
    losses    = [t for t in trades if t.result == 'Loss']
    reentries = [t for t in trades if t.is_reentry]
    re_wins   = [t for t in reentries if t.result == 'Win']

    total      = len(trades)
    win_rate   = round(len(wins) / total * 100, 1) if total else 0
    avg_rr     = round(sum(t.rr_achieved for t in wins) / len(wins), 2) if wins else 0
    avg_win    = sum(t.points_captured for t in wins)   / len(wins)   if wins   else 0
    avg_loss   = sum(t.points_captured for t in losses) / len(losses) if losses else 0
    expectancy = round((win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss), 1)
    re_wr      = round(len(re_wins) / len(reentries) * 100, 1) if reentries else 0

    stats = {
        'total_trades'    : total,
        'win_rate'        : win_rate,
        'avg_rr'          : avg_rr,
        'total_points'    : round(sum(t.points_captured for t in trades), 1),
        'expectancy'      : expectancy,
        'reentry_count'   : len(reentries),
        'reentry_win_rate': re_wr,
    }

    cumulative, running = [], 0
    for t in trades:
        running += t.points_captured
        cumulative.append({'date': t.date.strftime('%d %b'), 'points': round(running, 1)})

    return render_template('analytics.html', trades=trades, stats=stats,
                           backtest=backtest, cumulative_points=cumulative)

# ─────────────────────────────────────────────
# ROUTES — WEEKLY REVIEW
# ─────────────────────────────────────────────

@app.route('/weekly_review', methods=['GET', 'POST'])
@login_required
def weekly_review():
    user_id    = session['user_id']
    today      = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())

    week_trades = Trade.query.filter(
        Trade.user_id == user_id,
        Trade.date   >= week_start
    ).all()

    wins  = [t for t in week_trades if t.result == 'Win']
    total = len(week_trades)
    stats = {
        'total_trades': total,
        'net_points'  : round(sum(t.points_captured for t in week_trades), 1),
        'win_rate'    : round(len(wins) / total * 100, 1) if total else 0,
    }

    if request.method == 'POST':
        review = WeeklyReview(
            user_id            = user_id,
            week_start         = week_start,
            best_trade_reason  = request.form.get('best_trade_reason'),
            worst_trade_reason = request.form.get('worst_trade_reason'),
            main_mistake       = request.form.get('main_mistake'),
            next_week_focus    = request.form.get('next_week_focus'),
        )
        db.session.add(review)
        db.session.commit()
        flash('✅ Weekly review saved!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('weekly_review.html', stats=stats)

# ─────────────────────────────────────────────
# ROUTES — SETTINGS
# ─────────────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user_id = session['user_id']
    s = Settings.query.filter_by(user_id=user_id).first()
    if not s:
        s = Settings(user_id=user_id)
        db.session.add(s)
        db.session.commit()

    if request.method == 'POST':
        s.max_trades_per_day = int(request.form.get('max_trades_per_day', 2))
        db.session.commit()
        flash('✅ Settings saved!', 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html', settings=s)

# ─────────────────────────────────────────────
# DB INIT
# ─────────────────────────────────────────────

def init_db():
    with app.app_context():
        auto_migrate()   # Add missing columns to existing DB first
        db.create_all()  # Create any tables that don't exist yet

        if not User.query.filter_by(email='trader@tbl.com').first():
            user = User(
                email         = 'trader@tbl.com',
                password_hash = generate_password_hash('trader123')
            )
            db.session.add(user)
            db.session.commit()
            print("✓ User created: trader@tbl.com / trader123")

            backtest = BacktestData(
                user_id         = user.id,
                total_trades    = 18,
                win_rate        = 50.0,
                avg_rr          = 3.83,
                total_points    = 1593,
                expectancy      = 88.5,
                reentry_count   = 4,
                reentry_wins    = 3,
                reentry_win_rate= 75.0
            )
            db.session.add(backtest)
            db.session.commit()
            print("✓ Backtest data loaded")

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)

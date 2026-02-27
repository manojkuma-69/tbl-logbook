from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from collections import defaultdict
import sqlite3, os, base64, io, csv

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = 'tbl2026xK9mPqR7vNjW3'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////data/trading_journal.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db = SQLAlchemy(app)

LOT_VALUES = {'Nifty': 65, 'Bank Nifty': 30, 'Sensex': 20}

# ── Discipline scoring map ─────────────────────────────────────────────────
EMOTION_SCORES = {
    'emotion_before': {'Calm': 10, 'Confident': 8, 'Anxious': 4, 'FOMO': 1, 'Revenge': 0},
    'emotion_during': {'Patient': 10, 'Calm': 10, 'Anxious': 4, 'Fearful': 3, 'Greedy': 2},
    'emotion_after':  {'Satisfied': 10, 'Regretful': 5, 'Overconfident': 3, 'Frustrated': 2},
}

def calc_discipline_score(emotion_before, emotion_during, emotion_after, followed_rules):
    score = 0
    score += EMOTION_SCORES['emotion_before'].get(emotion_before, 5)
    score += EMOTION_SCORES['emotion_during'].get(emotion_during, 5)
    score += EMOTION_SCORES['emotion_after'].get(emotion_after, 5)
    if followed_rules:
        score += 20   # big bonus for rule discipline
    total = round((score / 50) * 100)  # out of 50 → percentage
    return min(total, 100)

# ── Models ─────────────────────────────────────────────────────────────────

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
    discipline_score    = db.Column(db.Integer, default=0)
    trade_rating        = db.Column(db.Integer, default=0)   # 1-5 stars
    mistakes            = db.Column(db.String(200))
    lesson_learned      = db.Column(db.Text)
    chart_image         = db.Column(db.Text)
    chart_url           = db.Column(db.String(500))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

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
    id                  = db.Column(db.Integer, primary_key=True)
    user_id             = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    max_trades_per_day  = db.Column(db.Integer, default=2)
    max_loss_per_day    = db.Column(db.Float, default=5000)
    custom_strategies   = db.Column(db.Text)

# ── Helpers ────────────────────────────────────────────────────────────────

def calculate_pnl(entry_premium, exit_premium, direction, lot_size, index):
    points     = exit_premium - entry_premium
    lot_value  = LOT_VALUES.get(index, 50)
    pnl_rupees = points * lot_size * lot_value
    return points, pnl_rupees

def calculate_rr(entry, exit_p, sl):
    risk   = abs(entry - sl)
    reward = abs(exit_p - entry)
    if risk == 0: return 0
    return round(reward / risk, 2)

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_breach_alarm():
    """Injects breach alarm data into every template automatically."""
    if 'user_id' not in session:
        return {'breach_alarm': {'active': False, 'today_pnl': 0, 'max_loss': 0}}
    try:
        user_id     = session['user_id']
        today       = datetime.now().date()
        s           = Settings.query.filter_by(user_id=user_id).first()
        max_loss    = s.max_loss_per_day if s else 5000
        today_trades= Trade.query.filter_by(user_id=user_id, date=today).all()
        today_pnl   = round(sum(t.pnl_rupees for t in today_trades), 2)
        active      = today_pnl <= -abs(max_loss)
        return {'breach_alarm': {'active': active, 'today_pnl': today_pnl, 'max_loss': max_loss}}
    except:
        return {'breach_alarm': {'active': False, 'today_pnl': 0, 'max_loss': 0}}


# ── Auth ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))

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

# ── Dashboard ──────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    user_id     = session['user_id']
    today       = datetime.now().date()
    week_start  = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    today_trades = Trade.query.filter_by(user_id=user_id, date=today).all()
    week_trades  = Trade.query.filter(Trade.user_id==user_id, Trade.date>=week_start).all()
    month_trades = Trade.query.filter(Trade.user_id==user_id, Trade.date>=month_start).all()
    total_trades = Trade.query.filter_by(user_id=user_id).count()

    # Max loss warning
    s = Settings.query.filter_by(user_id=user_id).first()
    max_loss   = s.max_loss_per_day if s else 5000
    today_pnl  = round(sum(t.pnl_rupees for t in today_trades), 2)
    loss_warning = today_pnl <= -abs(max_loss)

    stats = {
        'today_trades' : len(today_trades),
        'today_points' : round(sum(t.points_captured for t in today_trades), 1),
        'today_pnl'    : today_pnl,
        'week_points'  : round(sum(t.points_captured for t in week_trades), 1),
        'week_pnl'     : round(sum(t.pnl_rupees for t in week_trades), 2),
        'month_points' : round(sum(t.points_captured for t in month_trades), 1),
        'month_pnl'    : round(sum(t.pnl_rupees for t in month_trades), 2),
        'total_trades' : total_trades,
        'target_trades': 30,
        'loss_warning' : loss_warning,
        'max_loss'     : max_loss,
    }
    return render_template('dashboard.html', stats=stats)

# ── Add Trade ──────────────────────────────────────────────────────────────

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
            points, pnl   = calculate_pnl(entry_premium, exit_premium, direction, lot_size, index)
            risk_points   = abs(entry_premium - sl_premium)
            rr            = calculate_rr(entry_premium, exit_premium, sl_premium)
            result        = 'Win' if points > 0 else ('Loss' if points < 0 else 'BE')

            # Chart — file upload
            chart_image = None
            file = request.files.get('chart_file')
            if file and file.filename:
                chart_image = base64.b64encode(file.read()).decode('utf-8')

            # Chart — paste base64 (copy-paste from clipboard via JS)
            pasted_image = request.form.get('pasted_image', '').strip()
            if pasted_image and not chart_image:
                # strip data URL prefix if present
                if ',' in pasted_image:
                    pasted_image = pasted_image.split(',', 1)[1]
                chart_image = pasted_image

            chart_url   = request.form.get('chart_url', '').strip()
            emotion_b   = request.form.get('emotion_before', '')
            emotion_d   = request.form.get('emotion_during', '')
            emotion_a   = request.form.get('emotion_after', '')
            followed    = 'followed_all_rules' in request.form
            disc_score  = calc_discipline_score(emotion_b, emotion_d, emotion_a, followed)
            trade_rating= int(request.form.get('trade_rating', 0))

            trade = Trade(
                user_id=user_id, date=trade_date, entry_time=entry_time,
                exit_time=exit_time, index=index, direction=direction,
                strike=strike, entry_premium=entry_premium, exit_premium=exit_premium,
                lot_size=lot_size, initial_sl_premium=sl_premium,
                initial_risk_points=risk_points, points_captured=round(points, 2),
                pnl_rupees=round(pnl, 2), rr_achieved=rr, result=result,
                hit_1to1='hit_1to1' in request.form,
                sl_moved_to_entry='sl_moved_to_entry' in request.form,
                hit_1to2='hit_1to2' in request.form,
                sl_moved_to_1r='sl_moved_to_1r' in request.form,
                hit_1to3='hit_1to3' in request.form,
                booked_at_1to3='booked_at_1to3' in request.form,
                exit_reason=request.form.get('exit_reason'),
                is_reentry='is_reentry' in request.form,
                linked_trade_id=int(request.form['linked_trade_id']) if request.form.get('linked_trade_id') else None,
                followed_all_rules=followed,
                emotion_before=emotion_b, emotion_during=emotion_d, emotion_after=emotion_a,
                discipline_score=disc_score, trade_rating=trade_rating,
                mistakes=request.form.get('mistakes', ''),
                lesson_learned=request.form.get('lesson_learned', ''),
                chart_image=chart_image, chart_url=chart_url,
            )
            db.session.add(trade)
            db.session.commit()
            flash(f'✅ Trade saved! Discipline Score: {disc_score}/100', 'success')
            return redirect(url_for('trade_history'))
        except Exception as e:
            db.session.rollback()
            flash(f'❌ Error saving trade: {str(e)}', 'error')
    return render_template('add_trade.html', today=today, today_count=today_count)

# ── Trade History ──────────────────────────────────────────────────────────

@app.route('/trade_history')
@login_required
def trade_history():
    trades = Trade.query.filter_by(user_id=session['user_id']).order_by(Trade.date.desc()).all()
    return render_template('trade_history.html', trades=trades)

# ── Trade Detail ───────────────────────────────────────────────────────────

@app.route('/trade/<int:trade_id>')
@login_required
def trade_detail(trade_id):
    trade        = Trade.query.filter_by(id=trade_id, user_id=session['user_id']).first_or_404()
    linked_trade = Trade.query.get(trade.linked_trade_id) if trade.linked_trade_id else None
    return render_template('trade_detail.html', trade=trade, linked_trade=linked_trade)

# ── Delete Trade ───────────────────────────────────────────────────────────

@app.route('/trade/<int:trade_id>/delete', methods=['POST'])
@login_required
def delete_trade(trade_id):
    trade = Trade.query.filter_by(id=trade_id, user_id=session['user_id']).first_or_404()
    db.session.delete(trade)
    db.session.commit()
    flash('🗑️ Trade deleted.', 'success')
    return redirect(url_for('trade_history'))

# ── Analytics ──────────────────────────────────────────────────────────────

@app.route('/analytics')
@login_required
def analytics():
    user_id  = session['user_id']
    trades   = Trade.query.filter_by(user_id=user_id).order_by(Trade.date).all()

    if not trades:
        return render_template('analytics.html', trades=[], stats={}, cumulative_points=[],
                               weekly_data=[], monthly_data=[], time_slots=[], best_time='—')

    wins      = [t for t in trades if t.result == 'Win']
    losses    = [t for t in trades if t.result == 'Loss']
    reentries = [t for t in trades if t.is_reentry]
    re_wins   = [t for t in reentries if t.result == 'Win']

    total      = len(trades)
    win_rate   = round(len(wins)/total*100, 1) if total else 0
    avg_rr     = round(sum(t.rr_achieved for t in wins)/len(wins), 2) if wins else 0
    avg_win    = sum(t.points_captured for t in wins)/len(wins) if wins else 0
    avg_loss   = sum(t.points_captured for t in losses)/len(losses) if losses else 0
    expectancy = round((win_rate/100*avg_win)+((1-win_rate/100)*avg_loss), 1)
    re_wr      = round(len(re_wins)/len(reentries)*100, 1) if reentries else 0
    avg_disc   = round(sum(t.discipline_score for t in trades)/total) if total else 0

    stats = {
        'total_trades': total, 'win_rate': win_rate, 'avg_rr': avg_rr,
        'total_points': round(sum(t.points_captured for t in trades), 1),
        'total_pnl'   : round(sum(t.pnl_rupees for t in trades), 2),
        'expectancy'  : expectancy, 'reentry_count': len(reentries),
        'reentry_win_rate': re_wr, 'avg_discipline': avg_disc,
    }

    # Cumulative equity curve
    cumulative, running = [], 0
    for t in trades:
        running += t.points_captured
        cumulative.append({'date': t.date.strftime('%d %b'), 'points': round(running, 1)})

    # Weekly P&L data
    weekly = defaultdict(float)
    for t in trades:
        wk = t.date - timedelta(days=t.date.weekday())
        weekly[wk.strftime('%d %b')] += t.pnl_rupees
    weekly_data = [{'week': k, 'pnl': round(v, 2)} for k, v in sorted(weekly.items())]

    # Monthly P&L data
    monthly = defaultdict(float)
    for t in trades:
        monthly[t.date.strftime('%b %Y')] += t.pnl_rupees
    monthly_data = [{'month': k, 'pnl': round(v, 2)} for k, v in sorted(monthly.items())]

    # Best time of day to trade
    time_buckets = {
        '9:15–9:30': [], '9:30–10:00': [], '10:00–11:00': [],
        '11:00–12:00': [], '12:00+': []
    }
    for t in trades:
        try:
            h, m = map(int, t.entry_time.split(':'))
            mins = h*60+m
            if mins < 9*60+30:   time_buckets['9:15–9:30'].append(t)
            elif mins < 10*60:   time_buckets['9:30–10:00'].append(t)
            elif mins < 11*60:   time_buckets['10:00–11:00'].append(t)
            elif mins < 12*60:   time_buckets['11:00–12:00'].append(t)
            else:                time_buckets['12:00+'].append(t)
        except: pass

    time_slots = []
    best_time, best_wr = '—', -1
    for slot, ts in time_buckets.items():
        if not ts: continue
        w = [x for x in ts if x.result == 'Win']
        wr = round(len(w)/len(ts)*100, 1)
        time_slots.append({'slot': slot, 'trades': len(ts), 'win_rate': wr,
                           'pnl': round(sum(x.pnl_rupees for x in ts), 2)})
        if wr > best_wr:
            best_wr, best_time = wr, slot

    return render_template('analytics.html', trades=trades, stats=stats,
                           cumulative_points=cumulative, weekly_data=weekly_data,
                           monthly_data=monthly_data, time_slots=time_slots,
                           best_time=best_time)

# ── Weekly Review ──────────────────────────────────────────────────────────

@app.route('/weekly_review', methods=['GET', 'POST'])
@login_required
def weekly_review():
    user_id    = session['user_id']
    today      = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    week_trades = Trade.query.filter(Trade.user_id==user_id, Trade.date>=week_start).all()
    wins  = [t for t in week_trades if t.result == 'Win']
    total = len(week_trades)
    stats = {
        'total_trades': total,
        'net_points'  : round(sum(t.points_captured for t in week_trades), 1),
        'net_pnl'     : round(sum(t.pnl_rupees for t in week_trades), 2),
        'win_rate'    : round(len(wins)/total*100, 1) if total else 0,
    }
    if request.method == 'POST':
        review = WeeklyReview(
            user_id=user_id, week_start=week_start,
            best_trade_reason=request.form.get('best_trade_reason'),
            worst_trade_reason=request.form.get('worst_trade_reason'),
            main_mistake=request.form.get('main_mistake'),
            next_week_focus=request.form.get('next_week_focus'),
        )
        db.session.add(review)
        db.session.commit()
        flash('✅ Weekly review saved!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('weekly_review.html', stats=stats)

# ── Settings (with change password) ───────────────────────────────────────

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    user_id = session['user_id']
    user    = User.query.get(user_id)
    s = Settings.query.filter_by(user_id=user_id).first()
    if not s:
        s = Settings(user_id=user_id)
        db.session.add(s)
        db.session.commit()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_settings':
            s.max_trades_per_day = int(request.form.get('max_trades_per_day', 2))
            s.max_loss_per_day   = float(request.form.get('max_loss_per_day', 5000))
            db.session.commit()
            flash('✅ Settings saved!', 'success')

        elif action == 'change_credentials':
            new_email    = request.form.get('new_email', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_pass = request.form.get('confirm_password', '').strip()
            current_pass = request.form.get('current_password', '').strip()

            if not check_password_hash(user.password_hash, current_pass):
                flash('❌ Current password is incorrect.', 'error')
            elif new_password and new_password != confirm_pass:
                flash('❌ New passwords do not match.', 'error')
            else:
                if new_email and new_email != user.email:
                    existing = User.query.filter_by(email=new_email).first()
                    if existing:
                        flash('❌ That email is already in use.', 'error')
                        return redirect(url_for('settings'))
                    user.email = new_email
                if new_password:
                    user.password_hash = generate_password_hash(new_password)
                db.session.commit()
                flash('✅ Credentials updated! Please log in again.', 'success')
                session.pop('user_id', None)
                return redirect(url_for('login'))

        return redirect(url_for('settings'))

    return render_template('settings.html', settings=s, user=user)


# ── Missed Trade ───────────────────────────────────────────────────────────

@app.route('/missed_trade', methods=['GET', 'POST'])
@login_required
def missed_trade():
    user_id = session['user_id']
    today   = datetime.now().date()
    if request.method == 'POST':
        mt = MissedTrade(
            user_id = user_id,
            date    = datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
            time    = request.form.get('time', ''),
            index   = request.form.get('index', ''),
            reason  = request.form.get('reason', ''),
            notes   = request.form.get('notes', ''),
        )
        db.session.add(mt)
        db.session.commit()
        flash('✅ Missed trade logged! Good discipline — staying out is a skill.', 'success')
        return redirect(url_for('dashboard'))
    return render_template('missed_trade.html', today=today)

# ── Revenge Trade ──────────────────────────────────────────────────────────

@app.route('/revenge_trade', methods=['GET', 'POST'])
@login_required
def revenge_trade():
    user_id = session['user_id']
    today   = datetime.now().date()
    if request.method == 'POST':
        rt = RevengeTrade(
            user_id          = user_id,
            date             = datetime.strptime(request.form['date'], '%Y-%m-%d').date(),
            time             = request.form.get('time', ''),
            index            = request.form.get('index', ''),
            quantity         = int(request.form.get('quantity', 1)),
            pnl_rupees       = float(request.form.get('pnl_rupees', 0)),
            discipline_score = 5,
            notes            = request.form.get('notes', ''),
        )
        db.session.add(rt)
        db.session.commit()
        flash('⚠️ Revenge trade logged. Discipline score: 5/100. Reflect on what triggered this.', 'warning')
        return redirect(url_for('dashboard'))
    return render_template('revenge_trade.html', today=today)

# ── Export CSV (opens in Excel) ────────────────────────────────────────────

@app.route('/export/<period>')
@login_required
def export_csv(period):
    user_id = session['user_id']
    today   = datetime.now().date()
    if period == 'week':
        week_start = today - timedelta(days=today.weekday())
        trades     = Trade.query.filter(Trade.user_id==user_id, Trade.date>=week_start).order_by(Trade.date).all()
        filename   = f'TBL_trades_week_{week_start}.csv'
    elif period == 'month':
        month_start = today.replace(day=1)
        trades      = Trade.query.filter(Trade.user_id==user_id, Trade.date>=month_start).order_by(Trade.date).all()
        filename    = f'TBL_trades_{today.strftime("%b_%Y")}.csv'
    else:
        trades   = Trade.query.filter_by(user_id=user_id).order_by(Trade.date).all()
        filename = 'TBL_all_trades.csv'

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date','Entry Time','Exit Time','Index','Direction','Strike',
                     'Entry Premium','Exit Premium','SL Premium','Points','PnL (Rs)',
                     'R:R','Result','Discipline Score','Trade Rating','Followed Rules',
                     'Emotion Before','Emotion During','Emotion After',
                     'Exit Reason','Mistakes','Lesson Learned'])
    for t in trades:
        writer.writerow([
            t.date, t.entry_time, t.exit_time, t.index, t.direction, t.strike,
            t.entry_premium, t.exit_premium, t.initial_sl_premium,
            t.points_captured, t.pnl_rupees,
            t.rr_achieved if t.result == 'Win' else '—',
            t.result, t.discipline_score, t.trade_rating,
            'Yes' if t.followed_all_rules else 'No',
            t.emotion_before, t.emotion_during, t.emotion_after,
            t.exit_reason, t.mistakes, t.lesson_learned
        ])
    response = make_response(output.getvalue())
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return response

# ── DB Init ────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    if not User.query.filter_by(email='trader@tbl.com').first():
        user = User(email='trader@tbl.com',
                    password_hash=generate_password_hash('trader123'))
        db.session.add(user)
        db.session.commit()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

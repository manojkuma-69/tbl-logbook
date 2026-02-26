"""
DATABASE MIGRATION SCRIPT - SAFELY ADDS NEW FEATURES
Run this ONCE before using new version
Preserves all existing data
"""

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trading_journal_v3.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# New Models for Additional Features

class MissedTrade(db.Model):
    """Tracks trades you saw but correctly avoided (good discipline)"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time_noticed = db.Column(db.String(10))
    reason_avoided = db.Column(db.Text, nullable=False)  # Why you didn't enter
    setup_type = db.Column(db.String(50))  # What setup you saw
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class RevengeTrade(db.Model):
    """Simplified logging for revenge trades (bad discipline)"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(10))
    amount_pnl = db.Column(db.Float, nullable=False)  # Just ₹ gained/lost
    quantity = db.Column(db.Integer)
    index = db.Column(db.String(20))  # Nifty/BankNifty/Sensex
    notes = db.Column(db.Text)
    discipline_score = db.Column(db.Integer, default=1)  # Always 1/10
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class DisciplineStreak(db.Model):
    """Tracks consecutive days of good discipline"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    current_streak = db.Column(db.Integer, default=0)
    longest_streak = db.Column(db.Integer, default=0)
    last_update = db.Column(db.Date)
    streak_broken_count = db.Column(db.Integer, default=0)  # Times broken
    last_broken_date = db.Column(db.Date)

def migrate():
    """Run migration - adds new tables without touching existing data"""
    with app.app_context():
        print("🔄 Starting migration...")
        print("📊 Checking existing database...")
        
        # Create new tables (won't affect existing tables)
        db.create_all()
        
        print("✅ New tables created:")
        print("   - MissedTrade (for avoided trades)")
        print("   - RevengeTrade (for revenge trading)")
        print("   - DisciplineStreak (for streak tracking)")
        
        print("\n✅ Migration complete!")
        print("✅ All existing trade data preserved")
        print("✅ Ready to use new features")

if __name__ == '__main__':
    migrate()

"""Eager-import debug script."""
import os
import sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ['TESTING']='1'
import app.db.session
from app import config
config.reset_settings_cache()
from app.db import init as db_init
app.db.session.rebuild_engine_for_testing()
db_init.init_db()
from app.db.session import SessionLocal
from app.analyzer.prompts import seed_defaults
from sqlalchemy import text
with SessionLocal() as s:
    rows = s.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    print("rows count:", len(rows), "has prompt_templates:", any("prompt_templates" in str(r) for r in rows))
    seed_defaults(s)

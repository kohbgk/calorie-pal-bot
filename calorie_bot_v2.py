"""
Secure Calorie-Tracker Telegram Bot  (telebot edition)
──────────────────────────────────────────────────────
Commands
  /add <food> <kcals>   • log an entry  (blocked 22:00-00:00 SGT – scolds)
  /remove               • shows today's items with ❌ buttons to delete
  /summary              • lists *your* entries + subtotal for today
  /reset                • deletes *your* entries for today
Daily
  22:00 SGT             • group recap: every user, their foods & totals
Multi-group
  Works in any chat: IDs are learned automatically
Security
  BOT TOKEN **MUST** be supplied via the TB_TOKEN environment variable
  (optionally from a local .env file that is never committed).
"""
from __future__ import annotations

import os
import sqlite3
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message

# ──────────────────────────────────── SECRETS ─────────────────────────────────── #
# Token is read ONLY from the environment. If python-dotenv is available we
# also read a local .env file for convenience in development.
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()                   # silently ignored if .env missing
except ModuleNotFoundError:
    pass

TOKEN = os.environ["TB_TOKEN"]  # raises KeyError if not set!

BOT = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ──────────────────────────────────── SETTINGS ────────────────────────────────── #
DB_FILE       = "kcal.db"
TZ            = ZoneInfo("Asia/Singapore")

QUIET_START   = 22         # 22:00 inclusive
QUIET_END     = 0          # 00:00 exclusive
SUMMARY_HOUR  = 22         # when daily group recap is sent

# ──────────────────────────────── DB HELPERS ─────────────────────────────────── #
def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_FILE, isolation_level=None)

def init_db() -> None:
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS entries (
                       id       INTEGER PRIMARY KEY AUTOINCREMENT,
                       user_id  INTEGER,
                       chat_id  INTEGER,
                       ts_utc   TEXT,
                       food     TEXT,
                       kcal     INTEGER
                     )""")
        c.execute("""CREATE TABLE IF NOT EXISTS chats (
                       chat_id INTEGER PRIMARY KEY
                     )""")

def register_chat(chat_id: int) -> None:
    with db() as c:
        c.execute("INSERT OR IGNORE INTO chats VALUES (?)", (chat_id,))

def _today_range() -> tuple[str, str]:
    start = dt.datetime.combine(now_sgt().date(), dt.time.min, TZ).astimezone(dt.UTC)
    end   = start + dt.timedelta(days=1)
    return start.isoformat(), end.isoformat()

def add_entry(uid: int, cid: int, food: str, kcal: int) -> None:
    with db() as c:
        c.execute("""INSERT INTO entries (user_id, chat_id, ts_utc, food, kcal)
                     VALUES (?,?,?,?,?)""",
                  (uid, cid, dt.datetime.utcnow().isoformat(), food, kcal))

def todays_entries(uid: int, cid: int) -> list[tuple[int, str, int]]:
    start, end = _today_range()
    with db() as c:
        return c.execute("""SELECT id, food, kcal FROM entries
                            WHERE user_id=? AND chat_id=? AND
                                  ts_utc BETWEEN ? AND ?
                            ORDER BY id""",
                         (uid, cid, start, end)).fetchall()

def delete_entry(row_id: int) -> None:
    with db() as c:
        c.execute("DELETE FROM entries WHERE id=?", (row_id,))

def reset_today(uid: int, cid: int) -> None:
    start, end = _today_range()
    with db() as c:
        c.execute("""DELETE FROM entries
                     WHERE user_id=? AND chat_id=? AND
                           ts_utc BETWEEN ? AND ?""",
                  (uid, cid, start, end))

def day_details(cid: int) -> dict[int, list[tuple[str, int]]]:
    start, end = _today_range()
    with db() as c:
        rows = c.execute("""SELECT user_id, food, kcal FROM entries
                            WHERE chat_id=? AND ts_utc BETWEEN ? AND ?""",
                         (cid, start, end)).fetchall()
    out: dict[int, list[tuple[str, int]]] = {}
    for uid, food, kcal in rows:
        out.setdefault(uid, []).append((food, kcal))
    return out

def all_chat_ids() -> list[int]:
    with db() as c:
        return [row[0] for row in c.execute("SELECT chat_id FROM chats")]

# ─────────────────────────────── UTILITIES ───────────────────────────────────── #
def now_sgt() -> dt.datetime:
    return dt.datetime.now(TZ)

def in_quiet_window(ts: dt.datetime) -> bool:
    return ts.hour >= QUIET_START or ts.hour < QUIET_END

def mention_html(user) -> str:
    name = telebot.util.escape(user.first_name or "user")
    return f'<a href="tg://user?id={user.id}">{name}</a>'

# ─────────────────────────────── COMMANDS ────────────────────────────────────── #
# ────────── /add ────────── #
@BOT.message_handler(commands=['add'])
def cmd_add(msg: Message):
    """ /add <food name> <kcals>  → log an entry """
    register_chat(msg.chat.id)

    # Block logging during the quiet window (22:00–00:00)
    if in_quiet_window(now_sgt()):
        BOT.reply_to(msg, "you fat fuck why did u eat")
        return

    # Strip the command itself, keep the rest of the line
    # e.g. "/add fish and chips 500" -> "fish and chips 500"
    arg_line = msg.text.partition(" ")[2].strip()

    # Split once from the RIGHT: everything before last space = food,
    # last token = kcal string.
    try:
        food_part, kcal_str = arg_line.rsplit(" ", 1)
        kcal = int(kcal_str)                     # must be an int
    except (ValueError, IndexError):
        BOT.reply_to(
            msg,
            "Usage: /add <food name> <kcals>\n"
            "Example: /add fish and chips 500"
        )
        return

    food = food_part.strip()
    add_entry(msg.from_user.id, msg.chat.id, food, kcal)

    BOT.reply_to(
        msg,
        f"Added for {mention_html(msg.from_user)}: "
        f"<b>{telebot.util.escape(food)}</b> – <b>{kcal}</b> kcal ✔️"
    )


@BOT.message_handler(commands=["remove"])
def cmd_remove(msg: Message) -> None:
    register_chat(msg.chat.id)
    rows = todays_entries(msg.from_user.id, msg.chat.id)
    if not rows:
        BOT.reply_to(msg, "Nothing to remove today.")
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for row_id, food, kcal in rows:
        kb.add(
            InlineKeyboardButton(
                f"❌ {food} – {kcal} kcal",
                callback_data=f"del:{row_id}"
            )
        )
    BOT.reply_to(msg, "Tap an item to delete it:", reply_markup=kb)

@BOT.callback_query_handler(func=lambda q: q.data.startswith("del:"))
def cb_delete(q: CallbackQuery) -> None:
    delete_entry(int(q.data.split(":", 1)[1]))
    BOT.answer_callback_query(q.id, "Entry removed ✅")
    BOT.edit_message_text("Deleted.", q.message.chat.id, q.message.id)

@BOT.message_handler(commands=["summary"])
def cmd_summary(msg: Message) -> None:
    register_chat(msg.chat.id)
    rows = todays_entries(msg.from_user.id, msg.chat.id)
    if not rows:
        BOT.reply_to(msg, "No entries yet today.")
        return
    total = sum(k for _, _, k in rows)
    lines = [f"Your log for {now_sgt().date()}  –  {total} kcal"]
    lines += [f" • {food} – {kcal}" for _, food, kcal in rows]
    BOT.reply_to(msg, "\n".join(lines))

@BOT.message_handler(commands=["reset"])
def cmd_reset(msg: Message) -> None:
    register_chat(msg.chat.id)
    reset_today(msg.from_user.id, msg.chat.id)
    BOT.reply_to(msg, "Today's entries cleared. Start fresh! 🎯")

# ─────────────────────────────── SCHEDULER ──────────────────────────────────── #
def send_chat_recap(cid: int) -> None:
    det = day_details(cid)
    if not det:
        return
    lines = [f"**Daily calorie recap ({now_sgt().date()})**"]
    for uid, items in det.items():
        subtotal = sum(k for _, k in items)
        user_link = f'<a href="tg://user?id={uid}">User</a>'
        lines.append(f"\n{user_link}: <b>{subtotal}</b> kcal")
        for food, kcal in items:
            lines.append(f" • {telebot.util.escape(food)} – {kcal}")
    BOT.send_message(cid, "\n".join(lines), parse_mode="HTML")

def nightly_job() -> None:
    for cid in all_chat_ids():
        try:
            send_chat_recap(cid)
        except Exception as err:
            logging.warning("Recap failed for %s: %s", cid, err)

def start_scheduler() -> None:
    sched = BackgroundScheduler(timezone=str(TZ))
    sched.add_job(nightly_job, "cron", hour=SUMMARY_HOUR, minute=0)
    sched.start()

# ─────────────────────—— PASSIVE CHAT REGISTRATION ─────────────────────────── #
@BOT.message_handler(func=lambda _m: True, content_types=["text", "photo", "sticker", "video"])
def seen_any(msg: Message) -> None:
    register_chat(msg.chat.id)

# ─────────────────────────────── MAIN ──────────────────────────────────────── #
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    init_db()
    start_scheduler()
    BOT.infinity_polling(skip_pending=True)

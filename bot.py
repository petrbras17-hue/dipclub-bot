"""
DIP CLUB Bot (@dipclub_bot)
SCRUM-мастер для команды THE DIP + комьюнити бот.

Запуск: BOT_TOKEN=<token> python bot.py

Опциональные переменные:
  TEAM_CHAT_ID      -- ID группового чата команды
  COMMUNITY_CHAT_ID -- ID публичного канала/группы
  ADMIN_IDS         -- Telegram ID администраторов (через запятую)
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

TEAM_CHAT_ID = int(os.getenv("TEAM_CHAT_ID", "0"))
COMMUNITY_CHAT_ID = int(os.getenv("COMMUNITY_CHAT_ID", "0"))
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS team_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            role TEXT DEFAULT 'member',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sprints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sprint_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            assigned_to INTEGER,
            status TEXT DEFAULT 'todo',
            priority TEXT DEFAULT 'medium',
            created_by INTEGER,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sprint_id) REFERENCES sprints(id),
            FOREIGN KEY (assigned_to) REFERENCES team_members(id),
            FOREIGN KEY (created_by) REFERENCES team_members(id)
        );

        CREATE TABLE IF NOT EXISTS standups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            standup_date TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (member_id) REFERENCES team_members(id),
            UNIQUE(member_id, standup_date)
        );

        CREATE TABLE IF NOT EXISTS meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            meeting_date TEXT NOT NULL,
            meeting_time TEXT NOT NULL,
            location TEXT DEFAULT 'THE DIP',
            created_by INTEGER,
            reminder_sent INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES team_members(id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            event_date TEXT NOT NULL,
            event_time TEXT NOT NULL,
            location TEXT DEFAULT 'THE DIP, Николина Гора',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS event_rsvps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            username TEXT,
            status TEXT DEFAULT 'going',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events(id),
            UNIQUE(event_id, telegram_id)
        );

        CREATE TABLE IF NOT EXISTS content (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT NOT NULL,
            content_text TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_sprint ON tasks(sprint_id, status);
        CREATE INDEX IF NOT EXISTS idx_standups_date ON standups(standup_date);
        CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_or_create_member(telegram_id: int, username: str | None,
                         first_name: str | None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM team_members WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row:
        member_id = row[0]
    else:
        cur.execute(
            "INSERT INTO team_members (telegram_id, username, first_name) "
            "VALUES (?, ?, ?)",
            (telegram_id, username, first_name),
        )
        conn.commit()
        member_id = cur.lastrowid
    conn.close()
    return member_id


def get_active_sprint() -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM sprints WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_sprint(name: str) -> int:
    today = datetime.now(TZ).date()
    # End on Friday of this week (or next)
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        days_until_friday = 7
    end_date = today + timedelta(days=days_until_friday)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Close current active sprint
    cur.execute("UPDATE sprints SET status = 'completed' WHERE status = 'active'")
    cur.execute(
        "INSERT INTO sprints (name, start_date, end_date) VALUES (?, ?, ?)",
        (name, today.isoformat(), end_date.isoformat()),
    )
    conn.commit()
    sprint_id = cur.lastrowid
    # Move incomplete tasks to new sprint
    cur.execute(
        "UPDATE tasks SET sprint_id = ? "
        "WHERE status IN ('todo', 'in_progress') AND sprint_id != ?",
        (sprint_id, sprint_id),
    )
    conn.commit()
    conn.close()
    return sprint_id


def add_task(title: str, sprint_id: int | None, created_by: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (title, sprint_id, created_by) VALUES (?, ?, ?)",
        (title, sprint_id, created_by),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id


def get_tasks(sprint_id: int | None = None, status: str | None = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = "SELECT t.*, tm.first_name as assignee_name FROM tasks t " \
            "LEFT JOIN team_members tm ON t.assigned_to = tm.id WHERE 1=1"
    params: list = []
    if sprint_id is not None:
        query += " AND t.sprint_id = ?"
        params.append(sprint_id)
    if status:
        query += " AND t.status = ?"
        params.append(status)
    query += " ORDER BY t.id"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def complete_task(task_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET status = 'done', completed_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND status != 'done'",
        (task_id,),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def save_standup(member_id: int, content: str) -> bool:
    today = datetime.now(TZ).date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT OR REPLACE INTO standups (member_id, standup_date, content) "
            "VALUES (?, ?, ?)",
            (member_id, today, content),
        )
        conn.commit()
        success = True
    except sqlite3.Error:
        success = False
    conn.close()
    return success


def get_standup_status(date_str: str) -> tuple[list[int], list[int]]:
    """Return (submitted_telegram_ids, missing_telegram_ids) for a date."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT tm.telegram_id FROM standups s "
        "JOIN team_members tm ON s.member_id = tm.id "
        "WHERE s.standup_date = ?",
        (date_str,),
    )
    submitted = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT telegram_id FROM team_members WHERE is_active = 1"
    )
    all_members = [r[0] for r in cur.fetchall()]
    conn.close()

    missing = [m for m in all_members if m not in submitted]
    return submitted, missing


def add_meeting(title: str, meeting_date: str, meeting_time: str,
                created_by: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meetings (title, meeting_date, meeting_time, created_by) "
        "VALUES (?, ?, ?, ?)",
        (title, meeting_date, meeting_time, created_by),
    )
    conn.commit()
    meeting_id = cur.lastrowid
    conn.close()
    return meeting_id


def get_upcoming_meetings() -> list[dict]:
    today = datetime.now(TZ).date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM meetings WHERE meeting_date >= ? ORDER BY meeting_date, meeting_time",
        (today,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_pending_meeting_reminders() -> list[dict]:
    """Return meetings happening in ~1 hour that haven't had reminders sent."""
    now = datetime.now(TZ)
    target = now + timedelta(hours=1)
    target_date = target.strftime("%Y-%m-%d")
    target_time = target.strftime("%H:%M")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM meetings "
        "WHERE reminder_sent = 0 "
        "AND meeting_date = ? AND meeting_time <= ?",
        (target_date, target_time),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def mark_meeting_reminded(meeting_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE meetings SET reminder_sent = 1 WHERE id = ?", (meeting_id,))
    conn.commit()
    conn.close()


def add_event(title: str, description: str, event_date: str,
              event_time: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (title, description, event_date, event_time) "
        "VALUES (?, ?, ?, ?)",
        (title, description, event_date, event_time),
    )
    conn.commit()
    event_id = cur.lastrowid
    conn.close()
    return event_id


def toggle_rsvp(event_id: int, telegram_id: int, username: str | None,
                status: str) -> int:
    """Toggle RSVP and return current going count."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO event_rsvps (event_id, telegram_id, username, status) "
        "VALUES (?, ?, ?, ?)",
        (event_id, telegram_id, username, status),
    )
    # Update count
    cur.execute(
        "SELECT COUNT(*) FROM event_rsvps "
        "WHERE event_id = ? AND status = 'going'",
        (event_id,),
    )
    count = cur.fetchone()[0]
    cur.execute(
        "UPDATE events SET rsvp_count = ? WHERE id = ?",
        (count, event_id),
    )
    conn.commit()
    conn.close()
    return count


def get_content(content_type: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT content_text FROM content "
        "WHERE content_type = ? ORDER BY updated_at DESC LIMIT 1",
        (content_type,),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def set_content(content_type: str, text: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO content (content_type, content_text) VALUES (?, ?)",
        (content_type, text),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------------------------------------------------------------------------
# Handlers: /registerchat -- auto-detect group chat ID
# ---------------------------------------------------------------------------

async def cmd_registerchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save current group chat as TEAM_CHAT_ID (admin-only, groups only)."""
    global TEAM_CHAT_ID
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text("Эту команду нужно использовать в групповом чате.")
        return

    if ADMIN_IDS and user.id not in ADMIN_IDS:
        await update.message.reply_text("Только администратор может зарегистрировать чат.")
        return

    TEAM_CHAT_ID = chat.id
    # Persist to .env
    import pathlib
    env_path = pathlib.Path(__file__).parent / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    new_lines = [l for l in lines if not l.startswith("TEAM_CHAT_ID=")]
    new_lines.append(f"TEAM_CHAT_ID={chat.id}")
    env_path.write_text("\n".join(new_lines) + "\n")

    await update.message.reply_text(
        f"✅ Чат зарегистрирован как командный!\n"
        f"Chat ID: {chat.id}\n\n"
        f"Теперь я буду отправлять сюда стендапы, отчёты и напоминания."
    )


# ---------------------------------------------------------------------------
# Handlers: /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_or_create_member(user.id, user.username, user.first_name)

    chat_id = update.effective_chat.id
    is_team = chat_id == TEAM_CHAT_ID

    if is_team:
        text = (
            "THE DIP CLUB -- SCRUM Master\n\n"
            "Команды:\n"
            "/standup <текст> -- Заполнить стендап\n"
            "/addtask <задача> -- Добавить задачу\n"
            "/tasks -- Список задач\n"
            "/done <id> -- Закрыть задачу\n"
            "/sprint -- Текущий спринт\n"
            "/newsprint <название> -- Новый спринт\n"
            "/meet <дд.мм> <чч:мм> <тема> -- Встреча\n"
            "/meetings -- Список встреч\n"
            "/report -- Отчёт"
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("События", callback_data="menu_events"),
                InlineKeyboardButton("Меню недели", callback_data="menu_food"),
            ],
            [
                InlineKeyboardButton("Йога", callback_data="menu_yoga"),
                InlineKeyboardButton("Культура", callback_data="menu_culture"),
            ],
        ])
        text = (
            "Добро пожаловать в THE DIP CLUB!\n"
            "Николина Гора\n\n"
            "Выберите раздел:"
        )
        await update.message.reply_text(text, reply_markup=keyboard)
        return

    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Team Handlers: Standup
# ---------------------------------------------------------------------------

async def cmd_standup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    member_id = get_or_create_member(user.id, user.username, user.first_name)

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "Формат: /standup <что сделал вчера, план на сегодня, блокеры>"
        )
        return

    success = save_standup(member_id, text)
    if success:
        await update.message.reply_text(
            f"Стендап записан, {user.first_name}!"
        )
    else:
        await update.message.reply_text("Ошибка сохранения стендапа.")


# ---------------------------------------------------------------------------
# Team Handlers: Tasks
# ---------------------------------------------------------------------------

async def cmd_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    member_id = get_or_create_member(user.id, user.username, user.first_name)

    title = " ".join(context.args) if context.args else ""
    if not title:
        await update.message.reply_text("Формат: /addtask <описание задачи>")
        return

    sprint = get_active_sprint()
    sprint_id = sprint["id"] if sprint else None
    task_id = add_task(title, sprint_id, member_id)

    sprint_label = sprint["name"] if sprint else "без спринта"
    await update.message.reply_text(
        f"Задача #{task_id} добавлена ({sprint_label}):\n{title}"
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sprint = get_active_sprint()
    sprint_id = sprint["id"] if sprint else None

    tasks = get_tasks(sprint_id=sprint_id)
    if not tasks:
        await update.message.reply_text("Нет задач в текущем спринте.")
        return

    status_icons = {
        "todo": "[ ]",
        "in_progress": "[>>]",
        "done": "[ok]",
        "blocked": "[!!]",
    }

    lines = []
    if sprint:
        lines.append(f"{sprint['name']} ({sprint['start_date']} -- {sprint['end_date']})\n")

    for t in tasks:
        icon = status_icons.get(t["status"], "[ ]")
        assignee = t.get("assignee_name") or "---"
        lines.append(f"{icon} #{t['id']}: {t['title']} ({assignee})")

    done_count = sum(1 for t in tasks if t["status"] == "done")
    lines.append(f"\nВыполнено: {done_count}/{len(tasks)}")

    await update.message.reply_text("\n".join(lines))


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Формат: /done <task_id>")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID задачи должен быть числом.")
        return

    success = complete_task(task_id)
    if success:
        await update.message.reply_text(f"Задача #{task_id} выполнена!")
    else:
        await update.message.reply_text(f"Задача #{task_id} не найдена или уже выполнена.")


# ---------------------------------------------------------------------------
# Team Handlers: Sprint
# ---------------------------------------------------------------------------

async def cmd_sprint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sprint = get_active_sprint()
    if not sprint:
        await update.message.reply_text(
            "Нет активного спринта. Создайте: /newsprint <название>"
        )
        return

    tasks = get_tasks(sprint_id=sprint["id"])
    done = sum(1 for t in tasks if t["status"] == "done")
    total = len(tasks)

    await update.message.reply_text(
        f"Текущий спринт: {sprint['name']}\n"
        f"Период: {sprint['start_date']} -- {sprint['end_date']}\n"
        f"Задачи: {done}/{total} выполнено"
    )


async def cmd_newsprint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(context.args) if context.args else ""
    if not name:
        await update.message.reply_text("Формат: /newsprint <название>")
        return

    sprint_id = create_sprint(name)
    sprint = get_active_sprint()

    await update.message.reply_text(
        f"Новый спринт создан: {name}\n"
        f"ID: #{sprint_id}\n"
        f"Период: {sprint['start_date']} -- {sprint['end_date']}\n\n"
        f"Добавляйте задачи: /addtask <описание>"
    )


# ---------------------------------------------------------------------------
# Team Handlers: Report
# ---------------------------------------------------------------------------

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sprint = get_active_sprint()
    if not sprint:
        await update.message.reply_text("Нет активного спринта.")
        return

    tasks = get_tasks(sprint_id=sprint["id"])
    done_tasks = [t for t in tasks if t["status"] == "done"]
    ip_tasks = [t for t in tasks if t["status"] == "in_progress"]
    blocked_tasks = [t for t in tasks if t["status"] == "blocked"]
    todo_tasks = [t for t in tasks if t["status"] == "todo"]

    lines = [
        f"ОТЧЁТ {sprint['name']}",
        f"({sprint['start_date']} -- {sprint['end_date']})\n",
        f"Выполнено: {len(done_tasks)}/{len(tasks)}",
        f"В работе: {len(ip_tasks)}",
        f"Заблокировано: {len(blocked_tasks)}",
        f"В очереди: {len(todo_tasks)}",
        "",
    ]

    for t in done_tasks:
        lines.append(f"[ok] #{t['id']}: {t['title']}")
    for t in ip_tasks:
        lines.append(f"[>>] #{t['id']}: {t['title']}")
    for t in blocked_tasks:
        lines.append(f"[!!] #{t['id']}: {t['title']}")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Team Handlers: Meetings
# ---------------------------------------------------------------------------

async def cmd_meet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Schedule a meeting: /meet 05.04 15:00 Обсуждение навеса"""
    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /meet <дд.мм> <чч:мм> <тема>\n"
            "Пример: /meet 05.04 15:00 Обсуждение навеса"
        )
        return

    date_str = context.args[0]
    time_str = context.args[1]
    title = " ".join(context.args[2:])

    # Parse date (assume current year)
    try:
        now = datetime.now(TZ)
        day, month = date_str.split(".")
        meeting_date = f"{now.year}-{int(month):02d}-{int(day):02d}"
        # Validate time
        datetime.strptime(time_str, "%H:%M")
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат даты/времени.")
        return

    user = update.effective_user
    member_id = get_or_create_member(user.id, user.username, user.first_name)
    meeting_id = add_meeting(title, meeting_date, time_str, member_id)

    dt = datetime.strptime(meeting_date, "%Y-%m-%d")
    day_name = DAYS_RU[dt.weekday()]

    await update.message.reply_text(
        f"Встреча запланирована!\n\n"
        f"#{meeting_id}: {title}\n"
        f"Дата: {date_str} ({day_name})\n"
        f"Время: {time_str}\n"
        f"Место: THE DIP\n\n"
        f"Напоминание придёт за 1 час."
    )


async def cmd_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    meetings = get_upcoming_meetings()
    if not meetings:
        await update.message.reply_text("Нет запланированных встреч.")
        return

    lines = ["Запланированные встречи:\n"]
    for m in meetings:
        dt = datetime.strptime(m["meeting_date"], "%Y-%m-%d")
        day_name = DAYS_RU[dt.weekday()]
        lines.append(
            f"#{m['id']}: {m['title']}\n"
            f"   {dt.strftime('%d.%m')} ({day_name}) {m['meeting_time']}"
        )

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Community Handlers: Events
# ---------------------------------------------------------------------------

async def cmd_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create event: /event 06.04 19:00 Вечер живой музыки | Описание"""
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Только для администраторов.")
        return

    raw = " ".join(context.args) if context.args else ""
    if not raw or len(context.args) < 3:
        await update.message.reply_text(
            "Формат: /event <дд.мм> <чч:мм> <название> | <описание>\n"
            "Пример: /event 06.04 19:00 Вечер музыки | Живая джаз-группа на террасе"
        )
        return

    date_str = context.args[0]
    time_str = context.args[1]
    rest = " ".join(context.args[2:])

    if "|" in rest:
        title, description = rest.split("|", 1)
        title = title.strip()
        description = description.strip()
    else:
        title = rest
        description = ""

    try:
        now = datetime.now(TZ)
        day, month = date_str.split(".")
        event_date = f"{now.year}-{int(month):02d}-{int(day):02d}"
        datetime.strptime(time_str, "%H:%M")
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат даты/времени.")
        return

    event_id = add_event(title, description, event_date, time_str)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Пойду! (0)", callback_data=f"rsvp_going_{event_id}"),
            InlineKeyboardButton("Не смогу", callback_data=f"rsvp_no_{event_id}"),
        ],
    ])

    dt = datetime.strptime(event_date, "%Y-%m-%d")
    day_name = DAYS_RU[dt.weekday()]

    text = (
        f"{title.upper()}\n"
        f"{date_str} ({day_name}) {time_str}\n"
        f"THE DIP, Николина Гора\n"
    )
    if description:
        text += f"\n{description}\n"

    # Post to community channel if configured
    if COMMUNITY_CHAT_ID:
        await context.bot.send_message(
            chat_id=COMMUNITY_CHAT_ID,
            text=text,
            reply_markup=keyboard,
        )

    await update.message.reply_text(
        f"Событие #{event_id} создано и опубликовано!\n{text}",
        reply_markup=keyboard,
    )


async def cb_rsvp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle RSVP button presses."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = update.effective_user

    if data.startswith("rsvp_going_"):
        event_id = int(data.replace("rsvp_going_", ""))
        status = "going"
    elif data.startswith("rsvp_no_"):
        event_id = int(data.replace("rsvp_no_", ""))
        status = "not_going"
    else:
        return

    count = toggle_rsvp(event_id, user.id, user.username, status)

    # Update the button with new count
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Пойду! ({count})", callback_data=f"rsvp_going_{event_id}"
            ),
            InlineKeyboardButton("Не смогу", callback_data=f"rsvp_no_{event_id}"),
        ],
    ])

    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception:
        pass  # Message might not be editable

    if status == "going":
        await query.answer(f"Вы записались! Всего идут: {count}", show_alert=False)
    else:
        await query.answer("Записали, что не сможете.", show_alert=False)


# ---------------------------------------------------------------------------
# Community Handlers: Content
# ---------------------------------------------------------------------------

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    content = get_content("menu")
    if content:
        await update.message.reply_text(f"МЕНЮ НЕДЕЛИ\n\n{content}")
    else:
        await update.message.reply_text("Меню недели ещё не опубликовано.")


async def cmd_setmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Только для администраторов.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Формат: /setmenu <текст меню>")
        return

    set_content("menu", text)
    await update.message.reply_text("Меню недели обновлено!")


async def cmd_yoga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    content = get_content("yoga")
    if content:
        await update.message.reply_text(f"РАСПИСАНИЕ ЙОГИ\n\n{content}")
    else:
        await update.message.reply_text("Расписание йоги ещё не опубликовано.")


async def cmd_setyoga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Только для администраторов.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Формат: /setyoga <расписание>")
        return

    set_content("yoga", text)
    await update.message.reply_text("Расписание йоги обновлено!")


async def cmd_culture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    content = get_content("culture")
    if content:
        await update.message.reply_text(f"КУЛЬТУРНАЯ ПРОГРАММА\n\n{content}")
    else:
        await update.message.reply_text("Культурная программа ещё не опубликована.")


async def cmd_setculture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("Только для администраторов.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Формат: /setculture <программа>")
        return

    set_content("culture", text)
    await update.message.reply_text("Культурная программа обновлена!")


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder for weather -- can integrate with API later."""
    await update.message.reply_text(
        "ПОГОДА -- Николина Гора\n\n"
        "Для актуальной погоды интегрируем OpenWeather API.\n"
        "Пока смотрите: weather.yandex.ru"
    )


# ---------------------------------------------------------------------------
# Community Callbacks (menu buttons)
# ---------------------------------------------------------------------------

async def cb_menu_events(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    today = datetime.now(TZ).date().isoformat()
    cur.execute(
        "SELECT * FROM events WHERE event_date >= ? ORDER BY event_date LIMIT 5",
        (today,),
    )
    events = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not events:
        await query.edit_message_text("Нет предстоящих событий.")
        return

    lines = ["ПРЕДСТОЯЩИЕ СОБЫТИЯ\n"]
    for e in events:
        dt = datetime.strptime(e["event_date"], "%Y-%m-%d")
        day_name = DAYS_RU[dt.weekday()]
        lines.append(
            f"{e['title']}\n"
            f"  {dt.strftime('%d.%m')} ({day_name}) {e['event_time']}"
        )

    await query.edit_message_text("\n".join(lines))


async def cb_menu_food(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    content = get_content("menu")
    text = f"МЕНЮ НЕДЕЛИ\n\n{content}" if content else "Меню ещё не опубликовано."
    await query.edit_message_text(text)


async def cb_menu_yoga(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    content = get_content("yoga")
    text = f"РАСПИСАНИЕ ЙОГИ\n\n{content}" if content else "Расписание ещё не опубликовано."
    await query.edit_message_text(text)


async def cb_menu_culture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    content = get_content("culture")
    text = f"КУЛЬТУРНАЯ ПРОГРАММА\n\n{content}" if content else "Программа ещё не опубликована."
    await query.edit_message_text(text)


# ---------------------------------------------------------------------------
# Handlers: Help
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "THE DIP CLUB -- Помощь\n\n"
        "Команды команды:\n"
        "/standup <текст> -- Заполнить стендап\n"
        "/addtask <задача> -- Добавить задачу\n"
        "/tasks -- Список задач\n"
        "/done <id> -- Закрыть задачу\n"
        "/sprint -- Текущий спринт\n"
        "/newsprint <название> -- Новый спринт\n"
        "/report -- Отчёт\n"
        "/meet <дд.мм> <чч:мм> <тема> -- Встреча\n"
        "/meetings -- Список встреч\n\n"
        "Команды комьюнити:\n"
        "/menu -- Меню недели\n"
        "/yoga -- Расписание йоги\n"
        "/culture -- Культурная программа\n"
        "/weather -- Погода\n\n"
        "Админ-команды:\n"
        "/event <дд.мм> <чч:мм> <название> -- Событие\n"
        "/setmenu, /setyoga, /setculture -- Обновить контент"
    )


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

async def job_morning_standup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send daily standup prompt to team chat (Mon-Fri 9:00)."""
    if TEAM_CHAT_ID == 0:
        return

    now = datetime.now(TZ)
    if now.weekday() >= 5:  # Saturday/Sunday
        return

    await context.bot.send_message(
        chat_id=TEAM_CHAT_ID,
        text=(
            "Доброе утро, THE DIP!\n\n"
            "Время стендапа. Напишите:\n"
            "1. Что сделал вчера?\n"
            "2. Что буду делать сегодня?\n"
            "3. Есть ли блокеры?\n\n"
            "Формат: /standup <ваш текст>"
        ),
    )


async def job_standup_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remind team members who haven't submitted standup (18:00)."""
    if TEAM_CHAT_ID == 0:
        return

    now = datetime.now(TZ)
    if now.weekday() >= 5:
        return

    today_str = now.date().isoformat()
    _, missing = get_standup_status(today_str)

    if not missing:
        return

    # Get usernames for missing members
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    placeholders = ",".join("?" * len(missing))
    cur.execute(
        f"SELECT username, first_name FROM team_members "
        f"WHERE telegram_id IN ({placeholders})",
        missing,
    )
    names = []
    for row in cur.fetchall():
        name = f"@{row[0]}" if row[0] else row[1] or "???"
        names.append(name)
    conn.close()

    if names:
        await context.bot.send_message(
            chat_id=TEAM_CHAT_ID,
            text=f"Напоминание: стендап не заполнен!\n{', '.join(names)}",
        )


async def job_sprint_planning(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Monday 10:00 sprint planning prompt."""
    if TEAM_CHAT_ID == 0:
        return

    now = datetime.now(TZ)
    if now.weekday() != 0:  # Only Monday
        return

    sprint = get_active_sprint()
    if sprint:
        tasks = get_tasks(sprint_id=sprint["id"])
        done = sum(1 for t in tasks if t["status"] == "done")
        total = len(tasks)

        incomplete = [t for t in tasks if t["status"] in ("todo", "in_progress")]
        lines = [
            "Начинаем Sprint Planning!\n",
            f"Текущий спринт: {sprint['name']}",
            f"Выполнено: {done}/{total}\n",
        ]
        if incomplete:
            lines.append("Нерешённые задачи перенесены:")
            for t in incomplete:
                lines.append(f"  - #{t['id']}: {t['title']}")
            lines.append("")

        lines.append("Добавляйте задачи: /addtask <описание>")

        await context.bot.send_message(
            chat_id=TEAM_CHAT_ID,
            text="\n".join(lines),
        )
    else:
        await context.bot.send_message(
            chat_id=TEAM_CHAT_ID,
            text=(
                "Начинаем Sprint Planning!\n\n"
                "Нет активного спринта.\n"
                "Создайте: /newsprint <название>"
            ),
        )


async def job_friday_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Friday 17:00 weekly report."""
    if TEAM_CHAT_ID == 0:
        return

    now = datetime.now(TZ)
    if now.weekday() != 4:  # Only Friday
        return

    sprint = get_active_sprint()
    if not sprint:
        return

    tasks = get_tasks(sprint_id=sprint["id"])
    done_tasks = [t for t in tasks if t["status"] == "done"]
    ip_tasks = [t for t in tasks if t["status"] == "in_progress"]
    blocked_tasks = [t for t in tasks if t["status"] == "blocked"]

    lines = [
        f"ОТЧЁТ {sprint['name']}",
        f"({sprint['start_date']} -- {sprint['end_date']})\n",
        f"Выполнено: {len(done_tasks)}/{len(tasks)}",
        f"В работе: {len(ip_tasks)}",
        f"Заблокировано: {len(blocked_tasks)}\n",
    ]

    for t in done_tasks:
        lines.append(f"[ok] #{t['id']}: {t['title']}")
    for t in ip_tasks:
        lines.append(f"[>>] #{t['id']}: {t['title']}")
    for t in blocked_tasks:
        lines.append(f"[!!] #{t['id']}: {t['title']}")

    lines.append("\nХорошей пятницы, команда!")

    await context.bot.send_message(
        chat_id=TEAM_CHAT_ID,
        text="\n".join(lines),
    )


async def job_meeting_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check for upcoming meetings and send reminders."""
    if TEAM_CHAT_ID == 0:
        return

    meetings = get_pending_meeting_reminders()
    for m in meetings:
        try:
            await context.bot.send_message(
                chat_id=TEAM_CHAT_ID,
                text=(
                    f"Напоминание! Через 1 час встреча:\n\n"
                    f"{m['title']}\n"
                    f"Время: {m['meeting_time']}\n"
                    f"Место: {m['location']}"
                ),
            )
            mark_meeting_reminded(m["id"])
        except Exception as exc:
            logger.error("Failed to send meeting reminder %s: %s", m["id"], exc)


# ---------------------------------------------------------------------------
# Post-init: set commands & schedule jobs
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Set bot commands and schedule recurring jobs."""
    await application.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("standup", "Заполнить стендап"),
        BotCommand("addtask", "Добавить задачу"),
        BotCommand("tasks", "Список задач"),
        BotCommand("done", "Закрыть задачу"),
        BotCommand("sprint", "Текущий спринт"),
        BotCommand("newsprint", "Новый спринт"),
        BotCommand("report", "Отчёт"),
        BotCommand("meet", "Запланировать встречу"),
        BotCommand("meetings", "Список встреч"),
        BotCommand("menu", "Меню недели"),
        BotCommand("yoga", "Расписание йоги"),
        BotCommand("culture", "Культурная программа"),
        BotCommand("weather", "Погода"),
        BotCommand("event", "Создать событие (админ)"),
        BotCommand("help", "Справка"),
    ])

    jq = application.job_queue

    # Daily standup prompt: Mon-Fri 9:00
    jq.run_daily(
        job_morning_standup,
        time=time(hour=9, minute=0, tzinfo=TZ),
    )

    # Standup reminder: Mon-Fri 18:00
    jq.run_daily(
        job_standup_reminder,
        time=time(hour=18, minute=0, tzinfo=TZ),
    )

    # Sprint planning: Monday 10:00
    jq.run_daily(
        job_sprint_planning,
        time=time(hour=10, minute=0, tzinfo=TZ),
    )

    # Friday report: 17:00
    jq.run_daily(
        job_friday_report,
        time=time(hour=17, minute=0, tzinfo=TZ),
    )

    # Meeting reminders: every 15 minutes
    jq.run_repeating(job_meeting_reminders, interval=900, first=10)

    logger.info("DIP CLUB Bot initialized. Jobs scheduled.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN environment variable is not set. "
            "Run: BOT_TOKEN=<your_token> python bot.py"
        )

    init_db()

    app = Application.builder().token(token).post_init(post_init).build()

    # Team commands
    app.add_handler(CommandHandler("registerchat", cmd_registerchat))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("standup", cmd_standup))
    app.add_handler(CommandHandler("addtask", cmd_addtask))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("sprint", cmd_sprint))
    app.add_handler(CommandHandler("newsprint", cmd_newsprint))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("meet", cmd_meet))
    app.add_handler(CommandHandler("meetings", cmd_meetings))

    # Community commands
    app.add_handler(CommandHandler("event", cmd_event))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("setmenu", cmd_setmenu))
    app.add_handler(CommandHandler("yoga", cmd_yoga))
    app.add_handler(CommandHandler("setyoga", cmd_setyoga))
    app.add_handler(CommandHandler("culture", cmd_culture))
    app.add_handler(CommandHandler("setculture", cmd_setculture))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("help", cmd_help))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(cb_rsvp, pattern=r"^rsvp_"))
    app.add_handler(CallbackQueryHandler(cb_menu_events, pattern="^menu_events$"))
    app.add_handler(CallbackQueryHandler(cb_menu_food, pattern="^menu_food$"))
    app.add_handler(CallbackQueryHandler(cb_menu_yoga, pattern="^menu_yoga$"))
    app.add_handler(CallbackQueryHandler(cb_menu_culture, pattern="^menu_culture$"))

    logger.info("Starting DIP CLUB Bot...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

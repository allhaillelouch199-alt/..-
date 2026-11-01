import aiosqlite
from datetime import datetime, timedelta
from config import DB_PATH

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_blocked INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS blocked_entities (
            entity_id INTEGER PRIMARY KEY,
            entity_type TEXT,
            blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS forced_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT UNIQUE,
            channel_link TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_cliches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_limits (
            user_id INTEGER PRIMARY KEY,
            max_numbers INTEGER DEFAULT 1,
            max_groups INTEGER DEFAULT 1,
            max_cliches INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            session_name TEXT UNIQUE NOT NULL,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS groups_tbl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            group_title TEXT
        );
        CREATE TABLE IF NOT EXISTS cliches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bundles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER UNIQUE NOT NULL,
            group_id INTEGER NOT NULL,
            cliche_id INTEGER NOT NULL,
            interval INTEGER DEFAULT 60,
            is_active INTEGER DEFAULT 1,
            failure_count INTEGER DEFAULT 0,
            coma_until TEXT,
            flood_until TEXT
        );
        """)
        await db.commit()

        # Migration for existing DBs
        for col, definition in [
            ("failure_count", "INTEGER DEFAULT 0"),
            ("coma_until", "TEXT"),
            ("flood_until", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE bundles ADD COLUMN {col} {definition}")
                await db.commit()
            except aiosqlite.OperationalError:
                pass  # already exists

async def _fetchone(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, args)
        row = await cur.fetchone()
        return dict(row) if row else None

async def _fetchall(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, args)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def _exec(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, args)
        await db.commit()
        return cur.lastrowid

# -------- Users --------
async def ensure_user(user_id):
    await _exec("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    await _exec("INSERT OR IGNORE INTO user_limits(user_id) VALUES(?)", (user_id,))

async def is_blocked(entity_id):
    r = await _fetchone("SELECT 1 FROM blocked_entities WHERE entity_id=?", (entity_id,))
    return r is not None

async def block_entity(entity_id, etype):
    await _exec("INSERT OR IGNORE INTO blocked_entities(entity_id, entity_type) VALUES(?,?)", (entity_id, etype))

async def unblock_entity(entity_id):
    await _exec("DELETE FROM blocked_entities WHERE entity_id=?", (entity_id,))

# -------- Admin cliches --------
async def add_admin_cliche(text):
    await _exec("INSERT INTO admin_cliches(text) VALUES(?)", (text,))

async def get_all_admin_cliches():
    return await _fetchall("SELECT * FROM admin_cliches")

# -------- Forced channels --------
async def add_forced_channel(cid, link):
    await _exec("INSERT OR IGNORE INTO forced_channels(channel_id, channel_link) VALUES(?,?)", (cid, link))

async def get_forced_channels():
    return await _fetchall("SELECT * FROM forced_channels")

# -------- Limits --------
async def set_limits(user_id, mn, mg, mc):
    await _exec("INSERT INTO user_limits(user_id,max_numbers,max_groups,max_cliches) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET max_numbers=?, max_groups=?, max_cliches=?",
                (user_id, mn, mg, mc, mn, mg, mc))

async def get_limits(user_id):
    r = await _fetchone("SELECT * FROM user_limits WHERE user_id=?", (user_id,))
    return r or {"max_numbers": 1, "max_groups": 1, "max_cliches": 1}

# -------- Sessions --------
async def add_session(user_id, phone, session_name):
    return await _exec("INSERT INTO sessions(user_id,phone,session_name) VALUES(?,?,?)",
                       (user_id, phone, session_name))

async def get_user_sessions(user_id):
    return await _fetchall("SELECT * FROM sessions WHERE user_id=? AND is_active=1", (user_id,))

async def get_session(sid):
    return await _fetchone("SELECT * FROM sessions WHERE id=?", (sid,))

async def get_all_active_sessions():
    return await _fetchall("SELECT * FROM sessions WHERE is_active=1")

# -------- Groups --------
async def add_group(user_id, gid, title):
    return await _exec("INSERT INTO groups_tbl(user_id,group_id,group_title) VALUES(?,?,?)",
                       (user_id, gid, title))

async def get_user_groups(user_id):
    return await _fetchall("SELECT * FROM groups_tbl WHERE user_id=?", (user_id,))

async def get_group(gid):
    return await _fetchone("SELECT * FROM groups_tbl WHERE id=?", (gid,))

# -------- Cliches --------
async def add_cliche(user_id, text):
    return await _exec("INSERT INTO cliches(user_id,text) VALUES(?,?)", (user_id, text))

async def get_user_cliches(user_id):
    return await _fetchall("SELECT * FROM cliches WHERE user_id=?", (user_id,))

async def get_cliche(cid):
    return await _fetchone("SELECT * FROM cliches WHERE id=?", (cid,))

# -------- Bundles --------
async def add_bundle(user_id, session_id, group_id, cliche_id, interval):
    return await _exec(
        "INSERT INTO bundles(user_id,session_id,group_id,cliche_id,interval) VALUES(?,?,?,?,?)",
        (user_id, session_id, group_id, cliche_id, interval))

async def get_user_bundles(user_id):
    return await _fetchall("SELECT * FROM bundles WHERE user_id=?", (user_id,))

async def get_bundle(bid):
    return await _fetchone("SELECT * FROM bundles WHERE id=?", (bid,))

async def get_active_bundles():
    return await _fetchall("SELECT * FROM bundles WHERE is_active=1")

async def get_bundle_by_session(session_id):
    return await _fetchone("SELECT * FROM bundles WHERE session_id=?", (session_id,))

async def delete_bundle(bid):
    await _exec("DELETE FROM bundles WHERE id=?", (bid,))

# -------- Failure / Coma tracking --------
async def increment_bundle_failure(bid):
    await _exec("UPDATE bundles SET failure_count = failure_count + 1 WHERE id=?", (bid,))
    r = await _fetchone("SELECT failure_count FROM bundles WHERE id=?", (bid,))
    return r["failure_count"] if r else 0

async def reset_bundle_failures(bid):
    await _exec("UPDATE bundles SET failure_count = 0 WHERE id=?", (bid,))

async def set_bundle_coma(bid, hours=6):
    until = (datetime.now() + timedelta(hours=hours)).isoformat()
    await _exec("UPDATE bundles SET coma_until=? WHERE id=?", (until, bid))

async def clear_bundle_coma(bid):
    await _exec("UPDATE bundles SET coma_until=NULL WHERE id=?", (bid,))

async def set_bundle_flood(bid, seconds):
    until = (datetime.now() + timedelta(seconds=seconds)).isoformat()
    await _exec("UPDATE bundles SET flood_until=? WHERE id=?", (until, bid))

async def clear_bundle_flood(bid):
    await _exec("UPDATE bundles SET flood_until=NULL WHERE id=?", (bid,))

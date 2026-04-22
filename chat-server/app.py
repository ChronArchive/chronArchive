#!/usr/bin/env python3
"""
ChronoGraph Chat API — Flask instant messenger backend.
Supports HTTP and HTTPS, works with iOS 3 through modern iOS via long-polling.
"""

from flask import Flask, request, jsonify, make_response
import sqlite3, hashlib, hmac, secrets, time, os, threading, re

app = Flask(__name__)

DB_PATH = os.environ.get('CHAT_DB', '/opt/chronograph-chat/chat.db')
DB_LOCK = threading.Lock()

# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at   INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            text         TEXT NOT NULL,
            created_at   INTEGER DEFAULT (strftime('%s','now')),
            read_at      INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_msg_conv
            ON messages (from_user_id, to_user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_msg_unread
            ON messages (to_user_id, read_at);
    ''')
    conn.commit()
    conn.close()

# ── Auth helpers ───────────────────────────────────────────────────────────────

PBKDF2_SALT = b'chronograph-chat-v1'

def hash_password(password):
    return hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), PBKDF2_SALT, 200_000
    ).hex()

def verify_password(password, stored):
    return hmac.compare_digest(hash_password(password), stored)

def current_user(req):
    """Returns user_id or None. Accepts X-CG-Token header or cg_session cookie."""
    token = req.headers.get('X-CG-Token') or req.cookies.get('cg_session')
    if not token or len(token) != 64:
        return None
    conn = get_db()
    row = conn.execute(
        'SELECT user_id FROM sessions WHERE token=? AND expires_at>?',
        (token, int(time.time()))
    ).fetchone()
    conn.close()
    return row['user_id'] if row else None

def require_auth(req):
    uid = current_user(req)
    if not uid:
        return None, (jsonify({'ok': False, 'error': 'Not logged in'}), 401)
    return uid, None

# ── CORS ───────────────────────────────────────────────────────────────────────

ALLOWED_ORIGINS = {
    'https://beta.chronarchive.com',
    'http://beta.chronarchive.com',
    'https://chronarchive.com',
    'http://chronarchive.com',
    'https://chat.chronarchive.com',
    'http://chat.chronarchive.com',
}

def add_cors(resp):
    origin = request.headers.get('Origin', '')
    # 'null' = file:// origin from iOS app bundle — allow it
    if origin in ALLOWED_ORIGINS or origin == 'null' or not origin:
        resp.headers['Access-Control-Allow-Origin'] = origin or '*'
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-CG-Token'
    return resp

@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS':
        return add_cors(make_response('', 204))

@app.after_request
def after(resp):
    return add_cors(resp)

# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True, 'time': int(time.time())})


@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not re.match(r'^[a-zA-Z0-9_]{2,32}$', username):
        return jsonify({'ok': False, 'error': 'Username: 2-32 letters, numbers or _'}), 400
    if len(password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters'}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash) VALUES (?,?)',
            (username, hash_password(password))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': 'Username already taken'}), 409

    row = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    user_id = row['id']
    token = secrets.token_hex(32)
    conn.execute(
        'INSERT INTO sessions VALUES (?,?,?)',
        (token, user_id, int(time.time()) + 30 * 24 * 3600)
    )
    conn.commit()
    conn.close()

    resp = jsonify({'ok': True, 'token': token, 'user_id': user_id, 'username': username})
    resp.set_cookie('cg_session', token, max_age=30*24*3600, httponly=True, path='/')
    return resp, 201


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    conn = get_db()
    row = conn.execute(
        'SELECT id, password_hash FROM users WHERE username=?', (username,)
    ).fetchone()
    conn.close()

    if not row or not verify_password(password, row['password_hash']):
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401

    token = secrets.token_hex(32)
    conn = get_db()
    conn.execute(
        'INSERT INTO sessions VALUES (?,?,?)',
        (token, row['id'], int(time.time()) + 30 * 24 * 3600)
    )
    conn.commit()
    conn.close()

    resp = jsonify({'ok': True, 'token': token, 'user_id': row['id'], 'username': username})
    resp.set_cookie('cg_session', token, max_age=30*24*3600, httponly=True, path='/')
    return resp


@app.route('/api/logout', methods=['POST'])
def logout():
    token = request.headers.get('X-CG-Token') or request.cookies.get('cg_session')
    if token:
        conn = get_db()
        conn.execute('DELETE FROM sessions WHERE token=?', (token,))
        conn.commit()
        conn.close()
    resp = jsonify({'ok': True})
    resp.delete_cookie('cg_session')
    return resp


@app.route('/api/me')
def me():
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT id, username, created_at FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': row['id'], 'username': row['username']})


@app.route('/api/users')
def users():
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    rows = conn.execute(
        'SELECT id, username FROM users WHERE id!=? ORDER BY username COLLATE NOCASE', (uid,)
    ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'users': [{'id': r['id'], 'username': r['username']} for r in rows]})


@app.route('/api/conversations')
def conversations():
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    rows = conn.execute('''
        SELECT u.id, u.username,
               m.text        AS last_text,
               m.created_at  AS last_at,
               m.from_user_id,
               (SELECT COUNT(*) FROM messages
                WHERE to_user_id=:uid AND from_user_id=u.id AND read_at IS NULL) AS unread
        FROM users u
        JOIN messages m ON m.id = (
            SELECT id FROM messages
            WHERE (from_user_id=:uid AND to_user_id=u.id)
               OR (from_user_id=u.id   AND to_user_id=:uid)
            ORDER BY created_at DESC LIMIT 1
        )
        WHERE u.id != :uid
        ORDER BY last_at DESC
    ''', {'uid': uid}).fetchall()
    conn.close()
    return jsonify({'ok': True, 'conversations': [dict(r) for r in rows]})


@app.route('/api/messages', methods=['GET'])
def get_messages():
    uid, err = require_auth(request)
    if err: return err

    with_id  = request.args.get('with',     type=int)
    after_id = request.args.get('after_id', 0, type=int)

    if not with_id:
        return jsonify({'ok': False, 'error': 'Missing ?with='}), 400

    # Initial load (after_id=0): return last 50 messages immediately, no long-poll
    if after_id == 0:
        conn = get_db()
        rows = conn.execute('''
            SELECT id, from_user_id, to_user_id, text, created_at FROM messages
            WHERE (from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?)
            ORDER BY created_at DESC LIMIT 50
        ''', (uid, with_id, with_id, uid)).fetchall()
        # mark incoming as read
        conn.execute(
            'UPDATE messages SET read_at=? WHERE to_user_id=? AND from_user_id=? AND read_at IS NULL',
            (int(time.time()), uid, with_id)
        )
        conn.commit()
        conn.close()
        msgs = list(reversed([dict(r) for r in rows]))
        return jsonify({'ok': True, 'messages': msgs})

    # Subsequent polls: long-poll up to 25s for new messages
    deadline = time.time() + 25
    while True:
        conn = get_db()
        rows = conn.execute('''
            SELECT id, from_user_id, to_user_id, text, created_at FROM messages
            WHERE ((from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?))
              AND id > ?
            ORDER BY created_at ASC
        ''', (uid, with_id, with_id, uid, after_id)).fetchall()
        conn.execute(
            'UPDATE messages SET read_at=? WHERE to_user_id=? AND from_user_id=? AND read_at IS NULL',
            (int(time.time()), uid, with_id)
        )
        conn.commit()
        conn.close()

        if rows or time.time() >= deadline:
            return jsonify({'ok': True, 'messages': [dict(r) for r in rows]})
        time.sleep(0.5)


@app.route('/api/messages', methods=['POST'])
def send_message():
    uid, err = require_auth(request)
    if err: return err

    data  = request.get_json(force=True, silent=True) or {}
    to_id = data.get('to_user_id')
    text  = (data.get('text') or '').strip()

    if not to_id or not text:
        return jsonify({'ok': False, 'error': 'Missing to_user_id or text'}), 400
    if len(text) > 2000:
        return jsonify({'ok': False, 'error': 'Message too long (max 2000 chars)'}), 400
    if to_id == uid:
        return jsonify({'ok': False, 'error': 'Cannot message yourself'}), 400

    conn = get_db()
    if not conn.execute('SELECT id FROM users WHERE id=?', (to_id,)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    cur = conn.execute(
        'INSERT INTO messages (from_user_id, to_user_id, text) VALUES (?,?,?)',
        (uid, to_id, text)
    )
    conn.commit()
    msg = dict(conn.execute('SELECT * FROM messages WHERE id=?', (cur.lastrowid,)).fetchone())
    conn.close()
    return jsonify({'ok': True, 'message': msg}), 201


if __name__ == '__main__':
    init_db()
    print('ChronoGraph Chat API starting on :5000')
    app.run(host='0.0.0.0', port=5000, threaded=True)

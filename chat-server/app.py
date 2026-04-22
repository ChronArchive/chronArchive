#!/usr/bin/env python3
"""
ChronoGraph Chat API — Flask instant messenger backend.
Supports HTTP and HTTPS, works with iOS 3 through modern iOS via long-polling.

Features: accounts, messaging, read receipts, friends/blocks, profiles,
          message dedup, admin console, Web Push notifications, message filter.
"""

from flask import Flask, request, jsonify, make_response
import sqlite3, hashlib, hmac, secrets, time, os, threading, re, base64, json

app = Flask(__name__)

DB_PATH  = os.environ.get('CHAT_DB', '/opt/chronograph-chat/chat.db')
DB_LOCK  = threading.Lock()

# ── Bad-word filter (edit list as needed) ─────────────────────────────────────
FILTER_WORDS = []   # add strings to auto-censor, e.g. ['badword', 'spam']

def apply_filter(text):
    for word in FILTER_WORDS:
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        text = pattern.sub('*' * len(word), text)
    return text

# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            bio           TEXT DEFAULT '',
            avatar_b64    TEXT DEFAULT '',
            is_admin      INTEGER DEFAULT 0,
            is_banned     INTEGER DEFAULT 0,
            created_at    INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id    TEXT,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            text         TEXT NOT NULL,
            created_at   INTEGER DEFAULT (strftime('%s','now')),
            read_at      INTEGER,
            deleted      INTEGER DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_clientid
            ON messages (client_id) WHERE client_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_msg_conv
            ON messages (from_user_id, to_user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_msg_unread
            ON messages (to_user_id, read_at);
        CREATE TABLE IF NOT EXISTS friends (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(from_user_id, to_user_id)
        );
        CREATE TABLE IF NOT EXISTS blocks (
            blocker_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY(blocker_id, blocked_id)
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            endpoint  TEXT NOT NULL,
            p256dh    TEXT NOT NULL,
            auth      TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(user_id, endpoint)
        );
    ''')
    conn.commit()
    conn.close()

# ── Auth helpers ───────────────────────────────────────────────────────────────

PBKDF2_SALT = b'chronograph-chat-v1'

def hash_password(password):
    return hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), PBKDF2_SALT, 200_000
    ).hex()

def verify_password(password, stored_hash):
    return hmac.compare_digest(hash_password(password), stored_hash)

def require_auth(req):
    token = req.headers.get('X-CG-Token') or req.cookies.get('cg_session')
    if not token:
        return None, (jsonify({'ok': False, 'error': 'Not authenticated'}), 401)
    conn = get_db()
    row = conn.execute(
        'SELECT user_id FROM sessions WHERE token=? AND expires_at>?',
        (token, int(time.time()))
    ).fetchone()
    conn.close()
    if not row:
        return None, (jsonify({'ok': False, 'error': 'Session expired'}), 401)
    return row['user_id'], None

def require_admin(req):
    uid, err = require_auth(req)
    if err: return None, err
    conn = get_db()
    row = conn.execute('SELECT is_admin, is_banned FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    if not row or not row['is_admin']:
        return None, (jsonify({'ok': False, 'error': 'Forbidden'}), 403)
    return uid, None

# ── CORS ───────────────────────────────────────────────────────────────────────

ALLOWED_ORIGINS = {
    'https://chronarchive.com',
    'http://chronarchive.com',
    'https://beta.chronarchive.com',
    'http://beta.chronarchive.com',
    'https://www.chronarchive.com',
    'http://www.chronarchive.com',
    'https://chat.chronarchive.com',
    'http://chat.chronarchive.com',
}

def add_cors(resp):
    origin = request.headers.get('Origin', '')
    if origin in ALLOWED_ORIGINS or origin == 'null' or not origin:
        resp.headers['Access-Control-Allow-Origin']      = origin or '*'
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-CG-Token'
    return resp

@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS':
        return add_cors(make_response('', 204))

@app.after_request
def after(resp):
    return add_cors(resp)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    from flask import redirect
    return redirect('https://chronarchive.com/chat.html', code=302)

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True, 'time': int(time.time())})

# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    data     = request.get_json(force=True, silent=True) or {}
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

    row    = conn.execute('SELECT id, is_admin FROM users WHERE username=?', (username,)).fetchone()
    uid    = row['id']
    token  = secrets.token_hex(32)
    conn.execute('INSERT INTO sessions VALUES (?,?,?)',
                 (token, uid, int(time.time()) + 30 * 24 * 3600))
    conn.commit()
    conn.close()

    resp = jsonify({'ok': True, 'token': token, 'user_id': uid,
                    'username': username, 'is_admin': bool(row['is_admin'])})
    resp.set_cookie('cg_session', token, max_age=30*24*3600, httponly=True, path='/')
    return resp, 201


@app.route('/api/login', methods=['POST'])
def login():
    data     = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    conn = get_db()
    row  = conn.execute(
        'SELECT id, password_hash, is_admin, is_banned FROM users WHERE username=?', (username,)
    ).fetchone()
    conn.close()

    if not row or not verify_password(password, row['password_hash']):
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401
    if row['is_banned']:
        return jsonify({'ok': False, 'error': 'This account has been suspended'}), 403

    token = secrets.token_hex(32)
    conn  = get_db()
    conn.execute('INSERT INTO sessions VALUES (?,?,?)',
                 (token, row['id'], int(time.time()) + 30 * 24 * 3600))
    conn.commit()
    conn.close()

    resp = jsonify({'ok': True, 'token': token, 'user_id': row['id'],
                    'username': username, 'is_admin': bool(row['is_admin'])})
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
    row  = conn.execute(
        'SELECT id, username, bio, avatar_b64, is_admin, created_at FROM users WHERE id=?', (uid,)
    ).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': row['id'], 'username': row['username'],
                    'bio': row['bio'] or '', 'avatar_b64': row['avatar_b64'] or '',
                    'is_admin': bool(row['is_admin'])})


@app.route('/api/me', methods=['PUT'])
def update_me():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}

    bio        = (data.get('bio') or '')[:160]
    avatar_b64 = (data.get('avatar_b64') or '')

    # Validate base64 image (must be data URI or empty)
    if avatar_b64 and not re.match(r'^data:image/(jpeg|png|gif|webp);base64,[A-Za-z0-9+/=]+$', avatar_b64):
        return jsonify({'ok': False, 'error': 'Invalid avatar format'}), 400
    # Limit avatar to ~200 KB base64
    if len(avatar_b64) > 270000:
        return jsonify({'ok': False, 'error': 'Avatar too large (max ~200 KB)'}), 400

    conn = get_db()
    conn.execute('UPDATE users SET bio=?, avatar_b64=? WHERE id=?', (bio, avatar_b64, uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/me/password', methods=['POST'])
def change_password():
    uid, err = require_auth(request)
    if err: return err
    data     = request.get_json(force=True, silent=True) or {}
    old_pass = data.get('old_password') or ''
    new_pass = data.get('new_password') or ''

    if len(new_pass) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters'}), 400

    conn = get_db()
    row  = conn.execute('SELECT password_hash FROM users WHERE id=?', (uid,)).fetchone()
    if not verify_password(old_pass, row['password_hash']):
        conn.close()
        return jsonify({'ok': False, 'error': 'Current password incorrect'}), 401
    conn.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(new_pass), uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Users & search ─────────────────────────────────────────────────────────────

@app.route('/api/users')
def users():
    uid, err = require_auth(request)
    if err: return err
    q = (request.args.get('q') or '').strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT id, username, bio, avatar_b64 FROM users WHERE id!=? AND is_banned=0"
            " AND username LIKE ? COLLATE NOCASE ORDER BY username COLLATE NOCASE LIMIT 30",
            (uid, '%' + q + '%')
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, username, bio, avatar_b64 FROM users WHERE id!=? AND is_banned=0"
            " ORDER BY username COLLATE NOCASE",
            (uid,)
        ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'users': [
        {'id': r['id'], 'username': r['username'],
         'bio': r['bio'] or '', 'avatar_b64': r['avatar_b64'] or ''}
        for r in rows
    ]})


@app.route('/api/users/<int:target_id>')
def get_user(target_id):
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    row  = conn.execute(
        'SELECT id, username, bio, avatar_b64, created_at FROM users WHERE id=? AND is_banned=0',
        (target_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    return jsonify({'ok': True, 'user': {
        'id': row['id'], 'username': row['username'],
        'bio': row['bio'] or '', 'avatar_b64': row['avatar_b64'] or '',
        'created_at': row['created_at']
    }})

# ── Friends ────────────────────────────────────────────────────────────────────

@app.route('/api/friends')
def list_friends():
    uid, err = require_auth(request)
    if err: return err
    conn  = get_db()
    # accepted friends (either direction)
    rows  = conn.execute('''
        SELECT u.id, u.username, u.bio, u.avatar_b64,
               f.status, f.from_user_id, f.to_user_id
        FROM friends f
        JOIN users u ON u.id = CASE WHEN f.from_user_id=:uid THEN f.to_user_id ELSE f.from_user_id END
        WHERE (f.from_user_id=:uid OR f.to_user_id=:uid)
          AND u.is_banned=0
        ORDER BY u.username COLLATE NOCASE
    ''', {'uid': uid}).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            'id': r['id'], 'username': r['username'],
            'bio': r['bio'] or '', 'avatar_b64': r['avatar_b64'] or '',
            'status': r['status'],
            'direction': 'outgoing' if r['from_user_id'] == uid else 'incoming'
        })
    return jsonify({'ok': True, 'friends': result})


@app.route('/api/friends/<int:target_id>', methods=['POST'])
def friend_request(target_id):
    uid, err = require_auth(request)
    if err: return err
    if target_id == uid:
        return jsonify({'ok': False, 'error': 'Cannot friend yourself'}), 400
    conn = get_db()
    # check not blocked
    if conn.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',
                    (uid, target_id, target_id, uid)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Cannot send request'}), 403
    # check if reverse pending — auto-accept
    existing = conn.execute(
        'SELECT id, status FROM friends WHERE from_user_id=? AND to_user_id=?',
        (target_id, uid)
    ).fetchone()
    if existing and existing['status'] == 'pending':
        conn.execute('UPDATE friends SET status=? WHERE id=?', ('accepted', existing['id']))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'status': 'accepted'})
    try:
        conn.execute('INSERT INTO friends (from_user_id, to_user_id, status) VALUES (?,?,?)',
                     (uid, target_id, 'pending'))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'ok': False, 'error': 'Request already sent'}), 409
    conn.close()
    return jsonify({'ok': True, 'status': 'pending'})


@app.route('/api/friends/<int:target_id>', methods=['DELETE'])
def remove_friend(target_id):
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    conn.execute('''DELETE FROM friends WHERE
        (from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?)''',
        (uid, target_id, target_id, uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Blocking ───────────────────────────────────────────────────────────────────

@app.route('/api/blocks', methods=['GET'])
def list_blocks():
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    rows = conn.execute(
        'SELECT u.id, u.username FROM blocks b JOIN users u ON u.id=b.blocked_id WHERE b.blocker_id=?', (uid,)
    ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'blocks': [{'id': r['id'], 'username': r['username']} for r in rows]})


@app.route('/api/blocks/<int:target_id>', methods=['POST'])
def block_user(target_id):
    uid, err = require_auth(request)
    if err: return err
    if target_id == uid:
        return jsonify({'ok': False, 'error': 'Cannot block yourself'}), 400
    conn = get_db()
    # remove any friendship
    conn.execute('''DELETE FROM friends WHERE
        (from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?)''',
        (uid, target_id, target_id, uid))
    try:
        conn.execute('INSERT INTO blocks (blocker_id, blocked_id) VALUES (?,?)', (uid, target_id))
    except sqlite3.IntegrityError:
        pass
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/blocks/<int:target_id>', methods=['DELETE'])
def unblock_user(target_id):
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    conn.execute('DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?', (uid, target_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Conversations ──────────────────────────────────────────────────────────────

@app.route('/api/conversations')
def conversations():
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    rows = conn.execute('''
        SELECT u.id, u.username, u.avatar_b64,
               m.text        AS last_text,
               m.created_at  AS last_at,
               m.from_user_id,
               (SELECT COUNT(*) FROM messages
                WHERE to_user_id=:uid AND from_user_id=u.id AND read_at IS NULL AND deleted=0) AS unread
        FROM users u
        JOIN messages m ON m.id = (
            SELECT id FROM messages
            WHERE ((from_user_id=:uid AND to_user_id=u.id)
                OR (from_user_id=u.id AND to_user_id=:uid))
              AND deleted=0
            ORDER BY created_at DESC LIMIT 1
        )
        WHERE u.id != :uid AND u.is_banned=0
          AND NOT EXISTS (SELECT 1 FROM blocks WHERE blocker_id=:uid AND blocked_id=u.id)
        ORDER BY last_at DESC
    ''', {'uid': uid}).fetchall()
    conn.close()
    return jsonify({'ok': True, 'conversations': [dict(r) for r in rows]})

# ── Messages ───────────────────────────────────────────────────────────────────

@app.route('/api/messages', methods=['GET'])
def get_messages():
    uid, err = require_auth(request)
    if err: return err

    with_id  = request.args.get('with', type=int)
    after_id = request.args.get('after_id', 0, type=int)

    if not with_id:
        return jsonify({'ok': False, 'error': 'Missing ?with='}), 400

    # Check not blocked
    conn = get_db()
    if conn.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',
                    (uid, with_id, with_id, uid)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Cannot view this conversation'}), 403

    if after_id == 0:
        rows = conn.execute('''
            SELECT id, from_user_id, to_user_id, text, created_at, read_at FROM messages
            WHERE ((from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?))
              AND deleted=0
            ORDER BY created_at DESC LIMIT 50
        ''', (uid, with_id, with_id, uid)).fetchall()
        conn.execute(
            'UPDATE messages SET read_at=? WHERE to_user_id=? AND from_user_id=? AND read_at IS NULL AND deleted=0',
            (int(time.time()), uid, with_id)
        )
        conn.commit()
        conn.close()
        msgs = list(reversed([dict(r) for r in rows]))
        return jsonify({'ok': True, 'messages': msgs})

    # Long-poll for new messages
    conn.close()
    deadline = time.time() + 25
    while True:
        conn = get_db()
        rows = conn.execute('''
            SELECT id, from_user_id, to_user_id, text, created_at, read_at FROM messages
            WHERE ((from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?))
              AND id > ? AND deleted=0
            ORDER BY created_at ASC
        ''', (uid, with_id, with_id, uid, after_id)).fetchall()
        conn.execute(
            'UPDATE messages SET read_at=? WHERE to_user_id=? AND from_user_id=? AND read_at IS NULL AND deleted=0',
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

    data      = request.get_json(force=True, silent=True) or {}
    to_id     = data.get('to_user_id')
    text      = (data.get('text') or '').strip()
    client_id = data.get('client_id') or None   # dedup token from client

    if not to_id or not text:
        return jsonify({'ok': False, 'error': 'Missing to_user_id or text'}), 400
    if len(text) > 2000:
        return jsonify({'ok': False, 'error': 'Message too long (max 2000 chars)'}), 400
    if to_id == uid:
        return jsonify({'ok': False, 'error': 'Cannot message yourself'}), 400

    text = apply_filter(text)

    conn = get_db()
    if not conn.execute('SELECT id FROM users WHERE id=? AND is_banned=0', (to_id,)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    if conn.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',
                    (uid, to_id, to_id, uid)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Cannot message this user'}), 403

    # Idempotency: if same client_id already stored, return existing message
    if client_id:
        existing = conn.execute('SELECT * FROM messages WHERE client_id=?', (client_id,)).fetchone()
        if existing:
            conn.close()
            return jsonify({'ok': True, 'message': dict(existing)}), 200

    try:
        cur = conn.execute(
            'INSERT INTO messages (client_id, from_user_id, to_user_id, text) VALUES (?,?,?,?)',
            (client_id, uid, to_id, text)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # race: another request inserted the same client_id
        existing = conn.execute('SELECT * FROM messages WHERE client_id=?', (client_id,)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'message': dict(existing)}), 200

    msg = dict(conn.execute('SELECT * FROM messages WHERE id=?', (cur.lastrowid,)).fetchone())
    conn.close()

    # Fire-and-forget Web Push to recipient
    _push_notify_async(to_id, uid, text)

    return jsonify({'ok': True, 'message': msg}), 201

# ── Read receipts poll ─────────────────────────────────────────────────────────

@app.route('/api/read_receipts')
def read_receipts():
    """Returns read_at timestamps for messages sent by uid to a specific user."""
    uid, err = require_auth(request)
    if err: return err
    with_id  = request.args.get('with', type=int)
    after_id = request.args.get('after_id', 0, type=int)
    if not with_id:
        return jsonify({'ok': False, 'error': 'Missing ?with='}), 400
    conn = get_db()
    rows = conn.execute('''
        SELECT id, read_at FROM messages
        WHERE from_user_id=? AND to_user_id=? AND id>? AND read_at IS NOT NULL AND deleted=0
        ORDER BY id ASC
    ''', (uid, with_id, after_id)).fetchall()
    conn.close()
    return jsonify({'ok': True, 'receipts': [{'id': r['id'], 'read_at': r['read_at']} for r in rows]})

# ── Web Push ───────────────────────────────────────────────────────────────────

@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    endpoint = data.get('endpoint') or ''
    p256dh   = data.get('p256dh') or ''
    auth     = data.get('auth') or ''
    if not endpoint or not p256dh or not auth:
        return jsonify({'ok': False, 'error': 'Missing subscription fields'}), 400
    conn = get_db()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?,?,?,?)',
            (uid, endpoint, p256dh, auth)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    uid, err = require_auth(request)
    if err: return err
    data     = request.get_json(force=True, silent=True) or {}
    endpoint = data.get('endpoint') or ''
    conn = get_db()
    conn.execute('DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?', (uid, endpoint))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def _push_notify_async(to_uid, from_uid, text):
    """Send Web Push notification in a background thread (best-effort)."""
    def _send():
        try:
            from pywebpush import webpush, WebPushException
            vapid_private = os.environ.get('VAPID_PRIVATE_KEY', '')
            vapid_email   = os.environ.get('VAPID_EMAIL', 'mailto:admin@chronarchive.com')
            if not vapid_private:
                return
            conn = get_db()
            subs = conn.execute(
                'SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?', (to_uid,)
            ).fetchall()
            sender = conn.execute('SELECT username FROM users WHERE id=?', (from_uid,)).fetchone()
            conn.close()
            if not subs or not sender:
                return
            payload = json.dumps({
                'title': sender['username'],
                'body':  text[:80] + ('…' if len(text) > 80 else ''),
                'tag':   'cg-msg-' + str(from_uid)
            })
            for s in subs:
                try:
                    webpush(
                        subscription_info={'endpoint': s['endpoint'],
                                           'keys': {'p256dh': s['p256dh'], 'auth': s['auth']}},
                        data=payload,
                        vapid_private_key=vapid_private,
                        vapid_claims={'sub': vapid_email}
                    )
                except WebPushException:
                    pass
        except ImportError:
            pass  # pywebpush not installed; skip push
    threading.Thread(target=_send, daemon=True).start()

# ── Admin ──────────────────────────────────────────────────────────────────────

@app.route('/api/admin/users')
def admin_list_users():
    uid, err = require_admin(request)
    if err: return err
    q    = (request.args.get('q') or '').strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT id, username, bio, is_admin, is_banned, created_at FROM users"
            " WHERE username LIKE ? COLLATE NOCASE ORDER BY id LIMIT 100",
            ('%' + q + '%',)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, username, bio, is_admin, is_banned, created_at FROM users ORDER BY id LIMIT 100"
        ).fetchall()
    conn.close()
    return jsonify({'ok': True, 'users': [dict(r) for r in rows]})


@app.route('/api/admin/users/<int:target_id>', methods=['POST'])
def admin_update_user(target_id):
    uid, err = require_admin(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    if 'is_banned' in data:
        conn.execute('UPDATE users SET is_banned=? WHERE id=?', (1 if data['is_banned'] else 0, target_id))
    if 'is_admin' in data:
        conn.execute('UPDATE users SET is_admin=? WHERE id=?', (1 if data['is_admin'] else 0, target_id))
    if 'reset_password' in data:
        new_pw = data['reset_password']
        if len(new_pw) >= 4:
            conn.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(new_pw), target_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:target_id>', methods=['DELETE'])
def admin_delete_user(target_id):
    uid, err = require_admin(request)
    if err: return err
    conn = get_db()
    conn.execute('DELETE FROM users WHERE id=?', (target_id,))
    conn.execute('DELETE FROM sessions WHERE user_id=?', (target_id,))
    conn.execute('DELETE FROM messages WHERE from_user_id=? OR to_user_id=?', (target_id, target_id))
    conn.execute('DELETE FROM friends WHERE from_user_id=? OR to_user_id=?', (target_id, target_id))
    conn.execute('DELETE FROM blocks WHERE blocker_id=? OR blocked_id=?', (target_id, target_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/messages')
def admin_list_messages():
    uid, err = require_admin(request)
    if err: return err
    user_id = request.args.get('user_id', type=int)
    conn    = get_db()
    if user_id:
        rows = conn.execute('''
            SELECT m.id, m.from_user_id, m.to_user_id, m.text, m.created_at, m.deleted,
                   a.username AS from_name, b.username AS to_name
            FROM messages m
            JOIN users a ON a.id=m.from_user_id
            JOIN users b ON b.id=m.to_user_id
            WHERE m.from_user_id=? OR m.to_user_id=?
            ORDER BY m.created_at DESC LIMIT 200
        ''', (user_id, user_id)).fetchall()
    else:
        rows = conn.execute('''
            SELECT m.id, m.from_user_id, m.to_user_id, m.text, m.created_at, m.deleted,
                   a.username AS from_name, b.username AS to_name
            FROM messages m
            JOIN users a ON a.id=m.from_user_id
            JOIN users b ON b.id=m.to_user_id
            ORDER BY m.created_at DESC LIMIT 200
        ''').fetchall()
    conn.close()
    return jsonify({'ok': True, 'messages': [dict(r) for r in rows]})


@app.route('/api/admin/messages/<int:msg_id>', methods=['DELETE'])
def admin_delete_message(msg_id):
    uid, err = require_admin(request)
    if err: return err
    conn = get_db()
    conn.execute('UPDATE messages SET deleted=1 WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/filter', methods=['GET'])
def admin_get_filter():
    uid, err = require_admin(request)
    if err: return err
    return jsonify({'ok': True, 'words': FILTER_WORDS})


@app.route('/api/admin/filter', methods=['POST'])
def admin_update_filter():
    uid, err = require_admin(request)
    if err: return err
    global FILTER_WORDS
    data = request.get_json(force=True, silent=True) or {}
    words = data.get('words')
    if not isinstance(words, list):
        return jsonify({'ok': False, 'error': 'words must be an array'}), 400
    FILTER_WORDS = [str(w).lower().strip() for w in words if w]
    return jsonify({'ok': True, 'words': FILTER_WORDS})


@app.route('/api/admin/stats')
def admin_stats():
    uid, err = require_admin(request)
    if err: return err
    conn = get_db()
    users_total   = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    users_banned  = conn.execute('SELECT COUNT(*) FROM users WHERE is_banned=1').fetchone()[0]
    msgs_total    = conn.execute('SELECT COUNT(*) FROM messages WHERE deleted=0').fetchone()[0]
    msgs_today    = conn.execute('SELECT COUNT(*) FROM messages WHERE deleted=0 AND created_at>?',
                                 (int(time.time()) - 86400,)).fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'stats': {
        'users_total': users_total, 'users_banned': users_banned,
        'messages_total': msgs_total, 'messages_today': msgs_today
    }})


if __name__ == '__main__':
    init_db()
    print('ChronoGraph Chat API starting on :5000')
    app.run(host='0.0.0.0', port=5000, threaded=True)


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

@app.route('/')
def index():
    from flask import redirect
    return redirect('https://chronarchive.com/chat.html', code=302)

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

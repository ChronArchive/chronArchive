#!/usr/bin/env python3
"""
ChronoGraph Chat API — Flask instant messenger backend.
Supports HTTP and HTTPS, works with iOS 3 through modern iOS via long-polling.

Features: accounts, messaging, read receipts, friends/blocks, profiles,
          message dedup, admin console, Web Push notifications, message filter.
"""

from flask import Flask, request, jsonify, make_response, send_from_directory
import sqlite3, hashlib, hmac, secrets, time, os, threading, re, base64, json, string, shutil

app = Flask(__name__)

DB_PATH      = os.environ.get('CHAT_DB',      '/opt/chronograph-chat/chat.db')
UPLOADS_DIR  = os.environ.get('UPLOADS_DIR',  '/opt/chronograph-chat/uploads')
SITE_BASE    = os.environ.get('SITE_BASE',    'https://chat.chronarchive.com')
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Cap request body to 3 MB to prevent memory exhaustion
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024

# ── In-memory rate limiting (login / register brute-force) ────────────────────
# {ip: [timestamp, ...]}  — keeps only the last 60 seconds of attempts
_RATE_LOCK    = threading.Lock()
_RATE_BUCKETS = {}   # ip -> list of unix timestamps
_RATE_LIMIT   = 10   # max attempts per window
_RATE_WINDOW  = 60   # seconds

def _check_rate(ip):
    """Return True if the IP is within limits, False if exceeded."""
    now = time.time()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(ip, [])
        bucket = [t for t in bucket if now - t < _RATE_WINDOW]
        if len(bucket) >= _RATE_LIMIT:
            _RATE_BUCKETS[ip] = bucket
            return False
        bucket.append(now)
        _RATE_BUCKETS[ip] = bucket
        return True

# Trusted upstream proxies that may set X-Forwarded-For (nginx on localhost + Tailscale node)
_TRUSTED_PROXIES = {'127.0.0.1', '::1', '100.95.1.7'}

def _get_ip():
    """Return real client IP, honouring X-Forwarded-For only from trusted proxies."""
    remote = request.remote_addr or '0.0.0.0'
    if remote in _TRUSTED_PROXIES:
        xff = request.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',')[0].strip()
    return remote

# Pillow is optional — falls back to storing base64 inline if unavailable.
try:
    from PIL import Image
    import io as _io
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False

# ── Bad-word filter ───────────────────────────────────────────────────────────
FILTER_WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'filter_words.json')
try:
    FILTER_WORDS = json.load(open(FILTER_WORDS_FILE))
except Exception:
    FILTER_WORDS = []

def apply_filter(text):
    for word in FILTER_WORDS:
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        text = pattern.sub('*' * len(word), text)
    return text

def gen_friend_code():
    """Return a random 6-char uppercase alphanumeric friend code."""
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))

# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    conn = get_db()
    # WAL mode persists — set it once here, not on every connection
    conn.execute('PRAGMA journal_mode=WAL')
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
        CREATE TABLE IF NOT EXISTS posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            type        TEXT NOT NULL DEFAULT 'image',
            title       TEXT NOT NULL DEFAULT '',
            description TEXT DEFAULT '',
            tags        TEXT DEFAULT '',
            media_data  TEXT NOT NULL DEFAULT '',
            deleted     INTEGER DEFAULT 0,
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(created_at DESC);
        CREATE TABLE IF NOT EXISTS post_likes (
            post_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY(post_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS post_comments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            text       TEXT NOT NULL,
            deleted    INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_likes_post   ON post_likes(post_id);
        CREATE INDEX IF NOT EXISTS idx_comments_post ON post_comments(post_id, created_at);
        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id INTEGER NOT NULL,
            target_type TEXT NOT NULL,
            target_id   INTEGER NOT NULL,
            reason      TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            admin_note  TEXT DEFAULT '',
            created_at  INTEGER DEFAULT (strftime('%s','now')),
            resolved_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status, created_at DESC);
    ''')
    # Safe migration: add media_url column for file-based image storage
    try:
        conn.execute('ALTER TABLE posts ADD COLUMN media_url TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    # Safe migration: add is_private column if absent
    try:
        conn.execute('ALTER TABLE users ADD COLUMN is_private INTEGER DEFAULT 0')
        conn.commit()
    except Exception:
        pass
    # Safe migration: add device_tag to posts
    try:
        conn.execute('ALTER TABLE posts ADD COLUMN device_tag TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    # Safe migration: add friend_code column if absent (no UNIQUE in ALTER TABLE — SQLite restriction)
    try:
        conn.execute('ALTER TABLE users ADD COLUMN friend_code TEXT')
        conn.commit()
    except Exception:
        pass
    # Ensure unique index exists (safe to re-run)
    try:
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_friend_code ON users(friend_code)')
        conn.commit()
    except Exception:
        pass
    # Generate codes for any users that don't have one
    rows_no_code = conn.execute('SELECT id FROM users WHERE friend_code IS NULL').fetchall()
    for row in rows_no_code:
        for _ in range(20):
            code = gen_friend_code()
            try:
                conn.execute('UPDATE users SET friend_code=? WHERE id=?', (code, row['id']))
                conn.commit()
                break
            except sqlite3.IntegrityError:
                pass
    conn.commit()
    # Create ChronoGraph system bot user if not yet present
    if not conn.execute("SELECT id FROM users WHERE username='ChronoGraph' COLLATE NOCASE").fetchone():
        bot_pw = secrets.token_hex(32)  # random unguessable password — never used to log in
        conn.execute(
            "INSERT INTO users (username, password_hash, bio) VALUES (?,?,?)",
            ('ChronoGraph', hash_password(bot_pw),
             'Official ChronoGraph account. A Social Media Time Machine by ChronArchive.')
        )
        conn.commit()
    conn.close()

# ── ChronoGraph bot helpers ────────────────────────────────────────────────────

_BOT_USER_ID = None

def _get_bot_id():
    """Return the user id of the ChronoGraph system bot (cached)."""
    global _BOT_USER_ID
    if _BOT_USER_ID is None:
        conn = get_db()
        row  = conn.execute("SELECT id FROM users WHERE username='ChronoGraph' COLLATE NOCASE").fetchone()
        conn.close()
        if row:
            _BOT_USER_ID = row['id']
    return _BOT_USER_ID

BOT_WELCOME  = (
    "Welcome to ChronoGraph, A Social Media Time Machine by ChronArchive! "
    "Feel free to explore and connect with others."
)
BOT_AUTOREPLY = (
    "Hi there! I am a very basic server bot so I can't reply yet, "
    "but we are working on it! Stay tuned."
)

def _send_bot_welcome_async(to_uid):
    """Send the welcome DM from ChronoGraph bot if we haven't already."""
    def _do():
        bot_id = _get_bot_id()
        if not bot_id or to_uid == bot_id:
            return
        conn = get_db()
        try:
            already = conn.execute(
                'SELECT 1 FROM messages WHERE from_user_id=? AND to_user_id=? AND deleted=0',
                (bot_id, to_uid)
            ).fetchone()
            if not already:
                conn.execute(
                    'INSERT INTO messages (from_user_id, to_user_id, text) VALUES (?,?,?)',
                    (bot_id, to_uid, BOT_WELCOME)
                )
                conn.commit()
        finally:
            conn.close()
    threading.Thread(target=_do, daemon=True).start()

def _send_bot_autoreply_async(to_uid):
    """Send the canned auto-reply from ChronoGraph bot after a short delay."""
    def _do():
        bot_id = _get_bot_id()
        if not bot_id or to_uid == bot_id:
            return
        time.sleep(0.8)
        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO messages (from_user_id, to_user_id, text) VALUES (?,?,?)',
                (bot_id, to_uid, BOT_AUTOREPLY)
            )
            conn.commit()
        finally:
            conn.close()
    threading.Thread(target=_do, daemon=True).start()

# ── Auth helpers ───────────────────────────────────────────────────────────────

def hash_password(password, salt=None):
    """Hash password with a random per-user salt.
    Returns 'salt_hex:hash_hex'. If salt is provided (bytes) it is reused.
    """
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200_000)
    return salt.hex() + ':' + digest.hex()

def verify_password(password, stored_hash):
    """Verify against new 'salt:hash' format or legacy fixed-salt format."""
    if ':' in stored_hash:
        salt_hex, _ = stored_hash.split(':', 1)
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt_hex)), stored_hash)
    # Legacy fixed-salt — upgrade path
    legacy = hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), b'chronograph-chat-v1', 200_000
    ).hex()
    return hmac.compare_digest(legacy, stored_hash)

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
    if origin in ALLOWED_ORIGINS or not origin:
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

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    """Serve compressed post images from disk."""
    # Only allow safe filenames (hex + .jpg/.png)
    if not re.match(r'^[0-9a-f]{32}\.(jpg|png)$', filename):
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    return send_from_directory(UPLOADS_DIR, filename)

@app.route('/api/ping')
def ping():
    return jsonify({'ok': True, 'time': int(time.time())})

# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
def register():
    if not _check_rate(_get_ip()):
        return jsonify({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}), 429
    data     = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not re.match(r'^[a-zA-Z0-9_]{2,32}$', username):
        return jsonify({'ok': False, 'error': 'Username: 2-32 letters, numbers or _'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

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
    # Assign a friend code
    for _ in range(20):
        code = gen_friend_code()
        try:
            conn.execute('UPDATE users SET friend_code=? WHERE id=?', (code, uid))
            conn.commit()
            break
        except sqlite3.IntegrityError:
            pass
    token  = secrets.token_hex(32)
    conn.execute('INSERT INTO sessions VALUES (?,?,?)',
                 (token, uid, int(time.time()) + 30 * 24 * 3600))
    conn.commit()
    conn.close()

    resp = jsonify({'ok': True, 'token': token, 'user_id': uid,
                    'username': username, 'is_admin': bool(row['is_admin']),
                    'avatar_b64': '', 'friend_code': ''})
    resp.set_cookie('cg_session', token, max_age=30*24*3600, httponly=True, path='/')
    _send_bot_welcome_async(uid)
    return resp, 201


@app.route('/api/login', methods=['POST'])
def login():
    if not _check_rate(_get_ip()):
        return jsonify({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}), 429
    data     = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    conn = get_db()
    row  = conn.execute(
        'SELECT id, password_hash, is_admin, is_banned, avatar_b64, friend_code FROM users WHERE username=?', (username,)
    ).fetchone()
    conn.close()

    if not row or not verify_password(password, row['password_hash']):
        return jsonify({'ok': False, 'error': 'Invalid username or password'}), 401
    if row['is_banned']:
        return jsonify({'ok': False, 'error': 'This account has been suspended'}), 403

    # Upgrade legacy fixed-salt hash to per-user salt on successful login
    if ':' not in row['password_hash']:
        conn2 = get_db()
        conn2.execute('UPDATE users SET password_hash=? WHERE id=?',
                      (hash_password(password), row['id']))
        conn2.commit()
        conn2.close()

    token = secrets.token_hex(32)
    conn  = get_db()
    # Prune expired sessions for this user while we're here
    conn.execute('DELETE FROM sessions WHERE expires_at < ?', (int(time.time()),))
    conn.execute('INSERT INTO sessions VALUES (?,?,?)',
                 (token, row['id'], int(time.time()) + 30 * 24 * 3600))
    conn.commit()
    conn.close()

    resp = jsonify({'ok': True, 'token': token, 'user_id': row['id'],
                    'username': username, 'is_admin': bool(row['is_admin']),
                    'avatar_b64': row['avatar_b64'] or '',
                    'friend_code': row['friend_code'] or ''})
    resp.set_cookie('cg_session', token, max_age=30*24*3600, httponly=True, path='/')
    _send_bot_welcome_async(row['id'])
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
        'SELECT id, username, bio, avatar_b64, is_admin, friend_code, is_private FROM users WHERE id=?', (uid,)
    ).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': row['id'], 'username': row['username'],
                    'bio': row['bio'] or '', 'avatar_b64': row['avatar_b64'] or '',
                    'is_admin': bool(row['is_admin']),
                    'friend_code': row['friend_code'] or '',
                    'is_private': bool(row['is_private'])})


@app.route('/api/me', methods=['PUT'])
def update_me():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}

    bio        = (data.get('bio') or '')[:160]
    avatar_b64 = (data.get('avatar_b64') or '')
    is_private = 1 if data.get('is_private') else 0

    # Validate base64 image (must be data URI or empty)
    if avatar_b64 and not re.match(r'^data:image/(jpeg|png|gif|webp);base64,[A-Za-z0-9+/=]+$', avatar_b64):
        return jsonify({'ok': False, 'error': 'Invalid avatar format'}), 400
    # Limit avatar to ~200 KB base64
    if len(avatar_b64) > 270000:
        return jsonify({'ok': False, 'error': 'Avatar too large (max ~200 KB)'}), 400

    conn = get_db()
    conn.execute('UPDATE users SET bio=?, avatar_b64=?, is_private=? WHERE id=?', (bio, avatar_b64, is_private, uid))
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

    if len(new_pass) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

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
        rows = []
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
        'SELECT id, username, bio, avatar_b64, created_at, is_private, friend_code FROM users WHERE id=? AND is_banned=0',
        (target_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    return jsonify({'ok': True, 'user': {
        'id': row['id'], 'username': row['username'],
        'bio': row['bio'] or '', 'avatar_b64': row['avatar_b64'] or '',
        'created_at': row['created_at'],
        'is_private': bool(row['is_private']),
        'friend_code': row['friend_code'] or ''
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


@app.route('/api/friends/by-code', methods=['POST'])
def friend_by_code():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get('code') or '').strip().upper()
    if not code:
        return jsonify({'ok': False, 'error': 'Friend code required'}), 400
    conn = get_db()
    target = conn.execute(
        'SELECT id FROM users WHERE friend_code=? AND is_banned=0', (code,)
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({'ok': False, 'error': 'No user found with that code'}), 404
    target_id = target['id']
    if target_id == uid:
        conn.close()
        return jsonify({'ok': False, 'error': 'That is your own code'}), 400
    if conn.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',
                    (uid, target_id, target_id, uid)).fetchone():
        conn.close()
        return jsonify({'ok': False, 'error': 'Cannot send request'}), 403
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
               COALESCE(unr.unread, 0) AS unread
        FROM users u
        JOIN messages m ON m.id = (
            SELECT id FROM messages
            WHERE ((from_user_id=:uid AND to_user_id=u.id)
                OR (from_user_id=u.id AND to_user_id=:uid))
              AND deleted=0
            ORDER BY created_at DESC LIMIT 1
        )
        LEFT JOIN (
            SELECT from_user_id, COUNT(*) AS unread
            FROM messages
            WHERE to_user_id=:uid AND read_at IS NULL AND deleted=0
            GROUP BY from_user_id
        ) unr ON unr.from_user_id = u.id
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

    # If the user messaged the ChronoGraph bot, send the canned auto-reply
    if to_id == _get_bot_id():
        _send_bot_autoreply_async(uid)

    return jsonify({'ok': True, 'message': msg}), 201


@app.route('/api/messages/<int:msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    """Soft-delete a message. Only the sender can delete their own message."""
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT from_user_id FROM messages WHERE id=? AND deleted=0', (msg_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Message not found'}), 404
    if row['from_user_id'] != uid:
        conn.close()
        return jsonify({'ok': False, 'error': 'Cannot delete another user\'s message'}), 403
    conn.execute('UPDATE messages SET deleted=1 WHERE id=?', (msg_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

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
        if len(new_pw) >= 8:
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
    try:
        with open(FILTER_WORDS_FILE, 'w') as _f: json.dump(FILTER_WORDS, _f)
    except Exception:
        pass
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



# ── Posts ───────────────────────────────────────────────────────────────────────

@app.route('/api/posts', methods=['GET'])
def list_posts():
    uid, _  = require_auth(request)   # None if not logged in — that's fine
    q           = (request.args.get('q') or '').strip()
    type_       = (request.args.get('type') or '').strip()
    friends_only = request.args.get('friends') == '1'
    page   = max(0, int(request.args.get('page', 0) or 0))
    limit  = 20
    offset = page * limit

    conn = get_db()
    sql    = '''SELECT p.id, p.user_id, u.username, u.avatar_b64,
                       p.type, p.title, p.description, p.tags, p.media_data, p.media_url,
                       p.device_tag, p.created_at,
                       COUNT(DISTINCT pl.user_id) AS likes_count,
                       COUNT(DISTINCT pc.id)      AS comments_count
                FROM posts p JOIN users u ON u.id = p.user_id
                LEFT JOIN post_likes    pl ON pl.post_id = p.id
                LEFT JOIN post_comments pc ON pc.post_id = p.id AND pc.deleted = 0
                WHERE p.deleted = 0'''
    params = []
    if friends_only and uid:
        sql += ''' AND p.user_id IN (
            SELECT CASE WHEN from_user_id=? THEN to_user_id ELSE from_user_id END
            FROM friends WHERE (from_user_id=? OR to_user_id=?) AND status='accepted'
        )'''
        params += [uid, uid, uid]
    if q:
        sql += ' AND (p.title LIKE ? OR p.description LIKE ? OR p.tags LIKE ?)'
        params += ['%' + q + '%', '%' + q + '%', '%' + q + '%']
    if type_ in ('image', 'video'):
        sql += ' AND p.type = ?'
        params.append(type_)
    sql += ' GROUP BY p.id ORDER BY p.created_at DESC LIMIT ? OFFSET ?'
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()

    # Batch-fetch which posts the current user has liked
    liked_set = set()
    if uid:
        ids = [r['id'] for r in rows]
        if ids:
            marks = ','.join('?' * len(ids))
            liked_rows = conn.execute(
                'SELECT post_id FROM post_likes WHERE user_id=? AND post_id IN (' + marks + ')',
                [uid] + ids
            ).fetchall()
            liked_set = set(lr['post_id'] for lr in liked_rows)
    conn.close()

    posts = []
    for r in rows:
        # Prefer file URL; fall back to legacy inline base64 if no URL stored yet
        media_url  = r['media_url'] if r['media_url'] else ''
        media_data = ''
        if not media_url and r['media_data']:
            media_data = r['media_data']
        posts.append({
            'id':             r['id'],
            'user_id':        r['user_id'],
            'username':       r['username'],
            'avatar_b64':     r['avatar_b64'] or '',
            'type':           r['type'],
            'title':          r['title'],
            'description':    r['description'] or '',
            'tags':           r['tags'] or '',
            'media_url':      media_url,
            'media_data':     media_data,
            'device_tag':     r['device_tag'] or '',
            'created_at':     r['created_at'],
            'likes_count':    r['likes_count'],
            'comments_count': r['comments_count'],
            'user_liked':     r['id'] in liked_set,
        })
    return jsonify({'ok': True, 'posts': posts, 'page': page})


@app.route('/api/posts', methods=['POST'])
def create_post():
    uid, err = require_auth(request)
    if err: return err

    data        = request.get_json(force=True, silent=True) or {}
    type_       = (data.get('type') or 'image').strip()
    title       = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    tags        = (data.get('tags') or '').strip()
    media_data  = data.get('media_data') or ''
    device_tag  = (data.get('device_tag') or '').strip()[:40]

    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400
    if type_ not in ('image', 'video'):
        return jsonify({'ok': False, 'error': 'Type must be image or video'}), 400
    if not media_data:
        return jsonify({'ok': False, 'error': 'Media data is required'}), 400
    if type_ == 'image' and len(media_data) > 2_000_000:
        return jsonify({'ok': False, 'error': 'Image too large (max ~1.5 MB base64)'}), 413

    # 10 posts per day limit (UTC midnight)
    now            = int(time.time())
    today_midnight = now - (now % 86400)
    conn = get_db()
    day_count = conn.execute(
        'SELECT COUNT(*) FROM posts WHERE user_id=? AND created_at>=? AND deleted=0',
        (uid, today_midnight)
    ).fetchone()[0]
    if day_count >= 10:
        conn.close()
        return jsonify({'ok': False, 'error': 'Daily post limit (10) reached'}), 429

    # ── Disk quota guard (500 MB free minimum) ──────────────────────────────
    free_bytes = shutil.disk_usage(UPLOADS_DIR).free
    if free_bytes < 500 * 1024 * 1024:
        conn.close()
        return jsonify({'ok': False, 'error': 'Server storage is full. Please try again later.'}), 507

    # ── Server-side image processing ──────────────────────────────────────────
    saved_url  = ''
    saved_b64  = ''
    if type_ == 'image' and media_data:
        # Strip data-URI prefix to get raw base64
        if ',' in media_data:
            raw_b64 = media_data.split(',', 1)[1]
        else:
            raw_b64 = media_data
        try:
            img_bytes = base64.b64decode(raw_b64)
            if _HAS_PILLOW:
                img = Image.open(_io.BytesIO(img_bytes)).convert('RGB')
                # Resize if wider than 1200px, preserving aspect ratio
                max_dim = 1200
                if img.width > max_dim:
                    ratio = max_dim / img.width
                    img = img.resize((max_dim, int(img.height * ratio)), Image.LANCZOS)
                # Save as JPEG at 82% quality
                fname = secrets.token_hex(16) + '.jpg'
                fpath = os.path.join(UPLOADS_DIR, fname)
                out   = _io.BytesIO()
                img.save(out, format='JPEG', quality=82, optimize=True)
                with open(fpath, 'wb') as f:
                    f.write(out.getvalue())
                saved_url  = SITE_BASE + '/uploads/' + fname
                saved_b64  = ''   # not stored inline
            else:
                # Pillow not installed — keep base64 inline
                saved_b64 = media_data
        except Exception:
            # Malformed image — reject
            conn.close()
            return jsonify({'ok': False, 'error': 'Invalid image data'}), 400
    elif type_ == 'video':
        # Video posts store the URL string as media_data (no file upload)
        saved_b64 = media_data

    cur = conn.execute(
        'INSERT INTO posts (user_id, type, title, description, tags, media_data, media_url, device_tag) VALUES (?,?,?,?,?,?,?,?)',
        (uid, type_, title, description, tags, saved_b64, saved_url, device_tag)
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()
    return jsonify({'ok': True, 'post_id': post_id}), 201


@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
def delete_post(post_id):
    uid, err = require_auth(request)
    if err: return err

    conn = get_db()
    row  = conn.execute('SELECT user_id FROM posts WHERE id=? AND deleted=0', (post_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Not found'}), 404

    is_admin = bool(conn.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()['is_admin'])
    if row['user_id'] != uid and not is_admin:
        conn.close()
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403

    conn.execute('UPDATE posts SET deleted=1 WHERE id=?', (post_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Post likes ────────────────────────────────────────────────────────────────

@app.route('/api/posts/<int:post_id>/like', methods=['POST'])
def like_post(post_id):
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT id FROM posts WHERE id=? AND deleted=0', (post_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    try:
        conn.execute('INSERT INTO post_likes (post_id, user_id) VALUES (?,?)', (post_id, uid))
        conn.commit()
        liked = True
    except sqlite3.IntegrityError:
        conn.execute('DELETE FROM post_likes WHERE post_id=? AND user_id=?', (post_id, uid))
        conn.commit()
        liked = False
    count = conn.execute('SELECT COUNT(*) FROM post_likes WHERE post_id=?', (post_id,)).fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'liked': liked, 'likes_count': count})

# ── Post comments ──────────────────────────────────────────────────────────────

@app.route('/api/posts/<int:post_id>/comments', methods=['GET'])
def list_comments(post_id):
    uid, _ = require_auth(request)
    conn = get_db()
    rows = conn.execute('''
        SELECT c.id, c.user_id, u.username, u.avatar_b64, c.text, c.created_at
        FROM post_comments c JOIN users u ON u.id=c.user_id
        WHERE c.post_id=? AND c.deleted=0
        ORDER BY c.created_at ASC LIMIT 100
    ''', (post_id,)).fetchall()
    conn.close()
    comments = []
    for r in rows:
        comments.append({
            'id':         r['id'],
            'user_id':    r['user_id'],
            'username':   r['username'],
            'avatar_b64': r['avatar_b64'] or '',
            'text':       r['text'],
            'created_at': r['created_at'],
        })
    return jsonify({'ok': True, 'comments': comments})

@app.route('/api/posts/<int:post_id>/comments', methods=['POST'])
def add_comment(post_id):
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT id FROM posts WHERE id=? AND deleted=0', (post_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get('text') or '').strip()
    text = apply_filter(text)
    if not text:
        conn.close()
        return jsonify({'ok': False, 'error': 'Comment cannot be empty'}), 400
    if len(text) > 500:
        conn.close()
        return jsonify({'ok': False, 'error': 'Comment too long (max 500 chars)'}), 400
    cur = conn.execute(
        'INSERT INTO post_comments (post_id, user_id, text) VALUES (?,?,?)',
        (post_id, uid, text)
    )
    conn.commit()
    cid = cur.lastrowid
    user = conn.execute('SELECT username, avatar_b64 FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'comment': {
        'id':         cid,
        'user_id':    uid,
        'username':   user['username'],
        'avatar_b64': user['avatar_b64'] or '',
        'text':       text,
        'created_at': int(time.time()),
    }}), 201

@app.route('/api/posts/<int:post_id>/comments/<int:comment_id>', methods=['DELETE'])
def delete_comment(post_id, comment_id):
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    row = conn.execute(
        'SELECT user_id FROM post_comments WHERE id=? AND post_id=? AND deleted=0',
        (comment_id, post_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    is_admin = bool(conn.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()['is_admin'])
    if row['user_id'] != uid and not is_admin:
        conn.close()
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403
    conn.execute('UPDATE post_comments SET deleted=1 WHERE id=?', (comment_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ── Admin: delete post ─────────────────────────────────────────────────────────

@app.route('/api/admin/posts/<int:post_id>', methods=['DELETE'])
def admin_delete_post(post_id):
    uid, err = require_admin(request)
    if err: return err
    conn = get_db()
    conn.execute('UPDATE posts SET deleted=1 WHERE id=?', (post_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Reports ────────────────────────────────────────────────────────────────────

@app.route('/api/reports', methods=['POST'])
def submit_report():
    uid, err = require_auth(request)
    if err: return err
    data        = request.get_json(force=True, silent=True) or {}
    target_type = (data.get('target_type') or '').strip()
    target_id   = data.get('target_id')
    reason      = (data.get('reason') or '').strip()
    if target_type not in ('post', 'user', 'comment'):
        return jsonify({'ok': False, 'error': 'Invalid target_type'}), 400
    if not isinstance(target_id, int):
        return jsonify({'ok': False, 'error': 'target_id must be an integer'}), 400
    if not reason:
        return jsonify({'ok': False, 'error': 'Reason is required'}), 400
    if len(reason) > 1000:
        return jsonify({'ok': False, 'error': 'Reason too long (max 1000 chars)'}), 400
    # Rate limit: max 5 reports per hour per user
    conn = get_db()
    hour_ago = int(time.time()) - 3600
    recent = conn.execute(
        'SELECT COUNT(*) FROM reports WHERE reporter_id=? AND created_at>?',
        (uid, hour_ago)
    ).fetchone()[0]
    if recent >= 5:
        conn.close()
        return jsonify({'ok': False, 'error': 'Too many reports. Please wait before submitting more.'}), 429
    conn.execute(
        'INSERT INTO reports (reporter_id, target_type, target_id, reason) VALUES (?,?,?,?)',
        (uid, target_type, target_id, reason)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True}), 201


@app.route('/api/admin/reports', methods=['GET'])
def admin_list_reports():
    uid, err = require_admin(request)
    if err: return err
    status = (request.args.get('status') or 'open').strip()
    conn = get_db()
    rows = conn.execute('''
        SELECT r.id, r.reporter_id, ru.username AS reporter_username,
               r.target_type, r.target_id, r.reason, r.status, r.admin_note,
               r.created_at, r.resolved_at
        FROM reports r JOIN users ru ON ru.id = r.reporter_id
        WHERE r.status = ?
        ORDER BY r.created_at DESC LIMIT 100
    ''', (status,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            'id':                 r['id'],
            'reporter_id':        r['reporter_id'],
            'reporter_username':  r['reporter_username'],
            'target_type':        r['target_type'],
            'target_id':          r['target_id'],
            'reason':             r['reason'],
            'status':             r['status'],
            'admin_note':         r['admin_note'] or '',
            'created_at':         r['created_at'],
            'resolved_at':        r['resolved_at'],
        })
    return jsonify({'ok': True, 'reports': result})


@app.route('/api/admin/reports/<int:report_id>', methods=['POST'])
def admin_respond_report(report_id):
    uid, err = require_admin(request)
    if err: return err
    data   = request.get_json(force=True, silent=True) or {}
    note   = (data.get('note') or '').strip()
    status = (data.get('status') or 'resolved').strip()
    if status not in ('open', 'resolved', 'dismissed'):
        return jsonify({'ok': False, 'error': 'Invalid status'}), 400
    conn = get_db()
    row = conn.execute('SELECT id FROM reports WHERE id=?', (report_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    resolved_at = int(time.time()) if status != 'open' else None
    conn.execute(
        'UPDATE reports SET status=?, admin_note=?, resolved_at=? WHERE id=?',
        (status, note, resolved_at, report_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Run init_db on module load so gunicorn workers also initialise the schema and bot user
init_db()

if __name__ == '__main__':
    print('ChronoGraph Chat API starting on :5000')
    app.run(host='0.0.0.0', port=5000, threaded=True)

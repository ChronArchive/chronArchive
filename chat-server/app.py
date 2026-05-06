#!/usr/bin/env python3
"""
ChronoGraph Chat API — Flask instant messenger backend.
Supports HTTP and HTTPS, works with iOS 3 through modern iOS via long-polling.

Features: accounts, messaging, read receipts, friends/blocks, profiles,
          message dedup, admin console, Web Push notifications, message filter.
"""

from flask import Flask, request, jsonify, make_response, send_from_directory
import sqlite3, hashlib, hmac, secrets, time, os, threading, re, base64, json, string, shutil, smtplib, ssl
from email.mime.text import MIMEText

app = Flask(__name__)

DB_PATH      = os.environ.get('CHAT_DB',      '/opt/chronograph-chat/chat.db')
UPLOADS_DIR  = os.environ.get('UPLOADS_DIR',  '/opt/chronograph-chat/uploads')
SITE_BASE    = os.environ.get('SITE_BASE',    'https://chat.chronarchive.com')
os.makedirs(UPLOADS_DIR, exist_ok=True)

# DM image uploads live in a private subdirectory served only via the
# auth-checked /uploads/dm/<basename> route below — never auto-listed by nginx.
DM_UPLOADS_DIR = os.path.join(UPLOADS_DIR, 'dm')
os.makedirs(DM_UPLOADS_DIR, exist_ok=True)

APNS_TEAM_ID = os.environ.get('APNS_TEAM_ID', '')
APNS_KEY_ID = os.environ.get('APNS_KEY_ID', '')
APNS_BUNDLE_ID = os.environ.get('APNS_BUNDLE_ID', '')
APNS_AUTH_KEY_PATH = os.environ.get('APNS_AUTH_KEY_PATH', '')

# ── Email (password recovery) ────────────────────────────────────────────────
# If SMTP_HOST is unset, codes are still generated and stored in email_outbox
# (table created in init_db) for manual delivery / inspection.
SMTP_HOST     = os.environ.get('SMTP_HOST', '')
SMTP_PORT     = int(os.environ.get('SMTP_PORT', '587') or '587')
SMTP_USER     = os.environ.get('SMTP_USER', '')
SMTP_PASS     = os.environ.get('SMTP_PASS', '')
SMTP_FROM     = os.environ.get('SMTP_FROM', 'noreply@chronarchive.com')
SMTP_FROM_NAME= os.environ.get('SMTP_FROM_NAME', 'ChronoGraph')
SMTP_USE_SSL  = (os.environ.get('SMTP_USE_SSL', '0') == '1')   # implicit TLS on :465

# ── Voice / Video calling (coturn relay) ──────────────────────────────────────
TURN_HOST     = os.environ.get('TURN_HOST', 'chat.chronarchive.com')
TURN_SECRET   = os.environ.get('TURN_SECRET', '')   # static-auth-secret in turnserver.conf
TURN_REALM    = os.environ.get('TURN_REALM', 'chronarchive.com')
TURN_TTL      = int(os.environ.get('TURN_TTL', '300') or '300')   # seconds

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')

# Cap request body to 3 MB to prevent memory exhaustion
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024

# ── In-memory rate limiting (login / register brute-force) ────────────────────
# {ip: [timestamp, ...]}  — keeps only the last 60 seconds of attempts
_RATE_LOCK    = threading.Lock()
_RATE_BUCKETS = {}   # ip -> list of unix timestamps
_RATE_LIMIT   = 10   # max attempts per window
_RATE_WINDOW  = 60   # seconds

# ── In-memory voice-call audio relay ───────────────────────────────────────────
# Audio frames are μ-law 8 kHz mono, sent in ~200 ms batches (1600 bytes each).
# Buffers are kept per (call_id, recipient_uid) as a deque of (seq, bytes, ts).
# Older than CALL_AUDIO_TTL seconds are dropped; max CALL_AUDIO_QMAX frames retained.
CALL_AUDIO_TTL  = 6      # seconds — anything older than this is stale and skipped
CALL_AUDIO_QMAX = 30     # ≈6 s at 200 ms / frame
CALL_AUDIO_MAX_FRAME = 4096  # bytes — single batch hard cap (safety)
_CALL_AUDIO_LOCK = threading.Lock()
_CALL_AUDIO = {}  # (call_id, to_uid) -> deque[(seq:int, data:bytes, ts:float)]
_CALL_AUDIO_SEQ = {}  # (call_id, from_uid) -> next outgoing seq

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
        CREATE TABLE IF NOT EXISTS apns_devices (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            device_token TEXT NOT NULL,
            session_token TEXT,
            environment  TEXT NOT NULL DEFAULT 'production',
            platform     TEXT NOT NULL DEFAULT 'ios',
            created_at   INTEGER DEFAULT (strftime('%s','now')),
            updated_at   INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(user_id, device_token)
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
    # Safe migration: add media_url and media_thumb_url columns for file-based image storage
    try:
        conn.execute('ALTER TABLE posts ADD COLUMN media_url TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE posts ADD COLUMN media_thumb_url TEXT DEFAULT ""')
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
    # Notification preference columns (default enabled)
    try:
        conn.execute('ALTER TABLE users ADD COLUMN notify_dm INTEGER DEFAULT 1')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN notify_friend_posts INTEGER DEFAULT 1')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN notify_likes INTEGER DEFAULT 1')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN notify_comments INTEGER DEFAULT 1')
        conn.commit()
    except Exception:
        pass
    # Safe migration: add session_token to apns_devices for logout scoping
    try:
        conn.execute('ALTER TABLE apns_devices ADD COLUMN session_token TEXT')
        conn.commit()
    except Exception:
        pass
    # Safe migration: VoIP push token (PushKit) per device
    try:
        conn.execute('ALTER TABLE apns_devices ADD COLUMN voip_token TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_apns_user ON apns_devices(user_id)')
        conn.commit()
    except Exception:
        pass
    # Safe migration: optional recovery email (private — never returned to other users)
    try:
        conn.execute('ALTER TABLE users ADD COLUMN email TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE users ADD COLUMN email_verified_at INTEGER DEFAULT 0')
        conn.commit()
    except Exception:
        pass
    # Safe migration: DM image attachments
    try:
        conn.execute('ALTER TABLE messages ADD COLUMN media_url TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE messages ADD COLUMN media_thumb_url TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute('ALTER TABLE messages ADD COLUMN media_kind TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    # Safe migration: medium image variant URL for posts (640px square)
    try:
        conn.execute('ALTER TABLE posts ADD COLUMN media_mid_url TEXT DEFAULT ""')
        conn.commit()
    except Exception:
        pass
    # New tables for password recovery, email outbox, calls
    conn.execute('''
        CREATE TABLE IF NOT EXISTS password_resets (
            token_hash TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at    INTEGER DEFAULT 0
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pr_user ON password_resets(user_id)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS email_verifications (
            token_hash TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            email      TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at    INTEGER DEFAULT 0
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ev_user ON email_verifications(user_id)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS email_outbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            to_email    TEXT NOT NULL,
            subject     TEXT NOT NULL,
            body        TEXT NOT NULL,
            sent_at     INTEGER DEFAULT 0,
            error       TEXT DEFAULT "",
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON email_outbox(sent_at, created_at)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_uid  INTEGER NOT NULL,
            callee_uid  INTEGER NOT NULL,
            media_kind  TEXT NOT NULL DEFAULT 'audio',
            started_at  INTEGER NOT NULL,
            ended_at    INTEGER DEFAULT 0,
            end_reason  TEXT DEFAULT "",
            sdp_offer   TEXT DEFAULT "",
            sdp_answer  TEXT DEFAULT "",
            state       TEXT DEFAULT 'inviting'
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_uid, started_at DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_uid, started_at DESC)')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS call_signals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id    INTEGER NOT NULL,
            from_uid   INTEGER NOT NULL,
            to_uid     INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            payload    TEXT NOT NULL DEFAULT "",
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_csig_to ON call_signals(to_uid, id)')
    conn.commit()
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
    if not token and req.method in ('POST', 'PUT', 'DELETE'):
        token = req.form.get('cg_token') or req.form.get('token')
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


def current_session_token(req):
    token = req.headers.get('X-CG-Token') or req.cookies.get('cg_session')
    if not token and req.method in ('POST', 'PUT', 'DELETE'):
        token = req.form.get('cg_token') or req.form.get('token')
    return token or ''

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
    if origin in ALLOWED_ORIGINS or not origin or origin == 'null':
        resp.headers['Access-Control-Allow-Origin']      = origin or '*'
        resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-CG-Token'
    resp.headers['Access-Control-Expose-Headers'] = 'X-CG-Last-Seq'
    return resp

# ── Email helper ───────────────────────────────────────────────────────────────

def _store_outbox(to_email, subject, body, sent_at, error):
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO email_outbox (to_email, subject, body, sent_at, error) VALUES (?,?,?,?,?)',
            (to_email, subject, body, sent_at, error)
        )
        conn.commit()
    finally:
        conn.close()

def _send_email_async(to_email, subject, body):
    """Send mail via configured SMTP relay, else log to email_outbox.

    Designed to never block the request thread or raise. All paths (success,
    skip, error) leave a row in email_outbox so admins can audit recovery
    flows. SMTP is configured via SMTP_HOST/PORT/USER/PASS/FROM env vars.
    """
    def _do():
        if not to_email:
            return
        if not SMTP_HOST:
            _store_outbox(to_email, subject, body, 0, 'no SMTP_HOST configured')
            return
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From']    = '%s <%s>' % (SMTP_FROM_NAME, SMTP_FROM)
        msg['To']      = to_email
        try:
            ctx = ssl.create_default_context()
            if SMTP_USE_SSL:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=ctx) as s:
                    if SMTP_USER:
                        s.login(SMTP_USER, SMTP_PASS)
                    s.sendmail(SMTP_FROM, [to_email], msg.as_string())
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                    s.ehlo()
                    try:
                        s.starttls(context=ctx)
                        s.ehlo()
                    except smtplib.SMTPException:
                        pass   # plain submission only
                    if SMTP_USER:
                        s.login(SMTP_USER, SMTP_PASS)
                    s.sendmail(SMTP_FROM, [to_email], msg.as_string())
            _store_outbox(to_email, subject, body, int(time.time()), '')
        except Exception as exc:
            _store_outbox(to_email, subject, body, 0, str(exc)[:240])
    threading.Thread(target=_do, daemon=True).start()

# ── Friend-gate helper ─────────────────────────────────────────────────────────

def _are_friends(conn, a_uid, b_uid):
    if a_uid == b_uid:
        return True
    row = conn.execute(
        "SELECT 1 FROM friends WHERE status='accepted' AND ("
        "(from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?))",
        (a_uid, b_uid, b_uid, a_uid)
    ).fetchone()
    return bool(row)

# ── Image variant pipeline (post + DM) ─────────────────────────────────────────

def _encode_jpeg(img, quality, max_bytes=None):
    out = _io.BytesIO()
    img.save(out, format='JPEG', quality=quality, optimize=True)
    data = out.getvalue()
    if max_bytes and len(data) > max_bytes:
        out = _io.BytesIO()
        img.save(out, format='JPEG', quality=max(40, quality - 10), optimize=True)
        data = out.getvalue()
    return data

def _square_crop(img):
    if img.width == img.height:
        return img
    if img.width > img.height:
        left = (img.width - img.height) // 2
        return img.crop((left, 0, left + img.height, img.height))
    top = (img.height - img.width) // 2
    return img.crop((0, top, img.width, top + img.width))

def _generate_post_variants(img, base_name):
    """Write full / mid / thumb JPEGs. Returns (full_url, mid_url, thumb_url)."""
    full = img
    if max(img.width, img.height) > 1600:
        ratio = 1600.0 / float(max(img.width, img.height))
        full = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    full_path = os.path.join(UPLOADS_DIR, base_name + '.jpg')
    with open(full_path, 'wb') as f:
        f.write(_encode_jpeg(full, 78, max_bytes=1_500_000))

    sq = _square_crop(img)
    mid = sq.resize((640, 640), Image.LANCZOS)
    mid_path = os.path.join(UPLOADS_DIR, base_name + '-md.jpg')
    with open(mid_path, 'wb') as f:
        f.write(_encode_jpeg(mid, 72, max_bytes=180_000))

    thumb = sq.resize((256, 256), Image.LANCZOS)
    thumb_path = os.path.join(UPLOADS_DIR, base_name + '-sq.jpg')
    with open(thumb_path, 'wb') as f:
        f.write(_encode_jpeg(thumb, 70, max_bytes=60_000))

    return (SITE_BASE + '/uploads/' + base_name + '.jpg',
            SITE_BASE + '/uploads/' + base_name + '-md.jpg',
            SITE_BASE + '/uploads/' + base_name + '-sq.jpg')

def _generate_dm_variants(img, base_name):
    """DM uploads: full (1280) + thumb (256), saved under uploads/dm/.
    Returns (full_url, thumb_url) using auth-gated /uploads/dm/ route."""
    full = img
    if max(img.width, img.height) > 1280:
        ratio = 1280.0 / float(max(img.width, img.height))
        full = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
    full_path = os.path.join(DM_UPLOADS_DIR, base_name + '.jpg')
    with open(full_path, 'wb') as f:
        f.write(_encode_jpeg(full, 75, max_bytes=900_000))

    sq = _square_crop(img)
    thumb = sq.resize((256, 256), Image.LANCZOS)
    thumb_path = os.path.join(DM_UPLOADS_DIR, base_name + '-sq.jpg')
    with open(thumb_path, 'wb') as f:
        f.write(_encode_jpeg(thumb, 68, max_bytes=50_000))

    return (SITE_BASE + '/uploads/dm/' + base_name + '.jpg',
            SITE_BASE + '/uploads/dm/' + base_name + '-sq.jpg')

# ── TURN credentials (HMAC user/pwd per coturn use-auth-secret) ────────────────

def _make_turn_creds(uid):
    if not TURN_SECRET:
        return None
    expiry = int(time.time()) + TURN_TTL
    username = '%d:%d' % (expiry, uid)
    digest = hmac.new(TURN_SECRET.encode('utf-8'), username.encode('utf-8'), hashlib.sha1).digest()
    password = base64.b64encode(digest).decode('ascii')
    urls = [
        'turn:%s:3478?transport=udp' % TURN_HOST,
        'turn:%s:3478?transport=tcp' % TURN_HOST,
        'turns:%s:5349?transport=tcp' % TURN_HOST,
    ]
    return {'username': username, 'credential': password, 'ttl': TURN_TTL, 'urls': urls}

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
    """Serve compressed post images from disk (full + -md + -sq variants)."""
    # Only allow safe filenames (hex + optional -md/-sq + .jpg/.png)
    if not re.match(r'^[0-9a-f]{32}(?:-md|-sq)?\.(jpg|png)$', filename):
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
    email_in = (data.get('email') or '').strip().lower()

    if not re.match(r'^[a-zA-Z0-9_]{2,32}$', username):
        return jsonify({'ok': False, 'error': 'Username: 2-32 letters, numbers or _'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    if email_in and not EMAIL_RE.match(email_in):
        return jsonify({'ok': False, 'error': 'Invalid email address'}), 400
    if len(email_in) > 254:
        return jsonify({'ok': False, 'error': 'Email too long'}), 400

    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO users (username, password_hash, email) VALUES (?,?,?)',
            (username, hash_password(password), email_in)
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
    if email_in:
        try:
            _send_verification_code(uid, email_in)
        except Exception:
            pass
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
        conn.execute('DELETE FROM apns_devices WHERE session_token=?', (token,))
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
        'SELECT id, username, bio, avatar_b64, is_admin, friend_code, is_private, '
        'COALESCE(email,"") AS email, '
        'COALESCE(email_verified_at,0) AS email_verified_at, '
        'COALESCE(notify_dm,1) AS notify_dm, '
        'COALESCE(notify_friend_posts,1) AS notify_friend_posts, '
        'COALESCE(notify_likes,1) AS notify_likes, '
        'COALESCE(notify_comments,1) AS notify_comments '
        'FROM users WHERE id=?', (uid,)
    ).fetchone()
    ev_fallback = False
    if row and not int(row['email_verified_at'] or 0):
        used = None
        if (row['email'] or ''):
            used = conn.execute(
                'SELECT 1 FROM email_verifications '
                'WHERE user_id=? AND email=? AND COALESCE(used_at,0)>0 '
                'ORDER BY used_at DESC LIMIT 1',
                (uid, row['email'])
            ).fetchone()
        if not used:
            used = conn.execute(
                'SELECT 1 FROM email_verifications '
                'WHERE user_id=? AND COALESCE(used_at,0)>0 '
                'ORDER BY used_at DESC LIMIT 1',
                (uid,)
            ).fetchone()
        ev_fallback = bool(used)
    conn.close()
    return jsonify({'ok': True, 'id': row['id'], 'username': row['username'],
                    'bio': row['bio'] or '', 'avatar_b64': row['avatar_b64'] or '',
                    'is_admin': bool(row['is_admin']),
                    'friend_code': row['friend_code'] or '',
                    'is_private': bool(row['is_private']),
                    'email': row['email'] or '',
                    'email_verified': bool(row['email_verified_at']) or ev_fallback,
                    'notify_dm': bool(row['notify_dm']),
                    'notify_friend_posts': bool(row['notify_friend_posts']),
                    'notify_likes': bool(row['notify_likes']),
                    'notify_comments': bool(row['notify_comments'])})


def _is_user_email_verified(conn, uid):
    row = conn.execute(
        'SELECT COALESCE(email_verified_at,0) AS ev, COALESCE(email,"") AS em '
        'FROM users WHERE id=?',
        (uid,)
    ).fetchone()
    if not row:
        return False
    if int(row['ev'] or 0) > 0:
        return True
    used = None
    if row['em']:
        used = conn.execute(
            'SELECT 1 FROM email_verifications '
            'WHERE user_id=? AND email=? AND COALESCE(used_at,0)>0 '
            'ORDER BY used_at DESC LIMIT 1',
            (uid, row['em'])
        ).fetchone()
    if not used:
        used = conn.execute(
            'SELECT 1 FROM email_verifications '
            'WHERE user_id=? AND COALESCE(used_at,0)>0 '
            'ORDER BY used_at DESC LIMIT 1',
            (uid,)
        ).fetchone()
    return bool(used)


@app.route('/api/me', methods=['PUT'])
def update_me():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}

    conn = get_db()
    cur = conn.execute(
        'SELECT bio, avatar_b64, is_private, COALESCE(email,"") AS email, '
        'COALESCE(notify_dm,1) AS notify_dm, '
        'COALESCE(notify_friend_posts,1) AS notify_friend_posts, '
        'COALESCE(notify_likes,1) AS notify_likes, '
        'COALESCE(notify_comments,1) AS notify_comments '
        'FROM users WHERE id=?',
        (uid,)
    ).fetchone()
    if not cur:
        conn.close()
        return jsonify({'ok': False, 'error': 'User not found'}), 404

    bio = ((data.get('bio') if 'bio' in data else cur['bio']) or '')[:160]
    avatar_b64 = (data.get('avatar_b64') if 'avatar_b64' in data else (cur['avatar_b64'] or '')) or ''
    is_private = (1 if data.get('is_private') else 0) if 'is_private' in data else int(cur['is_private'] or 0)
    notify_dm = (1 if data.get('notify_dm') else 0) if 'notify_dm' in data else int(cur['notify_dm'] or 1)
    notify_friend_posts = (1 if data.get('notify_friend_posts') else 0) if 'notify_friend_posts' in data else int(cur['notify_friend_posts'] or 1)
    notify_likes = (1 if data.get('notify_likes') else 0) if 'notify_likes' in data else int(cur['notify_likes'] or 1)
    notify_comments = (1 if data.get('notify_comments') else 0) if 'notify_comments' in data else int(cur['notify_comments'] or 1)
    # Optional inline email update via /api/me PUT for convenience
    email_changed = False
    new_email = cur['email'] or ''
    if 'email' in data:
        candidate = (data.get('email') or '').strip().lower()
        if candidate and not EMAIL_RE.match(candidate):
            conn.close()
            return jsonify({'ok': False, 'error': 'Invalid email address'}), 400
        if len(candidate) > 254:
            conn.close()
            return jsonify({'ok': False, 'error': 'Email too long'}), 400
        if candidate != new_email:
            new_email = candidate
            email_changed = True

    # Validate base64 image (must be data URI or empty)
    if avatar_b64 and not re.match(r'^data:image/(jpeg|png|gif|webp);base64,[A-Za-z0-9+/=]+$', avatar_b64):
        conn.close()
        return jsonify({'ok': False, 'error': 'Invalid avatar format'}), 400
    # Limit avatar to ~200 KB base64
    if len(avatar_b64) > 270000:
        conn.close()
        return jsonify({'ok': False, 'error': 'Avatar too large (max ~200 KB)'}), 400

    if email_changed:
        conn.execute(
            'UPDATE users SET bio=?, avatar_b64=?, is_private=?, notify_dm=?, notify_friend_posts=?, notify_likes=?, notify_comments=?, email=?, email_verified_at=0 WHERE id=?',
            (bio, avatar_b64, is_private, notify_dm, notify_friend_posts, notify_likes, notify_comments, new_email, uid)
        )
    else:
        conn.execute(
            'UPDATE users SET bio=?, avatar_b64=?, is_private=?, notify_dm=?, notify_friend_posts=?, notify_likes=?, notify_comments=? WHERE id=?',
            (bio, avatar_b64, is_private, notify_dm, notify_friend_posts, notify_likes, notify_comments, uid)
        )
    conn.commit()
    conn.close()
    if email_changed and new_email:
        try:
            _send_verification_code(uid, new_email)
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/api/me', methods=['DELETE'])
def delete_me():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    password = data.get('password') or ''

    if not password:
        return jsonify({'ok': False, 'error': 'Current password required'}), 400

    conn = get_db()
    row = conn.execute('SELECT password_hash FROM users WHERE id=?', (uid,)).fetchone()
    if not row or not verify_password(password, row['password_hash']):
        conn.close()
        return jsonify({'ok': False, 'error': 'Current password incorrect'}), 401

    uploads = []
    try:
        upload_rows = conn.execute(
            'SELECT media_url, media_thumb_url FROM posts WHERE user_id=?',
            (uid,)
        ).fetchall()
        for upload_row in upload_rows:
            for field in ('media_url', 'media_thumb_url'):
                val = upload_row[field] or ''
                if val.startswith('/uploads/'):
                    uploads.append(os.path.join(UPLOADS_DIR, os.path.basename(val)))

        conn.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM push_subscriptions WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM post_likes WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM post_likes WHERE post_id IN (SELECT id FROM posts WHERE user_id=?)', (uid,))
        conn.execute('DELETE FROM post_comments WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM post_comments WHERE post_id IN (SELECT id FROM posts WHERE user_id=?)', (uid,))
        conn.execute('DELETE FROM posts WHERE user_id=?', (uid,))
        conn.execute('DELETE FROM messages WHERE from_user_id=? OR to_user_id=?', (uid, uid))
        conn.execute('DELETE FROM friends WHERE from_user_id=? OR to_user_id=?', (uid, uid))
        conn.execute('DELETE FROM blocks WHERE blocker_id=? OR blocked_id=?', (uid, uid))
        conn.execute('DELETE FROM reports WHERE reporter_id=?', (uid,))
        conn.execute("DELETE FROM users WHERE id=? AND username NOT LIKE 'ChronoGraph' COLLATE NOCASE", (uid,))
        conn.commit()
    finally:
        conn.close()

    for upload in uploads:
        try:
            if os.path.isfile(upload):
                os.remove(upload)
        except Exception:
            pass

    resp = jsonify({'ok': True})
    resp.delete_cookie('cg_session')
    return resp


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


# ── Recovery email & password reset ────────────────────────────────────────────

# Per-IP rate limit for /api/auth/forgot — 5 / 15 min
_FORGOT_LOCK    = threading.Lock()
_FORGOT_BUCKETS = {}
_FORGOT_LIMIT   = 5
_FORGOT_WINDOW  = 900

def _check_forgot_rate(ip):
    now = time.time()
    with _FORGOT_LOCK:
        bucket = [t for t in _FORGOT_BUCKETS.get(ip, []) if now - t < _FORGOT_WINDOW]
        if len(bucket) >= _FORGOT_LIMIT:
            _FORGOT_BUCKETS[ip] = bucket
            return False
        bucket.append(now)
        _FORGOT_BUCKETS[ip] = bucket
        return True


@app.route('/api/me/email', methods=['PUT'])
def update_email():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    new_email = (data.get('email') or '').strip().lower()
    if new_email and not EMAIL_RE.match(new_email):
        return jsonify({'ok': False, 'error': 'Invalid email address'}), 400
    if len(new_email) > 254:
        return jsonify({'ok': False, 'error': 'Email too long'}), 400
    conn = get_db()
    conn.execute('UPDATE users SET email=?, email_verified_at=0 WHERE id=?', (new_email, uid))
    conn.commit()
    conn.close()
    if new_email:
        try:
            _send_verification_code(uid, new_email)
        except Exception:
            pass
    return jsonify({'ok': True, 'email': new_email})


# ── Email verification ─────────────────────────────────────────────────────────

_VERIFY_LOCK    = threading.Lock()
_VERIFY_BUCKETS = {}
_VERIFY_LIMIT   = 5
_VERIFY_WINDOW  = 900

def _check_verify_rate(uid):
    now = time.time()
    with _VERIFY_LOCK:
        bucket = [t for t in _VERIFY_BUCKETS.get(uid, []) if now - t < _VERIFY_WINDOW]
        if len(bucket) >= _VERIFY_LIMIT:
            _VERIFY_BUCKETS[uid] = bucket
            return False
        bucket.append(now)
        _VERIFY_BUCKETS[uid] = bucket
        return True

def _send_verification_code(uid, email):
    """Generate a 6-digit verification code, store hashed, email it.
    Returns True if dispatched (or queued), False if no email/SMTP path."""
    if not email:
        return False
    code = ''.join(secrets.choice(string.digits) for _ in range(6))
    token_hash = hashlib.sha256((str(uid) + ':v:' + code).encode('utf-8')).hexdigest()
    now = int(time.time())
    conn = get_db()
    try:
        conn.execute('UPDATE email_verifications SET used_at=? WHERE user_id=? AND used_at=0',
                     (now, uid))
        conn.execute(
            'INSERT INTO email_verifications (token_hash, user_id, email, created_at, expires_at) '
            'VALUES (?,?,?,?,?)',
            (token_hash, uid, email, now, now + 1800)
        )
        urow = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        conn.commit()
    finally:
        conn.close()
    uname = urow['username'] if urow else 'there'
    body = (
        'Hi %s,\n\n'
        'Please confirm this email address for your ChronoGraph account.\n\n'
        'Your verification code is: %s\n\n'
        'This code expires in 30 minutes.\n\n'
        'If you did not request this, you can ignore this message.\n\n'
        '\u2014 ChronoGraph\n'
    ) % (uname, code)
    _send_email_async(email, 'Verify your ChronoGraph email', body)
    return True


@app.route('/api/email/verify-send', methods=['POST'])
def email_verify_send():
    uid, err = require_auth(request)
    if err: return err
    if not _check_verify_rate(uid):
        return jsonify({'ok': False, 'error': 'Please wait a few minutes before requesting another code.'}), 429
    conn = get_db()
    row = conn.execute(
        'SELECT COALESCE(email,"") AS email, COALESCE(email_verified_at,0) AS ev '
        'FROM users WHERE id=?', (uid,)
    ).fetchone()
    conn.close()
    if not row or not row['email']:
        return jsonify({'ok': False, 'error': 'Add an email address first.'}), 400
    if row['ev']:
        return jsonify({'ok': True, 'already_verified': True})
    _send_verification_code(uid, row['email'])
    return jsonify({'ok': True})


@app.route('/api/email/verify-confirm', methods=['POST'])
def email_verify_confirm():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get('code') or '').strip()
    if not re.match(r'^\d{6}$', code):
        return jsonify({'ok': False, 'error': 'Invalid code'}), 400
    token_hash = hashlib.sha256((str(uid) + ':v:' + code).encode('utf-8')).hexdigest()
    now = int(time.time())
    conn = get_db()
    ev = conn.execute(
        'SELECT user_id, email, expires_at, used_at FROM email_verifications WHERE token_hash=?',
        (token_hash,)
    ).fetchone()
    if not ev or ev['user_id'] != uid or ev['used_at'] or ev['expires_at'] < now:
        conn.close()
        return jsonify({'ok': False, 'error': 'Invalid or expired code'}), 400
    # Make sure the email on file still matches what was verified
    urow = conn.execute('SELECT COALESCE(email,"") AS email FROM users WHERE id=?', (uid,)).fetchone()
    if not urow or (urow['email'] or '').lower() != (ev['email'] or '').lower():
        conn.close()
        return jsonify({'ok': False, 'error': 'Email has changed since this code was sent.'}), 400
    conn.execute('UPDATE email_verifications SET used_at=? WHERE token_hash=?', (now, token_hash))
    conn.execute('UPDATE users SET email_verified_at=? WHERE id=?', (now, uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'email_verified': True})


@app.route('/api/auth/forgot', methods=['POST'])
def forgot_password():
    """Request a password-reset code by username or email.

    Always returns ok:true (does not reveal account existence). If matched,
    a 6-digit code is generated, hashed, stored with 30-minute TTL, and
    delivered to the user's recovery email (or queued in email_outbox if
    SMTP is not configured).
    """
    if not _check_forgot_rate(_get_ip()):
        return jsonify({'ok': True}), 200   # silent rate-limit
    data = request.get_json(force=True, silent=True) or {}
    ident = (data.get('username') or data.get('email') or '').strip()
    if not ident:
        return jsonify({'ok': True}), 200
    conn = get_db()
    if '@' in ident:
        row = conn.execute(
            'SELECT id, username, COALESCE(email,"") AS email FROM users '
            'WHERE LOWER(email)=LOWER(?) AND is_banned=0',
            (ident,)
        ).fetchone()
    else:
        row = conn.execute(
            'SELECT id, username, COALESCE(email,"") AS email FROM users '
            'WHERE username=? COLLATE NOCASE AND is_banned=0',
            (ident,)
        ).fetchone()
    if row and row['email']:
        code = ''.join(secrets.choice(string.digits) for _ in range(6))
        token_hash = hashlib.sha256((str(row['id']) + ':' + code).encode('utf-8')).hexdigest()
        now = int(time.time())
        # Invalidate previous unused tokens for this user, then insert
        conn.execute('UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at=0',
                     (now, row['id']))
        conn.execute(
            'INSERT INTO password_resets (token_hash, user_id, created_at, expires_at) '
            'VALUES (?,?,?,?)',
            (token_hash, row['id'], now, now + 1800)
        )
        conn.commit()
        body = (
            'Hi %s,\n\n'
            'A password reset was requested for your ChronoGraph account.\n\n'
            'Your reset code is: %s\n\n'
            'This code expires in 30 minutes.\n\n'
            'If you did not request this, you can ignore this message.\n\n'
            '— ChronoGraph\n'
        ) % (row['username'], code)
        _send_email_async(row['email'], 'ChronoGraph password reset code', body)
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/auth/reset', methods=['POST'])
def reset_password():
    """Consume a reset code + set new password.

    On success, all sessions for the user are invalidated so the actor must
    re-login (and any thief loses access).
    """
    if not _check_rate(_get_ip()):
        return jsonify({'ok': False, 'error': 'Too many attempts. Please wait a moment.'}), 429
    data = request.get_json(force=True, silent=True) or {}
    ident = (data.get('username') or data.get('email') or '').strip()
    code  = (data.get('code') or '').strip()
    new_pass = data.get('new_password') or ''
    if not ident or not code or len(new_pass) < 8:
        return jsonify({'ok': False, 'error': 'Missing fields or password too short'}), 400
    if not re.match(r'^\d{6}$', code):
        return jsonify({'ok': False, 'error': 'Invalid reset code'}), 400
    conn = get_db()
    if '@' in ident:
        urow = conn.execute('SELECT id FROM users WHERE LOWER(email)=LOWER(?) AND is_banned=0',
                            (ident,)).fetchone()
    else:
        urow = conn.execute('SELECT id FROM users WHERE username=? COLLATE NOCASE AND is_banned=0',
                            (ident,)).fetchone()
    if not urow:
        conn.close()
        return jsonify({'ok': False, 'error': 'Invalid code'}), 400
    token_hash = hashlib.sha256((str(urow['id']) + ':' + code).encode('utf-8')).hexdigest()
    pr = conn.execute(
        'SELECT user_id, expires_at, used_at FROM password_resets WHERE token_hash=?',
        (token_hash,)
    ).fetchone()
    if not pr or pr['user_id'] != urow['id'] or pr['used_at'] or pr['expires_at'] < int(time.time()):
        conn.close()
        return jsonify({'ok': False, 'error': 'Invalid or expired code'}), 400
    conn.execute('UPDATE password_resets SET used_at=? WHERE token_hash=?',
                 (int(time.time()), token_hash))
    conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                 (hash_password(new_pass), urow['id']))
    conn.execute('DELETE FROM sessions WHERE user_id=?', (urow['id'],))
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
            " AND username NOT LIKE 'ChronoGraph' COLLATE NOCASE"
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
        SELECT u.id AS id,
               u.id AS user_id,
               u.username,
               u.avatar_b64,
               m.text        AS last_text,
               m.text        AS last_message,
               m.created_at  AS last_at,
               m.created_at  AS last_ts,
               m.from_user_id,
               COALESCE(unr.unread, 0) AS unread,
               COALESCE(unr.unread, 0) AS unread_count
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
            SELECT id, from_user_id, to_user_id, text, created_at, read_at,
                   COALESCE(media_url,"") AS media_url,
                   COALESCE(media_thumb_url,"") AS media_thumb_url,
                   COALESCE(media_kind,"") AS media_kind
            FROM messages
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
            SELECT id, from_user_id, to_user_id, text, created_at, read_at,
                   COALESCE(media_url,"") AS media_url,
                   COALESCE(media_thumb_url,"") AS media_thumb_url,
                   COALESCE(media_kind,"") AS media_kind
            FROM messages
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
    image_data = data.get('image_data') or ''   # optional data:image/...;base64,...

    if not to_id or (not text and not image_data):
        return jsonify({'ok': False, 'error': 'Missing to_user_id or text/image'}), 400
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

    # ── Optional image attachment (DM-private — auth-gated /uploads/dm/) ──
    media_url = ''
    media_thumb_url = ''
    media_kind = ''
    if image_data:
        if not _HAS_PILLOW:
            conn.close()
            return jsonify({'ok': False, 'error': 'Server cannot process images'}), 500
        if not re.match(r'^data:image/(jpeg|png|gif|webp);base64,', image_data, re.IGNORECASE):
            conn.close()
            return jsonify({'ok': False, 'error': 'Invalid image data URI'}), 400
        try:
            raw_b64 = image_data.split(',', 1)[1].strip()
            if len(raw_b64) % 4 != 0:
                raw_b64 += '=' * (4 - (len(raw_b64) % 4))
            img_bytes = base64.b64decode(raw_b64)
            if len(img_bytes) > 12 * 1024 * 1024:
                conn.close()
                return jsonify({'ok': False, 'error': 'Image too large (max 12 MB)'}), 413
            img = Image.open(_io.BytesIO(img_bytes)).convert('RGB')
            base_name = secrets.token_hex(16)
            full_url, thumb_url = _generate_dm_variants(img, base_name)
            media_url = full_url
            media_thumb_url = thumb_url
            media_kind = 'image'
        except Exception:
            conn.close()
            return jsonify({'ok': False, 'error': 'Invalid image data'}), 400

    # Idempotency: if same client_id already stored, return existing message
    if client_id:
        existing = conn.execute('SELECT * FROM messages WHERE client_id=?', (client_id,)).fetchone()
        if existing:
            conn.close()
            return jsonify({'ok': True, 'message': dict(existing)}), 200

    try:
        cur = conn.execute(
            'INSERT INTO messages (client_id, from_user_id, to_user_id, text, media_url, media_thumb_url, media_kind) VALUES (?,?,?,?,?,?,?)',
            (client_id, uid, to_id, text, media_url, media_thumb_url, media_kind)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # race: another request inserted the same client_id
        existing = conn.execute('SELECT * FROM messages WHERE client_id=?', (client_id,)).fetchone()
        conn.close()
        return jsonify({'ok': True, 'message': dict(existing)}), 200

    msg = dict(conn.execute('SELECT * FROM messages WHERE id=?', (cur.lastrowid,)).fetchone())
    conn.close()

    # Fire-and-forget push notification to recipient (if enabled in preferences)
    push_text = text if text else ('[Photo]' if media_kind == 'image' else '')
    _push_dm_async(to_id, uid, push_text)

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


@app.route('/api/badge')
def badge_count():
    uid, err = require_auth(request)
    if err: return err
    conn = get_db()
    count = conn.execute(
        'SELECT COUNT(*) FROM messages WHERE to_user_id=? AND read_at IS NULL AND deleted=0',
        (uid,)
    ).fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'unread_dm': int(count)})


@app.route('/api/apns/register', methods=['POST'])
def apns_register():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    device_token = (data.get('device_token') or data.get('token') or '').strip().lower()
    environment = (data.get('environment') or 'production').strip().lower()
    platform = (data.get('platform') or 'ios').strip().lower()
    if environment not in ('sandbox', 'production'):
        environment = 'production'
    if platform != 'ios':
        platform = 'ios'
    if not re.match(r'^[0-9a-f]{64}$', device_token):
        return jsonify({'ok': False, 'error': 'Invalid APNs device token'}), 400
    sess_token = current_session_token(request)
    conn = get_db()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO apns_devices (user_id, device_token, session_token, environment, platform, created_at, updated_at) '
            "VALUES (?,?,?,?,?,COALESCE((SELECT created_at FROM apns_devices WHERE user_id=? AND device_token=?), strftime('%s','now')), strftime('%s','now'))",
            (uid, device_token, sess_token, environment, platform, uid, device_token)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/apns/unregister', methods=['POST'])
def apns_unregister():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    device_token = (data.get('device_token') or data.get('token') or '').strip().lower()
    if not re.match(r'^[0-9a-f]{64}$', device_token):
        return jsonify({'ok': False, 'error': 'Invalid APNs device token'}), 400
    conn = get_db()
    conn.execute('DELETE FROM apns_devices WHERE user_id=? AND device_token=?', (uid, device_token))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

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


def _send_push_payload_async(to_uid, payload, pref_col):
    """Send Web Push and APNs payloads in a background thread if pref is enabled."""
    def _send():
        conn = get_db()
        pref_row = conn.execute(
            'SELECT COALESCE(' + pref_col + ',1) AS enabled FROM users WHERE id=?',
            (to_uid,)
        ).fetchone()
        if not pref_row or not int(pref_row['enabled'] or 1):
            conn.close()
            return

        badge = conn.execute(
            'SELECT COUNT(*) FROM messages WHERE to_user_id=? AND read_at IS NULL AND deleted=0',
            (to_uid,)
        ).fetchone()[0]

        web_subs = conn.execute(
            'SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?',
            (to_uid,)
        ).fetchall()
        ios_subs = conn.execute(
            "SELECT device_token, environment FROM apns_devices WHERE user_id=? AND platform='ios'",
            (to_uid,)
        ).fetchall()
        conn.close()

        # Web Push branch (best effort)
        try:
            from pywebpush import webpush, WebPushException
            vapid_private = os.environ.get('VAPID_PRIVATE_KEY', '')
            vapid_email   = os.environ.get('VAPID_EMAIL', 'mailto:admin@chronarchive.com')
            if vapid_private and web_subs:
                web_payload = dict(payload)
                web_payload['badge'] = int(badge)
                payload_json = json.dumps(web_payload)
                for s in web_subs:
                    try:
                        webpush(
                            subscription_info={'endpoint': s['endpoint'],
                                               'keys': {'p256dh': s['p256dh'], 'auth': s['auth']}},
                            data=payload_json,
                            vapid_private_key=vapid_private,
                            vapid_claims={'sub': vapid_email}
                        )
                    except WebPushException:
                        pass
        except ImportError:
            pass  # pywebpush not installed; skip web push

        # APNs branch — uses httpx+PyJWT (works on Python 3.12+)
        if APNS_TEAM_ID and APNS_KEY_ID and APNS_BUNDLE_ID and APNS_AUTH_KEY_PATH and \
                os.path.isfile(APNS_AUTH_KEY_PATH) and ios_subs:
            try:
                import time, json as _json
                import jwt
                import httpx

                with open(APNS_AUTH_KEY_PATH, 'r') as _f:
                    _apns_key = _f.read()

                def _make_jwt():
                    now = int(time.time())
                    return jwt.encode(
                        {'iss': APNS_TEAM_ID, 'iat': now},
                        _apns_key,
                        algorithm='ES256',
                        headers={'kid': APNS_KEY_ID}
                    )

                aps_body = {
                    'aps': {
                        'alert': {'title': payload.get('title', ''), 'body': payload.get('body', '')},
                        'sound': 'default',
                        'badge': int(badge),
                    },
                    'type': payload.get('type', ''),
                    'from_user_id': payload.get('from_user_id'),
                    'post_id': payload.get('post_id'),
                }

                import logging as _logging
                _log = _logging.getLogger('gunicorn.error')

                for s in ios_subs:
                    env = (s['environment'] or 'production').strip().lower()
                    host = 'api.sandbox.push.apple.com' if env == 'sandbox' else 'api.push.apple.com'
                    url = f'https://{host}/3/device/{s["device_token"]}'
                    headers = {
                        'authorization': f'bearer {_make_jwt()}',
                        'apns-topic': APNS_BUNDLE_ID,
                        'apns-push-type': 'alert',
                    }
                    try:
                        with httpx.Client(http2=True) as _client:
                            resp = _client.post(url, json=aps_body, headers=headers, timeout=10)
                        if resp.status_code == 200:
                            _log.info('[APNS] sent ok token=%s env=%s', s['device_token'][:8], env)
                        else:
                            _log.error('[APNS] error token=%s env=%s status=%s body=%s',
                                       s['device_token'][:8], env, resp.status_code, resp.text)
                    except Exception as _exc:
                        _log.error('[APNS] request error token=%s env=%s err=%s',
                                   s['device_token'][:8], env, _exc)
            except Exception as _outer:
                import logging as _logging
                _logging.getLogger('gunicorn.error').error('[APNS] setup error: %s', _outer)

    threading.Thread(target=_send, daemon=True).start()


def _push_dm_async(to_uid, from_uid, text):
    conn = get_db()
    sender = conn.execute('SELECT username FROM users WHERE id=?', (from_uid,)).fetchone()
    conn.close()
    if not sender:
        return
    _send_push_payload_async(to_uid, {
        'type': 'dm',
        'from_user_id': int(from_uid),
        'title': sender['username'],
        'body': text[:80] + ('…' if len(text) > 80 else ''),
        'tag': 'cg-msg-' + str(from_uid)
    }, 'notify_dm')


def _push_like_async(post_owner_uid, liker_uid, post_id):
    if post_owner_uid == liker_uid:
        return
    conn = get_db()
    liker = conn.execute('SELECT username FROM users WHERE id=?', (liker_uid,)).fetchone()
    conn.close()
    if not liker:
        return
    _send_push_payload_async(post_owner_uid, {
        'type': 'like',
        'from_user_id': int(liker_uid),
        'post_id': int(post_id),
        'title': 'New Like',
        'body': liker['username'] + ' liked your post',
        'tag': 'cg-like-' + str(post_id)
    }, 'notify_likes')


def _push_comment_async(post_owner_uid, commenter_uid, post_id, text):
    if post_owner_uid == commenter_uid:
        return
    conn = get_db()
    commenter = conn.execute('SELECT username FROM users WHERE id=?', (commenter_uid,)).fetchone()
    conn.close()
    if not commenter:
        return
    _send_push_payload_async(post_owner_uid, {
        'type': 'comment',
        'from_user_id': int(commenter_uid),
        'post_id': int(post_id),
        'title': 'New Comment',
        'body': commenter['username'] + ': ' + (text[:70] + ('…' if len(text) > 70 else '')),
        'tag': 'cg-comment-' + str(post_id)
    }, 'notify_comments')


def _push_friend_post_async(author_uid, post_id, title):
    conn = get_db()
    author = conn.execute('SELECT username FROM users WHERE id=?', (author_uid,)).fetchone()
    if not author:
        conn.close()
        return
    rows = conn.execute('''
        SELECT CASE WHEN from_user_id=? THEN to_user_id ELSE from_user_id END AS friend_id
        FROM friends
        WHERE status='accepted' AND (from_user_id=? OR to_user_id=?)
    ''', (author_uid, author_uid, author_uid)).fetchall()
    conn.close()
    body = (title or '').strip() or 'Shared a new post'
    body = body[:80] + ('…' if len(body) > 80 else '')
    for r in rows:
        fid = int(r['friend_id'])
        if fid == author_uid:
            continue
        _send_push_payload_async(fid, {
            'type': 'friend_post',
            'from_user_id': int(author_uid),
            'post_id': int(post_id),
            'title': author['username'],
            'body': body,
            'tag': 'cg-friend-post-' + str(post_id)
        }, 'notify_friend_posts')


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
                       COALESCE(p.media_mid_url,"")   AS media_mid_url,
                       COALESCE(p.media_thumb_url,"") AS media_thumb_url,
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
        media_url       = r['media_url'] if r['media_url'] else ''
        media_mid_url   = r['media_mid_url'] if r['media_mid_url'] else ''
        media_thumb_url = r['media_thumb_url'] if r['media_thumb_url'] else ''
        media_data      = ''
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
            'media_mid_url':  media_mid_url,
            'media_thumb_url': media_thumb_url,
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

    if request.content_type and request.content_type.startswith('multipart/form-data'):
        form = request.form
        data = {
            'type': form.get('type', 'image'),
            'title': form.get('title', ''),
            'description': form.get('description', ''),
            'tags': form.get('tags', ''),
            'device_tag': form.get('device_tag', ''),
        }
        media_file = request.files.get('image_file')
        if media_file:
            raw = media_file.read()
            mimetype = media_file.mimetype or 'application/octet-stream'
            media_data = 'data:%s;base64,%s' % (
                mimetype,
                base64.b64encode(raw).decode('ascii')
            )
        else:
            media_data = ''
    else:
        data        = request.get_json(force=True, silent=True) or {}
        media_data  = data.get('media_data') or ''

    type_       = (data.get('type') or 'image').strip()
    title       = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    tags        = (data.get('tags') or '').strip()
    device_tag  = (data.get('device_tag') or '').strip()[:40]

    if not title:
        return jsonify({'ok': False, 'error': 'Title is required'}), 400
    if type_ not in ('image', 'video', 'text'):
        return jsonify({'ok': False, 'error': 'Type must be image, video, or text'}), 400
    if type_ in ('image', 'video') and not media_data:
        return jsonify({'ok': False, 'error': 'Media data is required for image/video posts'}), 400
    if type_ == 'text':
        media_data = ''
    if type_ == 'image' and len(media_data) > 75_000_000:
        return jsonify({'ok': False, 'error': 'Image too large (max ~50 MB)'}), 413

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
    saved_mid_url = ''
    saved_thumb_url = ''
    saved_b64  = ''
    if type_ == 'image' and media_data:
        # If the client sent an image URL instead of inline base64, store the URL directly.
        if media_data.lower().startswith('http://') or media_data.lower().startswith('https://'):
            saved_b64 = media_data
        else:
            # Strip data-URI prefix to get raw base64
            if ',' in media_data:
                raw_b64 = media_data.split(',', 1)[1]
            else:
                raw_b64 = media_data
            raw_b64 = raw_b64.strip()
            try:
                if len(raw_b64) % 4 != 0:
                    raw_b64 += '=' * (4 - (len(raw_b64) % 4))
                img_bytes = base64.b64decode(raw_b64)
                if _HAS_PILLOW:
                    img = Image.open(_io.BytesIO(img_bytes)).convert('RGB')
                    base_name = secrets.token_hex(16)
                    saved_url, saved_mid_url, saved_thumb_url = _generate_post_variants(img, base_name)
                    saved_b64 = ''   # not stored inline
                else:
                    # Pillow not installed — keep base64 inline
                    saved_b64 = media_data
            except Exception:
                conn.close()
                return jsonify({'ok': False, 'error': 'Invalid image data'}), 400
    elif type_ == 'video':
        # Video posts store the URL string as media_data (no file upload)
        saved_b64 = media_data

    cur = conn.execute(
        'INSERT INTO posts (user_id, type, title, description, tags, media_data, media_url, media_mid_url, media_thumb_url, device_tag) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (uid, type_, title, description, tags, saved_b64, saved_url, saved_mid_url, saved_thumb_url, device_tag)
    )
    conn.commit()
    post_id = cur.lastrowid
    conn.close()

    # Notify accepted friends about a new post if they opted in.
    _push_friend_post_async(uid, post_id, title)

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
    row = conn.execute('SELECT id, user_id FROM posts WHERE id=? AND deleted=0', (post_id,)).fetchone()
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
    post_owner_uid = int(row['user_id'])
    conn.close()

    if liked:
        _push_like_async(post_owner_uid, uid, post_id)

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
    row = conn.execute('SELECT id, user_id FROM posts WHERE id=? AND deleted=0', (post_id,)).fetchone()
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
    post_owner_uid = int(row['user_id'])
    conn.close()

    _push_comment_async(post_owner_uid, uid, post_id, text)

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


# ── Private DM uploads (auth-gated) ────────────────────────────────────────────

# Maps the SITE_BASE-rooted /uploads/dm/<basename> URLs to disk under
# DM_UPLOADS_DIR, but only after verifying the requester is the sender or
# recipient of a message that references this filename. Keeps DM photos out
# of the publicly listed /uploads tree.
@app.route('/uploads/dm/<path:filename>')
def serve_dm_upload(filename):
    if not re.match(r'^[0-9a-f]{32}(?:-sq)?\.jpg$', filename):
        return jsonify({'ok': False, 'error': 'Not found'}), 404
    # <img> tags cannot send custom headers, so accept ?t=<token> as a fallback
    # for this read-only endpoint. The disk filename is a 32-hex random base
    # name, so possession of the URL alone is also a capability — but we still
    # require a valid session token belonging to a participant of the message.
    token = (request.headers.get('X-CG-Token')
             or request.cookies.get('cg_session')
             or request.args.get('t')
             or '')
    if not token:
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403
    conn = get_db()
    sess = conn.execute(
        'SELECT user_id FROM sessions WHERE token=? AND expires_at>?',
        (token, int(time.time()))
    ).fetchone()
    if not sess:
        conn.close()
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403
    uid = sess['user_id']
    full_url  = SITE_BASE + '/uploads/dm/' + filename
    base_only = filename.replace('-sq.jpg', '.jpg')
    full_full = SITE_BASE + '/uploads/dm/' + base_only
    thumb_url = SITE_BASE + '/uploads/dm/' + base_only.replace('.jpg', '-sq.jpg')
    row = conn.execute(
        'SELECT 1 FROM messages WHERE deleted=0 AND (from_user_id=? OR to_user_id=?) '
        'AND (media_url IN (?,?) OR media_thumb_url IN (?,?)) LIMIT 1',
        (uid, uid, full_url, full_full, full_url, thumb_url)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({'ok': False, 'error': 'Forbidden'}), 403
    return send_from_directory(DM_UPLOADS_DIR, filename)


# ── Image variant backfill (admin only) ────────────────────────────────────────

@app.route('/api/admin/backfill-variants', methods=['POST'])
def admin_backfill_variants():
    """Generate -md.jpg + -sq.jpg variants for older posts that only have a
    full image saved. Processes up to `limit` rows per call so the admin can
    drive it incrementally (default 25, max 100). Safe to re-run."""
    uid, err = require_admin(request)
    if err: return err
    if not _HAS_PILLOW:
        return jsonify({'ok': False, 'error': 'Pillow not available'}), 500
    data = request.get_json(force=True, silent=True) or {}
    try:
        limit = max(1, min(100, int(data.get('limit', 25))))
    except Exception:
        limit = 25
    conn = get_db()
    rows = conn.execute(
        'SELECT id, media_url, media_thumb_url, COALESCE(media_mid_url,"") AS media_mid_url '
        'FROM posts WHERE deleted=0 AND media_url LIKE ? '
        'AND (media_mid_url IS NULL OR media_mid_url="" OR media_thumb_url="" ) '
        'ORDER BY id DESC LIMIT ?',
        (SITE_BASE + '/uploads/%', limit)
    ).fetchall()
    done = 0
    skipped = 0
    errors = 0
    for r in rows:
        try:
            full_path = os.path.join(UPLOADS_DIR, os.path.basename(r['media_url']))
            if not os.path.isfile(full_path):
                skipped += 1
                continue
            base_name = os.path.splitext(os.path.basename(full_path))[0]
            with Image.open(full_path) as src:
                img = src.convert('RGB')
                # Only (re)write the variants we are missing
                sq = _square_crop(img)
                mid_path   = os.path.join(UPLOADS_DIR, base_name + '-md.jpg')
                thumb_path = os.path.join(UPLOADS_DIR, base_name + '-sq.jpg')
                if not r['media_mid_url'] or not os.path.isfile(mid_path):
                    mid_img = sq.resize((640, 640), Image.LANCZOS)
                    with open(mid_path, 'wb') as f:
                        f.write(_encode_jpeg(mid_img, 72, max_bytes=180_000))
                if not r['media_thumb_url'] or not os.path.isfile(thumb_path):
                    th_img = sq.resize((256, 256), Image.LANCZOS)
                    with open(thumb_path, 'wb') as f:
                        f.write(_encode_jpeg(th_img, 70, max_bytes=60_000))
            mid_url   = SITE_BASE + '/uploads/' + base_name + '-md.jpg'
            thumb_url = SITE_BASE + '/uploads/' + base_name + '-sq.jpg'
            conn.execute(
                'UPDATE posts SET media_mid_url=?, media_thumb_url=COALESCE(NULLIF(media_thumb_url,""),?) WHERE id=?',
                (mid_url, thumb_url, r['id'])
            )
            done += 1
        except Exception:
            errors += 1
    conn.commit()
    remaining = conn.execute(
        'SELECT COUNT(*) FROM posts WHERE deleted=0 AND media_url LIKE ? '
        'AND (media_mid_url IS NULL OR media_mid_url="" OR media_thumb_url="")',
        (SITE_BASE + '/uploads/%',)
    ).fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'processed': done, 'skipped': skipped,
                    'errors': errors, 'remaining': remaining})


# ── VoIP signaling (audio + video) ─────────────────────────────────────────────

# Per-user concurrent invite cap (in-process). Prevents accidental signaling
# floods. Coupled with friend gate + TURN bandwidth caps in coturn config.
_CALL_LOCK    = threading.Lock()
_CALL_INVITES = {}   # uid -> [unix_ts ...]
_CALL_RINGED  = set()  # (call_id, callee_uid) that already produced caller 'ringing' ack
_CALL_INVITE_LIMIT  = 6
_CALL_INVITE_WINDOW = 60
_CALL_LAST_PRUNE = 0
_CALL_SIGNAL_TTL = 24 * 3600
_CALL_ROW_TTL = 3 * 24 * 3600
_CALL_MSG_PREFIX = '[Call] '

def _check_call_invite_rate(uid):
    now = time.time()
    with _CALL_LOCK:
        bucket = [t for t in _CALL_INVITES.get(uid, []) if now - t < _CALL_INVITE_WINDOW]
        if len(bucket) >= _CALL_INVITE_LIMIT:
            _CALL_INVITES[uid] = bucket
            return False
        bucket.append(now)
        _CALL_INVITES[uid] = bucket
        return True


def _maybe_prune_call_rows(conn):
    global _CALL_LAST_PRUNE
    now = int(time.time())
    with _CALL_LOCK:
        if now - int(_CALL_LAST_PRUNE or 0) < 60:
            return
        _CALL_LAST_PRUNE = now
    # Keep call_signals bounded so stale cursors don't walk huge history.
    conn.execute('DELETE FROM call_signals WHERE created_at < ?', (now - _CALL_SIGNAL_TTL,))
    conn.execute('DELETE FROM calls WHERE ended_at > 0 AND ended_at < ?', (now - _CALL_ROW_TTL,))


def _format_call_duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return '%ds' % seconds
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return '%dm %02ds' % (minutes, rem)
    hours, minutes = divmod(minutes, 60)
    return '%dh %02dm' % (hours, minutes)


def _call_status_text(reason, duration_seconds=0):
    reason = str(reason or 'ended').strip().lower()
    if reason == 'invite':
        return 'Incoming call'
    if reason == 'connected':
        return 'Call connected'
    if reason == 'declined':
        return 'Call declined'
    if reason == 'no_answer':
        return 'Missed call'
    if duration_seconds and duration_seconds > 0:
        return 'Call lasted %s' % _format_call_duration(duration_seconds)
    return 'Call ended'


def _insert_call_status_message(conn, from_uid, to_uid, text, created_at=None):
    conn.execute(
        'INSERT INTO messages (from_user_id, to_user_id, text, created_at) VALUES (?,?,?,?)',
        (int(from_uid), int(to_uid), (_CALL_MSG_PREFIX + str(text or '')).strip()[:2000], int(created_at or time.time()))
    )


@app.route('/api/voip/turn-creds', methods=['POST'])
def voip_turn_creds():
    uid, err = require_auth(request)
    if err: return err
    creds = _make_turn_creds(uid)
    if not creds:
        return jsonify({'ok': False, 'error': 'TURN not configured'}), 503
    return jsonify({'ok': True, 'iceServers': [{
        'urls': creds['urls'],
        'username': creds['username'],
        'credential': creds['credential']
    }], 'ttl': creds['ttl']})


@app.route('/api/voip-push/register', methods=['POST'])
def voip_push_register():
    """Store a PushKit (VoIP) APNs token alongside the user's regular device.
    Same shape as /api/apns/register but writes to the voip_token column.
    """
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    voip_token = (data.get('voip_token') or data.get('token') or '').strip().lower()
    if not re.match(r'^[0-9a-f]{64}$', voip_token):
        return jsonify({'ok': False, 'error': 'Invalid VoIP token'}), 400
    sess = current_session_token(request)
    conn = get_db()
    # Attach to the device row that owns this session (created by /api/apns/register)
    row = conn.execute(
        'SELECT id FROM apns_devices WHERE user_id=? AND session_token=? ORDER BY id DESC LIMIT 1',
        (uid, sess)
    ).fetchone()
    if row:
        conn.execute('UPDATE apns_devices SET voip_token=?, updated_at=strftime(\'%s\',\'now\') WHERE id=?',
                     (voip_token, row['id']))
    else:
        # No regular APNs registration yet — make a placeholder device row keyed by VoIP token
        conn.execute(
            'INSERT OR IGNORE INTO apns_devices (user_id, device_token, session_token, environment, voip_token) '
            'VALUES (?,?,?,?,?)',
            (uid, voip_token, sess, 'production', voip_token)
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/calls/invite', methods=['POST'])
def call_invite():
    """Caller posts an SDP offer + media kind. Stores the call row and a
    pending signal for the callee to long-poll. Friend-gated to prevent
    cold-call spam."""
    uid, err = require_auth(request)
    if err: return err
    if not _check_call_invite_rate(uid):
        return jsonify({'ok': False, 'error': 'Too many call attempts'}), 429
    data = request.get_json(force=True, silent=True) or {}
    callee_uid = data.get('callee_uid')
    media_kind = (data.get('media_kind') or 'audio').strip().lower()
    sdp_offer  = data.get('sdp_offer') or ''
    if media_kind not in ('audio', 'video'):
        app.logger.warning('call_invite invalid_media caller=%s callee=%r media=%r', uid, callee_uid, media_kind)
        return jsonify({'ok': False, 'error': 'media_kind must be audio or video'}), 400
    if not isinstance(callee_uid, int) or callee_uid == uid:
        app.logger.warning('call_invite invalid_callee caller=%s callee=%r', uid, callee_uid)
        return jsonify({'ok': False, 'error': 'Invalid callee'}), 400
    if sdp_offer and len(sdp_offer) > 16000:
        app.logger.warning('call_invite invalid_sdp caller=%s callee=%s len=%s', uid, callee_uid, len(sdp_offer))
        return jsonify({'ok': False, 'error': 'Invalid SDP offer'}), 400
    conn = get_db()
    if not _is_user_email_verified(conn, uid):
        conn.close()
        app.logger.warning('call_invite blocked_unverified_caller caller=%s callee=%s', uid, callee_uid)
        return jsonify({'ok': False, 'error': 'Verify your email to place calls'}), 403
    if not _is_user_email_verified(conn, callee_uid):
        conn.close()
        app.logger.warning('call_invite blocked_unverified_callee caller=%s callee=%s', uid, callee_uid)
        return jsonify({'ok': False, 'error': 'This user is not eligible for calls yet'}), 403
    if not _are_friends(conn, uid, callee_uid):
        conn.close()
        app.logger.warning('call_invite blocked_not_friends caller=%s callee=%s', uid, callee_uid)
        return jsonify({'ok': False, 'error': 'Calls require an accepted friendship'}), 403
    if conn.execute('SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)',
                    (uid, callee_uid, callee_uid, uid)).fetchone():
        conn.close()
        app.logger.warning('call_invite blocked_blocklist caller=%s callee=%s', uid, callee_uid)
        return jsonify({'ok': False, 'error': 'Cannot call this user'}), 403
    now = int(time.time())
    cur = conn.execute(
        'INSERT INTO calls (caller_uid, callee_uid, media_kind, started_at, sdp_offer, state) '
        'VALUES (?,?,?,?,?,?)',
        (uid, callee_uid, media_kind, now, sdp_offer, 'inviting')
    )
    call_id = cur.lastrowid
    caller_row = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    caller_name = caller_row['username'] if caller_row else ''
    conn.execute(
        'INSERT INTO call_signals (call_id, from_uid, to_uid, kind, payload) VALUES (?,?,?,?,?)',
        (call_id, uid, callee_uid, 'invite',
         json.dumps({'media_kind': media_kind, 'sdp_offer': sdp_offer,
                     'from_name': caller_name}))
    )
    _insert_call_status_message(conn, uid, callee_uid, _call_status_text('invite'), now)
    conn.commit()
    conn.close()
    app.logger.warning('call_invite ok caller=%s callee=%s call_id=%s media=%s', uid, callee_uid, call_id, media_kind)
    try:
        _push_dm_async(callee_uid, uid, _call_status_text('invite'))
    except Exception:
        pass
    # TODO: send PushKit silent payload to callee.voip_token (Phase 5b)
    return jsonify({'ok': True, 'call_id': call_id, 'state': 'inviting'}), 201


def _verify_call_party(conn, uid, call_id):
    row = conn.execute('SELECT * FROM calls WHERE id=?', (call_id,)).fetchone()
    if not row:
        return None, (jsonify({'ok': False, 'error': 'Call not found'}), 404)
    if uid not in (row['caller_uid'], row['callee_uid']):
        return None, (jsonify({'ok': False, 'error': 'Not a participant'}), 403)
    return row, None


@app.route('/api/calls/answer', methods=['POST'])
def call_answer():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    call_id = data.get('call_id')
    sdp_answer = data.get('sdp_answer') or ''
    if not isinstance(call_id, int) or len(sdp_answer) > 16000:
        return jsonify({'ok': False, 'error': 'Invalid params'}), 400
    conn = get_db()
    row, e = _verify_call_party(conn, uid, call_id)
    if e:
        conn.close()
        return e
    if uid != row['callee_uid']:
        conn.close()
        return jsonify({'ok': False, 'error': 'Only callee can answer'}), 403
    conn.execute('UPDATE calls SET sdp_answer=?, state=? WHERE id=?',
                 (sdp_answer, 'connected', call_id))
    conn.execute(
        'INSERT INTO call_signals (call_id, from_uid, to_uid, kind, payload) VALUES (?,?,?,?,?)',
        (call_id, uid, row['caller_uid'], 'answer', json.dumps({'sdp_answer': sdp_answer}))
    )
    _insert_call_status_message(conn, row['caller_uid'], row['callee_uid'], _call_status_text('connected'))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'state': 'connected'})


@app.route('/api/calls/ice', methods=['POST'])
def call_ice():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    call_id = data.get('call_id')
    candidate = data.get('candidate')
    if not isinstance(call_id, int) or candidate is None:
        return jsonify({'ok': False, 'error': 'Invalid params'}), 400
    payload = json.dumps({'candidate': candidate})
    if len(payload) > 4000:
        return jsonify({'ok': False, 'error': 'Candidate too large'}), 413
    conn = get_db()
    row, e = _verify_call_party(conn, uid, call_id)
    if e:
        conn.close()
        return e
    other = row['callee_uid'] if uid == row['caller_uid'] else row['caller_uid']
    conn.execute(
        'INSERT INTO call_signals (call_id, from_uid, to_uid, kind, payload) VALUES (?,?,?,?,?)',
        (call_id, uid, other, 'ice', payload)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/calls/hangup', methods=['POST'])
def call_hangup():
    uid, err = require_auth(request)
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    call_id = data.get('call_id')
    reason  = (data.get('reason') or 'ended').strip()[:32]
    if not isinstance(call_id, int):
        return jsonify({'ok': False, 'error': 'Invalid call_id'}), 400
    conn = get_db()
    row, e = _verify_call_party(conn, uid, call_id)
    if e:
        conn.close()
        return e
    ended_at = int(time.time())
    duration_seconds = max(0, ended_at - int(row['started_at'] or ended_at))
    if not row['ended_at']:
        conn.execute('UPDATE calls SET ended_at=?, end_reason=?, state=? WHERE id=?',
                     (ended_at, reason, 'ended', call_id))
    other = row['callee_uid'] if uid == row['caller_uid'] else row['caller_uid']
    conn.execute(
        'INSERT INTO call_signals (call_id, from_uid, to_uid, kind, payload) VALUES (?,?,?,?,?)',
        (call_id, uid, other, 'hangup', json.dumps({'reason': reason}))
    )
    _insert_call_status_message(
        conn,
        row['caller_uid'],
        row['callee_uid'],
        _call_status_text(reason, 0 if reason == 'no_answer' else duration_seconds),
        ended_at
    )
    conn.commit()
    conn.close()
    try:
        _push_dm_async(other, uid, _call_status_text(reason, 0 if reason == 'no_answer' else duration_seconds))
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/calls/events')
def call_events():
    """Long-poll the next batch of pending signals addressed to this user."""
    uid, err = require_auth(request)
    if err: return err
    after_id = request.args.get('after_id', 0, type=int)
    deadline = time.time() + 25
    while True:
        conn = get_db()
        _maybe_prune_call_rows(conn)
        head = conn.execute(
            'SELECT COALESCE(MAX(id),0) AS m FROM call_signals WHERE to_uid=?',
            (uid,)
        ).fetchone()
        m = int(head['m'] or 0) if head else 0
        if after_id <= 0:
            if m > 200:
                after_id = m - 200
        elif after_id > m:
            # Client cursor can become stale/corrupt (e.g. app reinstall or
            # storage mismatch). Clamp to the recent tail so new invite/answer
            # signals are never starved behind an impossible cursor.
            app.logger.warning(
                'call_events cursor clamp uid=%s after_id=%s max_id=%s',
                uid,
                after_id,
                m,
            )
            after_id = (m - 200) if m > 200 else 0
        rows = conn.execute(
            'SELECT id, call_id, from_uid, kind, payload, created_at '
            'FROM call_signals WHERE to_uid=? AND id>? ORDER BY id ASC LIMIT 50',
            (uid, after_id)
        ).fetchall()
        if rows:
            # Emit one 'ringing' ack to caller when callee has actually received
            # an invite event (lets caller UI distinguish delivered vs undelivered).
            ring_acks = []
            with _CALL_LOCK:
                for r in rows:
                    if r['kind'] != 'invite':
                        continue
                    key = (int(r['call_id']), uid)
                    if key in _CALL_RINGED:
                        continue
                    _CALL_RINGED.add(key)
                    ring_acks.append((int(r['call_id']), uid, int(r['from_uid'])))
            if ring_acks:
                for (call_id, callee_uid, caller_uid) in ring_acks:
                    conn.execute(
                        'INSERT INTO call_signals (call_id, from_uid, to_uid, kind, payload) VALUES (?,?,?,?,?)',
                        (call_id, callee_uid, caller_uid, 'ringing', json.dumps({'callee_uid': callee_uid}))
                    )
                conn.commit()
        conn.close()
        if rows or time.time() >= deadline:
            return jsonify({'ok': True, 'events': [
                {'id': r['id'], 'call_id': r['call_id'], 'from_uid': r['from_uid'],
                 'kind': r['kind'], 'payload': r['payload'],
                 'created_at': r['created_at']}
                for r in rows
            ]})
        time.sleep(0.5)


@app.route('/api/calls/audio', methods=['POST'])
def call_audio_post():
    """Caller/callee uploads one audio batch (μ-law 8 kHz mono, ~200 ms).
    Body: raw octet-stream. Query: call_id."""
    uid, err = require_auth(request)
    if err: return err
    try:
        call_id = int(request.args.get('call_id', '0'))
    except ValueError:
        return jsonify({'ok': False, 'error': 'Bad call_id'}), 400
    if call_id <= 0:
        return jsonify({'ok': False, 'error': 'Bad call_id'}), 400
    data = request.get_data(cache=False, as_text=False) or b''
    if not data or len(data) > CALL_AUDIO_MAX_FRAME:
        return jsonify({'ok': False, 'error': 'Bad frame'}), 400
    conn = get_db()
    row, e = _verify_call_party(conn, uid, call_id)
    conn.close()
    if e: return e
    other = row['callee_uid'] if uid == row['caller_uid'] else row['caller_uid']
    now = time.time()
    with _CALL_AUDIO_LOCK:
        seq = _CALL_AUDIO_SEQ.get((call_id, uid), 0) + 1
        _CALL_AUDIO_SEQ[(call_id, uid)] = seq
        key = (call_id, other)
        q = _CALL_AUDIO.get(key)
        if q is None:
            from collections import deque as _dq
            q = _dq(maxlen=CALL_AUDIO_QMAX)
            _CALL_AUDIO[key] = q
        q.append((seq, data, now))
    return jsonify({'ok': True, 'seq': seq})


@app.route('/api/calls/audio', methods=['GET'])
def call_audio_get():
    """Pull all audio batches addressed to the caller for a given call.
    Query: call_id, after_seq (optional, default 0). Returns concatenated batches
    as application/octet-stream with X-CG-Last-Seq header so the client can advance.
    Returns immediately (no long-poll) — clients tick at ~200 ms."""
    uid, err = require_auth(request)
    if err: return err
    try:
        call_id   = int(request.args.get('call_id', '0'))
        after_seq = int(request.args.get('after_seq', '0'))
    except ValueError:
        return ('', 400)
    if call_id <= 0:
        return ('', 400)
    conn = get_db()
    row, e = _verify_call_party(conn, uid, call_id)
    conn.close()
    if e: return e
    now = time.time()
    out  = bytearray()
    last = after_seq
    key  = (call_id, uid)
    with _CALL_AUDIO_LOCK:
        q = _CALL_AUDIO.get(key)
        if q:
            for seq, data, ts in list(q):
                if seq <= after_seq:
                    continue
                if (now - ts) > CALL_AUDIO_TTL:
                    continue
                out.extend(data)
                if seq > last:
                    last = seq
    resp = make_response(bytes(out))
    resp.headers['Content-Type']  = 'application/octet-stream'
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-CG-Last-Seq'] = str(last)
    return resp


# Run init_db on module load so gunicorn workers also initialise the schema and bot user
init_db()

if __name__ == '__main__':
    print('ChronoGraph Chat API starting on :5000')
    app.run(host='0.0.0.0', port=5000, threaded=True)

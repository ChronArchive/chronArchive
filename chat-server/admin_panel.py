#!/usr/bin/env python3
"""
Chronograph Admin Panel
Binds to Tailscale interface only — http://100.95.1.7:8001
NOT exposed through Cloudflare or the public internet.
"""
import os, sqlite3, time, html as _html, json, secrets as _secrets
from functools import wraps
from flask import Flask, request, redirect, session, Response

# ── Config ─────────────────────────────────────────────────────────────────────
DB_PATH          = os.environ.get('CG_DB',  '/opt/chronograph-chat/chat.db')
FILTER_WORDS_FILE = '/opt/chronograph-chat/filter_words.json'
PASS_FILE        = '/opt/chronograph-chat/admin_pass.txt'
SECRET_FILE      = '/opt/chronograph-chat/admin_secret.txt'
BIND_HOST        = '100.95.1.7'
BIND_PORT        = 8001

# ── Startup: load or create persistent secret key + admin password ─────────────
def _load_or_create(path, gen):
    if os.path.exists(path):
        return open(path).read().strip()
    val = gen()
    with open(path, 'w') as f:
        f.write(val)
    return val

SECRET_KEY = _load_or_create(SECRET_FILE, lambda: _secrets.token_hex(32))
ADMIN_PASS = _load_or_create(PASS_FILE,   lambda: _secrets.token_urlsafe(12))

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ── DB helpers ─────────────────────────────────────────────────────────────────
def get_db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def q1(sql, *args):
    c = get_db()
    r = c.execute(sql, args).fetchone()
    c.close()
    return r

def qa(sql, *args):
    c = get_db()
    r = c.execute(sql, args).fetchall()
    c.close()
    return r

def exe(sql, *args):
    c = get_db()
    c.execute(sql, args)
    c.commit()
    c.close()

def exe_many(ops):
    """Execute multiple (sql, args) tuples in one transaction."""
    c = get_db()
    for sql, args in ops:
        c.execute(sql, args)
    c.commit()
    c.close()

# ── Template helpers ───────────────────────────────────────────────────────────
def h(s):
    return _html.escape(str(s) if s is not None else '')

def fmt_bytes(n):
    n = int(n or 0)
    if n < 1024:        return f'{n} B'
    if n < 1048576:     return f'{n/1024:.1f} KB'
    if n < 1073741824:  return f'{n/1048576:.1f} MB'
    return f'{n/1073741824:.2f} GB'

def fmt_ts(ts):
    if not ts: return '—'
    return time.strftime('%b %d %Y %H:%M', time.localtime(int(ts)))

def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if not session.get('ok'):
            return redirect('/login')
        return f(*a, **kw)
    return wrapped

# ── Base layout ────────────────────────────────────────────────────────────────
_BASE = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ — Chronograph Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;background:#0d0d0d;color:#ddd;font-size:14px;min-height:100vh}
a{color:#4a9eff;text-decoration:none}a:hover{text-decoration:underline}
nav{background:#141414;border-bottom:1px solid #2a2a2a;padding:0 20px;display:flex;align-items:center;gap:0;height:46px}
nav .brand{font-weight:700;font-size:15px;color:#fff;margin-right:24px;white-space:nowrap}
nav a{color:#999;font-size:13px;padding:0 12px;height:46px;display:flex;align-items:center;border-bottom:2px solid transparent}
nav a:hover{color:#fff;text-decoration:none}
nav a.active{color:#fff;border-bottom-color:#4a9eff}
nav .spacer{flex:1}
.main{padding:24px;max-width:1280px;margin:0 auto}
h1{font-size:22px;font-weight:700;color:#fff;margin-bottom:18px}
h2{font-size:15px;font-weight:600;color:#ccc;margin:22px 0 10px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:#1c1c1c;border:1px solid #2a2a2a;border-radius:8px;padding:14px 16px}
.stat-card .val{font-size:28px;font-weight:700;color:#4a9eff;line-height:1}
.stat-card .lbl{font-size:11px;color:#666;margin-top:5px;text-transform:uppercase;letter-spacing:.5px}
table{width:100%;border-collapse:collapse;background:#141414;border:1px solid #222;border-radius:8px;overflow:hidden}
th{background:#1c1c1c;color:#888;font-size:11px;font-weight:600;text-align:left;padding:9px 12px;border-bottom:1px solid #2a2a2a;text-transform:uppercase;letter-spacing:.4px;white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid #1c1c1c;vertical-align:middle;font-size:13px}
tr:last-child td{border-bottom:none}
tr:hover td{background:#181818}
.badge{display:inline-block;padding:2px 7px;border-radius:10px;font-size:11px;font-weight:600;vertical-align:middle}
.badge.admin{background:#1a3060;color:#6ca3f5}
.badge.banned{background:#3d1010;color:#f56c6c}
.badge.ok{background:#0f2b0f;color:#6cf56c}
.btn{display:inline-block;padding:4px 11px;border-radius:5px;border:none;cursor:pointer;font-size:12px;font-weight:500;text-decoration:none;line-height:1.6;font-family:inherit}
.btn-red{background:#2d0e0e;color:#f56c6c;border:1px solid #4a1a1a}.btn-red:hover{background:#3d1414}
.btn-blue{background:#0e1e3d;color:#6ca3f5;border:1px solid #1a3060}.btn-blue:hover{background:#162a55}
.btn-green{background:#0e2a0e;color:#6cf56c;border:1px solid #1a4a1a}.btn-green:hover{background:#142e14}
.btn-gray{background:#222;color:#aaa;border:1px solid #3a3a3a}.btn-gray:hover{background:#2c2c2c}
.btn-orange{background:#2d1a00;color:#f5a56c;border:1px solid #4a2a00}.btn-orange:hover{background:#3d2200}
form{display:inline}
.flash{padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px;border:1px solid}
.flash.ok{background:#0f2b0f;border-color:#1a4a1a;color:#6cf56c}
.flash.err{background:#2d0e0e;border-color:#4a1a1a;color:#f56c6c}
.section{background:#141414;border:1px solid #222;border-radius:8px;padding:18px;margin-bottom:20px}
input[type=text],input[type=password],textarea,select{background:#0d0d0d;border:1px solid #333;border-radius:5px;color:#ddd;padding:8px 10px;font-size:13px;font-family:inherit}
input[type=text]:focus,input[type=password]:focus,textarea:focus{outline:none;border-color:#4a9eff}
input[type=submit],.submit-btn{padding:8px 22px;background:#0e1e3d;color:#6ca3f5;border:1px solid #1a3060;border-radius:5px;cursor:pointer;font-size:13px;font-family:inherit}
input[type=submit]:hover,.submit-btn:hover{background:#162a55}
.mono{font-family:'SF Mono',SFMono-Regular,Consolas,monospace;font-size:12px}
.actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.del-confirm{color:#888;font-size:11px;margin-top:4px}
.truncate{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
<nav>
  <span class="brand">⚙ Chronograph Admin</span>
  <a href="/" __ACT_DASH__>Dashboard</a>
  <a href="/users" __ACT_USERS__>Users</a>
  <a href="/posts" __ACT_POSTS__>Posts</a>
  <a href="/messages" __ACT_MSGS__>Messages</a>
  <a href="/filter" __ACT_FILTER__>Filter</a>
  <span class="spacer"></span>
  <a href="/settings" __ACT_SETTINGS__>Settings</a>
  <a href="/logout">Logout</a>
</nav>
<div class="main">
__FLASH__
__BODY__
</div></body></html>'''

_LOGIN_PAGE = '''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><title>Admin Login — Chronograph</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#0d0d0d;color:#ddd;font-family:-apple-system,BlinkMacSystemFont,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh}
.box{background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:36px 40px;width:340px}
h1{font-size:20px;font-weight:700;color:#fff;text-align:center;margin-bottom:22px}
.sub{text-align:center;font-size:12px;color:#555;margin-bottom:22px;margin-top:-16px}
label{display:block;font-size:12px;color:#666;margin-bottom:5px;text-transform:uppercase;letter-spacing:.4px}
input[type=password]{display:block;width:100%;padding:10px 12px;background:#0d0d0d;border:1px solid #333;border-radius:6px;color:#ddd;font-size:14px;margin-bottom:14px}
input[type=password]:focus{outline:none;border-color:#4a9eff}
input[type=submit]{width:100%;padding:10px;background:#0e1e3d;color:#6ca3f5;border:1px solid #1a3060;border-radius:6px;cursor:pointer;font-size:14px}
input[type=submit]:hover{background:#162a55}
.err{background:#2d0e0e;border:1px solid #4a1a1a;color:#f56c6c;padding:9px 12px;border-radius:6px;margin-bottom:14px;font-size:13px}
</style></head><body><div class="box">
<h1>⚙ Admin Panel</h1>
<p class="sub">Chronograph · Tailscale only</p>
{err_html}
<form method="post">
<label>Password</label>
<input type="password" name="password" autofocus>
<input type="submit" value="Sign In">
</form>
</div></body></html>'''

def page(body, title='', active=''):
    acts = {k: '' for k in ['dash','users','posts','msgs','filter','settings']}
    if active in acts:
        acts[active] = 'class="active"'
    flash = session.pop('flash', None)
    flash_html = ''
    if flash:
        cls = 'err' if flash[0] == '!' else 'ok'
        txt = flash[1:] if flash[0] == '!' else flash
        flash_html = f'<div class="flash {cls}">{h(txt)}</div>'
    return (_BASE
        .replace('__TITLE__',        h(title or active.capitalize() or 'Admin'))
        .replace('__FLASH__',         flash_html)
        .replace('__BODY__',          body)
        .replace('__ACT_DASH__',      acts['dash'])
        .replace('__ACT_USERS__',     acts['users'])
        .replace('__ACT_POSTS__',     acts['posts'])
        .replace('__ACT_MSGS__',      acts['msgs'])
        .replace('__ACT_FILTER__',    acts['filter'])
        .replace('__ACT_SETTINGS__',  acts['settings'])
    )

def flash(msg):
    session['flash'] = msg

# ── Auth ───────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    err_html = ''
    if request.method == 'POST':
        pw = request.form.get('password', '')
        if pw == ADMIN_PASS:
            session['ok'] = True
            return redirect('/')
        err_html = '<div class="err">Incorrect password.</div>'
    return _LOGIN_PAGE.replace('{err_html}', err_html)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    now = int(time.time())
    week_ago  = now - 604800
    day_ago   = now - 86400
    month_ago = now - 2592000

    total_users   = q1('SELECT COUNT(*) FROM users')[0]
    banned_users  = q1('SELECT COUNT(*) FROM users WHERE is_banned=1')[0]
    admin_users   = q1('SELECT COUNT(*) FROM users WHERE is_admin=1')[0]
    new_today     = q1('SELECT COUNT(*) FROM users WHERE created_at > ?', day_ago)[0]
    new_week      = q1('SELECT COUNT(*) FROM users WHERE created_at > ?', week_ago)[0]
    new_month     = q1('SELECT COUNT(*) FROM users WHERE created_at > ?', month_ago)[0]

    total_msgs    = q1('SELECT COUNT(*) FROM messages WHERE deleted=0')[0]
    msgs_today    = q1('SELECT COUNT(*) FROM messages WHERE deleted=0 AND created_at > ?', day_ago)[0]
    msgs_week     = q1('SELECT COUNT(*) FROM messages WHERE deleted=0 AND created_at > ?', week_ago)[0]

    total_posts   = q1('SELECT COUNT(*) FROM posts WHERE deleted=0')[0]
    posts_today   = q1('SELECT COUNT(*) FROM posts WHERE deleted=0 AND created_at > ?', day_ago)[0]

    av_storage    = q1('SELECT SUM(LENGTH(avatar_b64)) FROM users')[0] or 0
    post_storage  = q1('SELECT SUM(LENGTH(media_data)) FROM posts WHERE deleted=0')[0] or 0
    db_size       = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0

    last_msg_row  = q1('SELECT created_at FROM messages WHERE deleted=0 ORDER BY created_at DESC LIMIT 1')
    last_msg      = fmt_ts(last_msg_row[0]) if last_msg_row else '—'
    last_signup   = q1('SELECT created_at FROM users ORDER BY created_at DESC LIMIT 1')
    last_user_ts  = fmt_ts(last_signup[0]) if last_signup else '—'

    # Active users (sent a message in last 7 days)
    active_week   = q1('SELECT COUNT(DISTINCT from_user_id) FROM messages WHERE deleted=0 AND created_at > ?', week_ago)[0]

    # Top posters this week
    top_posters = qa('''
        SELECT u.username, COUNT(*) cnt
        FROM messages m JOIN users u ON u.id=m.from_user_id
        WHERE m.deleted=0 AND m.created_at > ?
        GROUP BY m.from_user_id ORDER BY cnt DESC LIMIT 5
    ''', week_ago)

    # Recent signups
    recent_users = qa('''
        SELECT id, username, created_at, is_admin, is_banned
        FROM users ORDER BY created_at DESC LIMIT 8
    ''')

    # Signup chart (users per day, last 14 days)
    chart_data = []
    for i in range(13, -1, -1):
        day_start = now - i * 86400 - (now % 86400)
        day_end   = day_start + 86400
        count     = q1('SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?', day_start, day_end)[0]
        label     = time.strftime('%m/%d', time.localtime(day_start))
        chart_data.append((label, count))

    max_val = max((c for _, c in chart_data), default=1) or 1
    bars = ''
    for label, count in chart_data:
        pct = int(count / max_val * 60)
        bars += f'''<div style="display:flex;flex-direction:column;align-items:center;gap:3px">
          <div style="font-size:10px;color:#555">{count if count else ''}</div>
          <div style="width:28px;height:{pct}px;min-height:2px;background:#1a3060;border-radius:2px 2px 0 0"></div>
          <div style="font-size:9px;color:#555;writing-mode:vertical-rl;transform:rotate(180deg);height:34px">{label}</div>
        </div>'''

    signup_rows = ''.join(f'''<tr>
      <td><a href="/users/{u['id']}">{h(u['username'])}</a></td>
      <td class="mono">{fmt_ts(u['created_at'])}</td>
      <td>{"<span class='badge admin'>Admin</span> " if u['is_admin'] else ""}{"<span class='badge banned'>Banned</span>" if u['is_banned'] else "<span class='badge ok'>Active</span>"}</td>
    </tr>''' for u in recent_users)

    poster_rows = ''.join(f'<tr><td>{h(r["username"])}</td><td style="color:#4a9eff;text-align:right">{r["cnt"]:,}</td></tr>' for r in top_posters)

    body = f'''
    <h1>Dashboard</h1>
    <div class="stat-grid">
      <div class="stat-card"><div class="val">{total_users:,}</div><div class="lbl">Total Users</div></div>
      <div class="stat-card"><div class="val">{new_today}</div><div class="lbl">New Today</div></div>
      <div class="stat-card"><div class="val">{new_week}</div><div class="lbl">New This Week</div></div>
      <div class="stat-card"><div class="val">{new_month}</div><div class="lbl">New This Month</div></div>
      <div class="stat-card"><div class="val">{active_week}</div><div class="lbl">Active (7d)</div></div>
      <div class="stat-card"><div class="val">{banned_users}</div><div class="lbl">Banned</div></div>
      <div class="stat-card"><div class="val">{total_msgs:,}</div><div class="lbl">Messages Total</div></div>
      <div class="stat-card"><div class="val">{msgs_today:,}</div><div class="lbl">Messages Today</div></div>
      <div class="stat-card"><div class="val">{msgs_week:,}</div><div class="lbl">Messages 7d</div></div>
      <div class="stat-card"><div class="val">{total_posts:,}</div><div class="lbl">Posts Total</div></div>
      <div class="stat-card"><div class="val">{posts_today}</div><div class="lbl">Posts Today</div></div>
      <div class="stat-card"><div class="val">{fmt_bytes(post_storage)}</div><div class="lbl">Post Storage</div></div>
      <div class="stat-card"><div class="val">{fmt_bytes(av_storage)}</div><div class="lbl">Avatar Storage</div></div>
      <div class="stat-card"><div class="val">{fmt_bytes(db_size)}</div><div class="lbl">DB File Size</div></div>
      <div class="stat-card"><div class="val" style="font-size:13px;line-height:1.3">{last_user_ts}</div><div class="lbl">Last Signup</div></div>
      <div class="stat-card"><div class="val" style="font-size:13px;line-height:1.3">{last_msg}</div><div class="lbl">Last Message</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 240px;gap:16px">
      <div>
        <h2>Signups (last 14 days)</h2>
        <div class="section" style="padding:16px">
          <div style="display:flex;align-items:flex-end;gap:4px;height:80px">
            {bars}
          </div>
        </div>
        <h2>Recent Signups</h2>
        <table>
          <tr><th>Username</th><th>Joined</th><th>Status</th></tr>
          {signup_rows}
        </table>
      </div>
      <div>
        <h2>Top Chatters (7d)</h2>
        <table>
          <tr><th>User</th><th style="text-align:right">Msgs</th></tr>
          {poster_rows or '<tr><td colspan="2" style="color:#555;text-align:center;padding:16px">No messages yet</td></tr>'}
        </table>
      </div>
    </div>
    '''
    return page(body, title='Dashboard', active='dash')

# ── Users ──────────────────────────────────────────────────────────────────────
@app.route('/users')
@login_required
def users_page():
    sort = request.args.get('sort', 'joined')
    order_map = {
        'joined':   'u.created_at DESC',
        'username': 'u.username ASC',
        'msgs':     'msg_count DESC',
        'posts':    'post_count DESC',
        'storage':  'storage DESC',
    }
    order = order_map.get(sort, 'u.created_at DESC')
    q = request.args.get('q', '').strip()

    if q:
        where = "WHERE u.username LIKE ?"
        args  = (f'%{q}%',)
    else:
        where = ''
        args  = ()

    users = qa(f'''
        SELECT u.id, u.username, u.created_at, u.is_admin, u.is_banned, u.friend_code,
               LENGTH(u.avatar_b64) AS av_size,
               COALESCE(mc.cnt, 0)  AS msg_count,
               COALESCE(pc.cnt, 0)  AS post_count,
               COALESCE(ps.sz, 0)   AS post_storage,
               LENGTH(u.avatar_b64) + COALESCE(ps.sz, 0) AS storage
        FROM users u
        LEFT JOIN (SELECT from_user_id, COUNT(*) cnt FROM messages WHERE deleted=0 GROUP BY from_user_id) mc ON mc.from_user_id=u.id
        LEFT JOIN (SELECT user_id, COUNT(*) cnt FROM posts WHERE deleted=0 GROUP BY user_id) pc ON pc.user_id=u.id
        LEFT JOIN (SELECT user_id, SUM(LENGTH(media_data)) sz FROM posts WHERE deleted=0 GROUP BY user_id) ps ON ps.user_id=u.id
        {where} ORDER BY {order}
    ''', *args)

    def sl(col, label):
        active_style = ' style="color:#fff"' if sort == col else ''
        return f'<a href="/users?sort={col}&q={h(q)}"{active_style}>{label}</a>'

    rows = ''
    for u in users:
        status = ''
        if u['is_admin']:  status += "<span class='badge admin'>Admin</span> "
        if u['is_banned']: status += "<span class='badge banned'>Banned</span>"
        else:              status += "<span class='badge ok'>Active</span>"

        ban_lbl   = 'Unban' if u['is_banned'] else 'Ban'
        ban_cls   = 'btn-green' if u['is_banned'] else 'btn-red'
        adm_lbl   = 'Demote' if u['is_admin'] else 'Admin'
        adm_cls   = 'btn-orange' if u['is_admin'] else 'btn-blue'

        rows += f'''<tr>
          <td><a href="/users/{u['id']}">{h(u['username'])}</a></td>
          <td class="mono" style="font-size:11px;color:#666">{fmt_ts(u['created_at'])}</td>
          <td style="text-align:right;color:#aaa">{u['msg_count']:,}</td>
          <td style="text-align:right;color:#aaa">{u['post_count']}</td>
          <td style="text-align:right;color:#aaa">{fmt_bytes(u['storage'])}</td>
          <td class="mono" style="color:#555;letter-spacing:2px">{h(u['friend_code'] or '—')}</td>
          <td>{status}</td>
          <td>
            <div class="actions">
              <form method="post" action="/users/{u['id']}/toggle_ban">
                <button class="btn {ban_cls}" type="submit">{ban_lbl}</button>
              </form>
              <form method="post" action="/users/{u['id']}/toggle_admin">
                <button class="btn {adm_cls}" type="submit">{adm_lbl}</button>
              </form>
            </div>
          </td>
        </tr>'''

    search_val = h(q)
    body = f'''
    <h1>Users <span style="font-size:16px;color:#666;font-weight:400">({len(users)} shown)</span></h1>
    <div style="display:flex;gap:12px;align-items:center;margin-bottom:14px">
      <form method="get" style="display:flex;gap:8px;align-items:center">
        <input type="text" name="q" value="{search_val}" placeholder="Search username…" style="width:240px">
        <input type="hidden" name="sort" value="{h(sort)}">
        <input type="submit" value="Search">
      </form>
      <span style="font-size:12px;color:#555">
        Sort: {sl('joined','Joined')} · {sl('username','Username')} · {sl('msgs','Messages')} · {sl('posts','Posts')} · {sl('storage','Storage')}
      </span>
    </div>
    <table>
      <tr><th>Username</th><th>Joined</th><th style="text-align:right">Msgs</th><th style="text-align:right">Posts</th><th style="text-align:right">Storage</th><th>Friend Code</th><th>Status</th><th>Actions</th></tr>
      {rows or '<tr><td colspan="8" style="color:#555;text-align:center;padding:20px">No users found</td></tr>'}
    </table>'''
    return page(body, title='Users', active='users')

@app.route('/users/<int:uid>')
@login_required
def user_detail(uid):
    u = q1('SELECT * FROM users WHERE id=?', uid)
    if not u:
        flash('!User not found')
        return redirect('/users')

    msg_sent  = q1('SELECT COUNT(*) FROM messages WHERE from_user_id=? AND deleted=0', uid)[0]
    msg_recv  = q1('SELECT COUNT(*) FROM messages WHERE to_user_id=? AND deleted=0', uid)[0]
    post_cnt  = q1('SELECT COUNT(*) FROM posts WHERE user_id=? AND deleted=0', uid)[0]
    post_sz   = q1('SELECT SUM(LENGTH(media_data)) FROM posts WHERE user_id=? AND deleted=0', uid)[0] or 0
    friend_cnt = q1("SELECT COUNT(*) FROM friends WHERE (from_user_id=? OR to_user_id=?) AND status='accepted'", uid, uid)[0]

    # Daily message volume last 14 days
    now = int(time.time())
    daily = []
    for i in range(13, -1, -1):
        ds = now - i * 86400 - (now % 86400)
        de = ds + 86400
        c  = q1('SELECT COUNT(*) FROM messages WHERE from_user_id=? AND deleted=0 AND created_at>=? AND created_at<?', uid, ds, de)[0]
        daily.append((time.strftime('%m/%d', time.localtime(ds)), c))
    max_d = max((c for _, c in daily), default=1) or 1
    dbars = ''
    for lbl, c in daily:
        pct = int(c / max_d * 40)
        dbars += f'<div style="display:flex;flex-direction:column;align-items:center;gap:2px"><div style="font-size:9px;color:#444">{c if c else ""}</div><div style="width:20px;height:{pct}px;min-height:2px;background:#1a3060;border-radius:2px 2px 0 0"></div><div style="font-size:8px;color:#444;writing-mode:vertical-rl;transform:rotate(180deg);height:28px">{lbl}</div></div>'

    recent_msgs = qa('''
        SELECT m.text, m.created_at, u2.username AS to_name
        FROM messages m JOIN users u2 ON u2.id=m.to_user_id
        WHERE m.from_user_id=? AND m.deleted=0
        ORDER BY m.created_at DESC LIMIT 30
    ''', uid)

    recent_posts = qa('''
        SELECT id, title, type, created_at, LENGTH(media_data) AS sz
        FROM posts WHERE user_id=? AND deleted=0
        ORDER BY created_at DESC LIMIT 20
    ''', uid)

    msg_rows = ''.join(f'''<tr>
      <td style="color:#666">{h(m["to_name"])}</td>
      <td class="truncate">{h(m["text"][:120])}</td>
      <td class="mono" style="font-size:11px;color:#555;white-space:nowrap">{fmt_ts(m["created_at"])}</td>
    </tr>''' for m in recent_msgs)

    post_rows = ''.join(f'''<tr>
      <td>{h(p["title"] or "(no title)")}</td>
      <td style="color:#666">{h(p["type"])}</td>
      <td style="text-align:right;color:#aaa">{fmt_bytes(p["sz"])}</td>
      <td class="mono" style="font-size:11px;color:#555;white-space:nowrap">{fmt_ts(p["created_at"])}</td>
      <td><form method="post" action="/posts/{p["id"]}/delete"><button class="btn btn-red" type="submit">Delete</button></form></td>
    </tr>''' for p in recent_posts)

    av_html = ''
    if u['avatar_b64']:
        av_html = f'<img src="{h(u["avatar_b64"])}" style="width:56px;height:56px;border-radius:28px;float:right;margin:0 0 12px 16px;border:2px solid #2a2a2a">'

    status = ''
    if u['is_admin']:  status += "<span class='badge admin'>Admin</span> "
    if u['is_banned']: status += "<span class='badge banned'>Banned</span>"
    else:              status += "<span class='badge ok'>Active</span>"

    ban_lbl = 'Unban User' if u['is_banned'] else 'Ban User'
    ban_cls = 'btn-green'   if u['is_banned'] else 'btn-red'
    adm_lbl = 'Demote from Admin' if u['is_admin'] else 'Promote to Admin'

    body = f'''
    <a href="/users" class="btn btn-gray" style="margin-bottom:18px;display:inline-block">← Back to Users</a>
    <div class="section">
      {av_html}
      <h1 style="margin-bottom:6px">{h(u["username"])} {status}</h1>
      <p style="color:#555;font-size:12px;margin-bottom:10px">
        ID: {u["id"]} &nbsp;·&nbsp; Joined: {fmt_ts(u["created_at"])} &nbsp;·&nbsp;
        Friend Code: <span class="mono" style="color:#888;letter-spacing:2px">{h(u["friend_code"] or "—")}</span>
      </p>
      <p style="color:#888;font-size:13px">{h(u["bio"] or "(no bio)")}</p>
    </div>
    <div class="stat-grid">
      <div class="stat-card"><div class="val">{msg_sent:,}</div><div class="lbl">Messages Sent</div></div>
      <div class="stat-card"><div class="val">{msg_recv:,}</div><div class="lbl">Messages Received</div></div>
      <div class="stat-card"><div class="val">{post_cnt}</div><div class="lbl">Posts</div></div>
      <div class="stat-card"><div class="val">{fmt_bytes(post_sz)}</div><div class="lbl">Post Storage</div></div>
      <div class="stat-card"><div class="val">{fmt_bytes(len(u["avatar_b64"] or ""))}</div><div class="lbl">Avatar Size</div></div>
      <div class="stat-card"><div class="val">{friend_cnt}</div><div class="lbl">Friends</div></div>
    </div>
    <div style="margin-bottom:22px;display:flex;gap:8px;flex-wrap:wrap">
      <form method="post" action="/users/{uid}/toggle_ban">
        <button class="btn {ban_cls}">{ban_lbl}</button>
      </form>
      <form method="post" action="/users/{uid}/toggle_admin">
        <button class="btn btn-blue">{adm_lbl}</button>
      </form>
      <form method="post" action="/users/{uid}/delete"
            onsubmit="return confirm('Delete {h(u["username"])} and ALL their data permanently?')">
        <button class="btn btn-red" type="submit">⚠ Delete User</button>
      </form>
    </div>
    <h2>Message Activity (14 days)</h2>
    <div class="section" style="padding:14px">
      <div style="display:flex;align-items:flex-end;gap:3px;height:60px">
        {dbars}
      </div>
    </div>
    {('<h2>Recent Posts</h2><table><tr><th>Title</th><th>Type</th><th style="text-align:right">Size</th><th>Posted</th><th></th></tr>'+post_rows+'</table>') if post_rows else ''}
    {('<h2>Recent Messages Sent</h2><table><tr><th>To</th><th>Message</th><th>Time</th></tr>'+msg_rows+'</table>') if msg_rows else ''}
    '''
    return page(body, title=u['username'], active='users')

@app.route('/users/<int:uid>/toggle_ban', methods=['POST'])
@login_required
def toggle_ban(uid):
    u = q1('SELECT is_banned, username FROM users WHERE id=?', uid)
    if u:
        exe('UPDATE users SET is_banned=? WHERE id=?', 0 if u['is_banned'] else 1, uid)
        flash(f'{"Unbanned" if u["is_banned"] else "Banned"} {u["username"]}')
    return redirect(request.referrer or '/users')

@app.route('/users/<int:uid>/toggle_admin', methods=['POST'])
@login_required
def toggle_admin(uid):
    u = q1('SELECT is_admin, username FROM users WHERE id=?', uid)
    if u:
        exe('UPDATE users SET is_admin=? WHERE id=?', 0 if u['is_admin'] else 1, uid)
        flash(f'{"Demoted" if u["is_admin"] else "Promoted"} {u["username"]}')
    return redirect(request.referrer or '/users')

@app.route('/users/<int:uid>/delete', methods=['POST'])
@login_required
def delete_user(uid):
    u = q1('SELECT username FROM users WHERE id=?', uid)
    if u:
        exe_many([
            ('DELETE FROM messages  WHERE from_user_id=? OR to_user_id=?', (uid, uid)),
            ('DELETE FROM posts     WHERE user_id=?',                       (uid,)),
            ('DELETE FROM friends   WHERE from_user_id=? OR to_user_id=?', (uid, uid)),
            ('DELETE FROM blocks    WHERE blocker_id=? OR blocked_id=?',   (uid, uid)),
            ('DELETE FROM sessions  WHERE user_id=?',                       (uid,)),
            ('DELETE FROM users     WHERE id=?',                             (uid,)),
        ])
        flash(f'Deleted user {u["username"]} and all their data')
    return redirect('/users')

# ── Posts ──────────────────────────────────────────────────────────────────────
@app.route('/posts')
@login_required
def posts_page():
    q = request.args.get('q', '').strip()
    tp = request.args.get('type', '')

    where_parts = ['p.deleted=0']
    args = []
    if q:
        where_parts.append('(p.title LIKE ? OR p.tags LIKE ? OR u.username LIKE ?)')
        args += [f'%{q}%', f'%{q}%', f'%{q}%']
    if tp:
        where_parts.append('p.type=?')
        args.append(tp)

    where = 'WHERE ' + ' AND '.join(where_parts)

    posts = qa(f'''
        SELECT p.id, p.title, p.type, p.tags, p.created_at, p.deleted,
               LENGTH(p.media_data) AS sz, u.username, u.id AS uid
        FROM posts p JOIN users u ON u.id=p.user_id
        {where}
        ORDER BY p.created_at DESC LIMIT 200
    ''', *args)

    total_posts   = q1('SELECT COUNT(*) FROM posts WHERE deleted=0')[0]
    total_storage = q1('SELECT SUM(LENGTH(media_data)) FROM posts WHERE deleted=0')[0] or 0
    img_count     = q1("SELECT COUNT(*) FROM posts WHERE deleted=0 AND type='image'")[0]
    vid_count     = q1("SELECT COUNT(*) FROM posts WHERE deleted=0 AND type='video'")[0]

    rows = ''.join(f'''<tr>
      <td style="color:#555;font-size:11px">{p["id"]}</td>
      <td class="truncate" style="max-width:220px">{h(p["title"] or "(no title)")}</td>
      <td><span class="badge {'ok' if p['type']=='image' else 'admin'}">{h(p["type"])}</span></td>
      <td><a href="/users/{p["uid"]}">{h(p["username"])}</a></td>
      <td style="text-align:right;color:#aaa">{fmt_bytes(p["sz"])}</td>
      <td class="mono" style="font-size:11px;color:#555;white-space:nowrap">{fmt_ts(p["created_at"])}</td>
      <td class="truncate" style="color:#555;max-width:140px">{h(p["tags"] or "")}</td>
      <td>
        <form method="post" action="/posts/{p["id"]}/delete"
              onsubmit="return confirm('Delete this post?')">
          <button class="btn btn-red" type="submit">Delete</button>
        </form>
      </td>
    </tr>''' for p in posts)

    body = f'''
    <h1>Posts</h1>
    <div class="stat-grid">
      <div class="stat-card"><div class="val">{total_posts:,}</div><div class="lbl">Total Posts</div></div>
      <div class="stat-card"><div class="val">{img_count:,}</div><div class="lbl">Images</div></div>
      <div class="stat-card"><div class="val">{vid_count:,}</div><div class="lbl">Videos</div></div>
      <div class="stat-card"><div class="val">{fmt_bytes(total_storage)}</div><div class="lbl">Total Storage</div></div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:14px">
      <form method="get" style="display:flex;gap:8px;align-items:center">
        <input type="text" name="q" value="{h(q)}" placeholder="Search title, tags, user…" style="width:240px">
        <select name="type" style="padding:8px 10px">
          <option value="" {'selected' if not tp else ''}>All types</option>
          <option value="image" {'selected' if tp=='image' else ''}>Images</option>
          <option value="video" {'selected' if tp=='video' else ''}>Videos</option>
        </select>
        <input type="submit" value="Search">
      </form>
      <span style="font-size:12px;color:#555">Showing last 200</span>
    </div>
    <table>
      <tr><th>#</th><th>Title</th><th>Type</th><th>Author</th><th style="text-align:right">Size</th><th>Posted</th><th>Tags</th><th></th></tr>
      {rows or '<tr><td colspan="8" style="color:#555;text-align:center;padding:20px">No posts found</td></tr>'}
    </table>'''
    return page(body, title='Posts', active='posts')

@app.route('/posts/<int:pid>/delete', methods=['POST'])
@login_required
def delete_post(pid):
    exe('UPDATE posts SET deleted=1 WHERE id=?', pid)
    flash(f'Post {pid} deleted')
    return redirect(request.referrer or '/posts')

# ── Messages ───────────────────────────────────────────────────────────────────
@app.route('/messages')
@login_required
def messages_page():
    q = request.args.get('q', '').strip()
    user_filter = request.args.get('user', '').strip()

    where_parts = ['m.deleted=0']
    args = []
    if q:
        where_parts.append('m.text LIKE ?')
        args.append(f'%{q}%')
    if user_filter:
        where_parts.append('(u1.username LIKE ? OR u2.username LIKE ?)')
        args += [f'%{user_filter}%', f'%{user_filter}%']

    where = 'WHERE ' + ' AND '.join(where_parts)

    msgs = qa(f'''
        SELECT m.id, m.text, m.created_at, m.deleted,
               u1.username AS from_name, u1.id AS from_id,
               u2.username AS to_name,   u2.id AS to_id
        FROM messages m
        JOIN users u1 ON u1.id=m.from_user_id
        JOIN users u2 ON u2.id=m.to_user_id
        {where}
        ORDER BY m.created_at DESC LIMIT 300
    ''', *args)

    now = int(time.time())
    total = q1('SELECT COUNT(*) FROM messages WHERE deleted=0')[0]
    today = q1('SELECT COUNT(*) FROM messages WHERE deleted=0 AND created_at > ?', now - 86400)[0]
    week  = q1('SELECT COUNT(*) FROM messages WHERE deleted=0 AND created_at > ?', now - 604800)[0]

    rows = ''.join(f'''<tr>
      <td class="mono" style="font-size:11px;color:#555;white-space:nowrap">{fmt_ts(m["created_at"])}</td>
      <td><a href="/users/{m["from_id"]}">{h(m["from_name"])}</a></td>
      <td style="color:#555">→</td>
      <td><a href="/users/{m["to_id"]}">{h(m["to_name"])}</a></td>
      <td class="truncate" style="max-width:360px">{h(m["text"][:200])}</td>
      <td>
        <form method="post" action="/messages/{m["id"]}/delete">
          <button class="btn btn-red" type="submit">Delete</button>
        </form>
      </td>
    </tr>''' for m in msgs)

    body = f'''
    <h1>Messages</h1>
    <div class="stat-grid">
      <div class="stat-card"><div class="val">{total:,}</div><div class="lbl">Total</div></div>
      <div class="stat-card"><div class="val">{today:,}</div><div class="lbl">Today</div></div>
      <div class="stat-card"><div class="val">{week:,}</div><div class="lbl">This Week</div></div>
    </div>
    <div style="display:flex;gap:10px;margin-bottom:14px">
      <form method="get" style="display:flex;gap:8px;align-items:center">
        <input type="text" name="q" value="{h(q)}" placeholder="Search message text…" style="width:220px">
        <input type="text" name="user" value="{h(user_filter)}" placeholder="Filter by username…" style="width:180px">
        <input type="submit" value="Search">
      </form>
      <span style="font-size:12px;color:#555;align-self:center">Showing last 300</span>
    </div>
    <table>
      <tr><th>Time</th><th>From</th><th></th><th>To</th><th>Message</th><th></th></tr>
      {rows or '<tr><td colspan="6" style="color:#555;text-align:center;padding:20px">No messages found</td></tr>'}
    </table>'''
    return page(body, title='Messages', active='msgs')

@app.route('/messages/<int:mid>/delete', methods=['POST'])
@login_required
def delete_message(mid):
    exe('UPDATE messages SET deleted=1 WHERE id=?', mid)
    flash(f'Message deleted')
    return redirect(request.referrer or '/messages')

# ── Content Filter ─────────────────────────────────────────────────────────────
@app.route('/filter', methods=['GET', 'POST'])
@login_required
def filter_page():
    words = []
    if os.path.exists(FILTER_WORDS_FILE):
        try:
            words = json.load(open(FILTER_WORDS_FILE))
        except Exception:
            pass

    if request.method == 'POST':
        raw = request.form.get('words', '')
        words = [w.strip().lower() for w in raw.replace('\n', ',').split(',') if w.strip()]
        try:
            with open(FILTER_WORDS_FILE, 'w') as f:
                json.dump(words, f)
            flash(f'Filter saved — {len(words)} word(s). Restart the main service to apply.')
        except Exception as e:
            flash(f'!Error saving filter: {e}')

    current = ', '.join(words)
    count = len(words)
    body = f'''
    <h1>Content Filter</h1>
    <div class="section" style="max-width:600px">
      <p style="color:#666;font-size:13px;margin-bottom:16px;line-height:1.6">
        Comma-separated words or phrases. Messages containing these are auto-censored.<br>
        Changes take effect immediately for new messages (the main app reloads this list on next startup or after a service restart).
        Currently <strong style="color:#ddd">{count}</strong> filtered word(s).
      </p>
      <form method="post">
        <textarea name="words" rows="8" style="width:100%;margin-bottom:12px;font-family:'SF Mono',monospace;font-size:13px;resize:vertical">{h(current)}</textarea>
        <input type="submit" value="Save Filter Words">
      </form>
    </div>'''
    return page(body, title='Filter', active='filter')

# ── Settings ───────────────────────────────────────────────────────────────────
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    msg = ''
    if request.method == 'POST':
        new_pw = request.form.get('new_password', '').strip()
        if len(new_pw) < 8:
            msg = '<div class="flash err">Password must be at least 8 characters.</div>'
        else:
            global ADMIN_PASS
            ADMIN_PASS = new_pw
            with open(PASS_FILE, 'w') as f:
                f.write(new_pw)
            msg = '<div class="flash ok">Password changed.</div>'

    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    body = f'''
    <h1>Settings</h1>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;max-width:800px">
      <div class="section">
        <h2 style="margin-top:0">Change Admin Password</h2>
        {msg}
        <form method="post">
          <label style="font-size:12px;color:#666;display:block;margin-bottom:5px">New Password (min 8 chars)</label>
          <input type="password" name="new_password" style="width:100%;margin-bottom:12px">
          <input type="submit" value="Change Password">
        </form>
      </div>
      <div class="section">
        <h2 style="margin-top:0">System Info</h2>
        <table style="background:none;border:none">
          <tr><td style="color:#555;padding:4px 0;border:none">DB Path</td><td class="mono" style="color:#999;border:none;font-size:11px">{h(DB_PATH)}</td></tr>
          <tr><td style="color:#555;padding:4px 0;border:none">DB Size</td><td style="color:#ddd;border:none">{fmt_bytes(db_size)}</td></tr>
          <tr><td style="color:#555;padding:4px 0;border:none">Panel URL</td><td class="mono" style="color:#4a9eff;border:none">http://100.95.1.7:8001</td></tr>
          <tr><td style="color:#555;padding:4px 0;border:none">Pass File</td><td class="mono" style="color:#999;border:none;font-size:11px">{h(PASS_FILE)}</td></tr>
          <tr><td style="color:#555;padding:4px 0;border:none">Filter File</td><td class="mono" style="color:#999;border:none;font-size:11px">{h(FILTER_WORDS_FILE)}</td></tr>
        </table>
      </div>
    </div>'''
    return page(body, title='Settings', active='settings')

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f'[admin] Starting Chronograph Admin Panel')
    print(f'[admin] URL:      http://{BIND_HOST}:{BIND_PORT}')
    print(f'[admin] Password: {ADMIN_PASS}')
    print(f'[admin] Pass file: {PASS_FILE}')
    app.run(host=BIND_HOST, port=BIND_PORT, debug=False)

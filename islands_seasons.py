"""Daily Yonko ranking and durable, atomic rank mail delivery (Vietnam time)."""
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager


@contextmanager
def connection(path):
    conn=sqlite3.connect(path,timeout=10)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def day_at(now=None):
    return (int(time.time() if now is None else now) + 25200) // 86400


def rank_rewards(rank):
    return [('gold', 1000000 // rank), ('material', 1, 60000, 300 // rank)] if 1 <= rank <= 5 else []


_INIT_PATHS = set()


def initialize(g, conn):
    path = str(g.STORE.db_path)
    if path in _INIT_PATHS:
        return
    conn.execute('CREATE TABLE IF NOT EXISTS island_yonko_scores(day INTEGER NOT NULL, uid INTEGER NOT NULL, damage INTEGER NOT NULL, PRIMARY KEY(day,uid))')
    conn.execute('CREATE INDEX IF NOT EXISTS island_yonko_ranking ON island_yonko_scores(day,damage DESC,uid)')
    conn.execute('CREATE TABLE IF NOT EXISTS island_yonko_awards(day INTEGER NOT NULL, uid INTEGER NOT NULL, rank INTEGER NOT NULL, mail_id INTEGER NOT NULL, PRIMARY KEY(day,uid))')
    conn.execute('CREATE TABLE IF NOT EXISTS island_yonko_seasons(day INTEGER PRIMARY KEY, settled_at INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS island_migrations(name TEXT PRIMARY KEY)')
    if not conn.execute("SELECT 1 FROM island_migrations WHERE name='yonko-scores-v1'").fetchone():
        # Recover actual saved results, including players who are offline at rollout.
        for uid, raw in conn.execute('SELECT uid,state_json FROM players'):
            state = json.loads(raw)
            record = state.get('island_yonko', {})
            day, damage = record.get('day'), int(record.get('damage', 0))
            if isinstance(day, int) and 0 <= day <= day_at() and damage > 0:
                conn.execute('INSERT OR IGNORE INTO island_yonko_scores VALUES(?,?,?)', (day, uid, damage))
        conn.execute("INSERT INTO island_migrations VALUES('yonko-scores-v1')")
    _INIT_PATHS.add(path)


def save_attack(g, uid, state):
    with connection(g.STORE.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        initialize(g, conn)
        record = state['island_yonko']
        if conn.execute('SELECT 1 FROM island_yonko_seasons WHERE day=?', (record['day'],)).fetchone():
            raise ValueError('Đợt Tân Tứ Hoàng đã chốt. Hãy mở lại đảo.')
        conn.execute('INSERT INTO island_yonko_scores VALUES(?,?,?) ON CONFLICT(day,uid) DO UPDATE SET damage=excluded.damage',
                     (record['day'], uid, record['damage']))
        g.arena_write_states(conn, [(uid, state)])


def ranking(g, day, uid):
    from state_decode_cache import _split_heroes_json
    with connection(g.STORE.db_path) as conn:
        initialize(g, conn)
        rows = conn.execute('SELECT s.uid,p.state_json,s.damage FROM island_yonko_scores s JOIN players p ON p.uid=s.uid WHERE s.day=? AND s.damage>0 ORDER BY s.damage DESC,s.uid LIMIT 50', (day,)).fetchall()
        own = conn.execute('SELECT damage FROM island_yonko_scores WHERE day=? AND uid=?', (day, uid)).fetchone()
        rank = 0
        if own and own[0] > 0:
            rank = 1 + conn.execute('SELECT COUNT(*) FROM island_yonko_scores WHERE day=? AND (damage>? OR (damage=? AND uid<?))', (day, own[0], own[0], uid)).fetchone()[0]
        out = []
        for r_uid, raw, damage in rows:
            shell_raw, _ = _split_heroes_json(raw)
            st = json.loads(shell_raw) if shell_raw is not None else json.loads(raw)
            st['arena_info'] = [damage]
            out.append(st)
        return out, rank


_SETTLED_DAYS = {}


def settle(g, now=None):
    """Commit each season's mail, player revision and delivery ledger together.

    The same player lock used by authenticated requests prevents an in-flight
    request from overwriting a mail appended by the maintenance worker.
    """
    now = int(time.time() if now is None else now)
    cur_day = day_at(now)
    path = str(g.STORE.db_path)
    if _SETTLED_DAYS.get(path) == cur_day:
        return 0
    count = 0
    with g.PLAYER_TRANSACTION_LOCK, connection(g.STORE.db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        initialize(g, conn)
        days = conn.execute('SELECT DISTINCT day FROM island_yonko_scores WHERE day<? AND day NOT IN (SELECT day FROM island_yonko_seasons) ORDER BY day', (cur_day,)).fetchall()
        for (day,) in days:
            ranked = conn.execute('SELECT s.uid,p.state_json FROM island_yonko_scores s JOIN players p ON p.uid=s.uid WHERE s.day=? AND s.damage>0 ORDER BY s.damage DESC,s.uid LIMIT 5', (day,)).fetchall()
            for rank, (uid, raw) in enumerate(ranked, 1):
                if conn.execute('SELECT 1 FROM island_yonko_awards WHERE day=? AND uid=?', (day, uid)).fetchone():
                    continue
                state = json.loads(raw)
                mails = state.setdefault('mails', [])
                mail_id = max((int(m['id']) for m in mails), default=0) + 1
                date = time.strftime('%d/%m/%Y', time.gmtime(day * 86400))
                mails.append(dict(id=mail_id, title=f'Tân Tứ Hoàng · Hạng {rank}',
                                  message=f'Thưởng xếp hạng sát thương ngày {date}. Đợt mới bắt đầu lúc 00:00 giờ Việt Nam.',
                                  bonus=g.make_bonus_preview(rank_rewards(rank)), received=False,
                                  time=now * 1000, senderId=0, island_season=day))
                g.arena_write_states(conn, [(uid, state)])
                conn.execute('INSERT INTO island_yonko_awards VALUES(?,?,?,?)', (day, uid, rank, mail_id))
                count += 1
            conn.execute('INSERT INTO island_yonko_seasons VALUES(?,?)', (day, now))
        _SETTLED_DAYS[path] = cur_day
    return count


_workers = set()


def start_worker(g):
    path = str(g.STORE.db_path)
    if path in _workers:
        return
    # Bootstrap before opening the HTTP listeners, so a first login cannot reset
    # yesterday's legacy state before its actual damage has been recovered.
    settle(g)
    _workers.add(path)

    def work():
        while True:
            time.sleep(60)
            try:
                delivered = settle(g)
                if delivered:
                    logging.getLogger('htht.runtime').info('yonko rank mail delivered=%s', delivered)
            except Exception:
                logging.getLogger('htht.runtime').exception('yonko settlement failed; will retry')

    threading.Thread(target=work, name='yonko-settlement-60s', daemon=True).start()

"""Nickname spelling rules; visual comparisons always require the same class."""
import logging

# Confirmed by the clan administrator on 2026-10-24: second letter is Cyrillic а.
CONFIRMED_ALIASES = {'macintosh': 'Mаcintosh'}

# Cyrillic/Latin lookalikes, not phonetic transliteration or OCR guesses.
LOOKALIKES = str.maketrans(dict(zip('авекмнорстух', 'abekmhopctyx')))


def visual_key(nick):
    """Comparison only: never use this value as a player's displayed nickname."""
    return canonical(nick).casefold().translate(LOOKALIKES)


def ensure_unique_profiles(profiles):
    owners = {}
    for uid, nick, cls, *_ in profiles:
        key = (cls, visual_key(nick))
        if key in owners and owners[key] != uid:
            raise ValueError(f'У ника {nick} ({cls}) несколько Telegram-профилей с похожими '
                             'русскими и английскими буквами. Проверьте владельца.')
        owners[key] = uid


def canonical(nick):
    nick = nick.strip()
    return CONFIRMED_ALIASES.get(nick.casefold(), nick)


def apply_database_corrections(conn):
    profiles = [(table, uid, nick) for table in ('users', 'pending_users')
                for uid, nick in conn.execute(f'SELECT user_id, nick FROM {table}').fetchall()]
    owners = {}
    for _, uid, nick in profiles:
        owners.setdefault(canonical(nick).casefold(), set()).add(uid)
    changed = 0
    for table, uid, old in profiles:
        if old.strip().casefold() not in CONFIRMED_ALIASES:
            continue
        new = canonical(old)
        if new == old:
            continue
        if len(owners[new.casefold()]) != 1:
            logging.error('Подтверждённое исправление ника %s пропущено: несколько Telegram-профилей', old)
            continue
        conn.execute(f'UPDATE {table} SET nick=? WHERE user_id=?', (new, uid))
        conn.execute('INSERT OR IGNORE INTO nick_aliases(user_id,old_nick) VALUES (?,?)', (uid, old))
        changed += 1
    return changed

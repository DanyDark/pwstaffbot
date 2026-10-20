"""Layout adapter for the dated PW attendance workbook."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import calendar
import re
from threading import RLock

MONTH_LOCK = RLock()

EVENTS = ('РБ', 'Комендант', 'Баньши', 'ГВГ')
CLASS_ALIASES = {
    'ВАР': 'ВАР', 'ВАРЫ': 'ВАР', 'ВОИНЫ': 'ВАР',
    'МАГ': 'МАГ', 'МАГИ': 'МАГ', 'ДРУ': 'ДРУ', 'ДРУЛИ': 'ДРУ', 'ДРУИДЫ': 'ДРУ',
    'ТАНК': 'ТАНК', 'ТАНКИ': 'ТАНК', 'ОБОРОТНИ': 'ТАНК',
    'ЛУК': 'ЛУК', 'ЛУКИ': 'ЛУК', 'ЛУЧНИКИ': 'ЛУК',
    'ПРИСТ': 'ПРИСТ', 'ПРИСТЫ': 'ПРИСТ', 'ЖРЕЦЫ': 'ПРИСТ',
}


def cell(row, column):
    return row[column - 1] if len(row) >= column else ''


def label(value):
    return str(value).strip().upper()


def a1(column, row):
    letters = ''
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return f'{letters}{row}'


def class_of_row(row):
    for column in (2, 3):
        value = label(cell(row, column))
        if value in CLASS_ALIASES:
            return CLASS_ALIASES[value]
    return None


def layout(rows):
    for index, row in enumerate(rows[:6]):
        headers = {label(v): i for i, v in enumerate(row, 1) if str(v).strip()}
        if 'НИК' in headers and all(label(e) in headers for e in EVENTS):
            break
    else:
        raise ValueError('Лист не соответствует «Новому шаблону»: нужны НИК, РБ, Комендант, Баньши и ГВГ.')
    event_columns = {}
    stops = sorted(set(headers.values()))
    for event in EVENTS:
        start = headers[label(event)]
        end = next((c for c in stops if c > start), len(row) + 1)
        event_columns[event] = list(range(start, end))
    first_player = next((i for i in range(index + 1, len(rows))
                         if str(cell(rows[i], headers['НИК'])).strip() and not class_of_row(rows[i])), len(rows))
    return {'header': index + 1, 'nick': headers['НИК'], 'headers': headers,
            'events': event_columns, 'first_player': first_player + 1}


def players(rows, spec=None):
    spec = spec or layout(rows)
    for i, row in enumerate(rows, 1):
        if i <= spec['header'] or class_of_row(row):
            continue
        nick = str(cell(row, spec['nick'])).strip()
        if nick and label(nick) not in ('НИК', 'ИТОГО', 'ИТОГ', 'ВСЕГО'):
            yield i, nick


def day_number(value):
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text.replace(',', '.'))
        if number > 30000:
            return (datetime(1899, 12, 30) + timedelta(days=number)).day
        if number.is_integer() and 1 <= number <= 31:
            return int(number)
    except ValueError:
        pass
    match = re.fullmatch(r'(\d{1,2})[./](\d{1,2})(?:[./]\d{2,4})?', text)
    return int(match[1]) if match and 1 <= int(match[1]) <= 31 else None


def event_dates(rows, event, year, month):
    spec = layout(rows)
    if event not in spec['events']:
        raise ValueError('Неизвестная активность')
    result = {}
    for col in spec['events'][event]:
        # Dates can be above or on a class header row, but never in player rows.
        for row_index in range(spec['header'], spec['first_player'] - 1):
            day = day_number(cell(rows[row_index], col))
            if day and day <= calendar.monthrange(year, month)[1]:
                date = f'{day:02d}.{month:02d}.{year}'
                if date in result:
                    raise ValueError(f'В таблице две колонки {event} на {date}. Исправьте даты.')
                result[date] = col
                break
    return result


def mark_attendance(ws, column, nick_rows, leader):
    leaders = {leader} if isinstance(leader, str) else set(leader or [])
    rows = ws.get_all_values()
    formula_rows = ws.get_all_values(value_render_option='FORMULA')
    available = {}
    requested = {nick.casefold() for nick in nick_rows}
    for row, nick in players(rows):
        key = nick.casefold()
        if key in available and key in requested:
            raise ValueError(f'Ник {nick} встречается в таблице дважды. Устраните дубликат.')
        available[key] = row
    if column not in {c for cols in layout(rows)['events'].values() for c in cols}:
        raise ValueError('Выбрана колонка вне активности')
    updates, missing = [], []
    for nick in dict.fromkeys(nick_rows):
        row = available.get(nick.casefold())
        if row is None:
            missing.append(nick)
            continue
        current = str(cell(rows[row - 1], column)).strip()
        if str(cell(formula_rows[row - 1], column)).startswith('='):
            raise ValueError(f'В {a1(column, row)} находится формула; запись отменена.')
        desired = 2 if nick in leaders else 1
        if current not in ('', '0', '1', '2'):
            raise ValueError(f'В {a1(column, row)} уже есть нестандартная отметка; запись отменена.')
        # Repeated screenshots cannot reduce a PL mark or create another attendance.
        value = max(int(current or 0), desired)
        if str(value) != current:
            updates.append({'range': a1(column, row), 'values': [[value]]})
    if updates:
        ws.batch_update(updates, value_input_option='RAW')
    return len(updates), missing


def ensure_month(spreadsheet, year, month):
    with MONTH_LOCK:
        return _ensure_month(spreadsheet, year, month)


def _ensure_month(spreadsheet, year, month):
    import gspread
    base = f'Активность {month:02d}.{year}'
    for name in (base, base + ' (136)'):
        try:
            ws = spreadsheet.worksheet(name)
        except gspread.WorksheetNotFound:
            continue
        try:
            layout(ws.get_all_values())
            return ws
        except ValueError:
            continue
    template = spreadsheet.worksheet('Новый шаблон')
    source = template.get_all_values()
    spec = layout(source)
    names = {ws.title for ws in spreadsheet.worksheets()}
    name = base if base not in names else base + ' (136)'
    if name in names:
        raise ValueError(f'Лист {name} существует, но имеет неподходящий формат.')
    formulas = template.get_all_values(value_render_option='FORMULA')
    ws = template.duplicate(insert_sheet_index=0, new_sheet_name=name)
    ranges = [a1(c, r) for r, _ in players(source, spec) for cols in spec['events'].values() for c in cols
              if not str(cell(formulas[r - 1], c)).startswith('=')]
    captain = spec['headers'].get('КАПИТАНСКИЕ')
    if captain:
        ranges += [a1(captain, r) for r, _ in players(source, spec)
                   if not str(cell(formulas[r - 1], captain)).startswith('=')]
    date_updates = []
    for cols in spec['events'].values():
        for col in cols:
            for index in range(spec['header'], spec['first_player'] - 1):
                value = cell(source[index], col)
                day = day_number(value)
                if day:
                    if day > calendar.monthrange(year, month)[1]:
                        ranges.append(a1(col, index + 1))
                    elif not str(value).strip().isdigit() or float(str(value).strip()) > 31:
                        date_updates.append({'range': a1(col, index + 1),
                                             'values': [[f'{day:02d}.{month:02d}.{year}']]})
                    break
    try:
        if ranges:
            ws.batch_clear(ranges)
        if date_updates:
            ws.batch_update(date_updates, value_input_option='USER_ENTERED')
    except Exception:
        # Only the newly created, incompletely initialized sheet is removed.
        spreadsheet.del_worksheet(ws)
        raise
    return ws


def player_input_columns(spec):
    columns = {c for cols in spec['events'].values() for c in cols}
    return columns | {spec['headers'][name] for name in ('ПРИМЕЧАНИЕ', 'КАПИТАНСКИЕ')
                      if name in spec['headers']}


def free_player_row(rows, spec, start, stop):
    inputs = player_input_columns(spec)
    note = spec['headers'].get('ПРИМЕЧАНИЕ')
    if note:
        inputs.discard(note)
    return next((r for r in range(start + 1, stop)
                 if not str(cell(rows[r - 1], spec['nick'])).strip()
                 and label(cell(rows[r - 1], 2)) not in ('ИТОГО', 'ИТОГ', 'ВСЕГО')
                 and (not note or label(cell(rows[r - 1], note)) in ('', 'ОСНОВА'))
                 and all(not str(cell(rows[r - 1], c)).strip() for c in inputs)
                 and any(str(v).startswith('=') for v in rows[r - 1])), None)


def sync_players(ws, users):
    rows = ws.get_all_values(value_render_option='FORMULA')
    spec = layout(rows)
    existing = {nick.casefold() for _, nick in players(rows, spec)}
    sections = [(i, class_of_row(row)) for i, row in enumerate(rows, 1) if i > spec['header'] and class_of_row(row)]
    updates = []
    for _, nick, cls, *_ in users:
        if nick.casefold() in existing:
            continue
        start = next((r for r, c in sections if c == cls), None)
        if start is None:
            raise ValueError(f'В листе нет раздела класса {cls}.')
        stop = next((r for r, _ in sections if r > start), len(rows) + 1)
        free = free_player_row(rows, spec, start, stop)
        if free is None:
            raise ValueError(f'Нет свободной строки в разделе {cls}; добавьте строку с формулами.')
        updates.append({'range': a1(spec['nick'], free), 'values': [[nick]]})
        while len(rows[free - 1]) < spec['nick']:
            rows[free - 1].append('')
        rows[free - 1][spec['nick'] - 1] = nick
        existing.add(nick.casefold())
    if updates:
        ws.batch_update(updates, value_input_option='RAW')
    return len(updates)


def moscow_month():
    now = datetime.now(ZoneInfo('Europe/Moscow'))
    return now.year, now.month


def player_stats(ws, nick):
    rows = ws.get_all_values(value_render_option='UNFORMATTED_VALUE')
    spec = layout(rows)
    matches = [r for r, name in players(rows, spec) if name.casefold() == nick.casefold()]
    if not matches:
        raise ValueError('Ваш ник не найден в месячном листе. Попросите администратора выполнить /sync_pa.')
    if len(matches) != 1:
        raise ValueError(f'Ник {nick} повторяется в строках {", ".join(map(str,matches))}. '
                         'Попросите администратора убрать дубликат.')
    row = rows[matches[0] - 1]
    events = {}
    for event, columns in spec['events'].items():
        marks = [str(cell(row, col)).strip() for col in columns]
        events[event] = {'visits':sum(v in ('1','2','1.0','2.0') for v in marks),
                         'leader':sum(v in ('2','2.0') for v in marks),
                         'points':sum(int(float(v)) for v in marks if v in ('1','2','1.0','2.0'))}
    values = {name:cell(row,col) for name,col in spec['headers'].items()}
    return {'sheet':ws.title,'nick':nick,'events':events,'values':values}


def edit_player(ws, old_nick, new_nick, character_class):
    """Move only input cells; keep each row's existing formula cells intact."""
    rows = ws.get_all_values(value_render_option='FORMULA')
    spec = layout(rows)
    matches = [r for r,n in players(rows,spec) if n.casefold() == old_nick.casefold()]
    if len(matches) > 1:
        raise ValueError('Старый ник встречается в таблице несколько раз.')
    if not matches:
        return False
    source = matches[0]
    if any(n.casefold() == new_nick.casefold() and r != source for r,n in players(rows,spec)):
        raise ValueError('Новый ник уже есть в таблице.')
    sections = [(r,class_of_row(row)) for r,row in enumerate(rows,1) if r > spec['header'] and class_of_row(row)]
    current_class = next((c for r,c in reversed(sections) if r < source),None)
    target = source
    if current_class != character_class:
        start = next((r for r,c in sections if c == character_class),None)
        if start is None:
            raise ValueError(f'Нет раздела класса {character_class}.')
        stop = next((r for r,_ in sections if r > start),len(rows)+1)
        target = free_player_row(rows, spec, start, stop)
        if target is None:
            raise ValueError(f'Нет свободной строки с формулами для класса {character_class}.')
    updates = [{'range':a1(spec['nick'],target),'values':[[new_nick]]}]
    if source != target:
        columns = player_input_columns(spec)
        for col in columns:
            original = cell(rows[source-1],col)
            destination = cell(rows[target-1],col)
            if str(original).startswith('=') or str(destination).startswith('='):
                raise ValueError('В переносимых ячейках есть формула. Перенесите строку вручную.')
            default_note = col == spec['headers'].get('ПРИМЕЧАНИЕ') and label(destination) == 'ОСНОВА'
            if str(destination).strip() and not default_note:
                raise ValueError('Свободная строка содержит отметки или примечания. Очистите её вручную.')
            if str(original).strip() or str(destination).strip():
                updates.extend([{'range':a1(col,target),'values':[[original]]},
                                {'range':a1(col,source),'values':[['']]}])
        updates.append({'range':a1(spec['nick'],source),'values':[['']]})
    ws.batch_update(updates,value_input_option='RAW')
    return True

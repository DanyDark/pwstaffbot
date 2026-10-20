import copy
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sheet_layout as a


def fixture():
    rows = [[''] * 38 for _ in range(12)]
    for col, name in {1:'Общая ЗП',2:'Профа',3:'НИК',4:'ПРИМЕЧАНИЕ',5:'АКТИВКА 1',
                      6:'АКТИВКА 2',7:'РБ',21:'Комендант',26:'Баньши',30:'ГВГ',
                      34:'ЯВКА',36:'зп 1/2',37:'зп 2/2',38:'Капитанские'}.items():
        rows[1][col - 1] = name
    rows[2][29:33] = ['6','13','20','27']
    rows[3][1] = 'МАГИ'
    rows[3][6:9] = ['11.10.2026','13.10.2026','15.10.2026']
    rows[3][20:24] = ['3','10','17','24']
    rows[3][25:28] = ['14','21','28']
    rows[4][2], rows[5][2] = 'Leader', 'Player'
    rows[4][35:37] = ['100000','200000']
    rows[6][4] = '=AH7/$AH$1'
    rows[7][2] = 'ВАРЫ'
    rows[8][2] = 'Warrior'
    rows[9][4] = '=AH10/$AH$1'
    rows[11][2] = 'Итого'
    return rows


class Worksheet:
    def __init__(self, title='Новый шаблон', rows=None):
        self.title = title
        self.rows = copy.deepcopy(rows if rows is not None else fixture())
        self.updates, self.clears = [], []

    def get_all_values(self, **kwargs):
        return copy.deepcopy(self.rows)

    def batch_update(self, updates, **kwargs):
        self.updates.extend(updates)
        for update in updates:
            address = update['range']
            letters = ''.join(c for c in address if c.isalpha())
            row = int(''.join(c for c in address if c.isdigit()))
            col = 0
            for c in letters:
                col = col * 26 + ord(c) - 64
            self.rows[row-1][col-1] = str(update['values'][0][0])

    def batch_clear(self, ranges):
        self.clears.extend(ranges)


class NotFound(Exception):
    pass


class Spreadsheet:
    def __init__(self):
        self.sheets = {'Новый шаблон': Worksheet()}
        self.sheets['Новый шаблон'].duplicate = self.duplicate

    def worksheet(self, title):
        if title not in self.sheets:
            raise NotFound()
        return self.sheets[title]

    def worksheets(self):
        return list(self.sheets.values())

    def duplicate(self, insert_sheet_index, new_sheet_name):
        ws = Worksheet(new_sheet_name)
        self.sheets[new_sheet_name] = ws
        return ws

    def del_worksheet(self, ws):
        del self.sheets[ws.title]


class SheetLayoutTests(unittest.TestCase):
    def test_realistic_layout(self):
        spec = a.layout(fixture())
        self.assertEqual(spec['nick'], 3)
        self.assertEqual(spec['events']['ГВГ'], [30,31,32,33])
        self.assertEqual(spec['headers']['ЗП 2/2'], 37)
        self.assertEqual(list(a.players(fixture())), [(5,'Leader'),(6,'Player'),(9,'Warrior')])

    def test_dates_on_two_header_rows(self):
        rows = fixture()
        self.assertEqual(a.event_dates(rows,'РБ',2026,10)['11.10.2026'], 7)
        self.assertEqual(a.event_dates(rows,'ГВГ',2026,10)['20.10.2026'], 32)

    def test_duplicate_date_is_rejected(self):
        rows = fixture()
        rows[2][29] = '13'
        with self.assertRaises(ValueError):
            a.event_dates(rows,'ГВГ',2026,10)

    def test_month_length_and_serial_dates(self):
        rows = fixture()
        rows[3][6:9] = ['31', '29', '28']
        self.assertEqual(a.event_dates(rows,'РБ',2027,2), {'28.02.2027':9})
        self.assertEqual(a.day_number('46306'), 11)

    def test_marks_are_numeric_and_idempotent(self):
        ws = Worksheet()
        count, missing = a.mark_attendance(ws, 32, ['Leader','Player','Unknown'], 'Leader')
        self.assertEqual((count,missing),(2,['Unknown']))
        self.assertEqual([u['values'][0][0] for u in ws.updates], [2,1])
        self.assertEqual(a.mark_attendance(ws,32,['Leader','Player'],None)[0],0)
        self.assertEqual(ws.rows[4][31], '2')

    def test_nonstandard_existing_mark_aborts_whole_batch(self):
        ws = Worksheet()
        ws.rows[5][31] = '=SUM(A1:A2)'
        with self.assertRaises(ValueError):
            a.mark_attendance(ws,32,['Leader','Player'],'Leader')
        self.assertEqual(ws.updates, [])

    def test_salary_column_cannot_be_written_as_attendance(self):
        with self.assertRaises(ValueError):
            a.mark_attendance(Worksheet(),36,['Leader'],'Leader')

    def test_duplicate_player_prevents_ambiguous_write(self):
        ws = Worksheet()
        ws.rows[5][2] = 'leader'
        with self.assertRaisesRegex(ValueError, 'дважды'):
            a.mark_attendance(ws,32,['Leader'],'Leader')
        self.assertEqual(ws.updates, [])

    def test_multiple_leaders_from_separate_screenshots(self):
        ws = Worksheet()
        a.mark_attendance(ws,32,['Leader','Player','Warrior'],['Leader','Warrior'])
        self.assertEqual([u['values'][0][0] for u in ws.updates],[2,1,2])

    def test_sync_keeps_existing_players_and_formulas(self):
        ws = Worksheet()
        count = a.sync_players(ws, [(1,'NewMage','МАГ'),(2,'Leader','МАГ'),(3,'NewWar','ВАР')])
        self.assertEqual(count,2)
        self.assertEqual([u['range'] for u in ws.updates],['C7','C10'])
        self.assertEqual(ws.rows[6][4], '=AH7/$AH$1')
        self.assertEqual(ws.rows[4][2], 'Leader')

    def test_sync_full_section_does_not_overwrite_players(self):
        ws = Worksheet()
        ws.rows[6][2] = 'Full'
        with self.assertRaises(ValueError):
            a.sync_players(ws, [(1,'NewMage','МАГ')])
        self.assertEqual(ws.updates, [])

    def test_new_registration_cannot_inherit_marks_from_blank_nickname_row(self):
        ws = Worksheet()
        ws.rows[6][6] = '2'
        with self.assertRaises(ValueError):
            a.sync_players(ws, [(1,'NewMage','МАГ')])
        self.assertEqual(ws.updates, [])

    def test_default_osnova_note_does_not_block_registration(self):
        ws = Worksheet()
        ws.rows[6][3] = 'ОСНОВА'
        self.assertEqual(a.sync_players(ws, [(1,'NewMage','МАГ')]), 1)
        self.assertEqual(ws.rows[6][3], 'ОСНОВА')

    def test_class_change_replaces_default_note_with_players_note(self):
        ws = Worksheet()
        ws.rows[4][3] = 'ТВИН'
        ws.rows[9][3] = 'ОСНОВА'
        self.assertTrue(a.edit_player(ws,'Leader','Leader','ВАР'))
        self.assertEqual(ws.rows[9][3], 'ТВИН')
        self.assertEqual(ws.rows[4][3], '')

    def test_rename_keeps_attendance_and_formulas_in_same_row(self):
        ws=Worksheet()
        ws.rows[4][6]='2'
        before=ws.rows[4][35:37]
        self.assertTrue(a.edit_player(ws,'Leader','NewName','МАГ'))
        self.assertEqual(ws.rows[4][2],'NewName')
        self.assertEqual(ws.rows[4][6],'2')
        self.assertEqual(ws.rows[4][35:37],before)

    def test_class_change_moves_inputs_without_overwriting_salary_formulas(self):
        ws=Worksheet()
        ws.rows[4][6]='2'
        ws.rows[4][37]='50000'
        self.assertTrue(a.edit_player(ws,'Leader','Leader','ВАР'))
        self.assertEqual(ws.rows[9][2],'Leader')
        self.assertEqual(ws.rows[9][6],'2')
        self.assertEqual(ws.rows[9][37],'50000')
        self.assertEqual(ws.rows[4][2],'')
        self.assertEqual(ws.rows[4][6],'')
        self.assertEqual(ws.rows[9][4],'=AH10/$AH$1')

    def test_class_change_refuses_destination_with_existing_attendance(self):
        ws=Worksheet()
        ws.rows[9][6]='1'
        with self.assertRaises(ValueError):
            a.edit_player(ws,'Leader','Leader','ВАР')
        self.assertEqual(ws.updates,[])

    def test_month_creation_preserves_template_and_is_idempotent(self):
        book = Spreadsheet()
        original = copy.deepcopy(book.worksheet('Новый шаблон').rows)
        with patch.dict(sys.modules, {'gspread': SimpleNamespace(WorksheetNotFound=NotFound)}):
            ws = a.ensure_month(book,2026,11)
            self.assertEqual(ws.title,'Активность 11.2026')
            self.assertIn('G5',ws.clears)
            self.assertIn('AL5',ws.clears)
            self.assertNotIn('AJ5',ws.clears)
            self.assertNotIn('G4',ws.clears)
            self.assertEqual(ws.rows[3][6],'11.11.2026')
            self.assertIs(a.ensure_month(book,2026,11),ws)
        self.assertEqual(book.worksheet('Новый шаблон').rows,original)

    def test_old_month_layout_is_preserved(self):
        book = Spreadsheet()
        book.sheets['Активность 10.2026'] = Worksheet('Активность 10.2026', [['НИК']])
        with patch.dict(sys.modules, {'gspread':SimpleNamespace(WorksheetNotFound=NotFound)}):
            ws = a.ensure_month(book,2026,10)
        self.assertEqual(ws.title,'Активность 10.2026 (136)')
        self.assertEqual(book.sheets['Активность 10.2026'].rows,[['НИК']])


if __name__ == '__main__':
    unittest.main()

"""Focused tests for half-month sheets and monthly salary reports."""
import copy
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import sheet_layout as attendance
import test_flows as flows


def template_rows():
    rows = [[''] * 44 for _ in range(8)]
    for col in range(5, 43):
        rows[0][col - 1] = 1
    headers = {1: 'Общая ЗП', 2: 'зп', 3: 'НИК', 4: 'ПРИМЕЧАНИЕ',
               5: 'РБ', 13: 'ГТЗ', 18: 'Рема', 23: 'ГВГ',
               28: 'Вендиго', 33: 'Комендант', 38: 'Банши',
               43: 'ПЛ', 44: 'Капитанские'}
    for col, value in headers.items():
        rows[1][col - 1] = value
    rows[3][2] = 'МАГ'
    rows[4][0], rows[4][1], rows[4][2], rows[4][3] = '=B5/10000000', '=SUM(E5:AR5)', 'Player', 'ОСНОВА'
    rows[5][0], rows[5][1], rows[5][2], rows[5][3] = '=B6/10000000', '=SUM(E6:AR6)', 'Other', 'ОСНОВА'
    rows[6][0], rows[6][1] = '=SUM(A5:A6)', '=SUM(B5:B6)'
    rows[4][4], rows[5][4] = '1', '1'
    rows[4][42], rows[4][43] = '=SUM(G5:AP5)', '=SUM(H5:AP5)'
    return rows


class Worksheet:
    def __init__(self, title, rows):
        self.title, self.rows = title, copy.deepcopy(rows)
        self.clears, self.updates = [], []
        self.book = None

    def get_all_values(self, **kwargs):
        return copy.deepcopy(self.rows)

    def batch_clear(self, ranges):
        self.clears.extend(ranges)
        for address in ranges:
            letters = ''.join(c for c in address if c.isalpha())
            row = int(''.join(c for c in address if c.isdigit()))
            col = 0
            for char in letters:
                col = col * 26 + ord(char) - 64
            self.rows[row - 1][col - 1] = ''

    def duplicate(self, insert_sheet_index=0, new_sheet_name=None):
        sheet = Worksheet(new_sheet_name, self.rows)
        self.book.sheets[new_sheet_name] = sheet
        return sheet


class Book:
    def __init__(self, template):
        sheet = Worksheet('Шаблон', template)
        sheet.book = self
        self.sheets = {'Шаблон': sheet}

    def worksheet(self, title):
        if title not in self.sheets:
            raise FakeNotFound()
        return self.sheets[title]

    def worksheets(self):
        return list(self.sheets.values())

    def duplicate(self, source, index, name):
        sheet = Worksheet(name, self.sheets[source].rows)
        self.sheets[name] = sheet
        return sheet


class FakeNotFound(Exception):
    pass


class HalfActivityTests(unittest.TestCase):
    def test_new_template_layout_and_event_slots(self):
        rows = template_rows()
        spec = attendance.layout(rows)
        self.assertEqual(spec['nick'], 3)
        self.assertEqual(spec['events']['РБ'], list(range(5, 13)))
        self.assertEqual(spec['events']['БАНШИ'], list(range(38, 43)))
        self.assertEqual(attendance.next_event_column(rows, 'РБ'), 6)
        rows[4][5] = '1'
        self.assertEqual(attendance.next_event_column(rows, 'РБ'), 7)

    def test_half_names_and_date_boundaries(self):
        self.assertEqual(attendance.half_sheet_name(2026, 11, 1), 'Активка 1-15 ноябрь (1/2)')
        self.assertEqual(attendance.half_sheet_name(2026, 11, 2), 'Активка 16-30 ноябрь (2/2)')
        self.assertEqual(attendance.half_for_date('15.11.2026'), 1)
        self.assertEqual(attendance.half_for_date('16.11.2026'), 2)

    def test_copy_keeps_template_nicks_and_formulas_but_clears_marks(self):
        book = Book(template_rows())
        fake_gspread = SimpleNamespace(WorksheetNotFound=FakeNotFound)
        with patch.dict(sys.modules, {'gspread': fake_gspread}):
            first, second = attendance.ensure_month_halves(book, 2026, 11)
        self.assertEqual(first.title, 'Активка 1-15 ноябрь (1/2)')
        self.assertEqual(second.title, 'Активка 16-30 ноябрь (2/2)')
        self.assertEqual(first.rows[4][2], 'Player')
        self.assertEqual(first.rows[4][0], '=B5/10000000')
        self.assertEqual(first.rows[4][4], '')
        self.assertEqual(first.rows[4][42], '=SUM(G5:AP5)')
        self.assertEqual(book.sheets['Шаблон'].rows[4][4], '1')

    def test_salary_report_sums_two_halves_and_uses_plain_labels(self):
        first = Worksheet('Активка 1-15 ноябрь (1/2)', template_rows())
        second = Worksheet('Активка 16-30 ноябрь (2/2)', template_rows())
        first.rows[4][1], second.rows[4][1] = 100, 200
        first.rows[4][42], second.rows[4][42] = 10, 0
        first.rows[4][43], second.rows[4][43] = 5, 7
        ns = flows.load_functions()
        ns['get_activity_sheets_for_month'] = lambda: (first, second)
        with patch.object(attendance, 'moscow_month', return_value=(2026, 11)):
            report = ns['personal_report']('Player', 'salary')
        self.assertIn('ЗП: 300', report)
        self.assertIn('ПЛские: 10', report)
        self.assertIn('Капитанские: 12', report)
        self.assertNotIn('ЗП 1/2', report)
        self.assertNotIn('ЗП 2/2', report)


if __name__ == '__main__':
    unittest.main()

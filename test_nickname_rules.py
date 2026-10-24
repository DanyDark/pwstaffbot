"""Confirmed nickname corrections preserve identity, marks and salary formulas."""
import copy
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import nickname_rules as nicknames
import sheet_layout as attendance
import test_flows as flows
import test_sheet_layout as sheets


OLD = 'Macintosh'
CORRECT = 'Mаcintosh'  # Cyrillic а, U+0430


def duplicate_sheet():
    ws = sheets.Worksheet('Активность 10.2026 (136)')
    ws.rows[4][2], ws.rows[5][2] = CORRECT, OLD
    for row in (5, 6):
        for col in (1, 5, 6, 34, 36, 37):
            ws.rows[row - 1][col - 1] = f'=SUM(G{row}:AG{row})'
    ws.rows[4][6:9] = ['1', '1', '1']
    return ws


class MergeTests(unittest.TestCase):
    def test_only_confirmed_pair_is_canonicalized(self):
        self.assertEqual(nicknames.canonical('  MACINTOSH  '), CORRECT)
        self.assertEqual(nicknames.canonical(CORRECT), CORRECT)
        for name in ('Mасintosh', 'ElMario', 'ЕlMario', 'P1ayer', 'Player'):
            self.assertEqual(nicknames.canonical(name), name)

    def test_merge_keeps_three_original_marks_and_all_formulas(self):
        ws = duplicate_sheet()
        before = copy.deepcopy(ws.rows)
        self.assertEqual(attendance.merge_confirmed_player(ws, OLD, CORRECT), 1)
        expected = copy.deepcopy(before)
        expected[5][2] = ''
        self.assertEqual(ws.rows, expected)
        self.assertEqual(len(ws.rows), len(before))
        self.assertEqual(list(attendance.players(ws.rows))[:1], [(5, CORRECT)])
        ws.updates.clear()
        self.assertEqual(attendance.merge_confirmed_player(ws, OLD, CORRECT), 0)
        self.assertEqual(ws.updates, [])

    def test_merge_uses_highest_mark_and_transfers_inputs_without_summing(self):
        ws = duplicate_sheet()
        ws.rows[5][6:10] = ['2', '1', '', '1']
        ws.rows[4][3] = 'ОСНОВА'
        ws.rows[5][3] = 'ТВИН'
        ws.rows[5][37] = '50000'
        attendance.merge_confirmed_player(ws, OLD, CORRECT)
        self.assertEqual(ws.rows[4][6:10], ['2', '1', '1', '1'])
        self.assertEqual(ws.rows[4][3], 'ТВИН')
        self.assertEqual(ws.rows[4][37], '50000')
        for col in attendance.player_input_columns(attendance.layout(ws.rows)) | {3}:
            self.assertEqual(ws.rows[5][col - 1], '')
        self.assertTrue(ws.rows[5][35].startswith('='))

    def test_single_old_spelling_is_renamed_in_place(self):
        ws = duplicate_sheet()
        ws.rows[4][2] = 'Other'
        ws.rows[5][6] = '2'
        expected = copy.deepcopy(ws.rows)
        expected[5][2] = CORRECT
        attendance.merge_confirmed_player(ws, OLD, CORRECT)
        self.assertEqual(ws.rows, expected)

    def test_conflicts_abort_without_any_cell_changes(self):
        scenarios = {
            'custom notes': [(4, 3, 'ТВИН'), (5, 3, 'РЕЗЕРВ')],
            'captain amounts': [(4, 37, '50000'), (5, 37, '100000')],
            'unexpected mark': [(5, 31, '3')],
            'formula in input': [(5, 31, '=SUM(A1:A2)')],
            'duplicate source': [(6, 2, OLD)],
            'duplicate target': [(6, 2, CORRECT)],
            'different classes': [(5, 2, 'Player'), (8, 2, OLD)],
        }
        for scenario, changes in scenarios.items():
            with self.subTest(scenario=scenario):
                ws = duplicate_sheet()
                ws.rows[5][6] = '2'  # A valid change before the later conflict.
                for row, col, value in changes:
                    ws.rows[row][col] = value
                before = copy.deepcopy(ws.rows)
                with self.assertRaises(ValueError):
                    attendance.merge_confirmed_player(ws, OLD, CORRECT)
                self.assertEqual(ws.updates, [])
                self.assertEqual(ws.rows, before)


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.BotFlows.setUp
    tearDown = flows.BotFlows.tearDown
    text = flows.BotFlows.text

    def seed_legacy(self, uid=2, table='users', nick=OLD):
        with closing(sqlite3.connect(self.ns['DB_FILE'])) as conn, conn:
            conn.execute(f'INSERT INTO {table}(user_id,nick,class) VALUES (?,?,?)',
                         (uid, nick, 'МАГ'))

    async def test_startup_migration_preserves_id_date_answers_and_cash_history(self):
        self.seed_legacy()
        with closing(sqlite3.connect(self.ns['DB_FILE'])) as conn, conn:
            conn.execute("UPDATE users SET registered_at='2026-10-01 12:00:00' WHERE user_id=2")
            conn.execute("INSERT INTO poll_responses(user_id,poll_id,meeting,answer) VALUES(2,1,'ГВГ','Да')")
            conn.execute("INSERT INTO cash_orders(user_id,nick,description) VALUES(2,?,'test')", (OLD,))
            profile = conn.execute('SELECT * FROM users').fetchall()
            answers = conn.execute('SELECT * FROM poll_responses').fetchall()
            orders = conn.execute('SELECT * FROM cash_orders').fetchall()
        self.ns['init_db']()
        self.ns['init_db']()
        with closing(sqlite3.connect(self.ns['DB_FILE'])) as conn, conn:
            expected = [(uid, CORRECT, cls, registered) for uid, _, cls, registered in profile]
            self.assertEqual(conn.execute('SELECT * FROM users').fetchall(), expected)
            self.assertEqual(conn.execute('SELECT * FROM poll_responses').fetchall(), answers)
            self.assertEqual(conn.execute('SELECT * FROM cash_orders').fetchall(), orders)
            self.assertEqual(conn.execute('SELECT * FROM nick_aliases').fetchall(), [(2, OLD)])
        self.assertEqual(self.ns['get_user_id_by_nick'](OLD), 2)
        self.assertEqual(self.ns['get_user_id_by_nick'](CORRECT), 2)

    async def test_pending_migration_keeps_request_and_does_not_approve(self):
        self.seed_legacy(table='pending_users')
        before = self.ns['get_pending_users']()[0]
        self.ns['init_db']()
        after = self.ns['get_pending_users']()[0]
        self.assertEqual(after, (before[0], CORRECT, before[2], before[3]))
        self.assertFalse(self.ns['is_registered'](2))

    async def test_multiple_telegram_owners_are_never_merged(self):
        for table in ('users', 'pending_users'):
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.ns['DB_FILE'])) as conn, conn:
                    conn.execute('DELETE FROM users')
                    conn.execute('DELETE FROM pending_users')
                self.seed_legacy()
                self.seed_legacy(uid=3, table=table, nick=CORRECT)
                with self.assertLogs(level='ERROR'):
                    self.ns['init_db']()
                self.assertEqual(self.ns['get_user_nick'](2), OLD)
                if table == 'users':
                    self.assertIsNone(self.ns['get_user_id_by_nick'](OLD))
                ws = duplicate_sheet()
                with self.assertRaisesRegex(ValueError, 'несколько Telegram'):
                    self.ns['reconcile_sheet_profiles'](ws, self.ns['get_all_users']())
                self.assertEqual(ws.updates, [])

    async def test_registration_and_edit_use_confirmed_spelling(self):
        self.ns['register_user'](2, OLD, 'МАГ')
        self.assertEqual(self.ns['get_user_nick'](2), CORRECT)
        for spelling in (OLD, CORRECT):
            self.assertTrue(self.ns['is_nick_taken'](spelling))
            self.context.user_data.clear()
            self.context.user_data['awaiting_nick'] = True
            await self.text(spelling)
            self.assertNotIn('temp_nick', self.context.user_data)
            self.assertIn('уже зарегистрирован', self.update.message.reply_text.call_args.args[0])
        self.ns['add_pending_user'](3, ' MACINTOSH ', 'МАГ')
        self.assertEqual(self.ns['get_pending_users']()[0][1], CORRECT)
        self.ns['register_user'](1, 'Admin', 'ВАР')
        self.context.user_data.clear()
        self.context.user_data.update(edit_nick_mode=True, edit_target_user_id=2, edit_old_nick=CORRECT)
        self.update.message.text = OLD
        await self.ns['handle_edit_nick'](self.update, self.context)
        self.assertEqual(self.ns['get_user_nick'](2), CORRECT)

    async def test_ocr_alias_resolves_leader_but_does_not_guess_other_lookalikes(self):
        state = {}
        result = self.ns['accumulate_screenshot'](state, [OLD, 'ЕlMario'], [CORRECT, 'ElMario'])
        self.assertEqual(result, ([CORRECT], ['ЕlMario'], CORRECT))
        self.assertEqual(state['activity_leaders'], [CORRECT])
        state = {}
        result = self.ns['accumulate_screenshot'](state, [OLD, 'Player'], [OLD, CORRECT, 'Player'])
        self.assertEqual(result, (['Player'], [OLD], None))
        self.assertEqual(state['activity_leaders'], [])

    async def test_reconciliation_and_next_month_never_recreate_old_spelling(self):
        self.seed_legacy()
        self.ns['init_db']()
        ws = duplicate_sheet()
        self.ns['reconcile_sheet_profiles'](ws, self.ns['get_all_users']())
        self.assertEqual([n for _, n in attendance.players(ws.rows)].count(CORRECT), 1)
        self.assertNotIn(OLD, [n for _, n in attendance.players(ws.rows)])
        self.assertEqual(ws.rows[4][6:9], ['1', '1', '1'])
        ws.updates.clear()
        self.assertEqual(self.ns['reconcile_sheet_profiles'](ws, self.ns['get_all_users']()), 0)
        self.assertEqual(ws.updates, [])
        book = sheets.Spreadsheet()
        with patch('gspread.WorksheetNotFound', sheets.NotFound):
            future = attendance.ensure_month(book, 2026, 11, self.ns['get_all_users']())
        names = [n for _, n in attendance.players(future.rows)]
        self.assertEqual(names, [CORRECT])


if __name__ == '__main__':
    unittest.main()

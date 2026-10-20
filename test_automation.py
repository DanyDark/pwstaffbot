"""Monthly rollover and bot-controlled event dates, without external services."""
import copy
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sheet_layout as a
from test_sheet_layout import Worksheet, Spreadsheet, NotFound
import test_flows as flows
import test_buttons as buttons


def undated_sheet():
    ws = Worksheet('Активность 10.2026')
    spec = a.layout(ws.rows)
    for cols in spec['events'].values():
        for col in cols:
            for row in spec['date_rows']:
                ws.rows[row - 1][col - 1] = ''
    return ws


class AutomaticSheets(unittest.TestCase):
    def setUp(self):
        today = patch.object(a, 'moscow_today', return_value=date(2026, 10, 31))
        today.start()
        self.addCleanup(today.stop)

    def test_new_date_and_marks_are_one_batch_and_repeat_does_not_downgrade_pl(self):
        ws = undated_sheet()
        self.assertEqual(a.save_event_attendance(ws, 'ГВГ', '20.10.2026', 2026, 10,
                                                ['Leader', 'Player'], ['Leader']), (2, []))
        self.assertEqual(ws.updates, [
            {'range':'AF3', 'values':[['20.10.2026']]},
            {'range':'AF5', 'values':[[2]]}, {'range':'AF6', 'values':[[1]]}])
        before = copy.deepcopy(ws.rows)
        self.assertEqual(a.save_event_attendance(ws, 'ГВГ', '20.10.2026', 2026, 10,
                                                ['Leader', 'Player'], []), (0, []))
        self.assertEqual(ws.rows, before)

    def test_first_half_uses_first_half_salary_columns(self):
        ws = undated_sheet()
        for event, expected in [('РБ', 7), ('Комендант', 21), ('Баньши', 26), ('ГВГ', 30)]:
            with self.subTest(event=event):
                self.assertEqual(a.event_date_slot(ws.rows, event, '15.10.2026', 2026, 10)[0], expected)
        for event, expected in [('РБ', 14), ('Комендант', 23), ('Баньши', 28), ('ГВГ', 32)]:
            with self.subTest(event=event):
                self.assertEqual(a.event_date_slot(ws.rows, event, '16.10.2026', 2026, 10)[0], expected)

    def test_no_free_column_preserves_all_data(self):
        ws = Worksheet()
        ws.rows[4][31:33] = ['1', '2']
        before = copy.deepcopy(ws.rows)
        with self.assertRaisesRegex(ValueError, 'нет свободной колонки'):
            a.save_event_attendance(ws, 'ГВГ', '21.10.2026', 2026, 10, ['Leader'], [])
        self.assertEqual(ws.rows, before)
        self.assertEqual(ws.updates, [])

    def test_unused_planned_date_can_be_replaced_but_used_date_is_preserved(self):
        ws = Worksheet()
        ws.rows[4][31] = '2'
        a.save_event_attendance(ws, 'ГВГ', '21.10.2026', 2026, 10, ['Player'], [])
        self.assertEqual(ws.rows[2][31:33], ['20', '21.10.2026'])
        self.assertEqual(ws.rows[4][31], '2')
        self.assertEqual(ws.rows[5][32], '1')

    def test_undated_marks_and_formulas_are_never_overwritten(self):
        ws = undated_sheet()
        ws.rows[4][31] = '2'
        ws.rows[2][32] = '=TODAY()'
        with self.assertRaisesRegex(ValueError, 'нет свободной колонки'):
            a.save_event_attendance(ws, 'ГВГ', '20.10.2026', 2026, 10, ['Player'], [])
        self.assertEqual(ws.updates, [])

    def test_marks_in_rows_without_nicknames_also_block_date_reassignment(self):
        ws = undated_sheet()
        ws.rows[6][31] = '2'
        ws.rows[9][32] = '1'
        with self.assertRaisesRegex(ValueError, 'нет свободной колонки'):
            a.save_event_attendance(ws, 'ГВГ', '20.10.2026', 2026, 10, ['Leader'], [])
        self.assertEqual(ws.updates, [])

    def test_invalid_mark_aborts_date_and_entire_attendance_batch(self):
        ws = undated_sheet()
        ws.rows[5][31] = 'комментарий'
        # An already dated column must also validate all marks before writing.
        ws.rows[2][31] = '20.10.2026'
        with self.assertRaisesRegex(ValueError, 'нестандартная отметка'):
            a.save_event_attendance(ws, 'ГВГ', '20.10.2026', 2026, 10, ['Leader', 'Player'], [])
        self.assertEqual(ws.updates, [])

    def test_concurrent_events_take_distinct_slots(self):
        ws = undated_sheet()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda day: a.save_event_attendance(
                ws, 'ГВГ', f'{day}.10.2026', 2026, 10, ['Leader'], []), [20, 21]))
        self.assertEqual(results, [(1, []), (1, [])])
        self.assertEqual(set(a.event_dates(ws.rows, 'ГВГ', 2026, 10)), {'20.10.2026', '21.10.2026'})

    def test_invalid_future_and_other_month_dates_are_rejected(self):
        with patch.object(a, 'moscow_today', return_value=date(2026, 10, 20)):
            self.assertEqual(a.parse_event_date('9.10', 2026, 10), '09.10.2026')
            for value in ['31.02.2026', '01.11.2026', '21.10.2026', 'текст']:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    a.parse_event_date(value, 2026, 10)

    def test_new_month_uses_registered_roster_and_keeps_template_and_history(self):
        book = Spreadsheet()
        old = Worksheet('Активность 10.2026')
        book.sheets[old.title] = old
        template_before = copy.deepcopy(book.sheets['Новый шаблон'].rows)
        old_before = copy.deepcopy(old.rows)
        users = [(1, 'NewMage', 'МАГ', None), (2, 'NewWarrior', 'ВАР', None)]
        with patch.dict(sys.modules, {'gspread': SimpleNamespace(WorksheetNotFound=NotFound)}):
            ws = a.ensure_month(book, 2027, 1, users=users)
            self.assertEqual(ws.title, 'Активность 01.2027')
            self.assertEqual({name for _, name in a.players(ws.rows)}, {'NewMage', 'NewWarrior'})
            self.assertEqual(ws.rows[6][4], '=AH7/$AH$1')
            self.assertTrue(all(not a.event_dates(ws.rows, event, 2027, 1) for event in a.EVENTS))
            ws.rows[6][6] = '2'
            self.assertIs(a.ensure_month(book, 2027, 1, users=users), ws)
            self.assertEqual(ws.rows[6][6], '2')
        self.assertEqual(book.sheets['Новый шаблон'].rows, template_before)
        self.assertEqual(old.rows, old_before)

    def test_failed_roster_initialization_can_be_retried(self):
        book = Spreadsheet()
        users = [(i, f'Mage{i}', 'МАГ', None) for i in range(10)]
        with patch.dict(sys.modules, {'gspread': SimpleNamespace(WorksheetNotFound=NotFound)}):
            with self.assertRaisesRegex(ValueError, 'Нет свободной строки'):
                a.ensure_month(book, 2026, 11, users=users)
            self.assertEqual(set(book.sheets), {'Новый шаблон'})
            ws = a.ensure_month(book, 2026, 11, users=[])
            self.assertEqual(list(a.players(ws.rows)), [])

    def test_month_creation_clears_marks_even_in_unnamed_template_rows(self):
        book = Spreadsheet()
        book.sheets['Новый шаблон'].rows[6][6] = '2'
        with patch.dict(sys.modules, {'gspread': SimpleNamespace(WorksheetNotFound=NotFound)}):
            ws = a.ensure_month(book, 2026, 11, users=[])
        self.assertEqual(ws.rows[6][6], '')
        self.assertEqual(book.sheets['Новый шаблон'].rows[6][6], '2')


class AutomaticBotFlows(unittest.IsolatedAsyncioTestCase):
    setUp = flows.BotFlows.setUp
    tearDown = flows.BotFlows.tearDown
    text = flows.BotFlows.text
    prepare = buttons.Buttons.prepare

    async def test_today_on_empty_headers_writes_date_only_after_confirmation(self):
        self.prepare()
        self.ws = undated_sheet()
        self.ns['get_current_activity_sheet'] = lambda: self.ws
        await self.text('📸 Отметить посещение')
        await self.text('РБ')
        await self.text('📅 Сегодня')
        self.assertEqual(self.context.user_data['activity_date'], '31.10.2026')
        self.assertEqual(self.ws.updates, [])
        self.ns['accumulate_screenshot'](self.context.user_data, ['Leader'], ['Leader'])
        self.context.user_data['activity_screenshots'] = 1
        await self.text('ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ')
        self.assertEqual(self.ws.updates, [])
        await self.text('✅ Записать')
        self.assertEqual(self.ws.rows[2][13], '31.10.2026')
        self.assertEqual(self.ws.rows[4][13], '2')

    async def test_yesterday_and_short_date_and_invalid_input(self):
        self.prepare()
        self.ws = undated_sheet()
        self.ns['get_current_activity_sheet'] = lambda: self.ws
        await self.text('📸 Отметить посещение')
        await self.text('РБ')
        await self.text('31.02.2026')
        self.assertEqual(self.context.user_data['activity_step'], 'select_date')
        await self.text('📅 Вчера')
        self.assertEqual(self.context.user_data['activity_date'], '30.10.2026')
        await self.text('← Назад')
        await self.text('15.10')
        self.assertEqual(self.context.user_data['activity_date'], '15.10.2026')

    async def test_first_day_hides_yesterday_and_rejects_previous_month(self):
        self.prepare()
        with patch.object(a, 'moscow_today', return_value=date(2026, 11, 1)):
            await self.text('📸 Отметить посещение')
            await self.text('РБ')
            keyboard = self.update.message.reply_text.call_args.kwargs['reply_markup']
            self.assertEqual(keyboard[0], ['📅 Сегодня'])
            await self.text('📅 Вчера')
            self.assertEqual(self.context.user_data['activity_step'], 'select_date')
            self.assertEqual(self.ws.updates, [])

    async def test_month_change_during_draft_cannot_write_to_new_month(self):
        self.prepare()
        await self.text('📸 Отметить посещение')
        await self.text('ГВГ')
        await self.text('20.10.2026')
        self.ns['accumulate_screenshot'](self.context.user_data, ['Leader'], ['Leader'])
        self.context.user_data['activity_screenshots'] = 1
        await self.text('ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ')
        self.ns['get_current_activity_sheet'] = lambda: Worksheet('Активность 11.2026')
        with patch.object(a, 'moscow_today', return_value=date(2026, 11, 1)):
            await self.text('✅ Записать')
        self.assertIn('Месяц сменился', self.update.message.reply_text.call_args.args[0])
        self.assertEqual(self.ws.updates, [])

    async def test_first_table_access_creates_full_roster_before_personal_report(self):
        self.ns['register_user'](1, 'NewMage', 'МАГ')
        self.ns['register_user'](2, 'NewWarrior', 'ВАР')
        book = Spreadsheet()
        self.ns['get_google_spreadsheet'] = lambda: book
        with patch.dict(sys.modules, {'gspread': SimpleNamespace(WorksheetNotFound=NotFound)}):
            ws = self.ns['get_current_activity_sheet']()
        self.assertEqual({name for _, name in a.players(ws.rows)}, {'NewMage', 'NewWarrior'})
        self.assertEqual(a.player_stats(ws, 'NewMage')['events']['РБ']['visits'], 0)

    async def test_startup_runs_sync_and_registers_midnight_and_retry_jobs(self):
        scheduler = Mock()
        self.ns['AsyncIOScheduler'] = Mock(return_value=scheduler)
        self.ns['refresh_activity_month'] = AsyncMock()
        app = SimpleNamespace(bot_data={})
        await self.ns['post_init'](app)
        self.ns['refresh_activity_month'].assert_awaited_once()
        midnight, retry = scheduler.add_job.call_args_list
        self.assertTrue(all(call.args[0] is self.ns['refresh_activity_month']
                            for call in scheduler.add_job.call_args_list))
        self.assertEqual(midnight.args[1], 'cron')
        self.assertEqual((midnight.kwargs['hour'], midnight.kwargs['minute']), (0, 0))
        self.assertEqual(retry.args[1], 'interval')
        self.assertEqual(retry.kwargs['hours'], 1)
        self.assertIs(app.bot_data['scheduler'], scheduler)

    async def test_temporary_google_failure_does_not_stop_bot_and_next_run_retries(self):
        self.ns.update(GOOGLE_CREDS_JSON='configured', GOOGLE_SHEET_ID='configured')
        sync = Mock(side_effect=[RuntimeError('Google unavailable'), 0])
        self.ns['sync_pa_internal'] = sync
        with self.assertLogs(level='ERROR'):
            await self.ns['refresh_activity_month']()
        await self.ns['refresh_activity_month']()
        self.assertEqual(sync.call_count, 2)


if __name__ == '__main__':
    unittest.main()

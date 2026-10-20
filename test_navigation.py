"""Navigation through user-visible buttons and reversible draft edits."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot_ui as ui
import sheet_layout as attendance
import test_buttons as button_fixtures
import test_flows as flows


class Navigation(unittest.IsolatedAsyncioTestCase):
    setUp = flows.BotFlows.setUp
    tearDown = flows.BotFlows.tearDown
    text = flows.BotFlows.text
    prepare = button_fixtures.Buttons.prepare
    query = button_fixtures.Buttons.query

    async def upload_draft(self):
        await self.text('📸 Отметить посещение')
        await self.text('ГВГ')
        await self.text('20.10.2026')
        self.ns['accumulate_screenshot'](self.context.user_data, ['Leader', 'Player'], ['Leader', 'Player'])
        self.context.user_data['activity_screenshots'] = 1
        await self.text(ui.FINISH_UPLOAD)

    async def test_player_menu_uses_clear_names_and_hides_admin_panel(self):
        self.prepare()
        self.ns['register_user'](2, 'Player', 'МАГ')
        keyboard = self.ns['get_main_keyboard'](2)
        self.assertEqual(keyboard, [
            ['👤 Мой профиль', '💰 Моя зарплата'],
            ['📊 Мои посещения', '🗳 Опрос ГВГ'],
            ['💸 Заявка на кешбек', '❓ Помощь'],
        ])

    async def test_attendance_is_one_click_from_admin_panel(self):
        self.prepare()
        await self.text('⚙️ Панель администратора')
        self.assertIn(['📸 Отметить посещение'], self.update.message.reply_text.call_args.kwargs['reply_markup'])
        await self.text('📸 Отметить посещение')
        self.assertEqual(self.context.user_data['activity_step'], 'select_activity')
        self.assertIn('Шаг 1 из 4', self.update.message.reply_text.call_args.args[0])
        await self.text(ui.BACK)
        self.assertEqual(self.context.user_data['menu'], 'admin')
        self.assertNotIn('activity_mode', self.context.user_data)

    async def test_date_can_be_corrected_after_review_without_reupload_or_early_write(self):
        self.prepare()
        with patch.object(attendance, 'moscow_month', return_value=(2026, 10)):
            await self.upload_draft()
            self.assertEqual(self.ws.updates, [])
            await self.text(ui.BACK)
            self.assertEqual(self.context.user_data['activity_step'], 'upload')
            await self.text(ui.BACK)
            self.assertEqual(self.context.user_data['activity_step'], 'select_date')
            await self.text('27.10.2026')
            self.assertEqual(self.context.user_data['activity_screenshots'], 1)
            await self.text(ui.FINISH_UPLOAD)
            self.assertIn('27.10.2026', self.update.message.reply_text.call_args.args[0])
            await self.text(ui.SAVE_ATTENDANCE)
        self.assertEqual(self.ws.rows[4][32], '2')
        self.assertEqual(self.ws.rows[5][32], '1')
        self.assertEqual(self.ws.rows[4][31], '')
        self.assertEqual(self.context.user_data, {'menu': 'admin'})
        buttons = self.update.message.reply_text.call_args.kwargs['reply_markup']
        self.assertIn(['📸 Отметить другое мероприятие'], buttons)
        await self.text('📸 Отметить другое мероприятие')
        self.assertEqual(self.context.user_data['activity_step'], 'select_activity')
        self.assertNotIn('activity_nicks', self.context.user_data)

    async def test_event_can_be_changed_without_mixing_old_and_new_columns(self):
        self.prepare()
        with patch.object(attendance, 'moscow_month', return_value=(2026, 10)):
            await self.upload_draft()
            for _ in range(3):
                await self.text(ui.BACK)
            await self.text('РБ')
            await self.text('15.10.2026')
            await self.text(ui.FINISH_UPLOAD)
            await self.text(ui.SAVE_ATTENDANCE)
        self.assertEqual(self.ws.rows[4][8], '2')
        self.assertEqual(self.ws.rows[4][31], '')

    async def test_back_from_review_accepts_more_screenshots(self):
        self.prepare()
        with patch.object(attendance, 'moscow_month', return_value=(2026, 10)):
            await self.upload_draft()
            await self.text(ui.BACK)
            self.ns['accumulate_screenshot'](self.context.user_data, ['Warrior', 'Player'], ['Warrior', 'Player'])
            self.context.user_data['activity_screenshots'] += 1
            await self.text(ui.FINISH_UPLOAD)
            await self.text(ui.SAVE_ATTENDANCE)
        self.assertEqual(self.ws.rows[8][31], '2')
        self.assertEqual(self.ws.rows[5][31], '1')

    async def test_cancel_or_home_discards_attendance_without_writing(self):
        self.prepare()
        for button, menu in [('❌ Отменить загрузку', 'admin'), (ui.HOME, 'main')]:
            with self.subTest(button=button), patch.object(attendance, 'moscow_month', return_value=(2026, 10)):
                await self.upload_draft()
                await self.text(button)
                self.assertEqual(self.context.user_data, {'menu': menu})
                self.assertEqual(self.ws.updates, [])
                await self.text(ui.SAVE_ATTENDANCE)
                self.assertEqual(self.ws.updates, [])

    async def test_date_selection_uses_quick_buttons_instead_of_sheet_dates(self):
        self.prepare()
        self.ws.rows[2][29:33] = ['24', '13', '20', '27']
        with patch.object(attendance, 'moscow_month', return_value=(2026, 10)):
            await self.text('📸 Отметить посещение')
            await self.text('ГВГ')
        keyboard = self.update.message.reply_text.call_args.kwargs['reply_markup']
        self.assertEqual(keyboard[0], ['📅 Сегодня', '📅 Вчера'])
        self.assertIn('ДД.ММ.ГГГГ', self.update.message.reply_text.call_args.args[0])

    async def test_profile_back_returns_to_player_selection_then_clan(self):
        self.prepare()
        for button, selected in [('✏️ Изменить ник игрока', 'edit_target_user_id'),
                                 ('✏️ Изменить класс игрока', 'edit_class_user_id')]:
            await self.text(button)
            await self.text('Leader')
            self.assertIn(selected, self.context.user_data)
            await self.text(ui.BACK)
            self.assertNotIn(selected, self.context.user_data)
            self.assertIn('Шаг 1 из 2', self.update.message.reply_text.call_args.args[0])
            await self.text(ui.BACK)
            self.assertEqual(self.context.user_data, {'menu': 'clan'})
        self.assertEqual(self.ns['get_user_nick'](1), 'Leader')
        self.assertEqual(self.ns['get_user_class'](1), 'МАГ')

    async def test_registration_back_allows_correcting_nick_and_home_cancels(self):
        self.prepare()
        self.update.effective_user.id = 2
        await self.ns['start'](self.update, self.context)
        await self.text('WrongName')
        await self.text(ui.BACK)
        await self.text('RightName')
        await self.text('ЛУК')
        self.assertEqual(self.ns['get_pending_users']()[0][1:3], ('RightName', 'ЛУК'))
        self.update.effective_user.id = 3
        await self.ns['start'](self.update, self.context)
        await self.text('AnotherName')
        await self.text(ui.HOME)
        await self.text('ЛУК')
        self.assertFalse(self.ns['is_pending'](3))

    async def test_gvg_back_edits_current_meeting_and_rejects_old_inline_button(self):
        self.prepare()
        self.update.message.reply_text.return_value = SimpleNamespace(message_id=101)
        await self.text('➕ Создать опрос ГВГ')
        await self.text('25.10.2026 19:00')
        await self.text('WrongClan')
        await self.text(ui.BACK)
        self.assertEqual(self.context.user_data['gvg_poll']['step'], 'opponents')
        self.update.message.reply_text.return_value = SimpleNamespace(message_id=202)
        await self.text('RightClan')
        query = self.query('gvg_finish')
        query.message.message_id = 101
        await self.ns['gvg_callback'](self.update, self.context)
        self.assertIsNone(self.ns['get_active_poll']())
        query.message.message_id = 202
        await self.ns['gvg_callback'](self.update, self.context)
        self.assertEqual(self.ns['get_active_poll']()['meetings'], ['25.10.2026 19:00 - RightClan'])

    async def test_gvg_back_from_new_meeting_restores_previous_once(self):
        self.prepare()
        await self.text('➕ Создать опрос ГВГ')
        await self.text('25.10.2026 19:00')
        await self.text('Clan')
        query = self.query('gvg_add_more')
        await self.ns['gvg_callback'](self.update, self.context)
        await self.text(ui.BACK)
        self.assertEqual(self.context.user_data['gvg_poll']['gvg_list'], [])
        query.data = 'gvg_finish'
        await self.ns['gvg_callback'](self.update, self.context)
        self.assertEqual(len(self.ns['get_active_poll']()['meetings']), 1)

    async def test_poll_back_allows_correcting_last_answer_before_save(self):
        self.prepare()
        self.ns['create_poll']('ГВГ', ['Первый', 'Второй'])
        poll_id = self.ns['get_active_poll']()['id']
        query = self.query(f'poll_{poll_id}_0_y')
        await self.ns['poll_callback'](self.update, self.context)
        query.data = f'poll_{poll_id}_1_n'
        await self.ns['poll_callback'](self.update, self.context)
        query.data = f'pollback_{poll_id}_1'
        await self.ns['poll_navigation_callback'](self.update, self.context)
        self.assertEqual(self.context.user_data['poll_session']['answers'], {'Первый': 'да'})
        query.data = f'poll_{poll_id}_1_y'
        await self.ns['poll_callback'](self.update, self.context)
        query.data = f'confirm_{poll_id}'
        await self.ns['confirm_callback'](self.update, self.context)
        self.assertEqual(self.ns['get_user_current_poll_answers'](1), {'Первый': 'да', 'Второй': 'да'})

    async def test_cancelled_poll_cannot_be_saved_by_an_old_confirm_button(self):
        self.prepare()
        self.ns['create_poll']('ГВГ', ['Встреча'])
        poll_id = self.ns['get_active_poll']()['id']
        query = self.query(f'poll_{poll_id}_0_y')
        await self.ns['poll_callback'](self.update, self.context)
        query.data = f'pollcancel_{poll_id}'
        await self.ns['poll_navigation_callback'](self.update, self.context)
        query.data = f'confirm_{poll_id}'
        await self.ns['confirm_callback'](self.update, self.context)
        self.assertEqual(self.ns['get_user_current_poll_answers'](1), {})

    async def test_sheet_link_uses_configured_workbook_and_requires_admin(self):
        self.prepare()
        self.ns['GOOGLE_SHEET_ID'] = 'configured-workbook'
        await self.text('📄 Открыть таблицу')
        keyboard = self.update.message.reply_text.call_args.kwargs['reply_markup']
        self.assertEqual(keyboard.inline_keyboard[0][0].url,
                         'https://docs.google.com/spreadsheets/d/configured-workbook/edit')
        self.ns['register_user'](2, 'Player', 'МАГ')
        self.update.effective_user.id = 2
        await self.text('📄 Открыть таблицу')
        self.assertIn('администратору', self.update.message.reply_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()

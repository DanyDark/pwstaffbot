"""Offline regression tests: no Telegram requests or production database access."""
import ast
import base64
import asyncio
import logging
import os
import sqlite3
import tempfile
import unittest
import json
from datetime import datetime, time, timedelta, date
from decimal import Decimal, InvalidOperation
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import sheet_layout as attendance
import bot_ui as ui
from test_sheet_layout import Worksheet


def load_functions():
    tree = ast.parse(Path(__file__).with_name('bot.py').read_text(encoding='utf-8'))
    wanted = {'CHARACTER_CLASSES', 'CLASS_SHEET_SECTIONS', 'MENU_PARENTS'}
    nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in wanted for t in node.targets
        ):
            nodes.append(node)
    namespace = dict(sqlite3=sqlite3, os=os, logging=logging, closing=closing,
                     asyncio=asyncio, attendance=attendance, ui=ui,
                     Decimal=Decimal, InvalidOperation=InvalidOperation, json=json, datetime=datetime, time=time, timedelta=timedelta,
                     InlineKeyboardButton=lambda text,callback_data=None,url=None: SimpleNamespace(text=text,callback_data=callback_data,url=url),
                     InlineKeyboardMarkup=lambda rows: SimpleNamespace(inline_keyboard=rows),
                     KeyboardButton=lambda text: text,
                     ReplyKeyboardMarkup=lambda rows, **kwargs: rows)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'bot.py', 'exec'), namespace)
    return namespace


class BotFlows(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        today = patch.object(attendance, 'moscow_today', return_value=date(2026, 10, 31))
        today.start()
        self.addCleanup(today.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.ns = load_functions()
        self.ns.update(DB_FILE=str(Path(self.tmp.name) / 'users.db'), ADMIN_LIST=[1])
        self.ns['init_db']()
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                                      message=SimpleNamespace(text='', reply_text=AsyncMock()))
        self.context = SimpleNamespace(user_data={}, bot=SimpleNamespace(send_message=AsyncMock()))

    def tearDown(self):
        self.tmp.cleanup()

    async def text(self, text):
        self.update.message.text = text
        await self.ns['handle_text'](self.update, self.context)

    async def test_pending_admin_can_start_without_approving_other_players(self):
        self.ns['add_pending_user'](1, 'Admin', 'ВАР')
        self.ns['add_pending_user'](2, 'Player', 'МАГ')
        await self.ns['start'](self.update, self.context)
        self.assertTrue(self.ns['is_registered'](1))
        self.assertFalse(self.ns['is_pending'](1))
        self.assertFalse(self.ns['is_registered'](2))
        self.assertTrue(self.ns['is_pending'](2))
        await self.text('📊 Админ-панель')
        await self.text('📊 Управление опросами')
        self.assertEqual(self.context.user_data['menu'], 'polls')

    async def test_regular_pending_player_is_not_promoted(self):
        self.ns['add_pending_user'](2, 'Player', 'МАГ')
        self.update.effective_user.id = 2
        await self.ns['start'](self.update, self.context)
        self.assertFalse(self.ns['is_registered'](2))
        await self.text('📊 Админ-панель')
        self.assertNotEqual(self.context.user_data.get('menu'), 'admin')

    async def test_new_admin_registration_is_immediate(self):
        self.context.user_data.update(awaiting_class=True, temp_nick='Admin')
        await self.text('ВАР')
        self.assertTrue(self.ns['is_registered'](1))
        self.assertFalse(self.ns['is_pending'](1))

    async def test_back_follows_parent_menus(self):
        self.ns['register_user'](1, 'Admin', 'ВАР')
        for button in ('📊 Админ-панель', '🏰 Управление кланом', '✏️ Исправить профиль'):
            await self.text(button)
        for expected in ('clan', 'admin', 'main'):
            await self.text('🔙 Назад')
            self.assertEqual(self.context.user_data['menu'], expected)

    async def test_cancel_clears_all_input_modes(self):
        self.context.user_data.update(menu='polls', gvg_poll={'step': 'datetime'}, admin_poll_step='awaiting_input')
        await self.text('❌ Отмена')
        self.assertEqual(self.context.user_data, {'menu': 'polls'})

    async def test_gvg_text_is_routed_once(self):
        self.ns['register_user'](1, 'Admin', 'ВАР')
        self.context.user_data['gvg_poll'] = {'step': 'datetime'}
        handler = AsyncMock()
        self.ns['handle_gvg_poll_creation'] = handler
        await self.text('25.10.2026 19:00')
        handler.assert_awaited_once()

    async def test_rejection_works_for_registered_admin(self):
        self.ns['register_user'](1, 'Admin', 'ВАР')
        self.ns['add_pending_user'](2, 'Player', 'МАГ')
        self.context.user_data.update(menu='registration', reject_mode=True)
        await self.text('❌ Player')
        self.assertFalse(self.ns['is_pending'](2))

    async def test_end_poll_rejects_non_admin(self):
        self.update.effective_user.id = 2
        self.ns['deactivate_poll'] = lambda: self.fail('Unauthorized mutation')
        await self.ns['end_poll_command'](self.update, self.context)

    async def test_new_regular_player_stays_pending(self):
        self.update.effective_user.id = 2
        self.context.user_data.update(awaiting_class=True, temp_nick='Player')
        await self.text('ЛУК')
        self.assertFalse(self.ns['is_registered'](2))
        self.assertTrue(self.ns['is_pending'](2))

    async def test_gvg_add_more_sends_reply_keyboard_in_new_message(self):
        self.context.user_data['gvg_poll'] = {
            'step': 'waiting_choice', 'datetime': '25.10.2026 19:00', 'opponents': 'Clan'
        }
        query = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1),
                                data='gvg_add_more', edit_message_reply_markup=AsyncMock(),
                                message=SimpleNamespace(reply_text=AsyncMock()))
        self.update.callback_query = query
        await self.ns['gvg_callback'](self.update, self.context)
        query.message.reply_text.assert_awaited_once()
        self.assertEqual(self.context.user_data['gvg_poll']['step'], 'datetime')
        self.assertEqual(len(self.context.user_data['gvg_poll']['gvg_list']), 1)

    async def test_stale_gvg_finish_does_not_create_poll(self):
        self.context.user_data['gvg_poll'] = {'step': 'datetime'}
        self.ns['create_poll'] = lambda *args: self.fail('Incomplete poll created')
        self.update.callback_query = SimpleNamespace(
            answer=AsyncMock(), from_user=SimpleNamespace(id=1), data='gvg_finish',
            message=SimpleNamespace(reply_text=AsyncMock())
        )
        await self.ns['gvg_callback'](self.update, self.context)

    async def test_ocr_missing_key_is_explained(self):
        self.ns['OCR_SPACE_API_KEY'] = ''
        with self.assertRaisesRegex(ValueError, 'OCR_SPACE_API_KEY'):
            self.ns['extract_nicks_from_image'](b'image')

    async def test_ocr_labels_jpeg_correctly_and_parses_all_results(self):
        response = Mock()
        response.json.return_value = {'ParsedResults': [
            {'ParsedText': 'PlayerOne\n123'}, {'ParsedText': 'PlayerTwo'}
        ]}
        post = Mock(return_value=response)
        self.ns.update(OCR_SPACE_API_KEY='test-key', base64=base64,
                       requests=SimpleNamespace(post=post))
        result = self.ns['extract_nicks_from_image'](bytes.fromhex('ffd8ff') + b'image')
        self.assertEqual(result, ['PlayerOne', '123', 'PlayerTwo'])
        self.assertTrue(post.call_args.kwargs['data']['base64Image'].startswith('data:image/jpeg;'))
        response.raise_for_status.assert_called_once()

    async def test_attendance_requires_date_leader_and_confirmation(self):
        ws = Worksheet('Активность 10.2026 (136)')
        self.ns['get_current_activity_sheet'] = lambda: ws
        with patch.object(attendance, 'moscow_month', return_value=(2026,10)):
            self.context.user_data.update(activity_mode=True, activity_step='select_activity')
            for text in ('ГВГ','20.10.2026'):
                self.update.message.text = text
                await self.ns['handle_activity_choice'](self.update,self.context)
                self.assertEqual(ws.updates, [])
            self.assertEqual(self.context.user_data['activity_step'], 'upload')
            self.ns['accumulate_screenshot'](self.context.user_data,['Leader','Player'],['Leader','Player'])
            self.context.user_data['activity_screenshots'] = 1
            self.update.message.text = 'ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ'
            await self.ns['handle_activity_choice'](self.update,self.context)
            self.assertEqual(ws.updates, [])
            self.assertEqual(self.context.user_data['activity_step'], 'confirm')
            self.update.message.text = '✅ Записать'
            await self.ns['handle_activity_choice'](self.update,self.context)
        self.assertEqual(len(ws.updates),2)
        self.assertEqual(ws.rows[4][31],'2')
        self.assertNotIn('activity_mode',self.context.user_data)

    async def test_multiple_screenshots_accumulate_and_keep_each_leader(self):
        state = {}
        self.ns['accumulate_screenshot'](state,['Leader','Player'],['Leader','Player','Warrior'])
        self.ns['accumulate_screenshot'](state,['Warrior','Player'],['Leader','Player','Warrior'])
        self.assertEqual(state['activity_nicks'],['Leader','Player','Warrior'])
        self.assertEqual(state['activity_leaders'],['Leader','Warrior'])

    async def test_unknown_top_never_promotes_next_player(self):
        state = {}
        result = self.ns['accumulate_screenshot'](state,['Unkn0wn','Player'],['Player'])
        self.assertEqual(state['activity_nicks'],['Player'])
        self.assertEqual(state['activity_leaders'],[])
        self.assertEqual(state['activity_unknown'],['Unkn0wn'])
        self.assertIsNone(result[2])

    async def test_unknown_nick_is_not_fuzzy_assigned_to_another_player(self):
        state = {}
        self.ns['accumulate_screenshot'](state,['P1ayer','Warrior'],['Player','Warrior'])
        self.assertEqual(state['activity_nicks'],['Warrior'])
        self.assertEqual(state['activity_leaders'],[])
        self.assertEqual(state['activity_unknown'],['P1ayer'])

    async def test_photo_warning_names_unknown_top_and_recommends_manual_entry(self):
        self.context.user_data.update(activity_mode=True,activity_step='upload',
            activity_known=['Player'],activity_screenshots=0)
        self.ns['extract_nicks_from_image'] = lambda image: ['Unkn0wn','Player']
        file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b'image')))
        self.update.message.photo = [SimpleNamespace(get_file=AsyncMock(return_value=file))]
        await self.ns['handle_photo'](self.update,self.context)
        text = self.update.message.reply_text.call_args.args[0]
        self.assertIn('Unkn0wn',text)
        self.assertIn('вручную',text)
        self.assertIn('ПЛ не назначен',text)
        self.assertEqual(self.context.user_data['activity_step'],'upload')

    async def test_finish_without_recognized_players_stays_in_upload(self):
        self.context.user_data.update(activity_mode=True,activity_step='upload',
            activity_screenshots=1,activity_nicks=[])
        with patch.object(attendance,'moscow_month',return_value=(2026,10)):
            self.update.message.text = 'ЗАВЕРШИТЬ ЗАГРУЗКУ СКРИНОВ'
            await self.ns['handle_activity_choice'](self.update,self.context)
        self.assertEqual(self.context.user_data['activity_step'],'upload')

    async def test_large_upload_report_fits_telegram_messages(self):
        await self.ns['send_activity_report'](self.update,'x'*8000,reply_markup='keyboard')
        calls = self.update.message.reply_text.call_args_list
        self.assertEqual(len(calls),3)
        self.assertTrue(all(len(call.args[0]) <= 3500 for call in calls))
        self.assertEqual(calls[-1].kwargs['reply_markup'],'keyboard')

    async def test_salary_is_read_from_table_without_writes(self):
        ws = Worksheet()
        self.ns['get_current_activity_sheet'] = lambda: ws
        _, values = self.ns['salary_summary']('Leader')
        self.assertEqual(values,['Leader (стр. 5): 1/2 — 100 000; 2/2 — 200 000'])
        self.assertEqual(ws.updates,[])


if __name__ == '__main__':
    unittest.main()

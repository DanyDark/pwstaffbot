"""Button routing and stateful scenarios, without external side effects."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import test_flows as flows
from test_sheet_layout import Worksheet


class Buttons(unittest.IsolatedAsyncioTestCase):
    setUp=flows.BotFlows.setUp
    tearDown=flows.BotFlows.tearDown
    text=flows.BotFlows.text

    def prepare(self):
        self.ns['register_user'](1,'Leader','МАГ')
        self.ws=Worksheet('Активность 10.2026 (136)')
        self.ws.rows[4][35:38]=[100000,200000,50000]
        self.ws.rows[4][4:6]=[0.5,0.25]
        self.ns['get_current_activity_sheet']=lambda:self.ws
        self.ns['export_poll_results']=lambda poll:['Опрос 1']
        self.context.bot.send_photo=AsyncMock()

    def query(self,data):
        self.update.callback_query=SimpleNamespace(
            data=data,from_user=SimpleNamespace(id=self.update.effective_user.id),
            answer=AsyncMock(),edit_message_text=AsyncMock(),edit_message_reply_markup=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()))
        return self.update.callback_query

    async def test_every_published_menu_button_routes_without_unknown_command(self):
        self.prepare()
        self.ns['create_poll']('Опрос',['25.10.2026 19:00 — Клан'])
        menus=[self.ns['get_main_keyboard'](1),self.ns['get_admin_keyboard'](),
               self.ns['get_clan_management_keyboard'](),self.ns['get_polls_management_keyboard']()]
        buttons=list(dict.fromkeys(button for menu in menus for row in menu for button in row))
        buttons += ['✏️ Исправить класс','🔄 Исправить ник','📋 Список ожидания','✅ Подтвердить всех','❌ Отказать']
        for button in buttons:
            with self.subTest(button=button):
                self.context.user_data.clear()
                self.update.message.reply_text.reset_mock()
                await self.text(button)
                replies=[str(c.args[0]) for c in self.update.message.reply_text.call_args_list]
                self.assertTrue(replies,button)
                self.assertFalse(any('Неизвестная команда' in text for text in replies),button)
                self.assertFalse(any('не выполнен' in text or 'Не удалось' in text for text in replies),replies)

    async def test_personal_activity_counts_visits_and_leadership_separately(self):
        self.prepare()
        self.ws.rows[4][6:9]=[2,1,0]
        self.ws.rows[4][20]=2
        await self.text('📊 Моя активность')
        text=self.update.message.reply_text.call_args.args[0]
        self.assertIn('РБ: посещений 2, из них ПЛ 1, баллов 3',text)
        self.assertIn('Всего посещений: 3',text)
        self.assertIn('Всего баллов: 5',text)
        self.assertIn('50%',text)

    async def test_salary_shows_halves_total_and_no_double_captain(self):
        self.prepare()
        await self.text('💰 Моя ЗП')
        text=self.update.message.reply_text.call_args.args[0]
        self.assertIn('100 000',text)
        self.assertIn('200 000',text)
        self.assertIn('Итого за месяц: 300 000',text)
        self.assertNotIn('350 000',text)
        self.assertEqual(self.ws.updates,[])

    async def test_formula_error_is_not_reported_as_zero_salary(self):
        self.prepare()
        self.ws.rows[4][35]='#REF!'
        await self.text('💰 Моя ЗП')
        self.assertIn('ошибка формулы (#REF!)',self.update.message.reply_text.call_args.args[0])

    async def test_duplicate_nickname_reports_rows_instead_of_another_players_salary(self):
        self.prepare()
        self.ws.rows[5][2]='leader'
        await self.text('💰 Моя ЗП')
        self.assertIn('5, 6',self.update.message.reply_text.call_args.args[0])
        self.assertEqual(self.ws.updates,[])
        with self.assertRaisesRegex(ValueError,'5, 6'):
            self.ns['reconcile_sheet_profiles'](self.ws,self.ns['get_all_users']())

    async def test_missing_player_has_actionable_error(self):
        self.prepare()
        self.ws.rows[4][2]='Different'
        await self.text('📊 Моя активность')
        self.assertIn('/sync_pa',self.update.message.reply_text.call_args.args[0])

    async def test_main_button_cancels_form_instead_of_becoming_nickname(self):
        self.prepare()
        await self.text('🔄 Исправить ник')
        await self.text('Leader')
        await self.text('👤 Мой профиль')
        self.assertEqual(self.ns['get_user_nick'](1),'Leader')
        self.assertNotIn('edit_nick_mode',self.context.user_data)

    async def test_back_after_cash_review_goes_to_main(self):
        self.prepare()
        await self.text('💸 Выдача кеша')
        await self.text('🔙 Назад')
        self.assertEqual(self.context.user_data['menu'],'main')

    async def test_cash_callback_works_without_message_and_is_single_use(self):
        self.prepare()
        self.ns['create_cash_order'](1,'Leader','photo-id')
        order_id=self.ns['get_pending_orders']()[0][0]
        query=self.query(f'cash_done_{order_id}')
        self.update.message=None
        await self.ns['cash_callback'](self.update,self.context)
        self.assertEqual(self.ns['get_pending_orders'](),[])
        self.context.bot.send_message.assert_awaited_once()
        self.assertIn('Нет новых',query.message.reply_text.call_args.args[0])
        query.data=f'cash_reject_{order_id}'
        await self.ns['cash_callback'](self.update,self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertIn('уже обработана',query.edit_message_text.call_args.args[0])

    async def test_cash_review_command_denies_non_admin(self):
        self.prepare()
        self.update.effective_user.id=2
        await self.ns['process_cash_orders'](self.update,self.context)
        self.assertIn('администратору',self.update.message.reply_text.call_args.args[0])
        self.context.bot.send_photo.assert_not_awaited()

    async def test_cash_form_explains_expected_input_and_accepts_photo(self):
        self.prepare()
        await self.text('💰 Заказ кеша')
        await self.text('Оплатил')
        self.assertIn('Ожидаю скриншот',self.update.message.reply_text.call_args.args[0])
        self.update.message.photo=[SimpleNamespace(file_id='existing-photo-id')]
        await self.ns['handle_photo'](self.update,self.context)
        self.assertEqual(self.ns['get_pending_orders']()[0][3],'existing-photo-id')
        self.assertNotIn('cash_order',self.context.user_data)

    async def test_registration_rechecks_nick_after_class_selection(self):
        self.prepare()
        self.update.effective_user.id=2
        self.context.user_data.update(awaiting_class=True,temp_nick='leader')
        await self.text('МАГ')
        self.assertFalse(self.ns['is_pending'](2))
        self.assertTrue(self.context.user_data['awaiting_nick'])

    async def test_poll_buttons_are_short_even_for_long_cyrillic_opponents(self):
        self.prepare()
        meeting='25.10.2026 19:00 — '+'Я'*70
        self.ns['create_poll']('ГВГ',[meeting])
        poll=self.ns['get_active_poll']()
        await self.ns['send_first_question'](1,poll,self.context)
        markup=self.context.bot.send_message.call_args.kwargs['reply_markup']
        self.assertTrue(all(len(b.callback_data.encode('utf-8'))<=64 for row in markup.inline_keyboard for b in row))
        query=self.query(f"poll_{poll['id']}_0_y")
        await self.ns['poll_callback'](self.update,self.context)
        await self.text('📊 Админ-панель')
        self.assertIn('poll_session',self.context.user_data)
        query.data=f"confirm_{poll['id']}"
        await self.ns['confirm_callback'](self.update,self.context)
        self.assertEqual(self.ns['get_user_current_poll_answers'](1),{meeting:'да'})

    async def test_closed_poll_cannot_be_confirmed(self):
        self.prepare()
        self.ns['create_poll']('ГВГ',['Встреча'])
        poll=self.ns['get_active_poll']()
        self.context.user_data['poll_session']={'id':poll['id'],'next':1,'answers':{'Встреча':'да'}}
        self.ns['deactivate_poll']()
        self.query(f"confirm_{poll['id']}")
        self.ns['save_responses']=Mock()
        await self.ns['confirm_callback'](self.update,self.context)
        self.ns['save_responses'].assert_not_called()

    async def test_poll_replay_cannot_skip_questions(self):
        self.prepare()
        self.ns['create_poll']('ГВГ',['Первый','Второй'])
        poll=self.ns['get_active_poll']()
        query=self.query(f"poll_{poll['id']}_0_y")
        await self.ns['poll_callback'](self.update,self.context)
        await self.ns['poll_callback'](self.update,self.context)
        self.assertEqual(self.context.user_data['poll_session']['next'],1)
        query.data=f"confirm_{poll['id']}"
        await self.ns['confirm_callback'](self.update,self.context)
        self.assertEqual(self.ns['get_user_current_poll_answers'](1),{})

    async def test_manual_poll_reply_updates_registered_users_answers(self):
        self.prepare()
        self.ns['create_poll']('ГВГ',['Встреча'])
        await self.text('👥 Пройти за другого')
        await self.text('Leader \\ МАГ \\ Буду')
        self.assertEqual(self.ns['get_user_current_poll_answers'](1),{'Встреча':'да'})
        self.assertEqual(self.ns['get_non_responders'](self.ns['get_active_poll']()['id']),[])

    async def test_non_admin_cannot_use_id_callback(self):
        self.prepare()
        self.update.effective_user.id=2
        query=self.query('show_all_ids')
        await self.ns['show_all_ids_callback'](self.update,self.context)
        self.assertIn('администратору',query.message.reply_text.call_args.args[0])

    async def test_nick_change_records_alias_and_preserves_case_insensitive_lookup(self):
        self.prepare()
        await self.text('🔄 Исправить ник')
        await self.text('leader')
        await self.text('NewLeader')
        self.assertEqual(self.ns['get_user_nick'](1),'NewLeader')
        self.assertEqual(self.ns['get_user_id_by_nick']('newleader'),1)
        self.assertEqual(self.context.user_data['menu'],'profile')

    async def test_sheet_edit_failure_does_not_change_database_profile(self):
        self.prepare()
        self.ns['update_sheet_profile']=Mock(side_effect=ValueError('Лист недоступен'))
        await self.text('🔄 Исправить ник')
        await self.text('Leader')
        await self.text('NewLeader')
        self.assertEqual(self.ns['get_user_nick'](1),'Leader')

    async def test_casefold_duplicate_nick_cannot_register(self):
        self.prepare()
        self.assertTrue(self.ns['is_nick_taken']('leader'))

    async def test_non_admin_receives_clear_denial_for_admin_buttons(self):
        self.prepare()
        self.ns['register_user'](2,'Player','МАГ')
        self.update.effective_user.id=2
        for text in ('💰 Зарплаты игроков','📤 Разослать опрос','✅ Подтвердить всех','💸 Выдача кеша'):
            await self.text(text)
            self.assertIn('администратору',self.update.message.reply_text.call_args.args[0])

    async def test_my_answers_can_start_poll_without_admin_resending(self):
        self.prepare()
        self.ns['create_poll']('ГВГ',['Первая встреча'])
        await self.text('📝 Мои ответы')
        keyboard=self.update.message.reply_text.call_args.kwargs['reply_markup']
        self.assertTrue(keyboard.inline_keyboard[0][0].callback_data.startswith('restart_'))

    async def test_new_month_reconciles_old_nick_alias_without_losing_attendance(self):
        self.prepare()
        await self.text('🔄 Исправить ник')
        await self.text('Leader')
        await self.text('NewLeader')
        self.ws.rows[4][6]='2'
        self.ns['reconcile_sheet_profiles'](self.ws,self.ns['get_all_users']())
        self.assertEqual(self.ws.rows[4][2],'NewLeader')
        self.assertEqual(self.ws.rows[4][6],'2')
        self.assertEqual(sum(row[2]=='NewLeader' for row in self.ws.rows),1)

    async def test_alias_owned_by_another_user_cannot_be_reclaimed(self):
        self.prepare()
        await self.text('🔄 Исправить ник')
        await self.text('Leader')
        await self.text('NewLeader')
        self.ns['register_user'](2,'Leader','МАГ')
        self.ns['reconcile_sheet_profiles'](self.ws,self.ns['get_all_users']())
        self.assertEqual(self.ws.rows[4][2],'Leader')
        self.assertEqual(self.ws.rows[6][2],'NewLeader')

    async def test_gvg_duplicate_is_rejected_before_finish_and_creation_can_continue(self):
        self.prepare()
        await self.text('📝 Создать опрос (ГВГ)')
        await self.text('25.10.2026 19:00')
        await self.text('Клан')
        query=self.query('gvg_add_more')
        await self.ns['gvg_callback'](self.update,self.context)
        await self.text('25.10.2026 19:00')
        await self.text('Клан')
        self.assertEqual(self.context.user_data['gvg_poll']['step'],'opponents')
        await self.text('Другой клан')
        query.data='gvg_finish'
        await self.ns['gvg_callback'](self.update,self.context)
        self.assertEqual(len(self.ns['get_active_poll']()['meetings']),2)
        self.assertEqual(self.context.user_data['menu'],'polls')

    async def test_export_names_are_separate_from_attendance_sheets(self):
        self.prepare()
        self.ns['create_poll']('ГВГ',['Новый шаблон'])
        poll=self.ns['get_active_poll']()
        self.ns['save_responses'](1,poll['id'],{'Новый шаблон':'да'})
        book=Mock()
        self.ns['get_google_spreadsheet']=lambda:book
        # Use the real function, rather than the stub used by the menu smoke test.
        real=flows.load_functions()['export_poll_results']
        self.ns['export_poll_results']=type(real)(real.__code__,self.ns)
        names=self.ns['export_poll_results'](poll)
        self.assertTrue(names[0].startswith('Опрос '))
        self.assertNotEqual(names[0],'Новый шаблон')


if __name__=='__main__':
    unittest.main()

"""Focused checks for the new class-aware Latin/Cyrillic duplicate guard."""
import copy
import unittest

import nickname_rules as rules
import sheet_layout as attendance
import test_flows as flows
import test_sheet_layout as sheets


class ClassNicknameTests(unittest.IsolatedAsyncioTestCase):
    setUp = flows.BotFlows.setUp
    tearDown = flows.BotFlows.tearDown
    text = flows.BotFlows.text

    async def test_visual_comparison_preserves_spelling_and_does_not_transliterate(self):
        self.assertEqual(rules.visual_key(' АВЕКМНОРСТУХ '), 'abekmhopctyx')
        self.assertEqual(rules.canonical('Аlpha'), 'Аlpha')
        for first, second in [('P1ayer', 'Player'), ('Иван', 'Ivan'), ('Вася', 'Bacяx')]:
            self.assertNotEqual(rules.visual_key(first), rules.visual_key(second))

    async def test_registration_blocks_same_class_and_pending_variants(self):
        self.ns['add_pending_user'](2, 'Alpha', 'МАГ')
        self.update.effective_user.id = 3
        self.context.user_data['awaiting_nick'] = True
        await self.text('Аlpha')
        self.assertTrue(self.context.user_data['awaiting_class'])
        await self.text('МАГ')
        self.assertFalse(self.ns['is_pending'](3))
        self.assertTrue(self.context.user_data['awaiting_nick'])
        self.assertIn('русских и английских', self.update.message.reply_text.call_args.args[0])

    async def test_different_classes_can_register_different_spellings(self):
        self.ns['register_user'](2, 'Alpha', 'МАГ')
        self.update.effective_user.id = 3
        self.context.user_data['awaiting_nick'] = True
        await self.text('Аlpha')
        await self.text('ВАР')
        self.assertEqual(self.ns['get_pending_users']()[0][1:3], ('Аlpha', 'ВАР'))
        self.assertFalse(self.ns['is_nick_taken']('Аlpha', 'ПРИСТ', exclude_user_id=3))
        self.assertTrue(self.ns['is_nick_taken']('Аlpha', 'МАГ', exclude_user_id=3))

    async def test_edit_nickname_and_class_reject_same_class_collision(self):
        self.ns['register_user'](1, 'Admin', 'ВАР')
        self.ns['register_user'](2, 'Alpha', 'МАГ')
        self.ns['register_user'](3, 'Other', 'МАГ')
        self.context.user_data.update(edit_nick_mode=True, edit_target_user_id=3, edit_old_nick='Other')
        await self.text('Аlpha')
        self.assertEqual(self.ns['get_user_nick'](3), 'Other')
        self.assertIn('русских и английских', self.update.message.reply_text.call_args.args[0])
        self.ns['register_user'](4, 'Аlpha', 'ВАР')
        self.context.user_data.clear()
        self.context.user_data.update(edit_class_mode=True, edit_class_user_id=4, edit_class_nick='Аlpha')
        await self.text('МАГ')
        self.assertEqual(self.ns['get_user_class'](4), 'ВАР')
        self.assertIn('русских и английских', self.update.message.reply_text.call_args.args[0])

    async def test_sync_reuses_row_preserving_marks_money_and_formulas(self):
        for through_bot in (False, True):
            with self.subTest(through_bot=through_bot):
                ws = sheets.Worksheet()
                ws.rows[5][2] = 'Plаyer'
                ws.rows[5][6], ws.rows[5][37], ws.rows[5][35] = '2', '50000', '=SUM(G6:AG6)'
                expected = copy.deepcopy(ws.rows)
                expected[5][2] = 'Player'
                if through_bot:
                    self.ns['register_user'](2, 'Player', 'МАГ')
                    count = self.ns['reconcile_sheet_profiles'](ws, self.ns['get_all_users']())
                else:
                    count = attendance.sync_players(ws, [(2, 'Player', 'МАГ')])
                self.assertEqual(count, 0)
                self.assertEqual(ws.rows, expected)
                self.assertEqual([update['range'] for update in ws.updates], ['C6'])

    async def test_sync_does_not_merge_different_classes(self):
        ws = sheets.Worksheet()
        ws.rows[4][2] = 'Alpha'
        count = attendance.sync_players(ws, [(2, 'Alpha', 'МАГ'), (3, 'Аlpha', 'ВАР')])
        self.assertEqual(count, 1)
        self.assertEqual(ws.rows[4][2], 'Alpha')
        self.assertEqual(ws.rows[9][2], 'Аlpha')

    async def test_multiple_rows_or_owners_report_conflict_without_writes(self):
        ws = sheets.Worksheet()
        ws.rows[4][2], ws.rows[5][2] = 'Alpha', 'Аlpha'
        before = copy.deepcopy(ws.rows)
        with self.assertRaisesRegex(ValueError, 'несколько строк'):
            attendance.sync_players(ws, [(2, 'Alpha', 'МАГ')])
        self.assertEqual(ws.rows, before)
        self.assertEqual(ws.updates, [])
        self.ns['register_user'](2, 'Alpha', 'МАГ')
        self.ns['add_pending_user'](3, 'Аlpha', 'МАГ')
        with self.assertRaisesRegex(ValueError, 'несколько Telegram'):
            self.ns['reconcile_sheet_profiles'](ws, self.ns['get_all_users']())
        self.assertEqual(ws.updates, [])

    async def test_similar_row_owned_by_other_class_is_not_reclaimed(self):
        ws = sheets.Worksheet()
        ws.rows[4][2] = 'Аlpha'  # The warrior's nickname was put in the mage section.
        users = [(2, 'Alpha', 'МАГ'), (3, 'Аlpha', 'ВАР')]
        with self.assertRaisesRegex(ValueError, 'другому профилю'):
            attendance.sync_players(ws, users)
        self.assertEqual(ws.updates, [])

    async def test_legacy_conflicting_requests_are_not_approved(self):
        self.ns['register_user'](2, 'Alpha', 'МАГ')
        self.ns['add_pending_user'](3, 'Аlpha', 'МАГ')
        await self.ns['confirm_all_pending_command'](self.update, self.context)
        self.assertFalse(self.ns['is_registered'](3))
        self.assertTrue(self.ns['is_pending'](3))
        self.assertIn('Заявки не подтверждены', self.update.message.reply_text.call_args.args[0])
        self.context.bot.send_message.assert_not_awaited()
        self.ns['add_pending_user'](1, 'Аlpha', 'МАГ')
        with self.assertLogs(level='ERROR'):
            self.ns['approve_admin_registration'](1)
        self.assertFalse(self.ns['is_registered'](1))


if __name__ == '__main__':
    unittest.main()

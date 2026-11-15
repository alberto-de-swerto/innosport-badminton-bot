from datetime import timedelta
import json
from unittest import mock
import ast
from pathlib import Path
import subprocess
import i18n

import sportsite
import telegram_bot as bot
from attendance_bot import AttendanceUI, run_worker
from attendance_editor import ActionStore
from test_sportsite import TemporaryProject, START, PEOPLE
from test_telegram_bot import SETTINGS, NOW, update


class AttendanceBotTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.at=START+timedelta(hours=3)
        clock=mock.patch.object(sportsite.Config,"now",lambda _:self.at)
        clock.start()
        self.addCleanup(clock.stop)
        self.api=mock.Mock()
        self.settings=dict(SETTINGS,language="en")
        self.card={"date":START.date().isoformat(),"start":START.isoformat(),"end":(START+timedelta(hours=2)).isoformat(),
                   "training_id":101,"group_id":17,"max_hours":2,"people":[dict(PEOPLE[0],hours=0)]}
        self.worker=mock.Mock(return_value={"card":self.card})
        self.ui=AttendanceUI(self.config,self.settings,self.api,self.worker)

    def callback(self,data,user=77,kind="private",bot_id=123456789):
        return {"callback_query":{"id":"fictional-hour-click","data":data,"from":{"id":user,"is_bot":False},
                "message":{"message_id":22,"from":{"id":bot_id},"chat":{"id":77,"type":kind}}}}

    def test_menu_excludes_future_sessions_and_preserves_language(self):
        self.ui.command(["/attendance"])
        text=self.api.send.call_args.args[1]
        self.assertIn("Choose a session",text)
        buttons=self.api.send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(buttons),1)
        self.assertIn("05.11.2026",buttons[0][0]["text"])
        self.worker.assert_not_called()

    def test_roster_person_and_hour_buttons_bind_owner_expected_value_and_target(self):
        self.ui.roster(START.date().isoformat())
        button=self.api.send.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertLessEqual(len(button["callback_data"].encode()),64)
        self.worker.return_value={"card":self.card,"person":self.card["people"][0]}
        self.ui.callback(button["callback_data"][3:],None)
        keyboard=self.api.send.call_args.kwargs["reply_markup"]["inline_keyboard"]
        token=keyboard[0][0]["callback_data"][3:]
        action=ActionStore(self.config).lookup(token,77)
        self.assertEqual((action["op"],action["owner"],action["hours"],action["expected"]),("set",77,1,0))
        self.assertEqual((action["training_id"],action["group_id"]),(101,17))
        self.assertIn("immediately",self.api.send.call_args.args[1])

    def test_non_owner_groups_and_other_bot_buttons_cannot_access_editor(self):
        token=self.ui.store.issue(77,"roster",date=START.date().isoformat(),page=0)
        with mock.patch('attendance_bot.AttendanceUI.callback') as handler:
            for value in (self.callback("at:"+token,user=88),self.callback("at:"+token,kind="group"),self.callback("at:"+token,bot_id=9)):
                bot.handle_update(self.config,SETTINGS,self.api,value,now=NOW)
            handler.assert_not_called()
            bot.handle_update(self.config,SETTINGS,self.api,self.callback("at:"+token),now=NOW)
            handler.assert_called_once()
        self.assertEqual(self.api.call.call_args.args[0],"answerCallbackQuery")

    def test_commands_route_only_after_private_owner_and_stale_message_checks(self):
        with mock.patch('attendance_bot.AttendanceUI.command') as handler:
            for kwargs in ({"user":88},{"kind":"group"},{"timestamp":NOW-901}):
                bot.handle_update(self.config,SETTINGS,self.api,update(text="/attendance",**kwargs),now=NOW)
            handler.assert_not_called()
            bot.handle_update(self.config,SETTINGS,self.api,update(text="/attendance"),now=NOW)
            handler.assert_called_once_with(["/attendance"])

    def test_pending_search_accepts_partial_email_and_cancel_stops_it(self):
        self.ui.store.pending(77,day=START.date().isoformat())
        self.worker.return_value={"card":self.card,"matches":[{"student_id":3,"full_name":"Fictional Extra","email":"extra@example.invalid"}]}
        self.assertTrue(self.ui.pending_reply("extra"))
        self.worker.assert_called_with(self.config,{"op":"search","date":START.date().isoformat(),"query":"extra"})
        self.assertIn("extra@example.invalid",self.api.send.call_args.args[1])
        self.ui.command(["/cancel"])
        self.assertFalse(self.ui.pending_reply("extra"))

    def test_direct_find_can_target_yesterday_instead_of_latest_session(self):
        self.worker.return_value={"card":self.card,"matches":[]}
        self.ui.command(["/find","extra_t",START.date().isoformat()])
        self.worker.assert_called_once_with(self.config,{"op":"search","date":START.date().isoformat(),"query":"extra_t"})
        self.assertIn("No matches",self.api.send.call_args.args[1])

    def test_expired_button_never_calls_worker(self):
        token=self.ui.store.issue(77,"roster",date=START.date().isoformat(),page=0)
        self.at+=timedelta(minutes=16)
        self.ui.callback(token,None)
        self.worker.assert_not_called()
        self.assertIn("expired",self.api.send.call_args.args[1])

    def test_confirmed_save_with_failed_refresh_does_not_report_failure_to_save(self):
        token=self.ui.store.issue(77,"set",date=START.date().isoformat(),person=self.card["people"][0],
             expected=0,hours=1,training_id=101,group_id=17)
        self.worker.side_effect=[{"date":START.date().isoformat(),"person":self.card["people"][0],"hours":1,
                                  "reconciliation":"pending","replay":False},sportsite.AuthError("login")]
        self.ui.callback(token,None)
        texts=[c.args[1] for c in self.api.send.call_args_list]
        self.assertIn("Hours confirmed",texts[0])
        self.assertIn("Hours are saved",texts[1])

    def test_all_attendance_system_strings_have_english_translations(self):
        for name in ('attendance_bot.py','attendance_editor.py'):
            tree=ast.parse((sportsite.ROOT/name).read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Constant) and isinstance(node.value,str) and any('А'<=c<='я' or c in 'ёЁ' for c in node.value):
                    self.assertIn(node.value,i18n.EN,name)

    def test_worker_timeout_terminates_gracefully_for_vpn_restoration(self):
        process=mock.Mock()
        process.communicate.side_effect=[subprocess.TimeoutExpired('worker',150),('', '')]
        process.poll.return_value=None
        with mock.patch('attendance_bot.subprocess.Popen',return_value=process), self.assertRaisesRegex(sportsite.SportError,'слишком много'):
            run_worker(self.config,{'op':'roster','date':START.date().isoformat()})
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertEqual(process.communicate.call_args.kwargs,{'timeout':55})

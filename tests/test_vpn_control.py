import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import vpn_control

# Fictional identifiers: tests never use a developer's real VPN profiles.
SOTA = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeNetwork:
    def __init__(self, active=True):
        self.states = {SOTA: "Connected" if active else "Disconnected", OTHER: "Disconnected"}
        self.calls = []
        self.fail = set()
        self.elapsed = 0
        self.on_switch = None

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == vpn_control.SCUTIL and args[2] == "list":
            body = '\n'.join('* (%s) %s VPN (test.provider) "%s" [VPN:test.provider]'
                             % (state, service_id, "Sota Connect" if service_id == SOTA else "Other VPN")
                             for service_id, state in self.states.items())
            return SimpleNamespace(returncode=0, stdout=body)
        if args[0] == vpn_control.SCUTIL and args[2] == "status":
            return SimpleNamespace(returncode=0, stdout=self.states[args[3]] + "\n<dictionary> {}")
        if args[0] == vpn_control.SHORTCUTS:
            action = "stop" if args[-1] == "Sota off" else "start"
            service_id = SOTA
        else:
            action, service_id = args[2:4]
        if (action, service_id) in self.fail:
            return SimpleNamespace(returncode=1, stdout="private output not to be logged")
        if self.on_switch:
            self.on_switch(action, service_id)
        self.states[service_id] = "Connected" if action == "start" else "Disconnected"
        return SimpleNamespace(returncode=0, stdout="")

    def sleep(self, seconds):
        self.elapsed += seconds

    def mutations(self):
        return [args for args in self.calls if args[0] == vpn_control.SHORTCUTS
                or (len(args) > 2 and args[2] in {"start", "stop"})]


class VPNTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = FakeNetwork()
        self.settings = {"enabled": True}
        patch = mock.patch.object(vpn_control.sys, "platform", "darwin")
        patch.start()
        self.addCleanup(patch.stop)

    def controller(self):
        return vpn_control.VPNController(self.root, self.settings, runner=self.network.run,
                                          clock=lambda: self.network.elapsed, sleeper=self.network.sleep)

    def test_active_vpn_is_paused_and_only_original_connection_is_restored(self):
        controller = self.controller()
        with controller.suspended():
            self.assertEqual(self.network.states[SOTA], "Disconnected")
            self.assertTrue(controller.journal.exists())
        self.assertEqual(self.network.states[SOTA], "Connected")
        self.assertEqual(self.network.states[OTHER], "Disconnected")
        self.assertFalse(controller.journal.exists())
        self.assertEqual([call[2] for call in self.network.mutations()], ["stop", "start"])

    def test_disabled_feature_does_not_run_any_commands(self):
        self.settings["enabled"] = False
        with self.controller().suspended():
            pass
        self.assertEqual(self.network.calls, [])

    def test_initially_disconnected_vpn_is_never_connected(self):
        self.network.states[SOTA] = "Disconnected"
        with self.controller().suspended():
            pass
        self.assertEqual(self.network.mutations(), [])

    def test_data_request_failure_still_restores_the_vpn(self):
        with self.assertRaisesRegex(RuntimeError, "fictional site failure"):
            with self.controller().suspended():
                raise RuntimeError("fictional site failure")
        self.assertEqual(self.network.states[SOTA], "Connected")

    def test_selected_service_uses_shortcuts_without_toggling_other_profiles(self):
        self.settings.update(shortcut_service_id=SOTA, disconnect_shortcut="Sota off", connect_shortcut="Sota on")
        with self.controller().suspended():
            self.assertEqual(self.network.states[SOTA], "Disconnected")
        self.assertEqual(self.network.mutations(), [
            [vpn_control.SHORTCUTS, "run", "Sota off"],
            [vpn_control.SHORTCUTS, "run", "Sota on"],
        ])

    def test_stop_failure_does_not_start_the_data_request(self):
        self.network.fail.add(("stop", SOTA))
        with self.assertRaises(vpn_control.VPNError):
            with self.controller().suspended():
                self.fail("Request must not run if VPN could not be stopped")
        self.assertEqual(self.network.states[SOTA], "Connected")
        self.assertFalse(self.controller().journal.exists())

    def test_restore_failure_keeps_journal_and_recovery_reconnects_later(self):
        self.network.fail.add(("start", SOTA))
        controller = self.controller()
        with self.assertLogs("sportsite", "ERROR"):
            with controller.suspended():
                pass
        self.assertEqual(self.network.states[SOTA], "Disconnected")
        self.assertTrue(controller.journal.exists())
        self.network.fail.clear()
        self.controller().recover()
        self.assertEqual(self.network.states[SOTA], "Connected")
        self.assertFalse(controller.journal.exists())

    def test_recovery_after_crash_does_not_depend_on_current_connected_list(self):
        controller = self.controller()
        controller.write_journal([{"id": SOTA, "name": "Sota Connect"}])
        self.network.states[SOTA] = "Disconnected"
        self.controller().recover()
        self.assertEqual(self.network.states[SOTA], "Connected")
        self.assertFalse(controller.journal.exists())

    def test_journal_is_written_before_disconnect(self):
        controller = self.controller()
        def verify(action, service_id):
            if action == "stop":
                saved = json.loads(controller.journal.read_text())
                self.assertEqual(saved["services"][0]["id"], SOTA)
                self.assertEqual(controller.journal.stat().st_mode & 0o777, 0o600)
        self.network.on_switch = verify
        with controller.suspended():
            pass

    def test_two_active_vpns_are_both_restored(self):
        self.network.states[OTHER] = "Connected"
        with self.controller().suspended():
            self.assertTrue(all(value == "Disconnected" for value in self.network.states.values()))
        self.assertTrue(all(value == "Connected" for value in self.network.states.values()))

    def test_concurrent_recovery_does_not_interrupt_an_active_export(self):
        controller = self.controller()
        with controller.suspended():
            self.controller().recover()
            self.assertEqual(self.network.states[SOTA], "Disconnected")
        self.assertEqual(self.network.states[SOTA], "Connected")

    def test_corrupt_journal_does_not_switch_networks(self):
        self.controller().journal.write_text('{"services":[{"id":"invalid"}]}')
        with self.assertRaises(vpn_control.VPNError):
            self.controller().recover()
        self.assertEqual(self.network.mutations(), [])

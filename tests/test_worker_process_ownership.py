import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module


class WorkerProcessOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name) / "Pecados VIP"
        self.other = Path(temporary.name) / "Las Fiesteras"
        self.data = self.base / "data"
        self.data.mkdir(parents=True)
        self.other.mkdir()
        self.pid = 900001
        self.read_snapshot = app_module._worker_process_snapshot
        for name, value in (("BASE_DIR", self.base), ("DATA_DIR", self.data)):
            self.enterContext(patch.object(app_module, name, value))
        self.snapshot = self.enterContext(patch.object(app_module, "_worker_process_snapshot", return_value=None))
        self.kill = self.enterContext(patch.object(app_module.os, "kill"))
        # Exercise the Linux force-kill branch on Windows without real signals.
        self.enterContext(patch.object(app_module.signal, "SIGKILL", 9, create=True))
        self.enterContext(patch.object(app_module.time, "sleep"))

    def owner(self, script="wa_bot.mjs", *, base=None, started="1234", relative=False):
        base = base or self.base
        interpreter = "node" if script.endswith(".mjs") else "python3.12"
        return [interpreter, script if relative else str(base / script)], str(base), started

    def pid_file(self, name="wa_bot.pid"):
        target = self.data / name
        target.write_text(str(self.pid), encoding="ascii")
        return target

    def destructive_signals(self):
        return [call.args[1] for call in self.kill.call_args_list if call.args[1] != 0]

    def test_exact_worker_path_and_entrypoint_relative_path_belong_to_this_instance(self):
        for script in ("wa_bot.mjs", "bot.py", "botfather_bot.py"):
            for relative in (False, True):
                with self.subTest(script=script, relative=relative):
                    arguments, cwd, _ = self.owner(script, relative=relative)
                    self.assertTrue(app_module._worker_arguments_match_instance(arguments, cwd, script))

    def test_other_checkout_and_unverified_relative_script_are_rejected(self):
        for script in ("wa_bot.mjs", "bot.py", "botfather_bot.py"):
            arguments, cwd, _ = self.owner(script, base=self.other)
            with self.subTest(script=script):
                self.assertFalse(app_module._worker_arguments_match_instance(arguments, cwd, script))
                self.assertFalse(app_module._worker_arguments_match_instance([arguments[0], script], None, script))
                self.assertFalse(app_module._worker_arguments_match_instance(
                    [arguments[0], str(self.base / script)], cwd, script,
                ))

    def test_windows_absolute_script_is_unambiguous_when_cwd_is_unavailable(self):
        arguments, _, _ = self.owner()
        self.assertTrue(app_module._worker_arguments_match_instance(arguments, None, "wa_bot.mjs"))
        self.assertFalse(app_module._worker_arguments_match_instance(
            ["node", str(self.other / "wa_bot.mjs")], None, "wa_bot.mjs",
        ))

    def test_homonyms_wrappers_and_script_name_inside_an_argument_are_rejected(self):
        cases = (
            (["node", str(self.base / "wa_bot.mjs.backup")], "wa_bot.mjs"),
            (["node", "--title=wa_bot.mjs"], "wa_bot.mjs"),
            (["node", "-e", str(self.base / "wa_bot.mjs")], "wa_bot.mjs"),
            (["echo", str(self.base / "wa_bot.mjs")], "wa_bot.mjs"),
            (["python", str(self.base / "restart_bot.py")], "bot.py"),
            (["python", str(self.base / "botfather_bot.py")], "bot.py"),
            (["python", "-c", str(self.base / "bot.py")], "bot.py"),
            (["python", str(self.base / "other.py"), str(self.base / "bot.py")], "bot.py"),
        )
        for arguments, script in cases:
            with self.subTest(arguments=arguments):
                self.assertFalse(app_module._worker_arguments_match_instance(arguments, str(self.base), script))

    def test_linux_snapshot_preserves_nul_argument_boundaries_and_reads_creation_identity(self):
        stat = f"{self.pid} (node worker) S " + "0 " * 18 + "1234 0 0"
        arguments = ["node", str(self.base / "wa_bot.mjs")]
        raw = b"\0".join(os.fsencode(value) for value in arguments) + b"\0"
        with (
            patch.object(app_module.sys, "platform", "linux"),
            patch.object(Path, "read_text", return_value=stat),
            patch.object(Path, "read_bytes", return_value=raw),
            patch.object(app_module.os, "readlink", return_value=str(self.base)),
        ):
            self.assertEqual((arguments, str(self.base), "1234"), self.read_snapshot(self.pid))

    def test_linux_snapshot_rejects_pid_reused_while_inspecting(self):
        stat = f"{self.pid} (node) S " + "0 " * 18
        with (
            patch.object(app_module.sys, "platform", "linux"),
            patch.object(Path, "read_text", side_effect=[stat + "1234", stat + "5678"]),
            patch.object(Path, "read_bytes", return_value=b"node\0wa_bot.mjs\0"),
            patch.object(app_module.os, "readlink", return_value=str(self.base)),
        ):
            self.assertIsNone(self.read_snapshot(self.pid))

    def test_unavailable_proc_does_not_authorize_a_worker_from_ps_display_text(self):
        with (
            patch.object(app_module.sys, "platform", "darwin"),
            patch.object(Path, "read_text", side_effect=OSError("not available")),
            patch.object(app_module.subprocess, "run") as run,
        ):
            self.assertIsNone(self.read_snapshot(self.pid))
        run.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows native quoting parser")
    def test_windows_native_parser_preserves_spaces_and_quoted_arguments(self):
        arguments = [r"C:\Program Files\nodejs\node.exe", str(self.base / "wa_bot.mjs")]
        self.assertEqual(arguments, app_module._windows_command_line_arguments(subprocess.list2cmdline(arguments)))
        self.assertEqual([], app_module._windows_command_line_arguments(""))

    def test_windows_snapshot_requires_creation_identity(self):
        with (
            patch.object(app_module.sys, "platform", "win32"),
            patch.object(app_module, "_windows_process_information", return_value={"command_line": "node wa_bot.mjs"}),
            patch.object(app_module, "_windows_command_line_arguments") as parse,
        ):
            self.assertIsNone(self.read_snapshot(self.pid))
        parse.assert_not_called()

    def test_whatsapp_stop_never_signals_a_worker_from_the_other_checkout(self):
        pid_file = self.pid_file()
        self.snapshot.return_value = self.owner(base=self.other)
        with self.assertRaises(app_module.WorkerOwnershipError):
            app_module._stop_wa_process(pid_file)
        self.assertEqual([], self.destructive_signals())
        self.assertTrue(pid_file.exists())

    def test_whatsapp_stop_revalidates_same_process_before_force_kill(self):
        pid_file = self.pid_file()
        self.snapshot.return_value = self.owner()
        alive = True

        def terminate(pid, sig):
            nonlocal alive
            if sig == signal.SIGKILL:
                alive = False
            elif sig == 0 and not alive:
                raise ProcessLookupError()

        self.kill.side_effect = terminate
        with patch.object(app_module.sys, "platform", "darwin"):
            app_module._stop_wa_process(pid_file)
        self.assertEqual([signal.SIGTERM, signal.SIGKILL], self.destructive_signals())
        self.assertFalse(pid_file.exists())

    def test_whatsapp_force_kill_rejects_reused_pid_even_for_same_project(self):
        pid_file = self.pid_file()
        self.snapshot.side_effect = [self.owner(started="1234"), self.owner(started="5678")]
        with patch.object(app_module.sys, "platform", "darwin"):
            with self.assertRaises(app_module.WorkerOwnershipError):
                app_module._stop_wa_process(pid_file)
        self.assertEqual([signal.SIGTERM], self.destructive_signals())
        self.assertTrue(pid_file.exists())

    def test_stop_preserves_a_pid_file_replaced_during_shutdown(self):
        pid_file = self.pid_file()
        self.snapshot.return_value = self.owner()

        def replace_pid(pid, sig):
            if sig == signal.SIGTERM:
                pid_file.write_text("900002", encoding="ascii")
            elif sig == 0 and pid_file.read_text(encoding="ascii") == "900002":
                raise ProcessLookupError()

        self.kill.side_effect = replace_pid
        with patch.object(app_module.sys, "platform", "win32"):
            app_module._stop_wa_process(pid_file)
        self.assertEqual("900002", pid_file.read_text(encoding="ascii"))

    def test_telegram_stop_rejects_botfather_and_other_project(self):
        for owner in (self.owner("bot.py", base=self.other), self.owner("botfather_bot.py")):
            with self.subTest(owner=owner):
                self.pid_file("tg_userbot.pid")
                self.snapshot.return_value = owner
                with self.assertRaises(app_module.WorkerOwnershipError):
                    app_module._stop_telegram_worker()
        self.assertEqual([], self.destructive_signals())

    def test_telegram_force_kill_revalidates_instance_and_creation_identity(self):
        pid_file = self.pid_file("tg_userbot.pid")
        health_file = self.data / "tg_userbot_health.json"
        health_file.write_text("{}", encoding="ascii")
        self.snapshot.side_effect = [self.owner("bot.py"), self.owner("bot.py", base=self.other)]
        with patch.object(app_module.sys, "platform", "darwin"):
            with self.assertRaises(app_module.WorkerOwnershipError):
                app_module._stop_telegram_worker()
        self.assertEqual([signal.SIGTERM], self.destructive_signals())
        self.assertTrue(pid_file.exists())
        self.assertTrue(health_file.exists())

    def test_signal_denied_preserves_pid_and_blocks_whatsapp_restart(self):
        pid_file = self.pid_file()
        self.snapshot.return_value = self.owner()

        def deny_stop(pid, sig):
            if sig != 0:
                raise PermissionError()

        self.kill.side_effect = deny_stop
        with patch.object(app_module, "_start_wa_process") as start:
            self.assertIsNone(app_module.restart_wa_bot())
        start.assert_not_called()
        self.assertTrue(pid_file.exists())
        self.assertEqual([signal.SIGTERM], self.destructive_signals())

    def test_permission_denied_on_liveness_blocks_all_worker_restarts(self):
        files = [self.pid_file(name) for name in ("wa_bot.pid", "tg_userbot.pid", "botfather.pid")]
        self.kill.side_effect = PermissionError()
        with (
            patch.dict(app_module.os.environ, {"TG_API_ID": "1234", "TG_API_HASH": "offline", "TG_PHONE": "+1234"}),
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "_start_wa_process") as start_wa,
            patch.object(app_module.subprocess, "Popen") as start,
            app_module.app.test_request_context("/api/start_botfather", method="POST"),
        ):
            self.assertIsNone(app_module.restart_wa_bot())
            self.assertFalse(app_module.restart_telegram_worker()[0])
            response, status = app_module.api_start_botfather()
        self.assertEqual(409, status)
        self.assertFalse(response.get_json()["ok"])
        start_wa.assert_not_called()
        start.assert_not_called()
        self.assertTrue(all(path.exists() for path in files))
        self.assertEqual([], self.destructive_signals())

    def test_unconfirmed_force_kill_preserves_pid(self):
        pid_file = self.pid_file()
        self.snapshot.return_value = self.owner()
        with patch.object(app_module.sys, "platform", "darwin"):
            with self.assertRaises(app_module.WorkerOwnershipError):
                app_module._stop_wa_process(pid_file)
        self.assertTrue(pid_file.exists())
        self.assertEqual([signal.SIGTERM, signal.SIGKILL], self.destructive_signals())

    def test_linux_zombie_is_confirmed_exited(self):
        with (
            patch.object(app_module.sys, "platform", "linux"),
            patch.object(Path, "read_text", return_value=f"{self.pid} (node worker) Z 1 2 3"),
        ):
            self.assertFalse(app_module._process_may_be_alive(self.pid))

    def test_botfather_running_requires_its_own_script_in_this_checkout(self):
        self.pid_file("botfather.pid")
        for owner, expected in (
            (self.owner("botfather_bot.py"), True),
            (self.owner("botfather_bot.py", base=self.other), False),
            (self.owner("bot.py"), False),
            (None, False),
        ):
            with self.subTest(owner=owner):
                self.snapshot.return_value = owner
                self.assertEqual(expected, app_module.bf_is_running())

    def test_botfather_launch_pins_working_directory_to_the_instance(self):
        process = Mock(pid=self.pid)
        with (
            patch.object(app_module, "bf_is_running", return_value=False),
            patch.dict(app_module.os.environ, {"AUTOREPLY_BOT_TOKEN": "offline:fixture"}),
            patch.object(app_module.subprocess, "Popen", return_value=process) as popen,
            app_module.app.test_request_context("/api/start_botfather", method="POST"),
        ):
            response = app_module.api_start_botfather()
        self.assertTrue(response.get_json()["ok"])
        self.assertEqual(str(self.base), popen.call_args.kwargs["cwd"])
        self.assertEqual([sys.executable, str(self.base / "botfather_bot.py")], popen.call_args.args[0])

    def test_unverifiable_live_whatsapp_pid_blocks_restart_instead_of_duplicating_worker(self):
        self.pid_file()
        with patch.object(app_module, "_start_wa_process") as start:
            self.assertIsNone(app_module.restart_wa_bot())
        start.assert_not_called()
        self.assertEqual([], self.destructive_signals())

    def test_unverifiable_live_telegram_pid_blocks_restart_instead_of_duplicating_worker(self):
        self.pid_file("tg_userbot.pid")
        with (
            patch.dict(app_module.os.environ, {"TG_API_ID": "1234", "TG_API_HASH": "offline", "TG_PHONE": "+1234"}),
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module.subprocess, "Popen") as start,
        ):
            ready, message = app_module.restart_telegram_worker()
        self.assertFalse(ready)
        self.assertIn("pertenencia", message)
        start.assert_not_called()
        self.assertEqual([], self.destructive_signals())

    def test_unverifiable_live_botfather_pid_blocks_duplicate_start(self):
        self.pid_file("botfather.pid")
        with (
            patch.object(app_module.subprocess, "Popen") as start,
            app_module.app.test_request_context("/api/start_botfather", method="POST"),
        ):
            response, status = app_module.api_start_botfather()
        self.assertEqual(409, status)
        self.assertFalse(response.get_json()["ok"])
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

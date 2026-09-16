"""Focused tests for independent Cut Over logging actions."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cutover_runner import CutoverEngine, run_cutover_trfs, load_cutover_config
from cutover_model import NodeSession, RunPhase


class AuxiliaryActionTests(unittest.TestCase):
    def test_execution_logs_stay_out_of_post_and_cutover_results(self):
        import tempfile
        from pathlib import Path
        cfg = load_cutover_config()
        cfg["persistence"]["enabled"] = False
        with tempfile.TemporaryDirectory() as folder:
            engine = CutoverEngine({"shortcode": "TEST"}, cfg=cfg, log_dir=folder)
            engine._hc_mode = "post"
            capture = Path(engine._save_preparation_log("BB1", "POSTHC", "execute"))
            self.assertTrue(capture.name.startswith("CUTOVER_"))
            self.assertEqual(capture.parent, Path(folder) / "MOSHELL")
            self.assertEqual(capture.read_text().strip(), "execute")
            ssh = Mock()
            engine.run.sessions["BB1"] = NodeSession(node_name="BB1", ssh=ssh)
            engine._start_group_logs("LB", ["BB1"])
            transcript = Path(ssh.start_step_log.call_args.args[0])
            self.assertTrue(transcript.name.startswith("CUTOVER_"))
            self.assertEqual(transcript.parent, Path(folder) / "SESSION")
            self.assertFalse((Path(folder) / "POST").exists())
            self.assertFalse((Path(folder) / "CUTOVER").exists())

    def test_downloaded_execution_logs_have_workflow_prefix(self):
        import tempfile
        from pathlib import Path
        from integration_runner import IntegrationSSH, download_remote_dir
        ssh = IntegrationSSH.__new__(IntegrationSSH)
        ssh._remote_logs = [("/remote/RELATION_BB1.log", None)]
        ssh.sftp_download = Mock()
        ssh.sftp_remove = Mock()
        with tempfile.TemporaryDirectory() as folder:
            paths = ssh.drain_remote_logs(folder, filename_prefix="INTEGRATION")
            self.assertTrue(Path(paths[0]).name.startswith("INTEGRATION_"))
            self.assertTrue(Path(paths[0]).name.endswith("RELATION_BB1.log"))
            ssh.sftp_remove.assert_called_once_with("/remote/RELATION_BB1.log")
            sftp = Mock()
            sftp.listdir_attr.return_value = [
                SimpleNamespace(filename="BB1.log", st_mode=0o100644)]
            ssh.client = Mock()
            ssh.client.open_sftp.return_value = sftp
            paths = download_remote_dir(
                ssh, "/remote", folder, Mock(), filename_prefix="AUDIT")
            self.assertTrue(Path(paths[0]).name.startswith("AUDIT_"))
            self.assertTrue(Path(paths[0]).name.endswith("BB1.log"))
            sftp.close.assert_called_once()

    def test_sector_label_requires_confirmed_unlocked_states(self):
        from gui.cutover_page import _sector_all_unlocked
        lte = SimpleNamespace(rat="LTE", admin_state="UNLOCKED")
        gsm = SimpleNamespace(rat="GSM", geran_state="ACTIVE")
        self.assertTrue(_sector_all_unlocked([lte, gsm]))
        self.assertFalse(_sector_all_unlocked([]))
        self.assertFalse(_sector_all_unlocked(
            [lte, SimpleNamespace(rat="NR", admin_state="LOCKED")]))
        self.assertFalse(_sector_all_unlocked(
            [SimpleNamespace(rat="LTE", admin_state="")]))
        gsm.geran_state = "HALTED"
        self.assertFalse(_sector_all_unlocked([lte, gsm]))

    def test_back_does_not_navigate_or_shutdown_before_cancel(self):
        from gui.cutover_page import CutOverPage
        controller = CutOverPage.__new__(CutOverPage)
        controller.engine = Mock()
        controller.engine.can_leave.return_value = False
        controller.page = Mock()
        controller._alert = Mock()
        controller._finished = False
        controller._on_back(None)
        controller._alert.assert_called_once()
        controller.page.go.assert_not_called()
        controller.engine.shutdown.assert_not_called()
        self.assertFalse(controller._finished)

    def test_cancel_monitoring_blocks_back_until_worker_exits(self):
        import tempfile
        cfg = load_cutover_config()
        cfg["persistence"]["enabled"] = False
        with tempfile.TemporaryDirectory() as folder:
            engine = CutoverEngine({"shortcode": "TEST"}, cfg=cfg, log_dir=folder)
            engine.run.sessions["BB1"] = NodeSession(node_name="BB1", ssh=Mock())
            engine.run.set_phase(RunPhase.READY)
            release = threading.Event()
            worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
            engine._monitor_thread = worker
            worker.start()
            self.assertFalse(engine.is_busy())
            self.assertTrue(engine.can_cancel())
            self.assertFalse(engine.can_leave())
            engine.cancel()
            self.assertTrue(engine.is_stopping())
            self.assertFalse(engine.can_leave())
            self.assertFalse(engine._spawn(lambda: None, "must-not-start"))
            release.set()
            engine._cancel_thread.join(timeout=5)
            self.assertFalse(engine._cancel_thread.is_alive())
            self.assertTrue(engine.can_leave())
            self.assertEqual(engine.run.phase, RunPhase.CANCELLED)
            self.assertEqual(engine.run.sessions, {})
            self.assertTrue(engine._cancel_complete.is_set())

    def test_cancel_interrupts_trfs_flush_delay(self):
        event = threading.Event()
        event.set()
        ssh = Mock()
        ok, _path = run_cutover_trfs(
            ssh, "NODE", "test.mos", "local", Mock(),
            {"trfs": {"idle_wait_s": 20}}, cancel_event=event)
        self.assertFalse(ok)
        ssh.download_newest_dir.assert_not_called()

    def snapshot_engine(self):
        engine = CutoverEngine.__new__(CutoverEngine)
        engine.run = SimpleNamespace(
            cells=[SimpleNamespace(node_name="BB1"), SimpleNamespace(node_name="BB2")],
            is_cancelled=lambda: False)
        engine.form = {}
        engine.cfg = {"discovery": {"amos_timeout_s": 90}, "vswr": {"enabled": True}}
        engine._bg_lock = threading.Lock()
        engine._bg_stop = threading.Event()
        engine._bg_thread = None
        engine._snapshot_initial_started = False
        engine._snapshot_sessions = {}
        engine.log = Mock()
        engine.emit = Mock()
        engine._apply_traffic_snapshot = Mock()
        engine._apply_vswr = Mock()
        return engine

    def test_snapshot_parallel_order_and_disconnect(self):
        engine = self.snapshot_engine()
        calls = {"BB1": [], "BB2": []}
        barrier = threading.Barrier(2)
        sessions = []
        def factory(**kwargs):
            ssh = Mock()
            sessions.append(ssh)
            return ssh
        def traffic(ssh, node, *args):
            calls[node].append("traffic")
            barrier.wait(timeout=5)
            return True, "", SimpleNamespace(ok=True)
        def vswr(ssh, node, *args):
            calls[node].append("vswr")
            return True, "", object()
        with patch("integration_runner.IntegrationSSH", side_effect=factory), \
             patch("cutover_runner.run_cutover_traffic", side_effect=traffic), \
             patch("cutover_runner.run_cutover_vswr", side_effect=vswr):
            self.assertTrue(engine._start_measurement_snapshot(initial=True))
            thread = engine._bg_thread
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertFalse(engine._start_measurement_snapshot(initial=True))
        self.assertEqual(calls, {"BB1": ["traffic", "vswr"], "BB2": ["traffic", "vswr"]})
        for ssh in sessions:
            ssh.disconnect.assert_called_once()
        self.assertEqual(engine._snapshot_sessions, {})
        self.assertFalse(engine.measurements_busy())

    def test_snapshot_failure_retains_values_and_closes_sessions(self):
        engine = self.snapshot_engine()
        sessions = []
        def factory(**kwargs):
            ssh = Mock()
            sessions.append(ssh)
            return ssh
        with patch("integration_runner.IntegrationSSH", side_effect=factory), \
             patch("cutover_runner.run_cutover_traffic", side_effect=RuntimeError("read failed")), \
             patch("cutover_runner.run_cutover_vswr", side_effect=RuntimeError("read failed")):
            engine._bg_loop()
        engine._apply_traffic_snapshot.assert_not_called()
        engine._apply_vswr.assert_not_called()
        for ssh in sessions:
            ssh.disconnect.assert_called_once()
        self.assertEqual(engine._snapshot_sessions, {})

    def test_trfs_reports_download_stage_and_completion(self):
        ssh = Mock()
        def download(*args, **kwargs):
            kwargs["progress_cb"]("downloading folder NODE_test")
            return "local/NODE_test"
        ssh.download_newest_dir.side_effect = download
        logs = []
        ok, path = run_cutover_trfs(
            ssh, "NODE", "test.mos", "local", logs.append,
            {"trfs": {"idle_wait_s": 0}})
        self.assertTrue(ok)
        self.assertEqual(path, "local/NODE_test")
        self.assertTrue(any("downloading folder NODE_test" in s for s in logs))
        self.assertTrue(any("finished successfully" in s for s in logs))

    def test_posthc_does_not_rediscover_and_reports_missing_download(self):
        import tempfile
        engine = CutoverEngine.__new__(CutoverEngine)
        cells = [object()]
        engine.run = SimpleNamespace(
            cancel_event=threading.Event(), node_names=["BB1", "BB2"],
            sessions={"BB1": object(), "BB2": object()}, artifacts={},
            lock=threading.RLock(), cells=cells, is_cancelled=lambda: False)
        engine.log = Mock()
        engine.emit = Mock()
        engine._ensure_sessions = Mock(return_value=[])
        engine._discovery_worker = Mock()
        engine._run_per_node = lambda nodes, worker: [
            worker(n, s) for n, s in nodes.items()]
        def prepare(node, session):
            if node == "BB1":
                engine.run.artifacts["BB1:POSTHC_LOG"] = "BB1.log"
            return True
        engine._run_preparation_for_node = prepare
        with tempfile.TemporaryDirectory() as folder:
            engine.log_dir = folder
            engine._posthc_worker()
        engine._discovery_worker.assert_not_called()
        self.assertIs(engine.run.cells, cells)
        event = engine.emit.call_args.args[0]
        self.assertEqual(event.kind, "posthc_done")
        self.assertIn("1/2", event.message)
        self.assertIn("No downloaded Post_HC logfile: BB2", event.message)
        self.assertEqual(engine._hc_mode, "pre")


if __name__ == "__main__":
    unittest.main()

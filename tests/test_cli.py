"""CLI 端到端测试：工作目录隔离、重启继续、完整筹备闭环。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from emergency_prep.cli import main
from tests.fixtures import write_inputs


class CliEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.input_dir = write_inputs(self.tmp)
        self.work1 = self.tmp / "work-a"
        self.work2 = self.tmp / "work-b"
        self.fixtures = self.tmp / "fixtures"
        self.fixtures.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_full_lifecycle_and_restart(self):
        code, out, _ = self._run("init", "--inputs", str(self.input_dir),
                                 "--workdir", str(self.work1))
        self.assertEqual(code, 0, out)

        code, out, _ = self._run("import-evidence", "--workdir", str(self.work1))
        self.assertEqual(code, 0)
        self.assertIn("5 条", out)

        # 重复导入不产生新事实
        code, out, _ = self._run("import-evidence", "--workdir", str(self.work1))
        self.assertIn("跳过 5 条", out)

        # 情景分析 JSON 可解析
        code, out, _ = self._run("analyze", "dual", "--workdir", str(self.work1), "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["scenario"], "dual")
        self.assertTrue(report["best_plan"]["valid"])

        # 登记安排
        code, out, _ = self._run(
            "book", "dual", "--workdir", str(self.work1),
            "--title", "首轮",
            "--window-start", "2026-11-25T08:00:00Z",
            "--window-end", "2026-11-25T10:00:00Z",
        )
        self.assertEqual(code, 0)
        self.assertIn("AR1", out)

        # 同时间窗第二组演练被拒绝（退出码 2）
        code, out, err = self._run(
            "register-drill", "dual", "--workdir", str(self.work1),
            "--title", "冲突演练",
            "--window-start", "2026-11-25T09:00:00Z",
            "--window-end", "2026-11-25T09:30:00Z",
        )
        self.assertEqual(code, 2)
        self.assertIn("不可重复确认", out + err)

        # 取消后可重新登记
        code, _, _ = self._run("cancel", "AR1", "--reason", "改期",
                               "--workdir", str(self.work1))
        self.assertEqual(code, 0)
        code, out, _ = self._run(
            "register-drill", "dual", "--workdir", str(self.work1),
            "--title", "改期演练",
            "--window-start", "2026-11-25T08:00:00Z",
            "--window-end", "2026-11-25T10:00:00Z",
        )
        self.assertEqual(code, 0)
        self.assertIn("DR1", out)

        # 回填
        steps = []
        plan_businesses = report["best_plan"]["steps"]
        for step in plan_businesses:
            for source in ("machine", "observer"):
                steps.append(
                    {
                        "business": step["business"],
                        "config": next(
                            c["config"] for c in report["commitments"]
                            if c["business"] == step["business"]
                        ),
                        "resource": step["terminal"],
                        "source": source,
                        "result": "success",
                        "start": "2026-11-25T08:00:00Z",
                        "end": "2026-11-25T08:20:00Z",
                    }
                )
        steps_file = self.fixtures / "steps.json"
        steps_file.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
        code, out, _ = self._run(
            "backfill", "DR1", str(steps_file), "--workdir", str(self.work1)
        )
        self.assertEqual(code, 0)

        # 重启：新进程视角的 status 仍能看到全部状态
        code, out, _ = self._run("status", "--workdir", str(self.work1))
        self.assertEqual(code, 0)
        self.assertIn("DR1", out)

        # 追溯
        code, out, _ = self._run("trace", "call-A", "--workdir", str(self.work1), "--json")
        self.assertEqual(code, 0)
        traced = json.loads(out)
        self.assertIn("dual", traced["scenarios"])
        self.assertTrue(traced["field_results"])

    def test_separate_workdirs_do_not_pollute_each_other(self):
        for work in (self.work1, self.work2):
            code, _, _ = self._run("init", "--inputs", str(self.input_dir),
                                   "--workdir", str(work))
            self.assertEqual(code, 0)
        self._run("import-evidence", "--workdir", str(self.work1))
        code, out1, _ = self._run("status", "--workdir", str(self.work1), "--json")
        code, out2, _ = self._run("status", "--workdir", str(self.work2), "--json")
        self.assertGreater(json.loads(out1)["observations"], 0)
        self.assertEqual(json.loads(out2)["observations"], 0)

    def test_init_refuses_nonempty_workdir_without_force(self):
        self._run("init", "--inputs", str(self.input_dir), "--workdir", str(self.work1))
        code, _, err = self._run("init", "--inputs", str(self.input_dir),
                                 "--workdir", str(self.work1))
        self.assertEqual(code, 2)
        self.assertIn("非空", err)

    def test_untrusted_import_rejects_bad_rows(self):
        bad_file = self.fixtures / "bad.json"
        bad_file.write_text(
            json.dumps([
                {
                    "observation": "BAD1", "drill": "DX", "business": "call-A",
                    "config": "v2", "resource": "T1", "source": "machine",
                    "result": "success",
                    "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T07:00:00Z",
                }
            ]),
            encoding="utf-8",
        )
        self._run("init", "--inputs", str(self.input_dir), "--workdir", str(self.work1))
        code, _, err = self._run("import-evidence", "--file", str(bad_file),
                                 "--workdir", str(self.work1))
        self.assertEqual(code, 2)
        self.assertIn("结束必须晚于开始", err)

    def test_action_item_lifecycle(self):
        self._run("init", "--inputs", str(self.input_dir), "--workdir", str(self.work1))
        code, out, _ = self._run(
            "action-add", "--workdir", str(self.work1),
            "--title", "补演 call-B", "--businesses", "call-B",
            "--reason", "证据不足",
        )
        self.assertEqual(code, 0)
        code, out, _ = self._run("action-close", "AC1", "--workdir", str(self.work1))
        self.assertEqual(code, 0)
        code, out, _ = self._run("status", "--workdir", str(self.work1), "--json")
        self.assertEqual(json.loads(out)["open_actions"], [])

    def test_supplement_lists_unconfirmed_commitments(self):
        self._run("init", "--inputs", str(self.input_dir), "--workdir", str(self.work1))
        self._run("import-evidence", "--workdir", str(self.work1))
        code, out, _ = self._run("supplement", "dual", "--workdir", str(self.work1), "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        target_names = {t["business"] for t in result["supplement_targets"]}
        # call-A 已确认，不应出现在补演目标；dispatch-A 争议、call-B 无 v2 证据
        self.assertNotIn("call-A", target_names)
        self.assertIn("dispatch-A", target_names)
        self.assertIn("call-B", target_names)


if __name__ == "__main__":
    unittest.main()

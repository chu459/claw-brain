import unittest
from pathlib import Path

from action_router import classify_action, execute_routed_action


class ActionRouterTests(unittest.TestCase):
    def test_explicit_codex_prefix(self):
        route = classify_action("[CODEX] 修复 core.py 的测试")
        self.assertEqual(route.route, "codex")
        self.assertIn("core.py", route.payload)

    def test_explicit_local_command_prefix(self):
        route = classify_action("[LOCAL_CMD] python -m py_compile core.py")
        self.assertEqual(route.route, "local_cmd")
        self.assertTrue(route.payload.startswith("python"))

    def test_command_like_text(self):
        route = classify_action("运行命令：python --version")
        self.assertEqual(route.route, "local_cmd")
        self.assertEqual(route.payload, "python --version")

    def test_browser_action_stays_openclaw(self):
        route = classify_action("打开网页并截图")
        self.assertEqual(route.route, "openclaw")

    def test_engineering_task_goes_codex(self):
        route = classify_action("修复代码里的测试失败")
        self.assertEqual(route.route, "codex")

    def test_dangerous_local_command_blocked(self):
        result = execute_routed_action(
            "[LOCAL_CMD] git reset --hard",
            project_root=Path(__file__).resolve().parents[1],
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["route"], "local_cmd")
        self.assertIn("拦截", result["content"])


if __name__ == "__main__":
    unittest.main()

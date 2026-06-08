import unittest

from codex_adapter import build_codex_prompt


class CodexAdapterTests(unittest.TestCase):
    def test_prompt_contains_task_and_execution_guard(self):
        prompt = build_codex_prompt("Run python --version.")
        self.assertIn("Run python --version.", prompt)
        self.assertIn("不要只回复", prompt)
        self.assertIn("当前必须完成的任务", prompt)


if __name__ == "__main__":
    unittest.main()

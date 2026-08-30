import unittest

from orchestra.runtime import RuntimeError, launch_plan


class RuntimeTests(unittest.TestCase):
    def test_custom_runtime_is_argv_not_shell(self):
        plan = launch_plan(
            {"adapter": "exec", "command": ["agent", "--dir", "{workdir}"],
             "config": {"prompt": "stdin"}},
            {"name": "custom", "config": {"env": {"LANE": "fast"}}},
            workdir="/tmp/a b", title="Research", prompt="hello; rm -rf nope",
            run_id=7, inherited_env={"PATH": "/bin"},
        )
        self.assertEqual(plan.argv, ("agent", "--dir", "/tmp/a b"))
        self.assertEqual(plan.stdin, "hello; rm -rf nope")
        self.assertEqual(plan.env["LANE"], "fast")

    def test_unknown_placeholder_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "placeholder"):
            launch_plan(
                {"adapter": "exec", "command": ["agent", "{token}"],
                 "config": {}}, {"name": "x"}, workdir="/tmp/w", title="T",
                prompt="M", run_id=1, inherited_env={},
            )

    def test_ordinary_braces_are_not_placeholders(self):
        plan = launch_plan(
            {"adapter": "exec",
             "command": ["python3", "-c", "print({'result': 'ok'})"],
             "config": {}},
            {"name": "x"}, workdir="/tmp/w", title="T", prompt="M",
            run_id=1, inherited_env={},
        )
        self.assertEqual(plan.argv[-1], "print({'result': 'ok'})")


if __name__ == "__main__":
    unittest.main()

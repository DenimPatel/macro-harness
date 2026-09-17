"""Containment and the built-in tools."""

import os
import sys
import tempfile
import unittest

from macroharness import tools


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = os.path.realpath(self.directory.name)
        self.containment = tools.Containment(self.root, timeout=10)
        self.registry = tools.default_registry(self.containment)

    def call(self, name, **kwargs):
        return self.registry.get(name).func(**kwargs)

    def test_write_then_read_roundtrip(self):
        self.assertEqual(self.call("write_file", path="a.txt", content="hello"),
                         "wrote 5 bytes to a.txt")
        self.assertEqual(self.call("read_file", path="a.txt"), "hello")

    def test_relative_escape_is_refused(self):
        self.assertIn("escapes the workspace",
                      self.call("read_file", path="../secret.txt"))

    def test_absolute_escape_is_refused(self):
        self.assertIn("escapes the workspace",
                      self.call("write_file", path="/tmp/macro-escaped.txt", content="x"))

    def test_symlink_escape_is_refused(self):
        handle, outside = tempfile.mkstemp()
        os.close(handle)
        self.addCleanup(os.unlink, outside)
        os.symlink(outside, os.path.join(self.root, "link.txt"))
        self.assertIn("escapes the workspace", self.call("read_file", path="link.txt"))

    def test_write_creates_parent_directories(self):
        self.call("write_file", path="deep/nested/a.txt", content="x")
        self.assertTrue(os.path.exists(os.path.join(self.root, "deep/nested/a.txt")))

    def test_read_offset_and_limit(self):
        self.call("write_file", path="lines.txt", content="one\ntwo\nthree\n")
        self.assertEqual(self.call("read_file", path="lines.txt", offset=1, limit=1), "two\n")

    def test_read_past_the_end_is_an_error(self):
        self.call("write_file", path="lines.txt", content="one\n")
        self.assertIn("past the end", self.call("read_file", path="lines.txt", offset=9))

    def test_edit_requires_a_unique_match(self):
        self.call("write_file", path="a.txt", content="x\nx\n")
        self.assertIn("appears 2 times",
                      self.call("edit_file", path="a.txt", old_string="x", new_string="y"))
        self.assertEqual(
            self.call("edit_file", path="a.txt", old_string="x", new_string="y", replace_all=True),
            "replaced 2 occurrence(s) in a.txt")

    def test_edit_reports_a_missing_match(self):
        self.call("write_file", path="a.txt", content="hello")
        self.assertIn("not found",
                      self.call("edit_file", path="a.txt", old_string="nope", new_string="y"))

    def test_edit_refuses_an_empty_old_string(self):
        self.call("write_file", path="a.txt", content="hello")
        self.assertIn("must not be empty",
                      self.call("edit_file", path="a.txt", old_string="", new_string="y"))

    def test_bash_runs_in_the_workspace(self):
        self.assertIn(self.root, self.call("run_bash", command="pwd"))

    def test_bash_env_is_scrubbed(self):
        os.environ["MACRO_TEST_SECRET"] = "leaked"
        self.addCleanup(os.environ.pop, "MACRO_TEST_SECRET", None)
        self.assertNotIn("leaked", self.call("run_bash", command="echo $MACRO_TEST_SECRET"))

    def test_bash_timeout_kills_the_process(self):
        containment = tools.Containment(self.root, timeout=1)
        registry = tools.default_registry(containment)
        result = registry.get("run_bash").func(
            command='%s -c "import time; time.sleep(10)"' % sys.executable)
        self.assertIn("timed out", result)


class DecodeTests(unittest.TestCase):
    def test_valid_object(self):
        self.assertEqual(tools.decode_args('{"a": 1}'), ({"a": 1}, None))

    def test_empty_string_is_an_empty_object(self):
        self.assertEqual(tools.decode_args(""), ({}, None))

    def test_invalid_json(self):
        self.assertIsNotNone(tools.decode_args("{")[1])

    def test_non_object(self):
        self.assertIsNotNone(tools.decode_args("[1, 2]")[1])


class TruncateTests(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(tools.truncate("abc", 10), "abc")

    def test_long_text_keeps_head_and_tail(self):
        result = tools.truncate("a" * 100 + "b" * 100, 40)
        self.assertIn("characters truncated", result)
        self.assertTrue(result.startswith("a" * 20))
        self.assertTrue(result.endswith("b" * 20))


class SchemaTests(unittest.TestCase):
    def test_every_tool_exposes_an_openai_schema(self):
        registry = tools.default_registry(tools.Containment("/tmp"))
        names = registry.names()
        self.assertEqual(names, ["read_file", "write_file", "edit_file", "run_bash"])
        for schema in registry.schemas():
            self.assertEqual(schema["type"], "function")
            self.assertIn("name", schema["function"])
            self.assertEqual(schema["function"]["parameters"]["type"], "object")

    def test_read_only_tools_are_flagged(self):
        registry = tools.default_registry(tools.Containment("/tmp"))
        self.assertTrue(registry.get("read_file").read_only)
        self.assertFalse(registry.get("run_bash").read_only)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for the ``octl`` CLI test suites (standard library only).

This module is imported by ``cli_offline_test.py`` and ``live_cli_test.py``. It
provides:

* :class:`Reporter` -- one ``PASS``/``FAIL``/``SKIP`` line per scenario plus a
  summary line and the process exit code.
* :func:`opencode_on_path` -- whether the ``opencode`` binary is available.

No host, credential or absolute path is hard-coded here; see ``tests/README.md``.
"""

import shutil


def opencode_on_path():
    """True when the ``opencode`` binary is on ``PATH``."""
    return shutil.which("opencode") is not None


class Reporter:
    """Prints one line per scenario and a final summary; drives the exit code."""

    def __init__(self, suite):
        self.suite = suite
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    @staticmethod
    def _line(tag, name, detail):
        suffix = " :: %s" % detail if detail else ""
        return "%s  %s%s" % (tag, name, suffix)

    def ok(self, name, detail=""):
        self.passed += 1
        print(self._line("PASS", name, detail))

    def fail(self, name, detail=""):
        self.failed += 1
        print(self._line("FAIL", name, detail))

    def skip(self, name, reason=""):
        self.skipped += 1
        print(self._line("SKIP", name, reason))

    def check(self, name, condition, detail="", skip_reason=None):
        """PASS on truthy, SKIP when ``skip_reason`` is given, else FAIL."""
        if condition:
            self.ok(name, detail)
        elif skip_reason is not None:
            self.skip(name, skip_reason)
        else:
            self.fail(name, detail)

    def header(self, text):
        print("\n-- %s" % text)

    def summary(self):
        print("%s: %d passed, %d failed, %d skipped" % (
            self.suite, self.passed, self.failed, self.skipped))
        return 1 if self.failed else 0

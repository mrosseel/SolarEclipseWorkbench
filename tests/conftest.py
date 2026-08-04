"""Settings every test in this suite needs before Qt is imported.

Qt must be told to run without a display before the first QWidget is built.
One test file set it at its own import, which worked only while that file
happened to be imported first: adding widget tests to another file aborted the
whole run with "Fatal Python error", because pytest had already imported the
other file and created a QApplication against a platform plugin that is not
there.  It belongs here, once, ahead of everything.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    """One QApplication for the run, before any widget is built.

    Building a QWidget without one aborts the interpreter outright - no
    traceback that names the test, just "Fatal Python error: Aborted".  The
    widget tests only passed because some other file happened to be imported
    first and made one, so running a single file failed while the whole suite
    passed.
    """
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app

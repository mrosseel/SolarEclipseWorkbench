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

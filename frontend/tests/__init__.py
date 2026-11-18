"""Console test package.

Importing ``server`` resolves the runtime root immediately, so every test in
this directory must redirect it to a scratch folder first.  Doing it here
covers the modules that import ``server`` indirectly as well; ``test_server``
keeps its own copy so a direct ``pytest frontend/tests/test_server.py`` run
(which does not import this package) is isolated too.
"""
import os
import tempfile

os.environ.setdefault(
    "APP_PRESS_TEST_OUTPUT_ROOT",
    tempfile.mkdtemp(prefix="app-press-console-tests-"),
)

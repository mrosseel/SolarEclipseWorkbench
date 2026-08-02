"""Console tee for bench scripts: everything printed lands in a log file too.

Born of two separate evenings where errors existed only in a terminal that
could not be copy-pasted from.  Import and call first thing in main():

    from bench_log import tee_console
    log_path = tee_console("iso_probe")
"""

import logging
import sys
import time
from pathlib import Path


class _Tee:
    def __init__(self, stream, handle):
        self.stream = stream
        self.handle = handle

    def write(self, text):
        self.stream.write(text)
        self.handle.write(text)
        self.handle.flush()

    def flush(self):
        self.stream.flush()
        self.handle.flush()


def tee_console(name: str) -> Path:
    """Mirror stdout, stderr and logging into <name>_<ts>.log; returns the path."""
    log_path = Path.cwd() / f"{name}_{int(time.time())}.log"
    handle = open(log_path, "w")
    sys.stdout = _Tee(sys.stdout, handle)
    sys.stderr = _Tee(sys.stderr, handle)
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("matplotlib", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print(f"console log: {log_path}")
    return log_path

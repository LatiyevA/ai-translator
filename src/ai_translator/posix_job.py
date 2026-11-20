"""Keep a provider group tied to a pipe owned by the requesting process."""

import os
import select
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _pipe():
    reader, writer = os.pipe()
    try:
        yield reader, writer
    finally:
        os.close(reader)
        os.close(writer)


def run_posix_supervised(command: list[str], execute):
    with _pipe() as (owner, _keep_open), _pipe() as (status, report):
        launcher = [sys.executable, "-I", str(Path(__file__).resolve()), str(owner), str(report), *command]
        return execute(launcher, {"process_group": 0, "pass_fds": (owner, report)}, status_fd=status)


def main(argv: list[str]) -> None:
    if os.getpgid(0) != os.getpid():
        raise RuntimeError("Expected a dedicated process group")
    owner, report = map(int, argv[:2])
    try:
        # The write end stays solely in the worker. EOF also covers a hard kill.
        owner_watch = select.poll()
        owner_watch.register(owner, select.POLLIN)
        if owner_watch.poll(0):
            return
        try:
            process = subprocess.Popen(argv[2:])
        except OSError:
            print("Не удалось запустить CLI провайдера.", file=sys.stderr)
            code = 127
        else:
            while (code := process.poll()) is None:
                if owner_watch.poll(50):
                    return
        os.write(report, str(code).encode("ascii"))
        # Keep the group leader alive until the worker receives the code.
        owner_watch.poll()
    finally:
        os.killpg(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main(sys.argv[1:])

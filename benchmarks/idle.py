"""Measure the CLI's idle RSS in a POSIX PTY; stdout is a single JSON object."""
import json, os, pty, select, signal, subprocess, sys, time

pid, fd = pty.fork()
if pid == 0:
    os.execv(sys.argv[1], [sys.argv[1]])
output = b''
trusted = False
try:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if select.select([fd], [], [], 0.2)[0]:
            output += os.read(fd, 65536)
            if not trusted and b'Trust ' in output:
                os.write(fd, b'yes\r')
                trusted = True
            if b'Idle' in output:
                break
    else:
        raise RuntimeError('terminal did not reach idle')
    rss = int(subprocess.check_output(['ps', '-o', 'rss=', '-p', str(pid)], text=True).strip())
    os.write(fd, b'/quit\r')
    limit = time.monotonic() + 5
    while time.monotonic() < limit:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            pid = 0
            if os.waitstatus_to_exitcode(status) != 0:
                raise RuntimeError('interactive CLI did not exit cleanly')
            break
        if select.select([fd], [], [], 0.05)[0]:
            try:
                os.read(fd, 65536)
            except OSError:
                pass
    if pid:
        raise RuntimeError('interactive CLI did not exit after /quit')
    print(json.dumps({'rssKiB': rss}))
finally:
    os.close(fd)
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except ProcessLookupError:
            pass

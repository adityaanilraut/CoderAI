"""Time startup and a deterministic interactive turn, checking terminal restoration."""
import fcntl, json, os, pty, re, select, signal, struct, sys, termios, time

started = time.monotonic()
r, w = os.pipe()
pid, fd = pty.fork()
if pid == 0:
    os.close(r)
    os.write(w, str(termios.tcgetattr(0)[3]).encode())
    os.close(w)
    os.execv(sys.argv[1], [sys.argv[1]])
os.close(w)
original = int(os.read(r, 64)); os.close(r)
fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 110, 0, 0))
output = b''
def wait_for(needle, start=0):
    global output
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        plain = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', output.decode('utf8', errors='replace'))
        if needle in plain[start:]:
            return len(plain)
        if select.select([fd], [], [], 0.05)[0]:
            try: output += os.read(fd, 65536)
            except OSError: break
    raise RuntimeError('Timed out waiting for ' + needle)
try:
    wait_for('Trust ')
    os.write(fd, b'yes\r')
    cursor = wait_for('Idle')
    startup = (time.monotonic() - started) * 1000
    os.write(fd, b'hello')
    time.sleep(0.1)  # Give the terminal time to accept the draft before Enter.
    turn_started = time.monotonic()
    os.write(fd, b'\r')
    wait_for('Hello from the deterministic provider.', cursor)
    turn = (time.monotonic() - turn_started) * 1000
    quit_started = time.monotonic()
    os.write(fd, b'/quit')
    time.sleep(0.1)
    os.write(fd, b'\r')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            pid = 0
            if os.waitstatus_to_exitcode(status) != 0: raise RuntimeError('CLI exited with error')
            break
        if select.select([fd], [], [], 0.05)[0]:
            try: output += os.read(fd, 65536)
            except OSError: pass
    if pid: raise RuntimeError('CLI did not exit')
    if termios.tcgetattr(fd)[3] != original or b'\x1b[?25h' not in output: raise RuntimeError('terminal was not restored')
    print(json.dumps({'startupIdleMs': startup, 'turnAnswerMs': turn, 'quitMs': (time.monotonic()-quit_started)*1000, 'terminalRestored': True}))
finally:
    os.close(fd)
    if pid:
        try: os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
        except ProcessLookupError: pass

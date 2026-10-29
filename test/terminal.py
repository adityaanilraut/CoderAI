import os, sys, pty, select, time, json, fcntl, termios, struct, re, signal, base64
spec=json.loads(sys.stdin.read())
state_read,state_write=os.pipe()
pid,fd=pty.fork()
if pid==0:
    os.close(state_read)
    os.write(state_write,str(termios.tcgetattr(0)[3]).encode())
    os.close(state_write)
    os.chdir(spec['cwd'])
    if 'executable' in spec:
        os.execve(spec['executable'],[spec['executable']]+spec.get('args',[]),spec['env'])
    os.execve(spec['node'],[spec['node'],spec['cli']]+spec.get('args',[]),spec['env'])
os.close(state_write)
original_flags=int(os.read(state_read,64))
os.close(state_read)
fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',32,110,0,0))
output=b''
plain=''
def pump(timeout):
    global output,plain
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if select.select([fd],[],[],min(0.05,max(0,end-time.monotonic())))[0]:
            try: chunk=os.read(fd,65536)
            except OSError: return
            if not chunk: return
            output+=chunk
            plain=re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))','',output.decode('utf8',errors='replace'))
try:
    cursor=0
    for action in spec['actions']:
        deadline=time.monotonic()+12
        while action['wait'] not in plain[cursor:]:
            pump(0.1)
            if time.monotonic()>deadline: raise RuntimeError('Timed out waiting for '+action['wait']+'\n'+plain[-5000:])
        cursor=plain.index(action['wait'],cursor)+len(action['wait'])
        if 'signal' in action: os.kill(pid,getattr(signal,action['signal']))
        if 'send' in action:
            text=action['send']
            if text.endswith('\r'):
                os.write(fd,text[:-1].encode());pump(0.2);os.write(fd,b'\r')
            else: os.write(fd,text.encode())
            pump(0.2)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        pump(0.1)
        done,status=os.waitpid(pid,os.WNOHANG)
        if done:
            print(json.dumps({'code':os.waitstatus_to_exitcode(status),'output':plain,'raw':base64.b64encode(output).decode(), 'terminalRestored':b'\x1b[?25h' in output and termios.tcgetattr(fd)[3]==original_flags}))
            sys.exit(0)
    raise RuntimeError('PTY child did not exit\n'+plain[-5000:])
except Exception as error:
    os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
    print(str(error),file=sys.stderr);sys.exit(1)
finally: os.close(fd)

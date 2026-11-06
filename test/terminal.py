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
    os.execvpe(spec['node'],[spec['node'],spec['cli']]+spec.get('args',[]),spec['env'])
os.close(state_write)
original_flags=int(os.read(state_read,64))
os.close(state_read)
fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',spec.get('rows',32),spec.get('columns',110),0,0))
output=b''
plain=''
background_queries=0
background_replies=[]
background_in_progress=False
def pump(timeout):
    global output,plain,background_queries,background_replies,background_in_progress
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        ready=[reply for reply in background_replies if reply[0]<=time.monotonic()]
        background_replies=[reply for reply in background_replies if reply not in ready]
        for _,reply,complete in ready:
            os.write(fd,reply)
            background_in_progress=not complete
        if select.select([fd],[],[],min(0.05,max(0,end-time.monotonic())))[0]:
            try: chunk=os.read(fd,65536)
            except OSError: return
            if not chunk: return
            output+=chunk
            count=output.count(b'\x1b]11;?\x07')
            while background_queries<count:
                background_queries+=1
                if 'backgroundReply' in spec:
                    reply=('\x1b]11;'+spec['backgroundReply']+'\x07').encode()
                    due=time.monotonic()+spec.get('backgroundDelayMs',0)/1000
                    if spec.get('splitBackgroundReply'):
                        background_replies.extend([(due,reply[:10],False),(due+0.02,reply[10:],True)])
                    else: background_replies.append((due,reply,True))
            plain=re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))','',output.decode('utf8',errors='replace'))
def send_input(data):
    # A terminal's ordered response can span packets, but key bytes cannot split its payload.
    while background_in_progress: pump(0.05)
    os.write(fd,data)
try:
    cursor=0
    for action in spec['actions']:
        deadline=time.monotonic()+12
        while action['wait'] not in plain[cursor:]:
            pump(0.1)
            if time.monotonic()>deadline: raise RuntimeError('Timed out waiting for '+action['wait']+'\n'+plain[-5000:])
        cursor=plain.index(action['wait'],cursor)+len(action['wait'])
        if 'resize' in action:
            size=action['resize']
            fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack('HHHH',size['rows'],size['columns'],0,0))
            os.kill(pid,signal.SIGWINCH)
            pump(0.2)
        if 'signal' in action: os.kill(pid,getattr(signal,action['signal']))
        if 'send' in action:
            text=action['send']
            if text.endswith('\r'):
                send_input(text[:-1].encode());pump(0.2);send_input(b'\r')
            else: send_input(text.encode())
            pump(0.2)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        pump(0.1)
        done,status=os.waitpid(pid,os.WNOHANG)
        if done:
            print(json.dumps({'code':os.waitstatus_to_exitcode(status),'output':plain,'raw':base64.b64encode(output).decode(), 'backgroundQueries':background_queries, 'terminalRestored':b'\x1b[?25h' in output and termios.tcgetattr(fd)[3]==original_flags}))
            sys.exit(0)
    raise RuntimeError('PTY child did not exit\n'+plain[-5000:])
except Exception as error:
    os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
    print(str(error),file=sys.stderr);sys.exit(1)
finally: os.close(fd)

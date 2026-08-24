# CoderAI Notifications

Set `notify` in user or project settings to an executable path or command:

```json
{
  "notify": "/Users/you/.coderai/notify.py"
}
```

CoderAI launches it in the project working directory after a run and does not surface its stdout or stderr. On Unix, a non-executable file is attempted with `/bin/sh`.

## Environment

The script inherits the process environment plus string values from the resolved settings `env` object. CoderAI adds:

| Variable | Value |
| --- | --- |
| `DURATION` | Elapsed whole seconds |
| `STATUS` | Run status when provided |
| `FAIL_REASON` | Failure text when provided |
| `BODY` | Last assistant output when provided |
| `TITLE` | Session title when provided |

Only `DURATION` is unconditional. Scripts must handle the other variables being absent.

## Safe webhook example

Use Python's JSON encoder instead of interpolating assistant output into JSON:

```python
#!/usr/bin/env python3
import json
import os
import urllib.request

url = os.environ.get("CODERAI_NOTIFY_WEBHOOK")
if not url:
    raise SystemExit(0)

payload = {
    "text": (
        f"CoderAI: {os.environ.get('TITLE', 'session')} "
        f"{os.environ.get('STATUS', 'finished')} "
        f"({os.environ.get('DURATION', '0')}s)"
    )
}
request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
urllib.request.urlopen(request, timeout=10).close()
```

Keep webhook URLs in the shell environment or a protected user-level file, never in chat or a committed project settings file:

```bash
export CODERAI_NOTIFY_WEBHOOK="https://hooks.example.invalid/..."
chmod 700 ~/.coderai/notify.py
```

Provider payload formats differ; adapt `payload` to the chosen service.

## Local notifications

Examples for an executable shell script:

```bash
# macOS
osascript -e "display notification \"Task ${STATUS:-finished} (${DURATION}s)\" with title \"CoderAI\""

# Linux with notify-send installed
notify-send "CoderAI" "Task ${STATUS:-finished} (${DURATION}s)"

# iTerm2 or Windows Terminal OSC 9
printf '\033]9;CoderAI task %s (%ss)\007' "${STATUS:-finished}" "$DURATION"
```

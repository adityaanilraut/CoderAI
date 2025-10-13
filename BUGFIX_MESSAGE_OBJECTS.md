# Bug Fix - Message Object Handling

## Issue Discovered

**Date**: October 12, 2025  
**Severity**: Medium  
**Affected Commands**: `/tokens`, `/history`, `/export`

### Error
```
Error: Fatal error: 'Message' object has no attribute 'get'
```

### Root Cause
The interactive commands were assuming messages in the context are always dictionaries, but when passed from the agent, they are Pydantic `Message` objects. The code was using `.get()` method which doesn't exist on Pydantic models.

---

## Affected Code

### Before (Broken)
```python
# /tokens command
total_chars = sum(len(str(msg.get("content", ""))) for msg in context["messages"])

# /history command
role = msg.get("role", "unknown")
content = msg.get("content", "")

# /export command
"messages": context["messages"]  # Can't serialize Message objects directly
```

---

## Solution

Updated all three commands to handle both dictionary and `Message` object types:

### After (Fixed)

#### 1. `/tokens` Command
```python
# Handle both dict and Message object types
total_chars = 0
for msg in context["messages"]:
    if hasattr(msg, 'content'):  # Message object
        total_chars += len(str(msg.content))
    elif isinstance(msg, dict):  # Dictionary
        total_chars += len(str(msg.get("content", "")))
```

#### 2. `/history` Command
```python
# Handle both dict and Message object types
if hasattr(msg, 'role'):  # Message object
    role = msg.role
    content = msg.content
elif isinstance(msg, dict):  # Dictionary
    role = msg.get("role", "unknown")
    content = msg.get("content", "")
```

#### 3. `/export` Command
```python
# Convert Message objects to dicts for JSON serialization
messages_data = []
for msg in context["messages"]:
    if hasattr(msg, 'model_dump'):  # Pydantic Message object
        messages_data.append(msg.model_dump())
    elif isinstance(msg, dict):
        messages_data.append(msg)

export_data = {
    "messages": messages_data  # Now serializable
}
```

---

## Testing

### Test Results
```bash
Testing /tokens command with Message objects...
✅ /tokens command works!

Testing /history command with Message objects...
✅ /history command works!

Testing /export command with Message objects...
✅ /export command works!

🎉 All commands handle Message objects correctly!
```

### Validation
- ✅ Works with Message objects (from agent.session.messages)
- ✅ Works with dictionaries (from saved context)
- ✅ Exports valid JSON
- ✅ No more attribute errors

---

## Impact

**Before Fix**: Commands would crash during interactive chat when trying to access message data.

**After Fix**: Commands work seamlessly with both data types, providing a smooth user experience.

---

## Files Modified

1. **coderAI/ui/interactive.py**
   - Updated `/tokens` command handler (lines 170-192)
   - Updated `/history` command handler (lines 111-131)
   - Updated `/export` command handler (lines 202-234)

---

## Prevention

This issue occurred because the context can contain messages in two different formats:
1. **Pydantic Message objects** - when passed from `agent.session.messages`
2. **Dictionaries** - when serialized/deserialized or manually created

**Lesson**: Always handle both formats in commands that process messages from context.

---

## Status

✅ **FIXED** - All commands now work correctly with both Message objects and dictionaries.

**Date Fixed**: October 12, 2025  
**Tested**: ✅ Passed all tests  
**Deployed**: ✅ Ready for use


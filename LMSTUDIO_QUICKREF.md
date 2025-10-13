# LM Studio Quick Reference Card

## 🚀 Quick Start

### If using localhost (default port 1234):
```bash
coderAI --model lmstudio chat
```
✓ No configuration needed!

---

## ⚙️ Custom Configuration

### Set custom server URL:
```bash
coderAI config set lmstudio_endpoint http://YOUR_SERVER:PORT/v1
```

### Set custom model name:
```bash
coderAI config set lmstudio_model your-model-name
```

---

## 📋 Common Configurations

| Scenario | Command |
|----------|---------|
| **Local default** | No config needed |
| **Custom port** | `coderAI config set lmstudio_endpoint http://localhost:5000/v1` |
| **Remote server** | `coderAI config set lmstudio_endpoint http://192.168.1.100:1234/v1` |
| **Docker container** | `coderAI config set lmstudio_endpoint http://container-name:1234/v1` |

---

## 🔍 Verification

```bash
# Show all configuration
coderAI config show

# Show system status
coderAI status

# View current model info
coderAI info
```

---

## 🛠️ Configuration Methods

1. **CLI** (Recommended)
   ```bash
   coderAI config set lmstudio_endpoint http://YOUR_URL/v1
   ```

2. **Setup Wizard** (Interactive)
   ```bash
   coderAI setup
   ```

3. **Environment Variable** (Temporary)
   ```bash
   export LMSTUDIO_ENDPOINT="http://YOUR_URL/v1"
   ```

4. **Config File** (Manual)
   ```bash
   nano ~/.coderAI/config.json
   ```

---

## 🐛 Troubleshooting

### Connection Error?
1. Check LM Studio is running
2. Verify the endpoint URL: `coderAI config show`
3. Test connection: `curl http://YOUR_ENDPOINT/models`

### Wrong Model?
```bash
coderAI config set lmstudio_model correct-model-name
```

### Reset to Defaults?
```bash
coderAI config set lmstudio_endpoint http://localhost:1234/v1
coderAI config set lmstudio_model local-model
```

---

## 📖 Full Documentation

- `LMSTUDIO_CONFIG.md` - Complete configuration guide
- `LMSTUDIO_UPDATE_SUMMARY.md` - Change details
- `README.md` - General documentation

---

## ✅ Default Values

| Setting | Default Value |
|---------|---------------|
| Endpoint | `http://localhost:1234/v1` |
| Model | `local-model` |

---

**Note:** LM Studio must be running with a model loaded before use.


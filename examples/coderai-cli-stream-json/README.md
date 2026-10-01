# Example: CoderAI Stream JSON

Run CoderAI for one prompt and receive JSON lines over stdout. Requires a configured
provider; running this example makes a provider request. Offline regression tests
use a local subprocess instead.

```sh
cd examples/coderai-cli-stream-json
python3 main.py
```

The current CLI reads one JSON object with a `prompt` field through stdin EOF.
The example supplies a launch prompt to enter `--print`, writes the JSON prompt,
closes stdin, consumes output lines, and waits for the child to exit. It also reaps
the child when cancelled or when decoding fails. Use `coderai --wire` and the SDK
for a persistent session protocol; this example demonstrates the single-shot CLI.

import asyncio
import json
import os

CODERAI_CLI_COMMAND = "coderai"


async def main():
    proc = await asyncio.create_subprocess_exec(
        CODERAI_CLI_COMMAND,
        "--work-dir",
        os.getcwd(),
        "--print",
        "--prompt",
        "Read the supplied JSON prompt.",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None, "stdout is None"
    assert proc.stdin is not None, "stdin is None"

    user_message = {
        "prompt": "How many lines of code are there in the current working directory?",
    }
    try:
        proc.stdin.write(json.dumps(user_message).encode("utf-8") + b"\n")
        await proc.stdin.drain()
        # The single-shot CLI reads its JSON input through EOF.
        proc.stdin.close()
        await proc.stdin.wait_closed()

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            message = json.loads(line.decode("utf-8"))
            print("Received message:", message)
        if await proc.wait():
            raise RuntimeError(f"CoderAI exited with status {proc.returncode}")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


if __name__ == "__main__":
    asyncio.run(main())

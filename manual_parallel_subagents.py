#!/usr/bin/env python3
"""Manual integration harness for parallel sub-agent delegation.

This script is intentionally separate from pytest discovery because it depends
on a live model/provider and writes temporary output files as part of the run.
"""

import asyncio
import time

from coderAI.agent import Agent


async def main():
    agent = Agent(streaming=False)  # auto_approve is mocked to True internally

    print("Testing parallel subagent delegation...")
    prompt = (
        "You are tasked with confirming parallel subagents work. "
        "Use the delegate_task tool TWICE in the same response block to spawn two subagents. "
        "Subagent 1 should write 'hello 1' to 'subagent_out_1.txt'. "
        "Subagent 2 should write 'hello 2' to 'subagent_out_2.txt'. "
        "DO NOT use write_file yourself. You must use delegate_task for both. "
        "Exit when both subagents finish."
    )

    start_time = time.time()
    await agent.process_message(prompt)
    duration = time.time() - start_time

    print(f"\nExecution finished in {duration:.2f} seconds.")

    # Verify outputs
    try:
        with open("subagent_out_1.txt", "r") as f:
            t1 = f.read()
            print("File 1 content:", t1)
        with open("subagent_out_2.txt", "r") as f:
            t2 = f.read()
            print("File 2 content:", t2)
    except FileNotFoundError:
        print("Test failed: One or more files not found.")


if __name__ == "__main__":
    asyncio.run(main())

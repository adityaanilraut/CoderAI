import asyncio
import os
import sys

# Add the project root to the python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from coderAI.agent import Agent

async def test_subagent():
    # Initialize the main agent
    agent = Agent(auto_approve=True)
    
    # Send a prompt that forces delegation
    prompt = "Download the first image you find when searching 'EarFun Air Pro 3 official product image' and save it to /tmp/test_image.jpg. You must delegate this to a sub-agent."
    
    print("Testing main agent message processing...")
    result = await agent.process_message(prompt)
    
    print("\n--- Result Content ---")
    print(result.get("content", ""))
    print("----------------------")
    
    # Check if the delegated tool was called
    messages = result.get("messages", [])
    delegated = False
    for msg in messages:
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if tc.get("function", {}).get("name") == "delegate_task":
                    delegated = True
                    break
                    
    if delegated:
        print("\nSUCCESS: The agent successfully delegated the task.")
    else:
        print("\nWARNING: The agent did not delegate the task. It may have failed or ignored the instruction.")

if __name__ == "__main__":
    asyncio.run(test_subagent())

import asyncio

from kaos.path import KaosPath

from coderai.app import CoderAICLI, enable_logging
from coderai.session import Session

from my_tools.ls import Ls


async def main():
    enable_logging()
    session = await Session.create(KaosPath.cwd())
    instance = await CoderAICLI.create(session)
    # ponytail: agent-file custom tools not honored yet (load_agent ignores
    # the yaml tool list), so register directly until yaml parsing lands.
    instance.soul.agent.toolset.add(Ls())
    async for msg in instance.run(
        user_input="What tools do you have?",
        cancel_event=asyncio.Event(),
    ):
        print(msg)


if __name__ == "__main__":
    asyncio.run(main())

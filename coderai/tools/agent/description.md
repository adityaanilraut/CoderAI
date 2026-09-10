Spawn an isolated sub-agent session for complex, modular, or exploratory tasks. Give it a complete, standalone prompt: it does not share this conversation's context.

Start a continuable sub-agent in the background and return an agent id. Use send_message / list_agents / interrupt_agent to steer it. For a one-shot child, use Task or subagent_fork.

"""Agent Teams Coordination Seam & Swarm package for CoderAI."""

from coderai.core.teams.manager import (
    TeamManager,
    TeamTaskBoard,
    get_team_manager,
    reset_team_manager,
)
from coderai.core.teams.models import TeamMessage, TeamTask, Teammate
from coderai.core.teams.tools import (
    handle_spawn_teammate_tool,
    handle_team_task_create_tool,
    handle_team_task_get_tool,
    handle_team_task_list_tool,
    handle_team_task_update_tool,
    handle_wait_agent_tool,
)

__all__ = [
    "TeamManager",
    "TeamMessage",
    "TeamTask",
    "TeamTaskBoard",
    "Teammate",
    "get_team_manager",
    "handle_spawn_teammate_tool",
    "handle_team_task_create_tool",
    "handle_team_task_get_tool",
    "handle_team_task_list_tool",
    "handle_team_task_update_tool",
    "handle_wait_agent_tool",
    "reset_team_manager",
]

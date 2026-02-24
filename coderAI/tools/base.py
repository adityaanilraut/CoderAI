"""Base tool interface and registry."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from pydantic import BaseModel, ValidationError


class Tool(ABC):
    """Abstract base class for MCP tools."""

    name: str = ""
    description: str = ""
    parameters_model: Optional[Type[BaseModel]] = None

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute the tool with given parameters.

        Args:
            **kwargs: Tool-specific parameters

        Returns:
            Dictionary with execution results
        """
        pass

    def get_schema(self) -> Dict[str, Any]:
        """Get the JSON schema for this tool.

        Returns:
            OpenAI function calling schema
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.get_parameters(),
            },
        }

    def get_parameters(self) -> Dict[str, Any]:
        """Get the parameters schema for this tool.

        Returns:
            JSON Schema for parameters
        """
        if self.parameters_model:
            # Generate JSON schema and simplify for LLMs
            schema = self.parameters_model.model_json_schema()
            # Pydantic puts things in $defs sometimes, but LLMs handle flat mostly.
            # model_json_schema is usually fine.
            return schema
            
        return {"type": "object", "properties": {}}


class ToolRegistry:
    """Registry for managing available tools."""

    def __init__(self):
        """Initialize the tool registry."""
        self.tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool.

        Args:
            tool: Tool instance to register
        """
        self.tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name.

        Args:
            name: Tool name

        Returns:
            Tool instance or None if not found
        """
        return self.tools.get(name)

    def get_all(self) -> List[Tool]:
        """Get all registered tools.

        Returns:
            List of all tools
        """
        return list(self.tools.values())

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Get schemas for all tools.

        Returns:
            List of tool schemas for OpenAI function calling
        """
        return [tool.get_schema() for tool in self.tools.values()]

    async def execute(self, name: str, **kwargs) -> Dict[str, Any]:
        """Execute a tool by name.

        Args:
            name: Tool name
            **kwargs: Tool parameters

        Returns:
            Execution results

        Raises:
            ValueError: If tool not found
        """
        tool = self.get(name)
        if tool is None:
            raise ValueError(f"Tool not found: {name}")
            
        if tool.parameters_model:
            try:
                # Validate and parse arguments using Pydantic
                parsed_args = tool.parameters_model(**kwargs)
                return await tool.execute(**parsed_args.model_dump())
            except ValidationError as e:
                # Return validation errors as friendly tool response
                return {
                    "success": False,
                    "error": f"Validation error for tool '{name}':\\n{str(e)}",
                    "error_code": "validation_error"
                }
                
        return await tool.execute(**kwargs)


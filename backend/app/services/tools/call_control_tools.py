"""Call control tools for voice agents.

Provides function calling tools that allow the AI agent to control the call:
- End/hangup the call

Transfer and DTMF tools were removed: they reported success and did nothing,
because nothing in the bridge acts on them. They come back only when they act
through the carrier and can report what the carrier did. A model that names
them is refused as an unknown tool, and an agent that saved them in its tool
selection simply never offers them.
"""

from typing import Any

import structlog

logger = structlog.get_logger()


class CallControlTools:
    """Call control tools for voice agents.

    These tools allow the AI to control call flow. The actual execution
    is handled by the realtime session which has access to the telephony
    connection.

    Tools return a special "action" field that signals the realtime
    session to perform the telephony action.
    """

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions for call control.

        Returns:
            List of tool definitions in OpenAI function calling format
        """
        return [
            {
                "type": "function",
                "name": "end_call",
                "description": (
                    "End the current phone call. Use this when the conversation is complete, "
                    "the caller wants to hang up, or you've said goodbye. Always say a brief "
                    "farewell before calling this function."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": (
                                "Brief reason for ending the call (e.g., 'conversation_complete', "
                                "'caller_requested', 'no_response')"
                            ),
                        },
                    },
                    "required": ["reason"],
                },
            },
        ]

    @staticmethod
    def _execute_end_call(arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute end_call tool."""
        reason = arguments.get("reason", "conversation_complete")
        logger.info("end_call_requested", reason=reason)
        return {
            "success": True,
            "action": "end_call",
            "reason": reason,
            "message": "Call will be ended after this response.",
        }

    @staticmethod
    async def execute_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a call control tool.

        These tools don't perform the action directly - they return a special
        response that signals the realtime session to perform the telephony action.

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments

        Returns:
            Dict with action type and parameters for the realtime session to handle
        """
        handlers = {
            "end_call": CallControlTools._execute_end_call,
        }

        handler = handlers.get(tool_name)
        if handler:
            return handler(arguments)

        return {"success": False, "error": f"Unknown call control tool: {tool_name}"}

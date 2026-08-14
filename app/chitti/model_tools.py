from __future__ import annotations

from typing import Any

MODEL_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List entries in a workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a bounded UTF-8 workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_bytes": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a UTF-8 file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run one allowlisted project command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                    "type": "string",
                        "enum": [
                            "sync-lockfile",
                            "install",
                            "build",
                            "test",
                            "export",
                            "poster-export",
                            "generate-images",
                        ],
                    },
                    "args": {"type": "array", "items": {}},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "visual_critique",
            "description": (
                "Have the host inspect the authoritative captured poster PNG "
                "against the brief. Use only after poster-export and capture_screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screenshot",
            "description": "Capture the declared website or poster format.",
            "parameters": {
                "type": "object",
                "properties": {
                    "route": {"type": "string"},
                    "width": {"type": "integer", "minimum": 1},
                    "height": {"type": "integer", "minimum": 1},
                    "scale": {"type": "integer", "minimum": 1, "maximum": 2},
                },
                "required": ["route", "width"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Declare the current task complete after its done condition passes.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
)


def model_tool_schemas() -> list[dict[str, Any]]:
    return [dict(tool) for tool in MODEL_TOOL_DEFINITIONS]


def model_tool_names() -> frozenset[str]:
    return frozenset(
        str(tool["function"]["name"]) for tool in MODEL_TOOL_DEFINITIONS
    )

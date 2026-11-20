"""Isolated execution runner, guarded tool broker and MCP bridge (M2 spike).

Importing this package starts nothing: no containers, sockets or processes.
"""
from .runner import DEFAULT_LIMITS, DockerRunner, RunnerError, RunnerHandle, compare_snapshots
from .broker import DENIAL_CODES, TOOL_NAMES, ToolBroker

__all__ = [
    'DEFAULT_LIMITS', 'DockerRunner', 'RunnerError', 'RunnerHandle', 'compare_snapshots',
    'DENIAL_CODES', 'TOOL_NAMES', 'ToolBroker',
]

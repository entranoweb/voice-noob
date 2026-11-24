"""Synthiq Voice caller.

An automated test caller: it plays a real person's recorded answers into a voice
agent, reacts to what the agent says, and grades the call from the agent's own
tool results.

The core (leg protocol, turn detection, clips, scenario, director, grading,
stats) knows nothing about any voice engine. Engine-specific code lives only in
``legs/`` (how audio reaches the agent) and ``traces/`` (how the agent's tool
calls are read back afterwards).
"""

__version__ = "0.1.0"

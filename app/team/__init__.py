"""Multi-agent Team: six specialised agents (Coordinator, Researcher, Writer,
Coder, Reviewer, Tester) that plan, hand off shared artifacts, review each
other's work and assemble one final result.

Deliberately separate from app.missions (the Planner/Critic mission system
that drives AgentSession tool loops): a Team run is a graph of plain model
calls with explicit artifact handoffs, no browser-driving tools, and no
external side effects. See ARCHITECTURE.md ("Team") and docs/team.md.

Nothing in this package imports Qt except runner.py; the engine is plain
Python so it is testable without a QApplication.
"""

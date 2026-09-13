"""Phase 16: Automation Recorder - "demonstrate once, replay safely".

A recorded workflow is optional data hanging off a Skill (see
app/agent/skills.py's Skill.workflow) rather than a second, parallel system:
running one still goes through Skills' own run path, Missions' own step/node
path, and Scheduled Tasks' own path - see app/automation/runner.py, which is
the only new *execution* code this phase adds, and which itself delegates to
AgentSession.run_routine for the actual assess/confirm/execute work.
"""

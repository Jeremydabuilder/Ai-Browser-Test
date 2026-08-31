"""The Claude browser agent.

Everything in this package sits *above* BrowserController and talks to web
pages only through it. Nothing here touches QWebEngineView, QWebEnginePage,
QWebEngineProfile, Qt widgets, or arbitrary JavaScript.

    AgentPanel (UI)
        └── AgentSession          - the loop, the state, the confirmations
              ├── ClaudeClient    - Anthropic SDK, on a worker thread
              ├── ToolRegistry    - JSON schemas + dispatch
              └── BrowserController (GUI thread)
"""

from app.agent.config import AgentConfig, ContextLimits
from app.agent.keys import ApiKeyStore
from app.agent.session import AgentSession, AgentState

__all__ = ["AgentConfig", "AgentSession", "AgentState", "ApiKeyStore", "ContextLimits"]

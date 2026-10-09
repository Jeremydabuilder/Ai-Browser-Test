"""Starting templates for common missions.

A template is an editable starting sentence plus three honest facts the panel shows before you
start: what it needs (sources and tools, and whether those are available right now), which agents
it will use, and what it will not use. Routing is enforced, not advisory: the Coordinator's plan is
rejected if it uses an agent the template does not allow, so a simple request never fans out to
all six agents.

Edits are stored per template in settings (``team_template_<id>``); nothing else is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.team.model import AgentId

REQUIRED, RECOMMENDED, OPTIONAL = "required", "recommended", "optional"


@dataclass(frozen=True)
class Need:
    kind: str           # tabs | material | web | workspace | sandbox
    level: str          # required | recommended | optional
    label: str          # shown to the user
    minimum: int = 1


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    goal: str
    summary: str
    agents: tuple[str, ...]
    needs: tuple[Need, ...]


TEMPLATES: tuple[Template, ...] = (
    Template(
        "compare_tabs", "Compare tabs",
        "Compare the products or pages in these tabs and write a recommendation.",
        "Reads the tabs you attach, compares them point by point and recommends one.",
        (AgentId.RESEARCHER, AgentId.WRITER, AgentId.REVIEWER),
        (Need("tabs", REQUIRED, "2 or more open tabs", 2), Need("web", OPTIONAL, "web search for outside facts"))),
    Template(
        "research_topic", "Research a topic",
        "Research this topic and produce a briefing with sources: ",
        "Searches the web, reads the best pages and writes a cited briefing.",
        (AgentId.RESEARCHER, AgentId.WRITER, AgentId.REVIEWER),
        (Need("web", RECOMMENDED, "web search (otherwise only what you attach is used)"),
         Need("material", OPTIONAL, "tabs, text or files you want included"))),
    Template(
        "study_guide", "Study guide",
        "Turn these pages into a clear study guide with key terms, a short summary and practice questions.",
        "Reads what you attach and writes a study guide. No review step, so it is quick.",
        (AgentId.RESEARCHER, AgentId.WRITER),
        (Need("material", REQUIRED, "a tab, text or file to study"),)),
    Template(
        "review_code", "Review code",
        "Review this code, identify bugs, and propose fixes.",
        "Reads your code, proposes fixes as files, and (if a sandbox is available) runs real checks.",
        (AgentId.CODER, AgentId.REVIEWER, AgentId.TESTER),
        (Need("code", REQUIRED, "a workspace folder, or code pasted/attached"),
         Need("sandbox", OPTIONAL, "a code sandbox to run tests (Docker on Windows)"))),
    Template(
        "draft_report", "Draft a report",
        "Draft a report on: ",
        "Researches, then writes and reviews a structured report.",
        (AgentId.RESEARCHER, AgentId.WRITER, AgentId.REVIEWER),
        (Need("material", OPTIONAL, "tabs, text or files to base it on"),
         Need("web", RECOMMENDED, "web search for facts beyond what you attach"))),
)
BY_ID = {t.id: t for t in TEMPLATES}
_KEY = "team_template_"


def _stored(settings, template_id: str) -> str:
    try:
        return (settings.get(_KEY + template_id, "") or "") if settings is not None else ""
    except Exception:  # noqa: BLE001 - preferences are never load-bearing
        return ""


def goal_for(settings, template: Template) -> str:
    """The template's starting text: the user's edit if there is one, else the default."""
    return _stored(settings, template.id) or template.goal


def is_edited(settings, template: Template) -> bool:
    text = _stored(settings, template.id)
    return bool(text) and text != template.goal


def save_goal(settings, template: Template, text: str) -> None:
    """Store an edit ('' or the default text resets it)."""
    text = text.strip("\n")
    settings.set(_KEY + template.id, "" if not text.strip() or text == template.goal else text)


def check(template: Template, *, tabs: int, material: int, web: bool, workspace: bool,
          sandbox: bool) -> list[tuple[str, str, str]]:
    """[(state, text, how to fix)] per need. state: ok | missing | optional-missing."""
    out = []
    for need in template.needs:
        have = {"tabs": tabs >= need.minimum, "material": material >= need.minimum, "web": web,
                "code": workspace or material >= 1, "workspace": workspace, "sandbox": sandbox}[need.kind]
        if have:
            out.append(("ok", need.label, ""))
            continue
        fix = {"tabs": "Add tabs…", "material": "Add tabs, text or files", "web": "Set up in Settings",
               "code": "Pick a Workspace or add the code", "workspace": "Pick a Workspace",
               "sandbox": "Check sandbox in Settings"}[need.kind]
        out.append(("missing" if need.level == REQUIRED else "optional-missing", need.label, fix))
    return out


def route_text(template: Template) -> str:
    return " → ".join(AgentId.LABELS[a] for a in template.agents)

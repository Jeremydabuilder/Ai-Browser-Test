"""Owns every QWebEngineProfile-backed BrowserProfile the app uses: the one
default profile every plain Workspace shares, plus, lazily, one real
QWebEngineProfile per Workspace that opted into ``isolated_profile`` - see
app/browser/profile.py's BrowserProfile and app/workspaces/model.py's
module docstring for why that is a materially different guarantee than
ordinary Workspace separation.

**The one disclosed trade-off.** app/browser/internal.py's claim_scheme()
measured a real Qt WebEngine 6.11 limit: only the first QWebEngineProfile
built in the process can ever serve PyBrowser's own ``pybrowser://`` pages
(New Tab dashboard, Mission Library). A second BrowserProfile still works
for everything else a real, separate profile is for - its own cookies, its
own local storage, its own cache, its own logins - it just cannot show
those two internal pages; see new_tab_url_for(), which is what keeps an
isolated workspace's "new tab" from silently trying to load a page that
will never respond.
"""

from __future__ import annotations

from app.browser.profile import BrowserProfile
from app.workspaces.model import Workspace


class WorkspaceProfileManager:
    """Never tears a profile down mid-session - a QWebEngineProfile must
    outlive every page built from it (see BrowserProfile's own docstring),
    so once an isolated workspace's profile is built it lives for the rest
    of the process, exactly like the default profile always has."""

    def __init__(self, default_profile: BrowserProfile) -> None:
        self._default = default_profile
        self._isolated: dict[str, BrowserProfile] = {}

    @property
    def default(self) -> BrowserProfile:
        return self._default

    def profile_for(self, workspace: Workspace) -> BrowserProfile:
        """The BrowserProfile a workspace's tabs should use. Plain
        Workspaces (the overwhelming default) all share ``default`` - real
        shared login state, exactly as if Workspaces did not exist. Only a
        workspace with ``isolated_profile=True`` ever gets a second,
        genuinely separate QWebEngineProfile, built lazily on first use and
        cached by workspace id thereafter."""
        if not workspace.isolated_profile:
            return self._default
        cached = self._isolated.get(workspace.id)
        if cached is not None:
            return cached
        storage_name = workspace.profile_storage_name or f"workspace-{workspace.id}"
        # Parented to the default profile (never itself torn down mid-
        # session, per this class's own docstring) rather than left as a
        # bare Python reference in ``_isolated`` - consistent with every
        # other long-lived QObject in the app, and removes the dependency
        # on this dict never being pruned for its downloadRequested
        # connection to stay safe.
        profile = BrowserProfile(parent=self._default, storage_name=storage_name)
        self._isolated[workspace.id] = profile
        return profile

    def has_isolated_profile(self, workspace_id: str) -> bool:
        return workspace_id in self._isolated

    def new_tab_url_for(self, workspace: Workspace, default_new_tab_url: str) -> str:
        """What "new tab" should navigate to for this workspace.

        An isolated-profile workspace's tabs cannot render PyBrowser's own
        ``pybrowser://`` pages (see the module docstring) - handing one a
        blank tab that will never load is worse than being honest that
        this one trade-off exists. A workspace's own configured homepage
        still wins either way, isolated or not.
        """
        if workspace.homepage:
            return workspace.homepage
        if workspace.isolated_profile:
            return "about:blank"
        return default_new_tab_url

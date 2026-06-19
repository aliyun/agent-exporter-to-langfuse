"""macOS menu bar application for langstash using rumps."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import rumps

_ICON_PATH = str(Path(__file__).resolve().parent.parent / "assets" / "icon.svg")


def _fmt_count(n: int) -> str:
    """Format a count with k/M suffix for compact display."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class LangstashApp(rumps.App):
    """macOS menu bar (status bar) app for monitoring langstash export."""

    def __init__(self, server_url: str) -> None:
        super().__init__("Langstash", icon=_ICON_PATH, template=True)
        self.server_url = server_url.rstrip("/")
        self._stats: dict[str, Any] = {}
        self._include_prerelease = False

        self.timer = rumps.Timer(self._poll, 10)
        self.timer.start()
        self._poll(None)

    def _poll(self, _sender: Any) -> None:
        """Fetch stats from the server and rebuild the menu."""
        try:
            req = urllib.request.Request(f"{self.server_url}/stats")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self._stats = json.loads(resp.read().decode())
        except Exception:
            self._stats = {}
            self.title = "⚠"
            self._rebuild_menu(error=True)
            return

        try:
            req = urllib.request.Request(f"{self.server_url}/settings")
            with urllib.request.urlopen(req, timeout=5) as resp:
                settings = json.loads(resp.read().decode())
                self._include_prerelease = settings.get("include_prerelease", self._include_prerelease)
        except Exception:
            pass

        self._update_title()
        self._rebuild_menu()

    def _update_title(self) -> None:
        """Set the status bar title based on pending count."""
        pending = self._stats.get("pending_count", 0)

        if pending > 0:
            self.title = str(pending)
        else:
            self.title = ""

    def _rebuild_menu(self, error: bool = False) -> None:
        """Rebuild the menu from current stats."""
        self.menu.clear()

        if error:
            self.menu.add(rumps.MenuItem("Server unreachable", callback=None))
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Open Web UI", callback=self._open_webui))
            self.menu.add(rumps.MenuItem("Restart Langstash", callback=self._restart))
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem("Quit Langstash", callback=self._quit))
            return

        # Counts
        traces = self._stats.get("total_traces", 0)
        sent = self._stats.get("total_sent", 0)
        pending = self._stats.get("pending_count", 0)

        header = rumps.MenuItem(
            f"Traces: {_fmt_count(traces)}  Sent: {_fmt_count(sent)}  "
            f"Pending: {_fmt_count(pending)}"
        )
        header.set_callback(None)
        self.menu.add(header)

        # Token info
        tokens = self._stats.get("tokens_today", {})
        tok_in = tokens.get("input", 0)
        tok_out = tokens.get("output", 0)
        if tok_in + tok_out > 0:
            cache_read = tokens.get("cache_read", 0)
            cache_creation = tokens.get("cache_creation", 0)
            tok = rumps.MenuItem(
                f"Tokens: {_fmt_count(tok_in)} in / "
                f"{_fmt_count(cache_read)} cache-r / "
                f"{_fmt_count(cache_creation)} cache-w / "
                f"{_fmt_count(tok_out)} out"
            )
            tok.set_callback(None)
            self.menu.add(tok)

        self.menu.add(rumps.separator)

        # Status details
        last_err = self._stats.get("last_error")
        if last_err:
            err_item = rumps.MenuItem(f"Last error: {last_err.get('error', '?')} (retry {last_err.get('retries', 0)})")
            err_item.set_callback(None)
            self.menu.add(err_item)

        # Storage
        storage_mb = self._stats.get("storage_used_mb", 0)
        stor = rumps.MenuItem(f"Storage: {storage_mb:.1f} MB")
        stor.set_callback(None)
        self.menu.add(stor)

        # Version & upgrade
        ver = self._stats.get("current_version", "")
        if ver:
            ver_item = rumps.MenuItem(f"v{ver}")
            ver_item.set_callback(None)
            self.menu.add(ver_item)
            if self._stats.get("update_available"):
                latest = self._stats.get("latest_version", "?")
                upgrade_item = rumps.MenuItem(
                    f"Upgrade to v{latest}",
                    callback=self._do_upgrade,
                )
                self.menu.add(upgrade_item)

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Open Web UI", callback=self._open_webui))
        pre_item = rumps.MenuItem("Pre-release Updates", callback=self._toggle_prerelease)
        pre_item.state = self._include_prerelease
        self.menu.add(pre_item)
        self.menu.add(rumps.MenuItem("Restart Langstash", callback=self._restart))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit Langstash", callback=self._quit))

    def _toggle_prerelease(self, sender: Any) -> None:
        new_val = not self._include_prerelease
        try:
            data = json.dumps({"include_prerelease": new_val}).encode()
            req = urllib.request.Request(
                f"{self.server_url}/settings",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            self._include_prerelease = new_val
            sender.state = new_val
        except Exception:
            pass

    def _do_upgrade(self, _sender: Any = None) -> None:
        try:
            req = urllib.request.Request(
                f"{self.server_url}/upgrade",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("status") == "started":
                rumps.notification("Langstash", "", f"Upgrading to v{data.get('upgrading_to', '?')}...")
            elif data.get("status") == "up_to_date":
                rumps.notification("Langstash", "", "Already up to date.")
        except Exception as e:
            rumps.notification("Langstash", "", f"Upgrade failed: {e}")

    def _open_webui(self, _sender: Any = None) -> None:
        """Open the web UI in the default browser."""
        webbrowser.open(f"{self.server_url}/")

    def _restart(self, _sender: Any = None) -> None:
        try:
            req = urllib.request.Request(
                f"{self.server_url}/restart",
                data=b"",
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        rumps.quit_application()

    def _quit(self, _sender: Any = None) -> None:
        """Quit the application."""
        rumps.quit_application()


def run_with_menubar(app: Any, config: Any) -> None:
    """Start the FastAPI server in a daemon thread and run the menubar app.

    Called from main.py when on macOS and not --server-only.
    """
    import threading
    import time

    import uvicorn

    host = config.server.host
    port = config.server.port

    server_thread = threading.Thread(
        target=uvicorn.run,
        args=(app,),
        kwargs={"host": host, "port": port, "log_level": "warning"},
        daemon=True,
        name="langstash-server",
    )
    server_thread.start()

    time.sleep(0.5)

    server_url = f"http://{host}:{port}"
    menubar = LangstashApp(server_url)
    menubar.run()

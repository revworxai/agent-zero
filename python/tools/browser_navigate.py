import asyncio
import base64
import json
import os
import logging
from python.helpers.tool import Tool, Response

logger = logging.getLogger(__name__)


class DedicatedCDPConnection:
    """Independent CDP connection for browser_navigate.

    Completely isolated from browser-use's internal CDP client.
    Uses cdp_use.CDPClient with its own WebSocket, message queue,
    and command/response pipeline. What browser_agent does on its
    connection has zero effect on this one.
    """

    def __init__(self):
        self.client = None       # CDPClient instance
        self.session_id = None   # CDP session ID for the attached target
        self.target_id = None    # Target ID we're attached to
        self.cdp_url = None      # WebSocket URL we connected to

    async def connect(self, cdp_url: str, target_id: str):
        """Open dedicated WebSocket and attach to the specified target."""
        from cdp_use import CDPClient

        # Close existing connection if any
        await self.disconnect()

        self.cdp_url = cdp_url
        self.target_id = target_id

        # Create independent CDPClient with its own WebSocket
        self.client = CDPClient(url=cdp_url)
        await self.client.start()

        # Attach to the target to get our own session ID
        result = await self.client.send.Target.attachToTarget(
            {"targetId": target_id, "flatten": True}
        )
        self.session_id = result["sessionId"]
        logger.info(
            f"[DedicatedCDP] Connected to {cdp_url[-20:]} "
            f"target={target_id[:8]} session={self.session_id[:8]}"
        )

    async def disconnect(self):
        """Close the dedicated connection."""
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None
            self.session_id = None
            self.target_id = None
            self.cdp_url = None

    @property
    def is_alive(self) -> bool:
        """Check if the WebSocket connection is still open."""
        if not self.client or not self.client.ws:
            return False
        try:
            from websockets.protocol import State
            return self.client.ws.state is State.OPEN
        except Exception:
            return False

    async def navigate(self, url: str) -> dict:
        """Navigate to URL via CDP Page.navigate."""
        return await asyncio.wait_for(
            self.client.send.Page.navigate(
                {"url": url}, session_id=self.session_id
            ),
            timeout=15.0,
        )

    async def evaluate(self, expression: str) -> dict:
        """Execute JavaScript via CDP Runtime.evaluate."""
        return await asyncio.wait_for(
            self.client.send.Runtime.evaluate(
                {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
                session_id=self.session_id,
            ),
            timeout=10.0,
        )

    async def screenshot(self) -> dict:
        """Capture screenshot via CDP Page.captureScreenshot."""
        return await asyncio.wait_for(
            self.client.send.Page.captureScreenshot(
                {"format": "png"}, session_id=self.session_id
            ),
            timeout=10.0,
        )


class BrowserNavigate(Tool):
    """Tier-1 fast browser tool: direct CDP navigation and JS execution.

    Uses a DEDICATED CDP WebSocket connection that is completely independent
    from browser-use's internal CDP client. This eliminates queue saturation
    from browser_agent tasks - both tools can operate freely without
    interfering with each other.

    Actions:
    - goto: Navigate to URL via event_bus (uses browser-use connection)
    - hash_navigate: SPA hash navigation via dedicated CDP Page.navigate
    - evaluate: Execute JS via dedicated CDP Runtime.evaluate
    - screenshot: Capture page via dedicated CDP Page.captureScreenshot
    - get_info: Get current URL/title/tabs (uses browser-use connection)
    - dismiss_dialog: Dismiss any native browser dialog
    """

    # Class-level dedicated connection (persists across tool invocations)
    _dedicated_cdp = None

    def _get_state(self):
        """Get current browser state from Agent Zero's data store."""
        state = self.agent.get_data("_browser_agent_state")
        if state and state.browser_session:
            return state
        raise RuntimeError("No browser session active. Call browser_agent first.")

    async def _get_dedicated(self, browser_session):
        """Get or create the dedicated CDP connection.

        Re-creates the connection if:
        - No connection exists yet
        - WebSocket is dead (server restart, connection drop)
        - Target has changed (user switched tabs)
        - CDP URL has changed (new browser process)
        """
        target_id = browser_session.agent_focus_target_id
        if not target_id:
            raise RuntimeError("No focused target in browser session.")

        cdp_url = browser_session.cdp_url
        if not cdp_url:
            raise RuntimeError("No CDP URL available from browser session.")

        conn = BrowserNavigate._dedicated_cdp

        # Check if existing connection is still valid
        needs_reconnect = (
            conn is None
            or not conn.is_alive
            or conn.target_id != target_id
            or conn.cdp_url != cdp_url
        )

        if needs_reconnect:
            if conn is None:
                conn = DedicatedCDPConnection()
                BrowserNavigate._dedicated_cdp = conn

            logger.info(
                f"[BrowserNavigate] Creating dedicated CDP connection "
                f"(alive={conn.is_alive}, target_match={conn.target_id == target_id if conn.target_id else False})"
            )
            await conn.connect(cdp_url, target_id)

        return conn

    async def _do_dismiss_dialog(self, state):
        """Dismiss any native browser dialog via root-level CDP command."""
        session = state.browser_session
        root_cdp = session.cdp_client
        try:
            await asyncio.wait_for(
                root_cdp.send.Page.handleJavaScriptDialog({"accept": True}),
                timeout=5.0,
            )
            return "Dialog dismissed (accepted)"
        except asyncio.TimeoutError:
            return "Dialog dismiss timed out (no dialog may be present)"
        except Exception as e:
            try:
                await asyncio.wait_for(
                    root_cdp.send.Page.handleJavaScriptDialog({"accept": False}),
                    timeout=5.0,
                )
                return "Dialog dismissed (declined)"
            except Exception as e2:
                return f"Dialog dismiss error: {type(e).__name__}: {e} / {e2}"

    async def _do_goto(self, state, url):
        """Navigate using event_bus NavigateToUrlEvent (for full URL changes)."""
        from browser_use.browser.events import NavigateToUrlEvent

        session = state.browser_session
        try:
            await asyncio.wait_for(
                session.event_bus.dispatch(
                    NavigateToUrlEvent(url=url, new_tab=False)
                ),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            return f"Navigation timeout after 20s: {url}"
        except Exception as e:
            return f"Navigation error: {type(e).__name__}: {e}"
        await asyncio.sleep(0.5)
        try:
            final_url = await asyncio.wait_for(
                session.get_current_page_url(), timeout=3.0
            )
            title = await asyncio.wait_for(
                session.get_current_page_title(), timeout=3.0
            )
        except Exception:
            final_url, title = url, "(unknown)"
        return f"Navigated to: {final_url}\nTitle: {title}"

    async def _do_hash_navigate(self, state, hash_fragment):
        """Navigate to SPA hash via DEDICATED CDP connection.

        Uses an independent WebSocket that is completely isolated from
        browser-use's internal CDP client. No pre-sleep needed.
        """
        session = state.browser_session
        conn = await self._get_dedicated(session)

        if not hash_fragment.startswith("#"):
            hash_fragment = "#" + hash_fragment
        base_url = "https://app.revworx.ai/app2/serenity.pl"
        full_url = f"{base_url}{hash_fragment}"

        try:
            await conn.navigate(full_url)
        except asyncio.TimeoutError:
            return f"Hash navigation timeout for {hash_fragment}"
        except Exception as e:
            # Connection may have died - clear it for next attempt
            BrowserNavigate._dedicated_cdp = None
            return f"Hash nav error: {type(e).__name__}: {e}"

        await asyncio.sleep(0.5)  # Allow SPA to process hash change
        try:
            final_url = await asyncio.wait_for(
                session.get_current_page_url(), timeout=3.0
            )
            title = await asyncio.wait_for(
                session.get_current_page_title(), timeout=3.0
            )
        except Exception:
            final_url, title = full_url, "(unknown)"
        return f"Hash navigated to: {final_url}\nTitle: {title}"

    async def _do_evaluate(self, state, js):
        """Execute JS using DEDICATED CDP connection."""
        session = state.browser_session
        conn = await self._get_dedicated(session)

        # Evaluate the expression directly via CDP Runtime.evaluate.
        # No arrow-function wrapping — CDP evaluates expressions natively.
        # Users can pass simple expressions (document.title), IIFEs
        # ((function(){...})()), or any valid JS expression.
        expression = js.strip()

        try:
            result = await conn.evaluate(expression)
        except asyncio.TimeoutError:
            return "JS evaluation timed out after 10s"
        except Exception as e:
            BrowserNavigate._dedicated_cdp = None
            return f"evaluate error: {type(e).__name__}: {e}"

        if "exceptionDetails" in result:
            return f"JS exception: {result['exceptionDetails']}"
        value = result.get("result", {}).get("value")
        if value is None:
            return ""
        return json.dumps(value) if isinstance(value, (dict, list)) else str(value)

    async def _do_screenshot(self, state):
        """Capture screenshot using DEDICATED CDP connection."""
        session = state.browser_session
        conn = await self._get_dedicated(session)

        save_dir = "/a0/usr/workdir/screenshots"
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, "browser_navigate.png")

        try:
            result = await conn.screenshot()
        except asyncio.TimeoutError:
            return "Screenshot timed out"
        except Exception as e:
            BrowserNavigate._dedicated_cdp = None
            return f"screenshot error: {type(e).__name__}: {e}"

        b64 = result.get("data", "")
        try:
            img = base64.b64decode(b64)
            with open(path, "wb") as f:
                f.write(img)
        except Exception as e:
            return f"Screenshot decode error: {e}"

        try:
            url = await asyncio.wait_for(
                session.get_current_page_url(), timeout=3.0
            )
            title = await asyncio.wait_for(
                session.get_current_page_title(), timeout=3.0
            )
        except Exception:
            url, title = "(unknown)", "(unknown)"
        return f"Screenshot saved: {path}\nURL: {url}\nTitle: {title}"

    async def _do_get_info(self, state):
        """Get current URL/title/tabs - uses BrowserSession methods."""
        session = state.browser_session
        try:
            url = await asyncio.wait_for(
                session.get_current_page_url(), timeout=3.0
            )
            title = await asyncio.wait_for(
                session.get_current_page_title(), timeout=3.0
            )
        except Exception:
            url, title = "(unknown)", "(unknown)"
        try:
            pages = await asyncio.wait_for(session.get_pages(), timeout=3.0)
            tabs = len(pages)
        except Exception:
            tabs = "?"
        return f"URL: {url}\nTitle: {title}\nTabs: {tabs}"

    async def execute(self, action="goto", url="", js="", hash="", **kwargs):
        try:
            state = self._get_state()
        except RuntimeError as e:
            return Response(message=str(e), break_loop=False)
        try:
            if action == "dismiss_dialog":
                result = await self._do_dismiss_dialog(state)
            elif action == "get_info":
                result = await self._do_get_info(state)
            elif action == "goto":
                result = await self._do_goto(state, url or kwargs.get("url", ""))
            elif action == "hash_navigate":
                result = await self._do_hash_navigate(
                    state, hash or url or kwargs.get("hash", "")
                )
            elif action == "evaluate":
                result = await self._do_evaluate(state, js or kwargs.get("js", ""))
            elif action == "screenshot":
                result = await self._do_screenshot(state)
            else:
                result = (
                    f"Unknown action: '{action}'. "
                    "Use: goto, hash_navigate, evaluate, screenshot, get_info, dismiss_dialog"
                )
        except Exception as e:
            result = f"browser_navigate error ({action}): {type(e).__name__}: {e}"
        return Response(message=result, break_loop=False)

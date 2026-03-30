import asyncio
import time
from typing import Optional, cast
from agent import Agent, InterventionException
from pathlib import Path

from python.helpers.tool import Tool, Response
from python.helpers import files, defer, persist_chat, strings
from python.helpers.browser_use import browser_use  # type: ignore[attr-defined]
from python.helpers.print_style import PrintStyle
from python.helpers.playwright import ensure_playwright_binary
from python.helpers.secrets import get_secrets_manager
from python.extensions.message_loop_start._10_iteration_no import get_iter_no
from pydantic import BaseModel
import uuid
from python.helpers.dirty_json import DirtyJson


class State:
    @staticmethod
    async def create(agent: Agent):
        state = State(agent)
        return state

    def __init__(self, agent: Agent):
        self.agent = agent
        self.browser_session: Optional[browser_use.BrowserSession] = None
        self.task: Optional[defer.DeferredTask] = None
        self.use_agent: Optional[browser_use.Agent] = None
        self.secrets_dict: Optional[dict[str, str]] = None
        self.iter_no = 0

    def __del__(self):
        self.kill_task()
        # files.delete_dir(self.get_user_data_dir()) # cleanup user data dir - disabled to persist CRM session cookies

    def get_user_data_dir(self):
        """Returns the user data directory path for Chromium.
        Uses __file__ to detect project root — works correctly on both Mac (server host)
        and Docker (where /a0/ is read-only from Mac perspective).
        Example:
          Mac:    /Users/dalesmith/.../agent-zero/tmp/browseruse_profiles/agent_{id}
          Docker: /a0/tmp/browseruse_profiles/agent_{id}
        Both point to the same physical directory via volume mount."""
        from pathlib import Path as _Path
        import os as _os
        # browser_agent.py is at {project_root}/python/tools/browser_agent.py
        project_root = _Path(__file__).resolve().parent.parent.parent
        profile_name = f"agent_{self.agent.context.id}"
        udd = project_root / 'tmp' / 'browseruse_profiles' / profile_name
        udd.mkdir(parents=True, exist_ok=True)
        return str(udd)

    async def _initialize(self):
        if self.browser_session:
            return

        # for some reason we need to provide exact path to headless shell, otherwise it looks for headed browser
        pw_binary = ensure_playwright_binary()
        # DEBUG: Log which binary and args are being used
        import os as _os
        _debug_log = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', 'tmp', 'browser_ainvoke_debug.log')
        with open(_debug_log, 'a') as _dbg:
            _dbg.write(f"\n=== BROWSER SESSION INIT ===\n")
            _dbg.write(f"pw_binary: {pw_binary}\n")
            _dbg.write(f"binary exists: {_os.path.exists(str(pw_binary))}\n")
            _udd_info = self.get_user_data_dir()
            _dbg.write("user_data_dir to Chromium: " + _udd_info + chr(10))
                
        # Write Chromium Preferences to disable Save Password dialog
        # This is more reliable than command-line flags alone
        import json as _json
        _udd = self.get_user_data_dir()  # Same path works for both Docker file ops and Chromium
        _prefs_dir = _os.path.join(_udd, 'Default')
        _os.makedirs(_prefs_dir, exist_ok=True)
        _prefs_file = _os.path.join(_prefs_dir, 'Preferences')
        _prefs = {
            "credentials_enable_service": False,
            "credentials_enable_autosign_in": False,
            "profile": {
                "password_manager_enabled": False
            }
        }
        if not _os.path.exists(_prefs_file):
            with open(_prefs_file, 'w') as _pf:
                _json.dump(_prefs, _pf)

        self.browser_session = browser_use.BrowserSession(
            browser_profile=browser_use.BrowserProfile(
                headless=False,
                disable_security=True,
                chromium_sandbox=False,
                accept_downloads=True,
                downloads_path=files.get_abs_path("usr/downloads"),
                allowed_domains=["*", "http://*", "https://*"],
                executable_path=pw_binary,
                keep_alive=True,
                minimum_wait_page_load_time=0.3,
                wait_for_network_idle_page_load_time=0.8,
                maximum_wait_page_load_time=5.0,
                window_size={"width": 1920, "height": 900},
                screen={"width": 1920, "height": 1080},
                no_viewport=True,
                timezone_id='America/Belize',  # Match account TZ to prevent CRM timezone modal
                args=["--remote-debugging-port=9222", "--disable-save-password-bubble", "--disable-features=PasswordManagerEnabled"],
                # Use a unique user data directory to avoid conflicts
                user_data_dir=self.get_user_data_dir(),
                # storage_state enables StorageStateWatchdog to save/load cookies
                # auto-saves every 30s and on cookie changes -> persistent login
                # storage_state removed: conflicts with user_data_dir causing StorageStateWatchdog issues
                extra_http_headers=self.agent.config.browser_http_headers or {},
                )
        )

        try:
            await self.browser_session.start() if self.browser_session else None
            # After session starts, ensure rememberedTZ cookie is always set correctly
            # This prevents the timezone modal from appearing on every session
            try:
                if self.browser_session and self.browser_session.cdp_client:
                    await self.browser_session._cdp_set_cookies([{
                        'name': 'rememberedTZ',
                        'value': 'America/Belize',
                        'domain': 'app.revworx.ai',
                        'path': '/app2',
                        'secure': True,
                        'httpOnly': False,
                        'sameSite': 'None',
                        'expires': 1806539467
                    }])
            except Exception as _tz_err:
                pass  # Non-critical — timezone modal may appear but login still works
        except Exception as _start_err:
            import traceback as _tb
            _err_msg = str(_start_err)
            _err_tb = _tb.format_exc()
            with open(_debug_log, 'a') as _dbg:
                _dbg.write("BrowserSession.start() ERROR: " + _err_msg + chr(10))
                _dbg.write(_err_tb)
            raise
        # self.override_hooks()

        # --------------------------------------------------------------------------
        # Patch to enforce vertical viewport size
        # --------------------------------------------------------------------------
        # Browser-use auto-configuration overrides viewport settings, causing wrong
        # aspect ratio. We fix this by directly setting viewport size after startup.
        # --------------------------------------------------------------------------

        # Viewport patch disabled — no_viewport=True allows page to adapt to window size naturally
        # if self.browser_session:
        #     try:
        #         page = await self.browser_session.get_current_page()
        #         if page:
        #             await page.set_viewport_size({"width": 1920, "height": 900})
        #     except Exception as e:
        #         PrintStyle().warning(f"Could not force set viewport size: {e}")

        # --------------------------------------------------------------------------    
        
        # Add init script to the browser session
        # Supports both old API (browser_context) and new CDP-based API (0.11+)
        if self.browser_session:
            try:
                js_override = files.get_abs_path("lib/browser/init_override.js")
                if getattr(self.browser_session, "browser_context", None):
                    # Old API (browser-use < 0.11)
                    await self.browser_session.browser_context.add_init_script(path=js_override)
                elif hasattr(self.browser_session, "_cdp_add_init_script"):
                    # New CDP-based API (browser-use 0.11+)
                    with open(js_override, "r") as f:
                        script_content = f.read()
                    await self.browser_session._cdp_add_init_script(script_content)
            except Exception as e:
                PrintStyle().warning(f"Could not add init script: {e}")

    def start_task(self, task: str):
        if self.task and self.task.is_alive():
            self.kill_task()

        self.task = defer.DeferredTask(
            thread_name="BrowserAgent" + self.agent.context.id
        )
        if self.agent.context.task:
            self.agent.context.task.add_child_task(self.task, terminate_thread=True)
        self.task.start_task(self._run_task, task) if self.task else None
        return self.task

    def kill_task_only(self):
        """Stops current browser task but PRESERVES the browser session.
        The Chromium window stays open and can be reused for the next task.
        Use this instead of kill_task() when reset=True to avoid spawning new windows."""
        if self.task:
            self.task.kill(terminate_thread=True)
            self.task = None
        self.use_agent = None
        self.iter_no = 0
        # browser_session intentionally NOT closed — window stays alive

    def kill_task(self):
        if self.task:
            self.task.kill(terminate_thread=True)
            self.task = None
        if self.browser_session:
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self.browser_session.close()) if self.browser_session else None
                loop.close()
            except Exception as e:
                PrintStyle().error(f"Error closing browser session: {e}")
            finally:
                self.browser_session = None
        self.use_agent = None
        self.iter_no = 0

    async def _run_task(self, task: str):
        await self._initialize()

        class DoneResult(BaseModel):
            title: str
            response: str
            page_summary: str

        # Initialize controller
        controller = browser_use.Controller(output_model=DoneResult)

        # Register custom completion action with proper ActionResult fields
        @controller.registry.action("Complete task", param_model=DoneResult)
        async def complete_task(params: DoneResult):
            result = browser_use.ActionResult(
                is_done=True, success=True, extracted_content=params.model_dump_json()
            )
            return result

        model = self.agent.get_browser_model()

        try:

            secrets_manager = get_secrets_manager(self.agent.context)
            secrets_dict = secrets_manager.load_secrets()

            self.use_agent = browser_use.Agent(
                task=task,
                browser_session=self.browser_session,
                llm=model,
                use_vision=self.agent.config.browser_model.vision,
                extend_system_message=self.agent.read_prompt(
                    "prompts/browser_agent.system.md"
                ),
                controller=controller,
                enable_memory=False,  # Disable memory to avoid state conflicts
                llm_timeout=3000, # TODO rem
                # TEMPORARILY DISABLED FOR TESTING
                # sensitive_data=cast(dict[str, str | dict[str, str]] | None, secrets_dict or {}),  # Pass secrets
            )
        except Exception as e:
            raise Exception(
                f"Browser agent initialization failed. This might be due to model compatibility issues. Error: {e}"
            ) from e

        self.iter_no = get_iter_no(self.agent)

        async def hook(agent: browser_use.Agent):
            await self.agent.wait_if_paused()
            if self.iter_no != get_iter_no(self.agent):
                raise InterventionException("Task cancelled")

        # try:
        result = None
        if self.use_agent:
            result = await self.use_agent.run(
                max_steps=20, on_step_start=hook, on_step_end=hook
            )
        return result

    async def get_page(self):
        if self.use_agent and self.browser_session:
            try:
                return await self.use_agent.browser_session.get_current_page() if self.use_agent.browser_session else None
            except Exception:
                # Browser session might be closed or invalid
                return None
        return None

    async def get_selector_map(self):
        """Get the selector map for the current page state."""
        if self.use_agent:
            await self.use_agent.browser_session.get_state_summary(cache_clickable_elements_hashes=True) if self.use_agent.browser_session else None
            return await self.use_agent.browser_session.get_selector_map() if self.use_agent.browser_session else None
            await self.use_agent.browser_session.get_state_summary(
                cache_clickable_elements_hashes=True
            )
            return await self.use_agent.browser_session.get_selector_map()
        return {}


class BrowserAgent(Tool):

    async def execute(self, message="", reset="", **kwargs):
        self.guid = self.agent.context.generate_id() # short random id
        reset = str(reset).lower().strip() == "true"
        await self.prepare_state(reset=reset)
        # TEMPORARILY DISABLED FOR TESTING - masking caused LLM format errors with sensitive_data
        # message = get_secrets_manager(self.agent.context).mask_values(message, placeholder="<secret>{key}</secret>") # mask any potential passwords passed from A0 to browser-use to browser-use format
        task = self.state.start_task(message) if self.state else None

        # wait for browser agent to finish and update progress with timeout
        timeout_seconds = 300  # 5 minute timeout
        start_time = time.time()

        fail_counter = 0
        while not task.is_ready() if task else False:
            # Check for timeout to prevent infinite waiting
            if time.time() - start_time > timeout_seconds:
                PrintStyle().warning(
                    self._mask(f"Browser agent task timeout after {timeout_seconds} seconds, forcing completion")
                )
                break

            await self.agent.handle_intervention()
            await asyncio.sleep(1)
            try:
                if task and task.is_ready():  # otherwise get_update hangs
                    break
                try:
                    update = await asyncio.wait_for(self.get_update(), timeout=10)
                    fail_counter = 0  # reset on success
                except asyncio.TimeoutError:
                    fail_counter += 1
                    PrintStyle().warning(
                        self._mask(f"browser_agent.get_update timed out ({fail_counter}/3)")
                    )
                    if fail_counter >= 3:
                        PrintStyle().warning(
                            self._mask("3 consecutive browser_agent.get_update timeouts, breaking loop")
                        )
                        break
                    continue
                update_log = update.get("log", get_use_agent_log(None))
                self.update_progress("\n".join(update_log))
                screenshot = update.get("screenshot", None)
                if screenshot:
                    self.log.update(screenshot=screenshot)
            except Exception as e:
                PrintStyle().error(self._mask(f"Error getting update: {str(e)}"))

        if task and not task.is_ready():
            PrintStyle().warning(self._mask("browser_agent.get_update timed out, killing the task"))
            self.state.kill_task() if self.state else None
            return Response(
                message=self._mask("Browser agent task timed out, not output provided."),
                break_loop=False,
            )

        # final progress update
        if self.state and self.state.use_agent:
            log_final = get_use_agent_log(self.state.use_agent)
            self.update_progress("\n".join(log_final))

        # collect result with error handling
        try:
            result = await task.result() if task else None
        except Exception as e:
            PrintStyle().error(self._mask(f"Error getting browser agent task result: {str(e)}"))
            # Return a timeout response if task.result() fails
            answer_text = self._mask(f"Browser agent task failed to return result: {str(e)}")
            self.log.update(answer=answer_text)
            return Response(message=answer_text, break_loop=False)
        # finally:
        #     # Stop any further browser access after task completion
        #     # self.state.kill_task()
        #     pass

        # Check if task completed successfully
        if result and result.is_done():
            answer = result.final_result()
            try:
                if answer and isinstance(answer, str) and answer.strip():
                    answer_data = DirtyJson.parse_string(answer)
                    answer_text = strings.dict_to_text(answer_data)  # type: ignore
                else:
                    answer_text = (
                        str(answer) if answer else "Task completed successfully"
                    )
            except Exception as e:
                answer_text = (
                    str(answer)
                    if answer
                    else f"Task completed with parse error: {str(e)}"
                )
        else:
            # Task hit max_steps without calling done()
            urls = result.urls() if result else []
            current_url = urls[-1] if urls else "unknown"
            answer_text = (
                f"Task reached step limit without completion. Last page: {current_url}. "
                f"The browser agent may need clearer instructions on when to finish."
            )

        # Mask answer for logs and response
        answer_text = self._mask(answer_text)

        # update the log (without screenshot path here, user can click)
        self.log.update(answer=answer_text)

        # add screenshot to the answer if we have it
        if (
            self.log.kvps
            and "screenshot" in self.log.kvps
            and self.log.kvps["screenshot"]
        ):
            path = self.log.kvps["screenshot"].split("//", 1)[-1].split("&", 1)[0]
            answer_text += f"\n\nScreenshot: {path}"

        # respond (with screenshot path)
        return Response(message=answer_text, break_loop=False)

    def get_log_object(self):
        return self.agent.context.log.log(
            type="browser",
            heading=f"icon://captive_portal {self.agent.agent_name}: Calling Browser Agent",
            content="",
            kvps=self.args,
        )

    async def get_update(self):
        await self.prepare_state()

        result = {}
        agent = self.agent
        ua = self.state.use_agent if self.state else None
        page = await self.state.get_page() if self.state else None

        if ua and page:
            try:

                async def _get_update():

                    # await agent.wait_if_paused() # no need here

                    # Build short activity log
                    result["log"] = get_use_agent_log(ua)

                    path = files.get_abs_path(
                        persist_chat.get_chat_folder_path(agent.context.id),
                        "browser",
                        "screenshots",
                        f"{self.guid}.png",
                    )
                    files.make_dirs(path)
                    await page.screenshot(path=path, full_page=False, timeout=3000)
                    result["screenshot"] = f"img://{path}&t={str(time.time())}"

                if self.state and self.state.task and not self.state.task.is_ready():
                    await self.state.task.execute_inside(_get_update)

            except Exception:
                pass

        return result

    async def prepare_state(self, reset=False):
        self.state = self.agent.get_data("_browser_agent_state")
        if reset and self.state:
            # Kill task only — preserve browser session to avoid spawning new windows
            # The existing Chromium window stays open and is reused for the next task
            self.state.kill_task_only()
        if not self.state:
            # Only create new State if none exists (not on reset — reuse existing)
            self.state = await State.create(self.agent)
        self.agent.set_data("_browser_agent_state", self.state)

    def update_progress(self, text):
        text = self._mask(text)
        short = text.split("\n")[-1]
        if len(short) > 50:
            short = short[:50] + "..."
        progress = f"Browser: {short}"

        self.log.update(progress=text)
        self.agent.context.log.set_progress(progress)

    def _mask(self, text: str) -> str:
        try:
            return get_secrets_manager(self.agent.context).mask_values(text or "")
        except Exception as e:
            return text or ""

    # def __del__(self):
    #     if self.state:
    #         self.state.kill_task()


def get_use_agent_log(use_agent: browser_use.Agent | None):
    result = ["🚦 Starting task"]
    if use_agent:
        action_results = use_agent.history.action_results() or []
        short_log = []
        for item in action_results:
            # final results
            if item.is_done:
                if item.success:
                    short_log.append("✅ Done")
                else:
                    short_log.append(
                        f"❌ Error: {item.error or item.extracted_content or 'Unknown error'}"
                    )

            # progress messages
            else:
                text = item.extracted_content
                if text:
                    first_line = text.split("\n", 1)[0][:200]
                    short_log.append(first_line)
        result.extend(short_log)
    return result

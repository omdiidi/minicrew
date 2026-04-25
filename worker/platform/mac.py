"""MacPlatform — osascript + launchd implementation of the Platform protocol.

Lift-and-shift from `worker/terminal/launcher.py`, `worker/terminal/shutdown.py`,
and `worker/utils/launchd.py`. Behavior is byte-identical to the pre-refactor code
on the Mac path; this module is consolidated so the OS seam has a single home.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

from worker.platform.base import (
    CloseError,
    LaunchError,
    PreflightError,
    SessionHandle,
    dispatch_preflight_common,
)
from worker.utils.paths import trust_directory

# Baseline PATH for the launchd environment — matches what a login shell sees for Homebrew + system bins.
STANDARD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _label(instance: int) -> str:
    # Dot-separated label per Apple conventions; avoids grep false positives vs. `com.minicrew.worker-1`.
    return f"com.minicrew.worker.{instance}"


def _plist_path(instance: int) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(instance)}.plist"


def _render_plist(
    *,
    label: str,
    program_args: list[str],
    working_dir: str,
    stdout: Path,
    stderr: Path,
    env: dict[str, str],
) -> str:
    arg_block = "\n".join(f"    <string>{escape(a)}</string>" for a in program_args)
    env_block = "\n".join(
        f"    <key>{escape(k)}</key>\n    <string>{escape(v)}</string>" for k, v in env.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{escape(label)}</string>
  <key>ProgramArguments</key>
  <array>
{arg_block}
  </array>
  <key>WorkingDirectory</key><string>{escape(working_dir)}</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{escape(str(stdout))}</string>
  <key>StandardErrorPath</key><string>{escape(str(stderr))}</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_block}
  </dict>
</dict>
</plist>
"""


def _ensure_env_locked_down(env_path: Path) -> None:
    """F2: ensure `.env` is `0600` so only the owner can read Supabase credentials."""
    try:
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Best-effort; some filesystems (network mounts) reject chmod. Don't block install.
        pass


class MacPlatform:
    name: str = "mac"

    def preflight(self) -> None:
        if shutil.which("osascript") is None:
            raise PreflightError(
                "osascript not found — this is macOS-only; minicrew cannot run here"
            )
        launch_agents = Path("~/Library/LaunchAgents").expanduser()
        if not launch_agents.is_dir():
            raise PreflightError(
                f"{launch_agents} is not a directory — cannot install user services"
            )

    def dispatch_preflight(self, cfg) -> None:
        """Extended preflight when cfg.dispatch is configured. See base.dispatch_preflight_common."""
        dispatch_preflight_common(cfg)

    def launch_session(self, cwd: Path) -> SessionHandle:
        """Open Terminal.app, run `_run.sh`, return a SessionHandle with the window id.

        The `return id of window 1 whose tabs contains t` incantation is load-bearing — osascript
        returns a tab reference from `do script`, and we need the enclosing window id for later
        cleanup via `close window id ...`. Ported verbatim from reference lines 264-285.
        """
        trust_directory(str(cwd))
        runner = cwd / "_run.sh"
        if not runner.exists():
            raise LaunchError(f"_run.sh missing at {runner}")
        runner.chmod(0o755)
        # F10: defense-in-depth — escape backslash and double-quote in the runner path before
        # interpolating into the AppleScript literal. Current callers produce random tempdir
        # paths, but an attacker who influences the path shouldn't be able to break out of the
        # "do script" string.
        runner_escaped = str(runner).replace("\\", "\\\\").replace('"', '\\"')
        # `; exit 0` makes the shell exit once Claude exits, so the window enters "Process completed".
        script = (
            'tell application "Terminal"\n'
            f'    set t to do script "bash \\"{runner_escaped}\\"; exit 0"\n'
            "    return id of window 1 whose tabs contains t\n"
            "end tell\n"
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired as e:
            raise LaunchError(f"osascript timed out launching Terminal at {cwd}") from e
        stdout = (result.stdout or "").strip()
        if result.returncode != 0 or not stdout.isdigit():
            raise LaunchError(
                f"osascript launch failed (rc={result.returncode}): {(result.stderr or '').strip()}"
            )
        window_id = int(stdout)
        print(f"[launcher] Terminal launched at {cwd} (window {window_id})", file=sys.stderr)
        return SessionHandle(kind="mac", data={"window_id": window_id})

    def close_session(self, handle: SessionHandle) -> None:
        """Send `/exit` to the Claude prompt, wait for graceful shutdown, close the window.

        `/exit` is Claude Code's built-in clean shutdown; SIGTERM against the process leaves
        zombies and pops dialogs. Verified chain: /exit -> Claude exits -> shell `exit 0` ->
        [Process completed] -> window closes with no dialog.
        """
        window_id = handle.data["window_id"]
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "Terminal"\n    do script "/exit" in tab 1 of window id {window_id}\nend tell\n',
                ],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[shutdown] /exit send failed (window may be gone): {e}", file=sys.stderr)

        # Wait for the exit chain: /exit -> Claude exits -> _run.sh finishes -> exit 0 -> shell exits.
        time.sleep(5)

        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "Terminal" to close window id {window_id} saving no',
                ],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[shutdown] window close failed (may already be closed): {e}", file=sys.stderr)

    def install_service(
        self,
        *,
        instance: int,
        role: str,
        poll_interval: int | None,
        config_path: Path,
        python: Path,
        worker_pkg_root: Path,
        log_dir: Path,
        replace_existing: bool,
    ) -> None:
        label = _label(instance)
        plist_path = _plist_path(instance)
        uid = os.getuid()

        # F9: when replace_existing is False, refuse to clobber an existing install. When
        # True (setup.sh idempotent re-run), fall through and let the bootout+retry loop
        # below handle the transition.
        if not replace_existing:
            if plist_path.exists():
                raise RuntimeError(
                    f"Instance {instance} is already installed at {plist_path}. "
                    "Run `bash teardown.sh` or `/minicrew:teardown` first, or pick a different instance number."
                )
            probe = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, check=False
            )
            if probe.returncode == 0 and label in (probe.stdout or ""):
                raise RuntimeError(
                    f"Instance {instance} label {label} is already loaded in launchctl. "
                    "Run `bash teardown.sh` or `/minicrew:teardown` first, or pick a different instance number."
                )

        # F2: require `.env` to exist at repo root — the worker process loads it at startup.
        # The plist no longer carries Supabase secrets, so `.env` is the sole secret store.
        env_path = worker_pkg_root / ".env"
        if not env_path.exists():
            raise RuntimeError(
                f"Required .env not found at {env_path}. Run the steps in SETUP.md first to create it."
            )
        _ensure_env_locked_down(env_path)

        # F2: plist carries ONLY non-secret env — MINICREW_CONFIG_PATH locates the consumer
        # config dir, PATH lets the worker find `claude` and system binaries. Supabase
        # credentials stay in `.env` and are loaded by the worker process at startup.
        plist_env = {
            "MINICREW_CONFIG_PATH": str(config_path),
            "PATH": STANDARD_PATH,
        }

        args = [str(python), "-m", "worker", "--instance", str(instance), "--role", role]
        if poll_interval:
            args += ["--poll-interval", str(poll_interval)]
        stdout = log_dir / f"worker-{instance}.log"
        stderr = log_dir / f"worker-{instance}.err"
        log_dir.mkdir(parents=True, exist_ok=True)
        plist_xml = _render_plist(
            label=label,
            program_args=args,
            working_dir=str(worker_pkg_root),
            stdout=stdout,
            stderr=stderr,
            env=plist_env,
        )
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        # F9: when replacing an existing install, proactively bootout the old label so the
        # new plist-write + bootstrap sees a clean slate.
        if replace_existing:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                check=False,
                capture_output=True,
            )
            time.sleep(0.5)
        plist_path.write_text(plist_xml)
        for _attempt in range(3):
            res = subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                return
            if "already loaded" in (res.stderr or "").lower():
                subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
                time.sleep(0.5)
                continue
            raise RuntimeError(f"launchctl bootstrap failed: {(res.stderr or '').strip()}")
        raise RuntimeError(f"launchctl bootstrap failed after 3 attempts for {label}")

    def uninstall_service(self, *, instance: int) -> None:
        label = _label(instance)
        plist_path = _plist_path(instance)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
        if plist_path.exists():
            try:
                plist_path.unlink()
            except OSError:
                pass

    def installed_instances(self) -> list[int]:
        launch_agents = Path("~/Library/LaunchAgents").expanduser()
        if not launch_agents.is_dir():
            return []
        instances: list[int] = []
        for plist in launch_agents.glob("com.minicrew.worker.*.plist"):
            # Filename shape: com.minicrew.worker.<int>.plist → stem is com.minicrew.worker.<int>
            suffix = plist.stem.rsplit(".", 1)[-1]
            try:
                instances.append(int(suffix))
            except ValueError:
                continue
        return sorted(instances)


# Re-export CloseError so callers that `from worker.platform.mac import CloseError`
# get the canonical exception from `worker.platform.base`. Avoids a divergent class.
__all__ = ["MacPlatform", "CloseError", "LaunchError", "PreflightError", "SessionHandle"]

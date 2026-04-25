"""LinuxPlatform — xfce4-terminal/xterm/tmux sessions + systemd-user service management."""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from worker.platform.base import (
    LaunchError,
    PreflightError,
    SessionHandle,
    dispatch_preflight_common,
)
from worker.utils.paths import trust_directory

SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=Minicrew worker instance {instance}
After=graphical-session.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={python} -m worker --instance {instance} --role {role}{poll_arg}
Restart=always
RestartSec=5
StandardOutput=append:{stdout}
StandardError=append:{stderr}
Environment=MINICREW_CONFIG_PATH={config_path}
Environment=PATH={path}
Environment=DISPLAY=:0
Environment=XAUTHORITY=%h/.Xauthority
Environment=XDG_SESSION_TYPE=x11

[Install]
WantedBy=default.target
"""


@dataclass
class LinuxPlatformConfig:
    display_mode: str = "visible"
    terminal_emulator: str = "xfce4-terminal"
    window_open_timeout_seconds: int = 15
    exit_grace_seconds: int = 30
    sigterm_to_sigkill_seconds: int = 9


class LinuxPlatform:
    name: str = "linux"

    def __init__(self, cfg: LinuxPlatformConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------ preflight

    def preflight(self) -> None:
        if self.cfg.display_mode == "tmux":
            if shutil.which("tmux") is None:
                raise PreflightError("tmux not found. Run: sudo apt install tmux")
            return

        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            raise PreflightError(
                "Wayland session detected — minicrew visible mode requires X11. "
                "At the LightDM login screen, open the session menu and pick 'Xfce Session' (X11). "
                "Alternatively set platform.linux.display_mode: tmux to run headless."
            )
        if not os.environ.get("DISPLAY"):
            raise PreflightError(
                "$DISPLAY is not set. systemd user services do not inherit DISPLAY automatically; "
                "the unit file should carry `Environment=DISPLAY=:0` and `Environment=XAUTHORITY=%h/.Xauthority`, "
                "or run `systemctl --user import-environment DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS` "
                "from your XFCE session's startup."
            )
        for tool in ("wmctrl", "xdotool", self.cfg.terminal_emulator):
            if shutil.which(tool) is None:
                raise PreflightError(
                    f"required binary {tool!r} not found. "
                    "Run: sudo apt install wmctrl xdotool xfce4-terminal"
                )
        try:
            wm_out = subprocess.check_output(["wmctrl", "-m"], text=True, timeout=3)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise PreflightError(
                "wmctrl cannot talk to the window manager — the session may be Wayland or the "
                "X server may not be reachable from this process. "
                f"Root cause: {e}"
            ) from e
        if "Name:" not in wm_out:
            raise PreflightError(
                "wmctrl -m returned no window manager. Cannot launch visible terminals. "
                f"Output was: {wm_out!r}"
            )

    def dispatch_preflight(self, cfg) -> None:
        """Extended preflight when cfg.dispatch is configured. See base.dispatch_preflight_common."""
        dispatch_preflight_common(cfg)

    # ------------------------------------------------------------------ launch

    def launch_session(self, cwd: Path) -> SessionHandle:
        if self.cfg.display_mode == "tmux":
            return self._launch_tmux(cwd)
        return self._launch_visible(cwd)

    def _launch_visible(self, cwd: Path) -> SessionHandle:
        trust_directory(str(cwd))
        runner = cwd / "_run.sh"
        if not runner.exists():
            raise LaunchError(f"_run.sh missing at {runner}")

        unique = f"minicrew-{uuid.uuid4().hex[:12]}"
        if self.cfg.terminal_emulator == "xfce4-terminal":
            argv = [
                "xfce4-terminal",
                "--disable-server",
                f"--working-directory={cwd}",
                f"--title={unique}",
                "-e",
                f"bash {shlex.quote(str(runner))}",
            ]
        elif self.cfg.terminal_emulator == "xterm":
            argv = [
                "xterm",
                "-T",
                unique,
                "-e",
                f"cd {shlex.quote(str(cwd))} && bash {shlex.quote(str(runner))}",
            ]
        else:
            raise LaunchError(
                f"unsupported terminal_emulator {self.cfg.terminal_emulator!r}"
            )

        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = proc.pid
        pgid = os.getpgid(pid)

        (cwd / "_pending_pid.txt").write_text(f"{pid}\n{pgid}\n", encoding="utf-8")

        deadline = time.time() + self.cfg.window_open_timeout_seconds
        window_id: str | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                self._force_kill_pgid(pgid)
                try:
                    (cwd / "_pending_pid.txt").unlink()
                except OSError:
                    pass
                raise LaunchError(
                    f"terminal exited before window opened (rc={proc.returncode})"
                )
            out = self._wmctrl_list()
            for line in out.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                wid, _desk, line_pid, _host, title = parts
                if unique in title or line_pid == str(pid):
                    window_id = wid
                    break
            if window_id:
                break
            time.sleep(0.4)

        if window_id is None:
            self._force_kill_pgid(pgid)
            try:
                (cwd / "_pending_pid.txt").unlink()
            except OSError:
                pass
            raise LaunchError(
                f"terminal window never appeared within {self.cfg.window_open_timeout_seconds}s "
                f"(pid={pid}, title={unique})"
            )

        try:
            (cwd / "_pending_pid.txt").unlink()
        except OSError:
            pass

        return SessionHandle(
            kind=f"linux_{self.cfg.terminal_emulator.replace('-terminal', '')}",
            data={"pid": pid, "pgid": pgid, "window_id": window_id, "title": unique},
        )

    def _launch_tmux(self, cwd: Path) -> SessionHandle:
        runner = cwd / "_run.sh"
        if not runner.exists():
            raise LaunchError(f"_run.sh missing at {runner}")

        session = f"minicrew-{uuid.uuid4().hex[:12]}"
        try:
            subprocess.run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "-c",
                    str(cwd),
                    f"bash {shlex.quote(str(runner))}",
                ],
                check=True,
                capture_output=True,
            )
        except FileNotFoundError as e:
            raise LaunchError("tmux not found on PATH") from e
        except subprocess.CalledProcessError as e:
            raise LaunchError(
                f"tmux new-session failed (rc={e.returncode}): {e.stderr!r}"
            ) from e

        return SessionHandle(kind="linux_tmux", data={"session_name": session})

    # ------------------------------------------------------------------ close

    def close_session(self, handle: SessionHandle) -> None:
        if handle.kind == "linux_tmux":
            self._close_tmux(handle)
            return

        pid = handle.data["pid"]
        pgid = handle.data.get("pgid") or pid
        wid = handle.data["window_id"]

        import worker.core.state as state
        fast_path = bool(state.shutdown_requested)

        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", wid],
                capture_output=True,
                timeout=3,
            )
            subprocess.run(
                [
                    "xdotool",
                    "key",
                    "--clearmodifiers",
                    "slash",
                    "e",
                    "x",
                    "i",
                    "t",
                    "Return",
                ],
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[platform.linux] /exit send failed: {e}", file=sys.stderr)

        if not fast_path:
            deadline = time.time() + self.cfg.exit_grace_seconds
            while time.time() < deadline:
                if not self._pid_alive(pid):
                    break
                time.sleep(0.5)

        if self._pid_alive(pid):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            deadline2 = time.time() + self.cfg.sigterm_to_sigkill_seconds
            while time.time() < deadline2 and self._pid_alive(pid):
                time.sleep(0.3)
            if self._pid_alive(pid):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass

        out = self._wmctrl_list()
        for line in out.splitlines():
            if line.startswith(wid):
                print(
                    f"[platform.linux] WARNING: window {wid} still present after teardown",
                    file=sys.stderr,
                )
                break

    def _close_tmux(self, handle: SessionHandle) -> None:
        name = handle.data["session_name"]
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", name, "/exit", "Enter"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[platform.linux] tmux send-keys failed: {e}", file=sys.stderr)

        deadline = time.time() + self.cfg.exit_grace_seconds
        while time.time() < deadline:
            try:
                rc = subprocess.run(
                    ["tmux", "has-session", "-t", name],
                    check=False,
                    capture_output=True,
                    timeout=5,
                ).returncode
            except (OSError, subprocess.TimeoutExpired):
                rc = 0
            if rc != 0:
                return
            time.sleep(0.5)

        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[platform.linux] tmux kill-session failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------------ service

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
        if not os.environ.get("XDG_RUNTIME_DIR"):
            raise RuntimeError(
                "XDG_RUNTIME_DIR is unset — systemctl --user requires a login session. "
                "Run setup.sh from inside an XFCE desktop session (Chrome Remote Desktop is fine), "
                "not from a plain ssh login. See docs/LINUX.md."
            )

        label = f"minicrew-worker-{instance}"
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit_path = unit_dir / f"{label}.service"

        if not replace_existing and unit_path.exists():
            raise RuntimeError(
                f"Instance {instance} already installed at {unit_path}. "
                "Run `bash teardown.sh` or `/minicrew:teardown` first."
            )

        env_file = (worker_pkg_root / ".env").resolve()
        if not env_file.exists():
            raise RuntimeError(f"Required .env not found at {env_file}. See SETUP.md.")
        env_file.chmod(0o600)

        log_dir.mkdir(parents=True, exist_ok=True)
        unit_text = SYSTEMD_UNIT_TEMPLATE.format(
            instance=instance,
            working_dir=str(worker_pkg_root),
            python=str(python),
            role=role,
            poll_arg=(f" --poll-interval {poll_interval}" if poll_interval else ""),
            stdout=str(log_dir / f"worker-{instance}.log"),
            stderr=str(log_dir / f"worker-{instance}.err"),
            config_path=str(config_path),
            path="/usr/local/bin:/usr/bin:/bin",
        )
        unit_path.write_text(unit_text)
        unit_path.chmod(0o644)

        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True,
            capture_output=True,
        )
        if replace_existing:
            subprocess.run(
                ["systemctl", "--user", "restart", f"{label}.service"],
                check=False,
                capture_output=True,
            )
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"{label}.service"],
            check=True,
            capture_output=True,
        )

    def uninstall_service(self, *, instance: int) -> None:
        label = f"minicrew-worker-{instance}"
        unit_path = Path.home() / ".config" / "systemd" / "user" / f"{label}.service"
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"{label}.service"],
            check=False,
            capture_output=True,
        )
        if unit_path.exists():
            try:
                unit_path.unlink()
            except OSError:
                pass
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
        )

    def installed_instances(self) -> list[int]:
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        out: list[int] = []
        if not unit_dir.is_dir():
            return out
        for p in unit_dir.glob("minicrew-worker-*.service"):
            stem = p.stem
            suffix = stem[len("minicrew-worker-"):]
            try:
                out.append(int(suffix))
            except ValueError:
                continue
        return sorted(out)

    # ------------------------------------------------------------------ helpers

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return False
        return True

    def _force_kill_pgid(self, pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        deadline = time.time() + self.cfg.sigterm_to_sigkill_seconds
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, OSError):
                return
            time.sleep(0.3)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def _wmctrl_list(self) -> str:
        try:
            return subprocess.check_output(
                ["wmctrl", "-lp"], text=True, timeout=5
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return ""

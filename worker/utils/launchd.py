"""launchd plist render + install/uninstall for per-instance worker services."""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

# Baseline PATH for the launchd environment — matches what a login shell sees for Homebrew + system bins.
STANDARD_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _label(instance: int) -> str:
    # Dot-separated label per Apple conventions; avoids grep false positives vs. `com.minicrew.worker-1`.
    return f"com.minicrew.worker.{instance}"


def _plist_path(instance: int) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(instance)}.plist"


def render_plist(
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


def install(
    *,
    instance: int,
    role: str,
    poll_interval: int | None,
    config_path: Path,
    python: Path,
    worker_pkg_root: Path,
    log_dir: Path,
    replace_existing: bool = False,
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
    plist_xml = render_plist(
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


def uninstall(*, instance: int) -> None:
    label = _label(instance)
    plist_path = _plist_path(instance)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
    if plist_path.exists():
        try:
            plist_path.unlink()
        except OSError:
            pass


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m worker.utils.launchd")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install", help="Install launchd service for one instance")
    p_install.add_argument("--instance", type=int, required=True)
    p_install.add_argument("--role", choices=["primary", "secondary"], default="primary")
    p_install.add_argument("--poll-interval", type=int, default=None)
    p_install.add_argument("--config-path", type=Path, required=True)
    p_install.add_argument("--python", type=Path, default=Path(sys.executable))
    p_install.add_argument("--worker-root", type=Path, default=None)
    p_install.add_argument("--log-dir", type=Path, default=None)
    p_install.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace any existing install for this instance (idempotent re-run).",
    )

    p_uninstall = sub.add_parser("uninstall", help="Uninstall launchd service for one instance")
    p_uninstall.add_argument("--instance", type=int, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "install":
        worker_root = args.worker_root or Path(__file__).resolve().parent.parent.parent
        log_dir = args.log_dir or (worker_root / "logs")
        install(
            instance=args.instance,
            role=args.role,
            poll_interval=args.poll_interval,
            config_path=args.config_path.resolve(),
            python=args.python,
            worker_pkg_root=worker_root,
            log_dir=log_dir,
            replace_existing=args.replace_existing,
        )
        return 0
    if args.cmd == "uninstall":
        uninstall(instance=args.instance)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

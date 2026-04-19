"""launchd plist render + install/uninstall for per-instance worker services."""
from __future__ import annotations

import argparse
import os
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


def install(
    *,
    instance: int,
    role: str,
    poll_interval: int | None,
    config_path: Path,
    python: Path,
    worker_pkg_root: Path,
    log_dir: Path,
) -> None:
    label = _label(instance)
    plist_path = _plist_path(instance)
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
        env={
            "MINICREW_CONFIG_PATH": str(config_path),
            "PATH": STANDARD_PATH,
        },
    )
    # Tear down any existing service; tolerate non-loaded case.
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False, capture_output=True)
    # bootout is async — short sleep + retry loop is simpler than parsing launchctl print.
    time.sleep(0.5)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
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
        )
        return 0
    if args.cmd == "uninstall":
        uninstall(instance=args.instance)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

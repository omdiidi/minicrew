"""Platform factory + CLI entrypoint for install/uninstall of OS services."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from worker.platform.base import Platform, PreflightError


def detect_platform(cfg) -> Platform:
    # Two-guard form: `cfg` may be None (unlikely) and `cfg.platform` may itself be
    # None when no `platform:` block is present in config.yaml.
    plat_cfg = getattr(cfg, "platform", None) if cfg else None
    kind = plat_cfg.kind if plat_cfg else "auto"
    if kind == "auto":
        if sys.platform == "darwin":
            kind = "mac"
        elif sys.platform == "linux":
            kind = "linux"
        else:
            raise PreflightError(f"unsupported sys.platform={sys.platform!r}")
    if kind == "mac":
        from worker.platform.mac import MacPlatform
        return MacPlatform()
    if kind == "linux":
        from worker.platform.linux import LinuxPlatform, LinuxPlatformConfig
        # Always populate a LinuxPlatformConfig with defaults on Linux, even if
        # the user omitted the `platform.linux:` sub-block.
        lin_cfg = (plat_cfg.linux if plat_cfg and plat_cfg.linux else LinuxPlatformConfig())
        return LinuxPlatform(lin_cfg)
    raise PreflightError(f"unsupported platform.kind={kind!r}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m worker.platform",
        description="Install/uninstall minicrew worker OS services (launchd on Mac, systemd-user on Linux).",
    )
    config_path_help = (
        "Path to the minicrew config directory (containing config.yaml). "
        "Falls back to $MINICREW_CONFIG_PATH."
    )
    parser.add_argument(
        "--config-path",
        dest="top_config_path",
        type=str,
        default=None,
        help=config_path_help,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install", help="Install one worker instance service.")
    p_install.add_argument("--instance", type=int, required=True)
    p_install.add_argument("--role", type=str, required=True)
    p_install.add_argument("--poll-interval", type=int, default=None)
    p_install.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace the unit if it already exists.",
    )
    p_install.add_argument("--config-path", type=str, default=None, help=config_path_help)

    p_uninstall = sub.add_parser("uninstall", help="Uninstall one worker instance service.")
    p_uninstall.add_argument("--instance", type=int, required=True)
    p_uninstall.add_argument("--config-path", type=str, default=None, help=config_path_help)

    p_uninstall_all = sub.add_parser(
        "uninstall-all",
        help="Uninstall every installed minicrew worker instance (glob-based).",
    )
    p_uninstall_all.add_argument(
        "--config-path", type=str, default=None, help=config_path_help
    )

    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Config-first: resolve the config path, load it, then dispatch.
    from worker.config.loader import load_config

    # --config-path may be passed before the subcommand (top-level) or after it
    # (subparser). Prefer the subparser value when both are set.
    sub_config_path = getattr(args, "config_path", None)
    top_config_path = getattr(args, "top_config_path", None)
    config_path = sub_config_path or top_config_path or os.environ.get("MINICREW_CONFIG_PATH")
    cfg = load_config(config_path)
    platform = detect_platform(cfg)

    if args.command == "install":
        # Derive paths consistent with the plan's install_service signature.
        python = Path(sys.executable)
        worker_pkg_root = Path(__file__).resolve().parent.parent.parent
        log_dir = worker_pkg_root / "logs"
        resolved_config_path = Path(config_path).resolve() if config_path else Path(
            os.environ["MINICREW_CONFIG_PATH"]
        ).resolve()
        platform.install_service(
            instance=args.instance,
            role=args.role,
            poll_interval=args.poll_interval,
            config_path=resolved_config_path,
            python=python,
            worker_pkg_root=worker_pkg_root,
            log_dir=log_dir,
            replace_existing=args.replace_existing,
        )
        print(f"installed instance {args.instance}")
        return 0

    if args.command == "uninstall":
        platform.uninstall_service(instance=args.instance)
        print(f"uninstalled instance {args.instance}")
        return 0

    if args.command == "uninstall-all":
        instances = platform.installed_instances()
        if not instances:
            print("no installed instances found")
            return 0
        for inst in instances:
            platform.uninstall_service(instance=inst)
            print(f"uninstalled instance {inst}")
        return 0

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())

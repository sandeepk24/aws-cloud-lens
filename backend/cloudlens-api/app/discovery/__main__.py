"""CloudLens discovery CLI.

  python -m app.discovery collect --project P [--account A] [--collector X ...] [--region R ...]
  python -m app.discovery export  --project P [--account-id ID]
                                  --view account|vpc|application|service
                                  [--vpc-id ...] [--service ...] [--seed ...] [--app-tag K=V]
                                  [--format json --format mmd --format svg] [--out DIR]

`collect` makes read-only AWS calls with the credentials configured in the accounts
YAML. `export` reads only the local inventory database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict

from app.accounts.loader import get_accounts_config
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.discovery.runner import DiscoveryOptions, discover_account, discover_project
from app.graph.artifacts import FORMATS, write_artifacts
from app.graph.models import GraphScope
from app.graph.service import DEFAULT_MAX_NODES, build_document


def _collect(args: argparse.Namespace) -> int:
    config = get_accounts_config()
    opts = DiscoveryOptions(
        collectors=args.collector or None, regions=args.region or None, max_workers=args.max_workers
    )
    factory = get_session_factory()
    if args.account:
        reports = [discover_account(config, args.project, args.account, factory, options=opts)]
    else:
        reports = discover_project(config, args.project, factory, options=opts)
    print(
        json.dumps([asdict(r) | {"succeeded": r.succeeded} for r in reports], indent=2, default=str)
    )
    return 0 if all(r.succeeded for r in reports) else 1


def _export(args: argparse.Namespace) -> int:
    config = get_accounts_config()
    scope = GraphScope(
        project_alias=args.project,
        account_ids=args.account_id or [],
        regions=args.region or [],
        view=args.view,
        vpc_id=args.vpc_id,
        service=args.service,
        seed=args.seed,
        application_tag=args.app_tag,
        include_inferred=not args.verified_only,
        include_stale=args.include_stale,
        detail=args.detail,
    )
    factory = get_session_factory()
    with factory() as session:
        doc = build_document(session, config, scope, max_nodes=args.max_nodes)
    paths = write_artifacts(
        doc, args.out or get_settings().cloudlens_export_dir, formats=tuple(args.format or FORMATS)
    )
    for path in paths:
        print(path)
    print(
        f"nodes={doc.stats.node_count} edges={doc.stats.edge_count} "
        f"truncated={doc.stats.truncated} missing_permissions="
        f"{len(doc.coverage.missing_permissions)}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.discovery")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="Discover resources (read-only AWS calls)")
    c.add_argument("--project", required=True)
    c.add_argument("--account", help="account alias (default: all accounts in project)")
    c.add_argument("--collector", action="append")
    c.add_argument("--region", action="append")
    c.add_argument("--max-workers", type=int, default=4)
    c.set_defaults(func=_collect)

    e = sub.add_parser("export", help="Write diagrams from the local inventory")
    e.add_argument("--project", required=True)
    e.add_argument("--account-id", action="append")
    e.add_argument("--region", action="append")
    e.add_argument(
        "--view", choices=["account", "vpc", "application", "service"], default="account"
    )
    e.add_argument("--vpc-id")
    e.add_argument("--service")
    e.add_argument("--seed")
    e.add_argument("--app-tag")
    e.add_argument("--verified-only", action="store_true")
    e.add_argument("--include-stale", action="store_true")
    e.add_argument("--detail", choices=["standard", "full"], default="standard")
    e.add_argument("--max-nodes", type=int, default=DEFAULT_MAX_NODES)
    e.add_argument("--format", action="append", choices=list(FORMATS))
    e.add_argument("--out", help="override CLOUDLENS_EXPORT_DIR")
    e.set_defaults(func=_export)

    args = parser.parse_args(argv)
    logging.basicConfig(level=get_settings().log_level)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

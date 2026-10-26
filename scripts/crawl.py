#!/usr/bin/env python3
"""Run one resumable stage, or the complete component indexing pipeline."""

import argparse
import logging
import os
from pathlib import Path
import time
from indexlib.discovery import discover
from indexlib.fetching import extract, fetch
from indexlib.http import DeadlineReached, HttpClient
from indexlib.prefixes import prefixes
from indexlib.reporting import group, report
from indexlib.repositories import enumerate_artifacts, probe
from indexlib.storage import Store

STAGES = {"discover": discover, "probe": probe, "enumerate": enumerate_artifacts,
          "fetch": fetch, "extract": extract, "group": group, "prefixes": prefixes, "report": report}


def positive(value):
    result = float(value)
    if result <= 0 or not result < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return result


def run(store, client, stage, **options):
    stages = list(STAGES) if stage == "all" else [stage]
    completed = store.state.setdefault("completed", [])
    failed = False
    try:
        for name in stages:
            if stage == "all" and name in completed and name != "discover":
                continue
            client.check()
            logging.info("Stage: %s", name)
            try:
                try:
                    finished = STAGES[name](store, client, **options)
                except (OSError, ValueError) as exc:
                    if stage != "all":
                        raise
                    # One failing stage (e.g. GitHub search denied) must not block the others.
                    logging.error("Stage %s failed; progress saved, continuing: %s", name, exc)
                    failed, finished = True, False
                if stage == "all" and name == "discover" and not finished:
                    completed.clear()
                preceding = list(STAGES)[:list(STAGES).index(name)]
                if finished and stage == "all" and name not in completed and all(previous in completed for previous in preceding):
                    completed.append(name)
            finally:
                store.save(force=True)
        if stage == "all" and all(name in completed for name in STAGES):
            completed.clear()
            store.save(force=True)
        return 1 if failed else 0
    except DeadlineReached:
        logging.info("Time budget reached; progress saved for the next run")
        store.save(force=True)
        return 0
    except (OSError, ValueError) as exc:
        logging.error("Progress saved; retry on the next run: %s", exc)
        store.save(force=True)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=[*STAGES, "all"])
    parser.add_argument("--max-minutes", type=positive, default=300)
    parser.add_argument("--max-versions", type=int, default=20)
    parser.add_argument("--max-aar-mb", type=positive, default=50)
    parser.add_argument("--rules-dir", type=Path)
    args = parser.parse_args()
    if args.max_versions < 1:
        parser.error("--max-versions must be at least 1")
    if args.stage in ("all", "prefixes", "report") and args.rules_dir is None:
        parser.error("--rules-dir is required for all/prefixes/report")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = Store(Path(__file__).resolve().parent.parent)
    client = HttpClient(deadline=time.monotonic() + args.max_minutes * 60,
                        token=os.environ.get("CODE_SEARCH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    return run(store, client, args.stage, max_versions=args.max_versions,
               max_aar_mb=args.max_aar_mb, rules_dir=args.rules_dir, max_minutes=args.max_minutes)


if __name__ == "__main__":
    raise SystemExit(main())

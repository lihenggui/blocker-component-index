#!/usr/bin/env python3
"""Run one resumable stage, or the complete component indexing pipeline."""

import argparse
import logging
import os
from pathlib import Path
import time
from indexlib.discovery import bootstrap, discover
from indexlib.fetching import extract, fetch
from indexlib.http import DeadlineReached, HttpClient
from indexlib.reporting import group, report
from indexlib.repositories import enumerate_artifacts, probe
from indexlib.storage import Store

STAGES = {"discover": discover, "probe": probe, "enumerate": enumerate_artifacts,
          "fetch": fetch, "extract": extract, "group": group, "report": report}


def positive(value):
    result = float(value)
    if result <= 0 or not result < float("inf"):
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return result


def run(store, client, stage, **options):
    stages = list(STAGES) if stage == "all" else [stage]
    completed = store.state.setdefault("completed", [])
    try:
        if stage == "all" and bootstrap(store):
            completed[:] = [name for name in completed if name == "discover"]
        for name in stages:
            if stage == "all" and name in completed:
                continue
            client.check()
            logging.info("Stage: %s", name)
            finished = STAGES[name](store, client, **options)
            preceding = list(STAGES)[:list(STAGES).index(name)]
            if finished and stage == "all" and all(previous in completed for previous in preceding):
                completed.append(name)
                store.save()
        if stage == "all" and all(name in completed for name in STAGES):
            completed.clear()
            store.save()
        return 0
    except DeadlineReached:
        logging.info("Time budget reached; progress saved for the next run")
        store.save()
        return 0
    except (OSError, ValueError) as exc:
        logging.error("Progress saved; retry on the next run: %s", exc)
        store.save()
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
    if args.stage in ("all", "report") and args.rules_dir is None:
        parser.error("--rules-dir is required for all/report")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = Store(Path(__file__).resolve().parent.parent)
    client = HttpClient(deadline=time.monotonic() + args.max_minutes * 60,
                        token=os.environ.get("CODE_SEARCH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    return run(store, client, args.stage, max_versions=args.max_versions,
               max_aar_mb=args.max_aar_mb, rules_dir=args.rules_dir)


if __name__ == "__main__":
    raise SystemExit(main())

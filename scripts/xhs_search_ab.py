#!/usr/bin/env python3
"""Bounded serial search-method A/B; uses the existing account, locks and STOP."""
import argparse
import json
from pathlib import Path
import sqlite3
import uuid

import xhs_project as xp
import xhs_research as legacy
import xhs_search as search


def project_stop(project):
    database = Path(project).resolve() / "project.sqlite3"
    if not database.is_file() or database.is_symlink():
        raise legacy.ResearchError("Expected an existing research project")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM meta WHERE key='stop'").fetchone()
    if row and json.loads(row[0]):
        raise legacy.StopRun("Project has a preserved STOP; A/B cannot bypass it")


def run_ab(project, run, output, query, sorts=("latest", "popular"), repeats=2):
    if not query.strip() or not 1 <= repeats <= 2:
        raise legacy.ResearchError("Supply one query and 1..2 repetitions")
    if not sorts or any(sort not in xp.SORTS for sort in sorts) or len(set(sorts)) != len(sorts):
        raise legacy.ResearchError("Supply distinct supported sorts")
    run = legacy.load_run(run)
    # Preserve project -> run -> account lock order used by ordinary collection.
    with xp.project_lock(project), legacy.run_lock(run):
        project_stop(project)
        legacy.ensure_active(run)
        if not legacy.read_json(run / "account.json", {}).get("key"):
            raise legacy.ResearchError("A/B requires the already-authorized account binding")
        output = Path(output).resolve()
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        summary = {"format": "xiaohongshu-search-ab-v1", "query": query, "sorts": list(sorts),
                   "repeats": repeats, "records": [], "not_project_pagination": True,
                   "no_platform_recall_estimate": True, "state": "running"}
        legacy.write_json(output / "summary.json", summary)
        try:
            with legacy.client(run) as api:
                for repeat in range(repeats):
                    methods = [search.LEGACY_SEARCH_METHOD, search.SEARCH_METHOD]
                    if repeat % 2: methods.reverse()
                    for sort in sorts:
                        for method in methods:
                            project_stop(project)
                            legacy.ensure_active(run)
                            api.search_method_version = method
                            data = api.search_notes(query, page=1, sort=xp.SORTS[sort])
                            request = api.last_search_request
                            if not isinstance(request, dict) or request.get("method_version") != method:
                                raise legacy.ResearchError("A/B actual request evidence missing")
                            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                                raise legacy.ResearchError("A/B search response schema changed")
                            record = {"operation": "search_ab", "captured_at": legacy.now(),
                                      "query": query, "sort": sort, "api_sort": xp.SORTS[sort], "page": 1,
                                      "repeat": repeat + 1, "sequence": len(summary["records"]) + 1,
                                      "search_method_version": method, "search_request": request,
                                      "data": legacy.clean_data(data), "not_project_pagination": True}
                            filename = "%02d-%s.json" % (record["sequence"], uuid.uuid4().hex)
                            legacy.write_json(output / filename, record)
                            summary["records"].append(filename)
                            legacy.write_json(output / "summary.json", summary)
            summary["state"] = "complete"
        except Exception as exc:
            summary["state"] = "stopped" if isinstance(exc, legacy.StopRun) else "incomplete"
            summary["error_type"] = type(exc).__name__
            legacy.write_json(output / "summary.json", summary)
            raise
        legacy.write_json(output / "summary.json", summary)
    return {"output": str(output), "records": len(summary["records"]), "state": summary["state"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True, type=Path)
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path, help="New output directory; never production raw")
    p.add_argument("--query", required=True)
    p.add_argument("--sort", action="append", choices=list(xp.SORTS))
    p.add_argument("--repeats", type=int, default=2)
    args = p.parse_args()
    try:
        print(json.dumps(run_ab(args.project, args.run, args.out, args.query,
                                args.sort or ("latest", "popular"), args.repeats), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, legacy.ResearchError) else "Inspect local environment"}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

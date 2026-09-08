#!/usr/bin/env python3
"""
Compare monkit function_times across two Prometheus instances (or two pod sets,
two regions, etc.).

Usage: adapt HOSTS, METHODS, and the time window to your investigation, then
run. Output is one line per method with avg-ms on each side and the ratio.

Note: queries hit Prometheus directly. For the Storj datasource behind Grafana
auth, swap the query_range() body for an mcp__grafana-cloud__query_prometheus
call against uid ffqrvx0pyhp8gf instead.
"""

import urllib.parse
import urllib.request
import json
from collections import defaultdict


def query_range(host, promql, start, end, step="60"):
    url = f"http://{host}/api/v1/query_range?" + urllib.parse.urlencode({
        "query": promql, "start": start, "end": end, "step": step,
    })
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)["data"]["result"]


def summarize(series_list):
    """Aggregate all samples for the same `name` across pods, convert to ms."""
    by_method = defaultdict(list)
    for s in series_list:
        name = s["metric"].get("name", "?")
        by_method[name].extend(float(v[1]) * 1000 for v in s["values"])
    return {k: {"avg": sum(v) / len(v), "max": max(v)} for k, v in by_method.items() if v}


if __name__ == "__main__":
    HOSTS = {"a": "127.0.0.1:9090", "b": "127.0.0.1:9091"}
    METHODS = ["__Endpoint__CommitObject", "__Endpoint__BeginObject"]
    START, END = "2026-05-19T22:00:00Z", "2026-05-19T23:00:00Z"

    expr = (
        'function_times{name=~"' + "|".join(METHODS) + '",'
        'field="r99",kind="success"}'
    )

    summaries = {label: summarize(query_range(host, expr, START, END))
                 for label, host in HOSTS.items()}

    for name in METHODS:
        a = summaries["a"].get(name, {}).get("avg", 0)
        b = summaries["b"].get(name, {}).get("avg", 0)
        ratio = b / a if a else float("inf")
        print(f"{name}: a={a:.1f}ms b={b:.1f}ms ratio={ratio:.2f}x")

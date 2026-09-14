#!/usr/bin/env python3
"""deadlock.py — wait-for-graph deadlock detection + resolution (ADR 0005 §4).

Reads the `waits` table (waiter -> awaited edges), finds strongly-connected components
(Tarjan). Any SCC with >1 node, or a self-loop, is a deadlock set. Picks a victim
deterministically (the most-recently-blocked waiter — least work lost) to break the cycle.

    deadlock.py detect      # print cycles + chosen victims
    deadlock.py edge <waiter> <awaited>     # add a wait edge (for testing)
    deadlock.py clear        # clear all waits
Run with the agent-os venv python.
"""
import sys

from dbpool import connection


def _load_graph():
    edges, since = {}, {}
    with connection() as c, c.cursor() as cur:
        cur.execute("SELECT waiter, awaited, since FROM waits")
        for waiter, awaited, ts in cur.fetchall():
            edges.setdefault(waiter, []).append(awaited)
            edges.setdefault(awaited, [])
            since[waiter] = ts
    return edges, since


def _tarjan(edges):
    """Return list of SCCs (each a list of nodes)."""
    index = {}; low = {}; onstack = {}; stack = []; sccs = []; counter = [0]

    def strongconnect(v):
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); onstack[v] = True
        for w in edges.get(v, []):
            if w not in index:
                strongconnect(w); low[v] = min(low[v], low[w])
            elif onstack.get(w):
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop(); onstack[w] = False; comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for v in list(edges):
        if v not in index:
            strongconnect(v)
    return sccs


def detect():
    edges, since = _load_graph()
    # self-loops count as deadlock too
    selfloops = [v for v in edges if v in edges.get(v, [])]
    cycles = [c for c in _tarjan(edges) if len(c) > 1] + [[v] for v in selfloops]
    if not cycles:
        print("no deadlock (wait-for graph is acyclic)")
        return []
    out = []
    for cyc in cycles:
        # victim = most-recently-blocked waiter in the cycle (least work to lose)
        victim = max(cyc, key=lambda n: since.get(n) or 0)
        print(f"DEADLOCK: cycle {cyc} -> break by aborting victim '{victim}'")
        out.append({"cycle": cyc, "victim": victim})
    return out


def _main(argv):
    if not argv or argv[0] == "detect":
        sys.exit(1 if detect() else 0)
    if argv[0] == "edge":
        with connection() as c, c.cursor() as cur:
            cur.execute("INSERT INTO waits(waiter,awaited) VALUES(%s,%s) ON CONFLICT DO NOTHING", (argv[1], argv[2]))
        print(f"edge {argv[1]} -> {argv[2]}")
    elif argv[0] == "clear":
        with connection() as c, c.cursor() as cur:
            cur.execute("TRUNCATE waits")
        print("cleared waits")


if __name__ == "__main__":
    _main(sys.argv[1:])

#!/usr/bin/env python3
"""Hand-validate the dependency cone against real Verilator output.

Run this before wiring cones into the repair prompt. It answers the only
question that matters: does the cone for a mismatching output actually
contain the line that is wrong?

    python tools/cone_check.py <rtl.v> [--top TopModule]
    python tools/cone_check.py <rtl.v> --signal q
    python tools/cone_check.py <rtl.v> --sim-log sim.txt
    python tools/cone_check.py <rtl.v> --dump-keys

--dump-keys prints the child key names Verilator actually used, which is
the one thing the unit tests cannot confirm from a hand-written sample.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtlforge.astdump import dump_ast                       # noqa: E402
from rtlforge.cone import (                                 # noqa: E402
    backward_cone,
    build_graph,
    parse_mismatch_hints,
    render_cone,
)


def dump_keys(tree: dict) -> None:
    """Report which child keys and node types the real AST uses."""
    keys: Counter[str] = Counter()
    types: Counter[str] = Counter()
    assign_keys: Counter[tuple[str, ...]] = Counter()

    def walk(node: dict) -> None:
        types[node.get("type", "?")] += 1
        child_keys = []
        for key, value in node.items():
            kids = None
            if isinstance(value, list) and value and all(
                    isinstance(v, dict) and "type" in v for v in value):
                kids = value
            elif isinstance(value, dict) and "type" in value:
                kids = [value]
            if kids is None:
                continue
            keys[key] += 1
            child_keys.append(key)
            for kid in kids:
                walk(kid)
        if node.get("type", "").startswith("ASSIGN"):
            assign_keys[tuple(child_keys)] += 1

    walk(tree)
    print("node types:")
    for t, n in types.most_common():
        print(f"  {t:16s} {n}")
    print("\nchild keys:")
    for k, n in keys.most_common():
        print(f"  {k:16s} {n}")
    print("\nassignment child key orders (first key should be the RHS):")
    for order, n in assign_keys.most_common():
        print(f"  {' , '.join(order):34s} {n}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rtl")
    ap.add_argument("--top", default=None)
    ap.add_argument("--signal", action="append", default=[],
                    help="output to walk back from; repeatable")
    ap.add_argument("--sim-log", default=None,
                    help="simulation output to take 'Hint: Output ...' from")
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--context", type=int, default=0,
                    help="source lines of context around each suspect line")
    ap.add_argument("--dump-keys", action="store_true")
    ap.add_argument("--save-ast", default=None)
    args = ap.parse_args()

    with open(args.rtl) as fh:
        source = fh.read()

    result = dump_ast(source, top=args.top)
    if not result.available:
        print(f"AST unavailable: {result.note}", file=sys.stderr)
        if result.raw:
            print(result.raw[:2000], file=sys.stderr)
        return 2

    if args.save_ast:
        with open(args.save_ast, "w") as fh:
            json.dump(result.tree, fh, indent=1)
        print(f"AST written to {args.save_ast}\n")

    if args.dump_keys:
        dump_keys(result.tree)
        print()

    graph = build_graph(result.tree, module=args.top)
    print(f"module: {graph.module or '(none found)'}")
    print(f"ports: {graph.ports}")
    print(f"sequential: {sorted(graph.sequential)}")
    print(f"undriven: {sorted(graph.unknown)}")
    print(f"unread inputs: {sorted(graph.unread_inputs)}")
    print(f"instances: {graph.instances}")
    print(f"positional LHS/RHS guess: {graph.positional}")
    if graph.warning:
        print(f"\n*** {graph.warning} ***")
        print("*** the cone below points at the wrong lines; fix the "
              "key mapping in cone.py before trusting it ***\n")
    else:
        print("orientation check: ok")

    targets = list(args.signal)
    if args.sim_log:
        with open(args.sim_log) as fh:
            hints = parse_mismatch_hints(fh.read())
        if not hints:
            print("\nno 'Hint: Output ...' lines in the sim log")
        for hint in hints:
            print(f"\nhint: {hint.signal} wrong {hint.count}x"
                  + (f", first at t={hint.first_time}"
                     if hint.first_time is not None else ""))
            targets.append(hint.signal)
    if not targets:
        targets = graph.outputs()

    seen = set()
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        print()
        cone = backward_cone(graph, target, max_depth=args.depth)
        print(render_cone(cone, graph, source=source, context=args.context))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

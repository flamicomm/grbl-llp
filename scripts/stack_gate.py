#!/usr/bin/env python3
"""Static stack gate for the AVR build (STK, CNC-029 Ola 3).

Why this exists
---------------
The real stack peak of the firmware is only measurable on the bench (the
keep-alive reports it as `pk=`). This script gives a *host-side* gate that fails
before flashing when the worst case no longer fits in RAM.

How it works
------------
1. Every translation unit of the measurement env writes a GCC `.su` file
   (`-fstack-usage`, see `env:uno_stack` in `platformio.ini`). `.su` gives the
   fixed frame size of each function.
2. The call graph is read from the linked ELF disassembly: direct `call`,
   `rcall` and `jmp` (tail call) edges.
3. The worst path is the longest weighted path from each root, where roots are
   `main`/`__vectors` (main context) and every `__vector_N` (interrupt
   context). AVR clears the I flag on interrupt entry, so interrupts do not
   nest: the worst case is the deepest main-context path plus the deepest
   interrupt path, since an interrupt can land on the deepest main frame.
4. The gate fails when `worst_stack + .data + .bss > 2048 - 200` (1848 B), the
   margin pinned by docs/plans/CNC-029-grbl-llp.md.

Known limits (deliberate, documented, cross-checked on the bench)
----------------------------------------------------------------
- LTO must be off in the measurement env: GCC writes empty `.su` files when
  LTO is enabled (frames are only finalized in the LTRANS stage). Frame sizes
  therefore come from a non-LTO build and are an estimate, not the exact
  production layout. Compare with `pk=` on the bench; diverging more than 20 %
  means the gate needs revisiting.
- Indirect calls (`icall`/`eicall`) cannot be resolved statically. Every
  reachable site adds `INDIRECT_SLACK` bytes so the estimate stays
  conservative; the sites are listed in the output.
- A function called from both contexts is accounted in whichever path is
  deeper; the graph is acyclic (recursion is forbidden on this target) and a
  cycle is a hard error.
- `.noinit` (`system.c` diagnostics) is *below* the stack and is not part of
  the plan's formula; it is reported for information only.

Usage
-----
    pio run -e uno_stack
    python3 scripts/stack_gate.py

Exit codes: 0 = fits, 1 = gate exceeded, 2 = analysis error.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import subprocess
import sys

DEFAULT_LIMIT = 2048 - 200  # 1848 B, pinned by the plan (M3 of the review).
INDIRECT_SLACK = 8  # Call frame + typical sink behind an `icall`.

RE_FUNC_HDR = re.compile(r"^([0-9a-f]+) <(.+)>:$")
RE_SU = re.compile(r"^(?P<file>.*):(?P<line>\d+):(?P<col>\d+):(?P<func>[^\t]+)\t"
                   r"(?P<size>\d+)\t(?P<qual>[^\t]+)$")
RE_TARGET_ADDR = re.compile(r";\s*0x([0-9a-f]+)")


def toolchain_bin(explicit: str | None) -> str:
    """Directory with avr-objdump/avr-size."""
    if explicit:
        return explicit
    env_dir = os.environ.get("AVR_TOOLCHAIN_BIN")
    if env_dir:
        return env_dir
    default = os.path.expanduser("~/.platformio/packages/toolchain-atmelavr/bin")
    if os.path.isdir(default):
        return default
    sys.exit("stack_gate: avr toolchain not found; pass --toolchain")


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def norm_name(name: str) -> str:
    """Normalize a symbol to the bare C name (`void foo()` -> `foo`)."""
    name = name.split("(")[0].strip()
    if name.startswith("void "):
        name = name[len("void "):]
    return name


def parse_su(su_dir: str) -> tuple[dict[str, int], dict[str, str]]:
    """Frame size per function name; duplicates keep the largest (conservative).

    Also returns the non-static (`dynamic`, `dynamic,bounded`) qualifiers, which
    make the static estimate a lower bound.
    """
    frames: dict[str, int] = {}
    qualifiers: dict[str, str] = {}
    for root, _dirs, files in os.walk(su_dir):
        for name in sorted(files):
            if not name.endswith(".su"):
                continue
            with open(os.path.join(root, name), encoding="utf-8",
                      errors="replace") as handle:
                for line in handle:
                    match = RE_SU.match(line.rstrip("\n"))
                    if not match:
                        continue
                    func = norm_name(match.group("func"))
                    size = int(match.group("size"))
                    qual = match.group("qual").strip()
                    if qual != "static":
                        qualifiers[func] = qual
                    frames[func] = max(frames.get(func, 0), size)
    return frames, qualifiers


def parse_calls(disassembly: str) -> tuple[dict[str, set[str]],
                                           dict[str, int],
                                           dict[str, set[int]]]:
    """Call graph from the disassembly.

    Targets are resolved to the function that contains the target address, so a
    `jmp` to an internal label collapses into a self-edge (dropped) and a tail
    call to another function becomes an edge. Returns (edges, function
    addresses, indirect sites per caller).
    """
    edges: dict[str, set[str]] = {}
    present: dict[str, int] = {}
    indirect: dict[str, set[int]] = {}
    raw_edges: list[tuple[str, int]] = []
    current: str | None = None
    for line in disassembly.splitlines():
        header = RE_FUNC_HDR.match(line)
        if header:
            current = norm_name(header.group(2))
            present[current] = int(header.group(1), 16)
            edges.setdefault(current, set())
            continue
        if current is None:
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        mnemonic = fields[2].split()[0] if fields[2].strip() else ""
        if mnemonic in ("call", "rcall", "jmp"):
            target = RE_TARGET_ADDR.search(line)
            if target:
                raw_edges.append((current, int(target.group(1), 16)))
        elif mnemonic in ("icall", "eicall", "ijmp", "eijmp"):
            indirect.setdefault(current, set()).add(int(fields[0].strip().rstrip(":"), 16))

    starts = sorted((address, name) for name, address in present.items())
    addresses = [address for address, _name in starts]

    def owner(address: int) -> str | None:
        index = bisect.bisect_right(addresses, address) - 1
        if index < 0:
            return None
        return starts[index][1]

    for caller, address in raw_edges:
        callee = owner(address)
        if callee is None or callee == caller:
            continue
        edges[caller].add(callee)
    return edges, present, indirect


def section_sizes(size_tool: str, elf: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for line in run([size_tool, "-A", elf]).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            sizes[parts[0]] = int(parts[1])
    return sizes


def reachable_from(edges: dict[str, set[str]], roots: list[str]) -> set[str]:
    """Every function reachable from the roots (cycles allowed, no recursion)."""
    seen: set[str] = set()
    pending = [root for root in roots if root in edges]
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(child for child in edges.get(node, ()) if child not in seen)
    return seen


def worst_path(edges: dict[str, set[str]], frames: dict[str, int],
               roots: list[str], indirect: dict[str, set[int]],
               cut_edges: set[tuple[str, str]]) \
        -> tuple[int, list[str], int, set[str]]:
    """Longest weighted path from any root.

    Edges that would close a cycle are cut and recorded: on this target the
    firmware is not recursive (grbl's `delay_sec` deliberately calls
    `protocol_exec_rt_system()` in suspend mode "to avoid nesting suspend
    loops"), so the static cycle is not a runtime path. The cut edges are
    printed so a real recursion bug cannot hide.

    Returns (size, path, indirect_sites, unknown_functions).
    """
    best: dict[str, tuple[int, list[str], int, set[str]]] = {}
    on_stack: set[str] = set()

    def visit(node: str) -> tuple[int, list[str], int, set[str]]:
        if node in best:
            return best[node]
        on_stack.add(node)
        tail = (0, [], 0, set())
        for child in sorted(edges.get(node, ())):
            if child in on_stack:
                cut_edges.add((node, child))
                continue
            candidate = visit(child)
            if candidate[0] > tail[0]:
                tail = candidate
        on_stack.discard(node)
        unknown = set() if node in frames else {node}
        result = (frames.get(node, 0) + tail[0],
                  [node] + tail[1],
                  len(indirect.get(node, ())) + tail[2],
                  unknown | tail[3])
        best[node] = result
        return result

    size, path, sites, unknown = 0, [], 0, set()
    for root in roots:
        if root not in edges:
            continue
        candidate = visit(root)
        if candidate[0] > size:
            size, path, sites, unknown = candidate
    return size, path, sites, unknown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-dir", default=".pio/build/uno_stack",
                        help="measurement build with the .su files")
    parser.add_argument("--data-elf", default=".pio/build/uno/firmware.elf",
                        help="production ELF whose .data/.bss feed the formula")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"stack + .data + .bss budget (default {DEFAULT_LIMIT})")
    parser.add_argument("--toolchain", default=None,
                        help="directory with avr-objdump/avr-size")
    parser.add_argument("--strict-cycles", action="store_true",
                        help="fail when the call graph has a cycle")
    parser.add_argument("--verbose", action="store_true",
                        help="print indirect call sites")
    args = parser.parse_args()

    bin_dir = toolchain_bin(args.toolchain)
    objdump = os.path.join(bin_dir, "avr-objdump")
    size_tool = os.path.join(bin_dir, "avr-size")

    stack_elf = os.path.join(args.build_dir, "firmware.elf")
    for required in (stack_elf, args.data_elf):
        if not os.path.isfile(required):
            print(f"stack_gate: missing {required}", file=sys.stderr)
            return 2
    if not os.path.isdir(args.build_dir):
        print(f"stack_gate: missing {args.build_dir}", file=sys.stderr)
        return 2

    frames, qualifiers = parse_su(args.build_dir)
    if not frames:
        print(f"stack_gate: no .su data under {args.build_dir}; "
              "build it first (pio run -e uno_stack)", file=sys.stderr)
        return 2
    disassembly = run([objdump, "-d", stack_elf])
    edges, present, indirect = parse_calls(disassembly)

    isr_roots = [name for name in present if re.fullmatch(r"__vector_\d+", name)]
    main_roots = [name for name in ("main", "__vectors") if name in present]

    cut_edges: set[tuple[str, str]] = set()
    main_size, main_path, main_sites, main_unknown = worst_path(
        edges, frames, main_roots, indirect, cut_edges)
    isr_size, isr_path, isr_sites, isr_unknown = worst_path(
        edges, frames, isr_roots, indirect, cut_edges)

    indirect_count = main_sites + isr_sites
    if main_size == 0 or isr_size == 0:
        print("stack_gate: no roots found in the call graph "
              f"({', '.join(main_roots) or 'main'} / vectors); wrong ELF?",
              file=sys.stderr)
        return 2
    # An interrupt lands on the deepest main-context frame; interrupts do not
    # nest themselves (AVR clears I on entry).
    static_peak = main_size + isr_size + indirect_count * INDIRECT_SLACK

    sizes = section_sizes(size_tool, args.data_elf)
    data_bss = sizes.get(".data", 0) + sizes.get(".bss", 0)
    noinit = sizes.get(".noinit", 0)
    total = static_peak + data_bss

    def show(label: str, size: int, path: list[str]) -> None:
        print(f"{label}: {size} B")
        if path:
            steps = [f"{name}({frames.get(name, '?')})" for name in path]
            print("  " + " -> ".join(steps))

    print(f"stack_gate: build={args.build_dir} data={args.data_elf}")
    print(f"symbols: linked {len(present)}, with .su frame {len(frames)}")
    show("worst main-context path", main_size, main_path)
    show("worst interrupt path", isr_size, isr_path)
    print(f"indirect call sites on those paths: {indirect_count} "
          f"(+{INDIRECT_SLACK} B each)")
    print(f"static stack peak: {static_peak} B")
    print(f".data + .bss: {data_bss} B  (.noinit {noinit} B, below the stack, "
          f"not in the formula)")
    print(f"total: {total} B / {args.limit} B ({args.limit - total} B free)")
    if cut_edges:
        print("cycles cut (not reachable at runtime, see docstring): "
              + ", ".join(f"{a}->{b}" for a, b in sorted(cut_edges)))
    if args.verbose and indirect:
        for site, addresses in sorted(indirect.items()):
            print(f"  icall in {site} at " + ", ".join(f"0x{a:04x}" for a in sorted(addresses)))

    unknown = main_unknown | isr_unknown
    if unknown:
        print(f"functions without .su frame on the worst path ({len(unknown)}): "
              + ", ".join(sorted(unknown)) + "  (assumed 0 B)")

    reachable = reachable_from(edges, main_roots + isr_roots)
    dynamic = {name: qual for name, qual in qualifiers.items() if name in reachable}
    unreachable_dynamic = sorted(set(qualifiers) - set(dynamic))
    if unreachable_dynamic:
        print("non-static frames outside the call graph (dead code, not counted): "
              + ", ".join(unreachable_dynamic))
    for name, qual in sorted(dynamic.items()):
        print(f"stack_gate: FAIL reachable non-static frame: {name} ({qual})",
              file=sys.stderr)
    if dynamic:
        return 2
    if args.strict_cycles and cut_edges:
        print("stack_gate: FAIL call graph has a cycle (--strict-cycles)",
              file=sys.stderr)
        return 2
    if total > args.limit:
        print(f"stack_gate: FAIL {total} B > {args.limit} B", file=sys.stderr)
        return 1
    print("stack_gate: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

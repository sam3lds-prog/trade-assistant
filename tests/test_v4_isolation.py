"""4.0: the learning engine cannot reach an order path.

tradeassist/intelligence reads market data only through a function it is handed and never imports a module that can
create, send, approve or fill an order, or a data provider or the app's service. Checked two ways: statically (every
import in every module of the package, function-local ones included, followed transitively through the app) and by
importing the whole package in a fresh interpreter and looking at what got loaded.
"""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "tradeassist"

FORBIDDEN = {
    "tradeassist.v3.trading", "tradeassist.v3.drafts", "tradeassist.brokers", "tradeassist.robinhood_exec",
    "tradeassist.robinhood_account", "tradeassist.paper", "tradeassist.autopilot", "tradeassist.desk", "tradeassist.policy",
    "tradeassist.riskengine", "tradeassist.reconcile", "tradeassist.remote", "tradeassist.allocator", "tradeassist.service",
    "tradeassist.market", "tradeassist.api", "tradeassist.api_v2", "tradeassist.api_v3", "tradeassist.daemon",
    "tradeassist.providers.robinhood", "tradeassist.providers.mcp_bridge", "tradeassist.v4",
    "tradeassist.providers", "tradeassist.providers.tradier", "tradeassist.providers.demo", "tradeassist.providers.base",
}


def _module_of(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


MODULES = {_module_of(p): p for p in PKG.rglob("*.py")}


def _imports(mod: str) -> set[str]:
    path = MODULES[mod]
    is_pkg = path.name == "__init__.py"
    base = mod.split(".") if is_pkg else mod.split(".")[:-1]
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join(anchor + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            out.add(target)
            for a in node.names:
                out.add(f"{target}.{a.name}")
    return {m for m in out if m in MODULES}


def _closure(start: set[str]) -> dict[str, str]:
    """Every module reachable from `start`, with the module that first imported it."""
    seen: dict[str, str] = {m: "" for m in start}
    todo = list(start)
    while todo:
        m = todo.pop()
        # importing a.b.c runs a/__init__.py and a/b/__init__.py first
        parents = {".".join(m.split(".")[:i]) for i in range(1, len(m.split(".")))}
        for dep in (_imports(m) | {p for p in parents if p in MODULES}):
            if dep not in seen:
                seen[dep] = m
                todo.append(dep)
    return seen


def test_no_module_of_the_engine_can_reach_an_order_path_or_a_data_source():
    engine = {m for m in MODULES if m.startswith("tradeassist.intelligence")}
    assert len(engine) > 15
    reach = _closure(engine)
    bad = sorted(m for m in reach if m in FORBIDDEN)
    chains = []
    for m in bad:
        chain, cur = [m], reach[m]
        while cur:
            chain.append(cur)
            cur = reach[cur]
        chains.append(" <- ".join(chain))
    assert not bad, "the engine reaches: " + "; ".join(chains)


def test_importing_the_engine_loads_nothing_that_can_trade():
    code = ("import sys, pkgutil, importlib, tradeassist.intelligence as I\n"
            "for m in pkgutil.walk_packages(I.__path__, 'tradeassist.intelligence.'):\n"
            "    importlib.import_module(m.name)\n"
            "print('\\n'.join(sorted(k for k in sys.modules if k.startswith('tradeassist'))))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, check=True).stdout
    loaded = set(out.split())
    assert "tradeassist.intelligence.replay.runner" in loaded
    assert not (loaded & FORBIDDEN), sorted(loaded & FORBIDDEN)


def test_the_closure_walker_would_notice_a_forbidden_import():
    # a sanity check on the test itself: the autopilot does reach the Trading Desk and the live broker
    reach = _closure({"tradeassist.autopilot"})
    assert "tradeassist.v3.trading" in reach and "tradeassist.robinhood_exec" in reach

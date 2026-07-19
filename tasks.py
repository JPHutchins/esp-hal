# /// script
# requires-python = ">=3.11"
# dependencies = ["camas[mcp]>=0.1.27"]
# ///
"""esp-hal tasks — the camas SSOT for local dev, CI, and agents.

Monorepo root: each crate carries a thin ``tasks.py`` (a Project over the shared
engine in ``camas_shared.py``) that computes its own check/clippy from its
``[package.metadata.espressif]`` config. Crates are auto-discovered — dropping a
``tasks.py`` into a crate mounts it (``camas esp-hal.check``) and folds it into
``ci`` below.

check and clippy are camas-native (direct cargo). fmt, docs, doc-tests and
host-tests stay ``cargo xtask`` leaves — that detailed tooling (the doc-site
generator, the example-metadata builder) is left in Rust on purpose.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import camas_shared as cs
from camas import Claude, Config, Parallel, Project, Task, run_cli

_ROOT = Path(__file__).parent
CRATES = [
    p.name
    for p in sorted(_ROOT.iterdir())
    if p.is_dir() and p.name.startswith(("esp-", "xtensa-")) and (p / "tasks.py").exists()
]
_projects = {c: Project(c) for c in CRATES}
for _c, _p in _projects.items():
    globals()[_c] = _p

_RISCV = tuple(c for c in cs.chips() if c not in cs.XTENSA)

fmt = Task("cargo xtask fmt-packages", mutates=True)
fmt_check = Task("cargo xtask fmt-packages --check")
metadata = Task("cargo xtask update-metadata --check")
host_tests = Task("cargo xtask host-tests")
docs = Parallel(
    Task(f"cargo xtask build documentation --chips {','.join(cs.XTENSA)}"),
    Task(f"cargo xtask build documentation --chips {','.join(_RISCV)}"),
)
doc_tests = Parallel(Task("cargo xtask run doc-tests {CHIP}"), matrix={"CHIP": cs.chips()})

for _chip in cs.chips():
    if _cc := cs.chip_check(_chip):
        globals()[f"check_{_chip}"] = Parallel(*_cc)
    if _lc := cs.chip_clippy(_chip):
        globals()[f"clippy_{_chip}"] = Parallel(*_lc)

chips = Parallel(Task("true"), matrix={"CHIP": cs.chips()})
gate = Task("cargo check --manifest-path esp-config/Cargo.toml --no-default-features")
ci = Parallel(*_projects.values(), fmt_check, metadata, host_tests, docs, doc_tests)

_ = Config(default_task=gate, github_task=ci, agent=Claude(fix=fmt, check=gate))

if __name__ == "__main__":
    run_cli(globals())

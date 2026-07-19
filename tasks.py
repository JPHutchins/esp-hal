# /// script
# requires-python = ">=3.11"
# dependencies = ["camas[mcp]>=0.1.27"]
# ///
"""esp-hal tasks — the camas SSOT for local dev, CI, and agents.

camas owns the one axis this repo hand-unrolled everywhere: the chip list, read
straight from ``esp-metadata/devices`` so it can never drift. Every leaf is the
existing ``cargo xtask``/``cargo xcheck`` engine — camas orchestrates it, it is
not reimplemented here. Running a task reproduces the matching CI job exactly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from camas import Claude, Config, Parallel, Task, run_cli

ROOT = Path(__file__).parent
XTENSA = ("esp32", "esp32s2", "esp32s3")


def chips() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in (ROOT / "esp-metadata" / "devices").iterdir() if p.is_dir()))


def msrv() -> str:
    return tomllib.loads((ROOT / "esp-hal" / "Cargo.toml").read_text())["package"]["rust-version"]


CHIPS = chips()
RISCV = tuple(c for c in CHIPS if c not in XTENSA)

check = Parallel(Task("cargo xcheck ci {CHIP} --steps check"), matrix={"CHIP": CHIPS})
doc_tests = Parallel(Task("cargo xtask run doc-tests {CHIP}"), matrix={"CHIP": CHIPS})

clippy_xtensa = Task(f"cargo xtask lint-packages --chips {','.join(XTENSA)} --toolchain esp")
clippy_riscv = Task(f"cargo xtask lint-packages --chips {','.join(RISCV)} --toolchain {msrv()}")
clippy = Parallel(clippy_xtensa, clippy_riscv)

docs_xtensa = Task(f"cargo xtask build documentation --chips {','.join(XTENSA)}")
docs_riscv = Task(f"cargo xtask build documentation --chips {','.join(RISCV)}")
docs = Parallel(docs_xtensa, docs_riscv)

fmt = Task("cargo xtask fmt-packages", mutates=True)
fmt_check = Task("cargo xtask fmt-packages --check")
metadata = Task("cargo xtask update-metadata --check")
host_tests = Task("cargo xtask host-tests")

gate = Task("cargo xcheck ci esp32c6 --steps check")
ci = Parallel(check, clippy, doc_tests, docs, fmt_check, metadata, host_tests)

_ = Config(default_task=gate, github_task=ci, agent=Claude(fix=fmt, check=gate))

if __name__ == "__main__":
    run_cli(globals())

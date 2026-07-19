"""Shared camas engine for the esp-hal monorepo.

Each crate's ``tasks.py`` is a thin ``Project`` that calls into here; this module
computes a crate's check/clippy leaves from its own ``[package.metadata.espressif]``
config and the per-chip capability model in ``esp-metadata/devices``. Functions
(not module-level snapshots) so a re-exec by the MCP server stays fresh.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from camas import Config, Parallel, Task

ROOT = Path(__file__).parent
XTENSA = ("esp32", "esp32s2", "esp32s3")
LP_CHIPS = ("esp32c6", "esp32s2", "esp32s3")
_TOKENS = re.compile(r"\(|\)|&&|\|\||!|[A-Za-z_][A-Za-z0-9_]*")


def chips() -> tuple[str, ...]:
    return tuple(sorted(p.name for p in (ROOT / "esp-metadata" / "devices").iterdir() if p.is_dir()))


def msrv() -> str:
    return tomllib.loads((ROOT / "esp-hal" / "Cargo.toml").read_text())["package"]["rust-version"]


def _soc(chip: str) -> str:
    return (ROOT / "esp-metadata" / "devices" / chip / "soc.toml").read_text()


def _symbols(chip: str) -> frozenset[str]:
    text = _soc(chip)
    syms = {chip}
    if arch := re.search(r'^arch\s*=\s*"(\w+)"', text, re.M):
        syms.add(arch.group(1))
    syms |= {f"{d}_driver_supported" for d in re.findall(r"^\[device\.([a-z0-9_]+)\]", text, re.M)}
    if re.search(r'name\s*=\s*"USB_DEVICE"', text):
        syms.add("soc_has_usb_device")
    if re.search(r"^\s*csi_supported\s*=\s*true", text, re.M):
        syms.add("wifi_csi_supported")
    return frozenset(syms)


def _target(chip: str) -> str:
    m = re.search(r'^target\s*=\s*"([^"]+)"', _soc(chip), re.M)
    assert m is not None
    return m.group(1)


def _truth(expr: str, syms: frozenset[str]) -> bool:
    toks, i = _TOKENS.findall(expr), 0

    def orx() -> bool:
        nonlocal i
        v = andx()
        while i < len(toks) and toks[i] == "||":
            i += 1
            v = andx() or v
        return v

    def andx() -> bool:
        nonlocal i
        v = notx()
        while i < len(toks) and toks[i] == "&&":
            i += 1
            v = notx() and v
        return v

    def notx() -> bool:
        nonlocal i
        if i < len(toks) and toks[i] == "!":
            i += 1
            return not notx()
        t = toks[i]
        i += 1
        if t == "(":
            v = orx()
            i += 1
            return v
        return t in syms

    return orx()


def _cases(raw: list[dict], syms: frozenset[str]) -> list[tuple[list[str], dict[str, str]]]:
    out = []
    for case in raw:
        if "if" in case and not _truth(case["if"], syms):
            continue
        features, env = list(case.get("features", [])), dict(case.get("env", {}))
        for app in case.get("append", []):
            if "if" in app and not _truth(app["if"], syms):
                continue
            features += app.get("features", [])
            env |= app.get("env", {})
        out.append((features, env))
    return out


def _manifest(crate: str) -> dict:
    return tomllib.loads((ROOT / crate / "Cargo.toml").read_text())


def _espressif(crate: str) -> dict:
    return _manifest(crate).get("package", {}).get("metadata", {}).get("espressif", {})


def _chip_features(crate: str) -> bool:
    meta = _espressif(crate)
    if "has_chip_features" in meta:
        return bool(meta["has_chip_features"])
    return any(f in chips() for f in _manifest(crate).get("features", {}))


def _valid(crate: str, chip: str) -> bool:
    meta = _espressif(crate)
    if meta.get("targets_lp_core") and chip not in LP_CHIPS:
        return False
    if _chip_features(crate) and chip not in _manifest(crate).get("features", {}):
        return False
    req = meta.get("requires_target")
    return req is None or _target(chip) in req


def _leaf_target(crate: str, chip: str) -> str:
    if _espressif(crate).get("targets_lp_core"):
        return "riscv32imac-unknown-none-elf" if chip in ("esp32c5", "esp32c6") else "riscv32imc-unknown-none-elf"
    return _target(chip)


def _on_host(crate: str, features: list[str]) -> bool:
    return crate in ("esp-config", "esp-metadata") or (crate == "esp-metadata-generated" and "build-script" in features)


def _leaf(sub: str, crate: str, chip: str, features: list[str], base: dict[str, str], env: dict[str, str], root: bool = False) -> Task:
    host, xtensa = _on_host(crate, features), chip in XTENSA
    toolchain = ("esp" if xtensa else msrv()) if sub == "clippy" else ("esp" if xtensa and not host else None)
    cmd = ["cargo"]
    if toolchain:
        cmd.append(f"+{toolchain}")
    cmd.append(sub)
    if root:
        cmd += ["--manifest-path", f"{crate}/Cargo.toml"]
    if not host:
        cmd.append(f"--target={_leaf_target(crate, chip)}")
    if toolchain == "esp" and not host:
        cmd.append("-Zbuild-std=core,alloc")
    cmd.append("--no-default-features")
    if features:
        cmd.append(f"--features={','.join(features)}")
    if sub == "clippy":
        cmd += ["--", "-D", "warnings", "--no-deps"]
    return Task(tuple(cmd), env={**base, **env}, name=f"{crate} {'+'.join(features) or '-'}")


def _groups(crate: str, sub: str, key: str, base: dict[str, str], default_empty: bool) -> dict[str, Parallel]:
    out = {}
    raw_all = _espressif(crate).get(key)
    for chip in chips():
        if not _valid(crate, chip):
            continue
        cases = _cases(raw_all, _symbols(chip)) if raw_all else []
        if not cases and default_empty:
            cases = [([], {})]
        leaves = [
            _leaf(sub, crate, chip, features + ([chip] if _chip_features(crate) else []), base, env)
            for features, env in cases
        ]
        if leaves:
            out[chip] = Parallel(*leaves, name=chip)
    return out


def check_groups(crate: str) -> dict[str, Parallel]:
    return _groups(crate, "check", "check-configs", {"CI": "1", "DEFMT_LOG": "trace", "ESP_LOG": "trace"}, True)


def clippy_groups(crate: str) -> dict[str, Parallel]:
    return _groups(crate, "clippy", "clippy-configs", {"CI": "1", "DEFMT_LOG": "trace"}, False)


def crates() -> list[str]:
    out = []
    for p in sorted(ROOT.iterdir()):
        if p.is_dir() and p.name.startswith(("esp-", "xtensa-")) and (p / "Cargo.toml").exists():
            m = tomllib.loads((p / "Cargo.toml").read_text())
            if m.get("package", {}).get("publish", True) is not False:
                out.append(p.name)
    return out


def _chip_leaves(chip: str, sub: str, key: str, base: dict[str, str], default_empty: bool) -> list[Task]:
    leaves = []
    for crate in crates():
        if not _valid(crate, chip):
            continue
        raw = _espressif(crate).get(key)
        cases = _cases(raw, _symbols(chip)) if raw else []
        if not cases and default_empty:
            cases = [([], {})]
        for features, env in cases:
            full = features + ([chip] if _chip_features(crate) else [])
            leaves.append(_leaf(sub, crate, chip, full, base, env, root=True))
    return leaves


def chip_check(chip: str) -> list[Task]:
    return _chip_leaves(chip, "check", "check-configs", {"CI": "1", "DEFMT_LOG": "trace", "ESP_LOG": "trace"}, True)


def chip_clippy(chip: str) -> list[Task]:
    return _chip_leaves(chip, "clippy", "clippy-configs", {"CI": "1", "DEFMT_LOG": "trace"}, False)


def setup(file: str, ns: dict) -> None:
    """Bind this crate's check/clippy tasks + Config into its ``tasks.py`` namespace."""
    crate = Path(file).parent.name
    ns["check"] = Parallel(*check_groups(crate).values())
    clippy = clippy_groups(crate)
    if clippy:
        ns["clippy"] = Parallel(*clippy.values())
        ns["dev"] = Parallel(ns["check"], ns["clippy"])
        ns["_camas"] = Config(default_task=ns["dev"])
    else:
        ns["_camas"] = Config(default_task=ns["check"])

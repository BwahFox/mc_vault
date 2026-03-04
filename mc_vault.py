#!/usr/bin/env python3
"""
MC Vault v0.3.1 — Cross-platform GUI tool for backing up and restoring
Minecraft Java worlds for PrismLauncher instances.

Single-file architecture. See DESIGN.md for full specification.

Requires:
  - Python 3.9+
  - rclone installed and configured (for Rclone backend)
  - tkinter (ships with most Python installations)

Environment variables (optional overrides):
  REMOTE  — rclone remote root (default: gdrive:MinecraftVault)
  RCLONE  — rclone executable path (default: rclone or ~/.bin/rclone/rclone)
"""

# =============================================================================
# Constants / Defaults
# =============================================================================

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

APP_NAME = "MC Vault"
APP_VERSION = "0.3.1"
CONFIG_VERSION = 1

REMOTE_DEFAULT = os.environ.get("REMOTE", "gdrive:MinecraftVault")

_candidate_rclone = Path.home() / ".bin" / "rclone" / "rclone"
RCLONE_DEFAULT = os.environ.get(
    "RCLONE",
    str(_candidate_rclone) if _candidate_rclone.exists() else "rclone",
)

DH_FILES = (
    "DistantHorizons.sqlite",
    "DistantHorizons.sqlite-wal",
    "DistantHorizons.sqlite-shm",
)

KEEP_DEFAULT = 3


# =============================================================================
# Utility Functions
# =============================================================================

def is_windows() -> bool:
    return os.name == "nt"


def utc_now_iso() -> str:
    """Return current UTC time as a compact ISO-8601 string ending in Z."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def local_timestamp() -> str:
    """Filename-safe local timestamp: 2026-02-28_14-30-00"""
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def parse_iso_utc(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 string (with optional trailing Z) into a UTC datetime."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError):
        return None


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def safe_unlink(p: Path) -> None:
    """Delete a file, ignoring errors if it doesn't exist."""
    try:
        p.unlink(missing_ok=True)
    except TypeError:
        # Python 3.7 fallback (missing_ok added in 3.8)
        if p.exists():
            p.unlink()


def format_size(n: int) -> str:
    """Human-readable file size."""
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024.0 or unit == "TB":
            return f"{int(x)} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024.0
    return f"{n} B"


def run_cmd(cmd: List[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command, optionally capturing combined stdout+stderr."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def stream_cmd(
    cmd: List[str],
    log: Callable[[str], None],
    clear: Optional[Callable[[], None]] = None,
) -> int:
    """
    Run a command and stream combined stdout/stderr line-by-line to a log function.
    Returns the process exit code, or 127 if the executable wasn't found.
    The `clear` parameter is accepted for API compatibility but not currently used.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except FileNotFoundError:
        log(f"ERROR: Could not execute '{cmd[0]}' — not found on PATH.")
        return 127

    assert proc.stdout is not None
    for line in proc.stdout:
        log(line.rstrip("\n"))
    return proc.wait()


def read_json(path: Path) -> Optional[Dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, data: Dict) -> None:
    """Atomic-ish JSON write via temp file + rename."""
    ensure_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def sanitize_path_component(name: str) -> str:
    """Clean a single path component for safe use in remote/USB paths."""
    s = (name or "").strip().strip("/\\")
    s = s.replace("..", "_").replace("/", "_").replace("\\", "_")
    return s


def list_usb_candidates() -> List[str]:
    """
    Return plausible USB / removable drive mount points.
    Windows: drives reported as DRIVE_REMOVABLE.
    Linux:   /run/media/<user>/*, /media/*, /mnt/*
    """
    found: List[str] = []

    if is_windows():
        try:
            import ctypes
            DRIVE_REMOVABLE = 2
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
            for i in range(26):
                if bitmask & (1 << i):
                    root = f"{chr(65 + i)}:\\"
                    dtype = ctypes.windll.kernel32.GetDriveTypeW(  # type: ignore[attr-defined]
                        ctypes.c_wchar_p(root)
                    )
                    if dtype == DRIVE_REMOVABLE:
                        found.append(root)
        except Exception:
            pass
        return found

    # Linux / Steam Deck
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    search_roots: List[Path] = []
    if user:
        search_roots.append(Path("/run/media") / user)
    search_roots.extend([Path("/media"), Path("/mnt")])

    for root in search_roots:
        try:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir():
                    found.append(str(child))
        except PermissionError:
            pass

    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: List[str] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


# =============================================================================
# Config System
# =============================================================================

def config_local_path() -> Path:
    """Platform-appropriate local config file path."""
    if is_windows():
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "MCVault" / "config.json"
    return Path.home() / ".config" / "mcvault" / "config.json"


def default_config() -> Dict:
    return {
        "config_version": CONFIG_VERSION,
        "device_id": str(uuid.uuid4()),
        "last_modified_utc": utc_now_iso(),
        "dark_mode": False,
        "keep_backups": KEEP_DEFAULT,
        "default_backend": "rclone",
        "remote_root": REMOTE_DEFAULT,
        "rclone_cmd": RCLONE_DEFAULT,
        "dh_policy": "exclude",
        "dh_remember_choice": False,
        "usb_root": "",
        "usb_vault_name": "MinecraftVault",
    }


def normalize_config(cfg: Dict) -> Dict:
    """Merge incoming config over defaults, coerce types, clamp values."""
    base = default_config()
    if not isinstance(cfg, dict):
        return base
    base.update(cfg)

    # Type coercion + validation
    try:
        base["keep_backups"] = max(0, int(base["keep_backups"]))
    except (ValueError, TypeError):
        base["keep_backups"] = KEEP_DEFAULT

    if base.get("default_backend") not in ("rclone", "local", "usb"):
        base["default_backend"] = "rclone"

    if base.get("dh_policy") not in ("exclude", "include", "delete"):
        base["dh_policy"] = "exclude"

    base["remote_root"] = base.get("remote_root") or REMOTE_DEFAULT
    base["rclone_cmd"] = base.get("rclone_cmd") or RCLONE_DEFAULT
    base["dark_mode"] = bool(base.get("dark_mode", False))
    base["dh_remember_choice"] = bool(base.get("dh_remember_choice", False))

    if not base.get("device_id"):
        base["device_id"] = str(uuid.uuid4())
    if not base.get("last_modified_utc"):
        base["last_modified_utc"] = utc_now_iso()

    base["config_version"] = CONFIG_VERSION
    return base


def touch_config(cfg: Dict) -> Dict:
    """Update the last_modified_utc timestamp on a config dict."""
    cfg["last_modified_utc"] = utc_now_iso()
    return cfg


# Keys that are device-specific and must never be synced to the cloud.
# Each device manages these independently.
_DEVICE_LOCAL_KEYS = ("rclone_cmd", "usb_root")


def strip_device_local_keys(cfg: Dict) -> Dict:
    """Return a copy of cfg with device-local keys removed (for remote upload)."""
    return {k: v for k, v in cfg.items() if k not in _DEVICE_LOCAL_KEYS}


def merge_remote_config(local_cfg: Dict, remote_cfg: Dict) -> Dict:
    """
    Apply remote_cfg over local_cfg, but preserve device-local keys from
    local_cfg so that paths valid only on this machine are never overwritten.
    """
    merged = dict(remote_cfg)
    for key in _DEVICE_LOCAL_KEYS:
        if key in local_cfg:
            merged[key] = local_cfg[key]
    return merged


# =============================================================================
# Prism Discovery
# =============================================================================

def find_prism_root() -> Path:
    """Locate the PrismLauncher data directory (must contain an instances/ folder)."""
    home = Path.home()
    candidates = [
        home / "PrismLauncher",
        home / ".local" / "share" / "PrismLauncher",
        # Flatpak path
        home / ".var" / "app" / "org.prismlauncher.PrismLauncher" / "data" / "PrismLauncher",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "PrismLauncher")

    for c in candidates:
        if (c / "instances").is_dir():
            return c
    raise RuntimeError(
        "Could not locate PrismLauncher directory. "
        "Ensure PrismLauncher is installed and has at least one instance."
    )


def instance_mcdir(inst_path: Path) -> Path:
    """Return the .minecraft or minecraft dir inside a Prism instance."""
    for name in (".minecraft", "minecraft"):
        p = inst_path / name
        if p.is_dir():
            return p
    return inst_path / ".minecraft"


def list_local_instances(prism_root: Path) -> List[str]:
    inst_dir = prism_root / "instances"
    if not inst_dir.is_dir():
        return []
    return sorted(
        p.name for p in inst_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def list_local_worlds(prism_root: Path, instance: str) -> List[str]:
    saves = instance_mcdir(prism_root / "instances" / instance) / "saves"
    if not saves.is_dir():
        return []
    return sorted(w.name for w in saves.iterdir() if w.is_dir())


# =============================================================================
# Backend Layer
# =============================================================================

class BackendError(RuntimeError):
    """Raised when a backend operation fails."""


class BackendBase:
    """
    Abstract base for backup storage backends.
    Every backend must implement the core six methods.
    Config sync methods have sensible defaults (no-op).
    """
    name: str = "base"

    def list_instances(self) -> List[str]:
        raise NotImplementedError

    def list_worlds(self, instance: str) -> List[str]:
        raise NotImplementedError

    def list_backups(self, instance: str, world: str) -> List[str]:
        raise NotImplementedError

    def upload_backup(
        self, local_zip: Path, instance: str, world: str,
        zip_name: str, log: Callable[[str], None],
        clear: Optional[Callable[[], None]] = None,
    ) -> None:
        raise NotImplementedError

    def download_backup(
        self, instance: str, world: str, zip_name: str,
        local_zip: Path, log: Callable[[str], None],
        clear: Optional[Callable[[], None]] = None,
    ) -> None:
        raise NotImplementedError

    def prune_backups(
        self, instance: str, world: str, keep: int,
        log: Callable[[str], None],
    ) -> None:
        raise NotImplementedError

    # Config sync (optional; only Rclone currently supports this)
    def config_sync_supported(self) -> bool:
        return False

    def remote_config_exists(self) -> bool:
        return False

    def download_remote_config(self, dest: Path, log: Callable[[str], None]) -> bool:
        return False

    def upload_remote_config(self, src: Path, log: Callable[[str], None]) -> bool:
        return False


class RcloneBackend(BackendBase):
    """Cloud storage backend using rclone."""
    name = "rclone"

    def __init__(self, remote_root: str, rclone_cmd: str):
        self.remote = remote_root.rstrip("/")
        self.rclone = rclone_cmd

    def _remote_path(self, *parts: str) -> str:
        cleaned = [sanitize_path_component(p) for p in parts]
        return "/".join([self.remote] + cleaned)

    def _lsf(self, path: str, dirs_only: bool = False) -> List[str]:
        cmd = [self.rclone, "lsf"]
        if dirs_only:
            cmd.append("--dirs-only")
        cmd.append(path)
        result = run_cmd(cmd, capture=True)
        if result.returncode != 0:
            return []
        return [
            line.strip().rstrip("/")
            for line in (result.stdout or "").splitlines()
            if line.strip()
        ]

    def list_instances(self) -> List[str]:
        return self._lsf(self.remote, dirs_only=True)

    def list_worlds(self, instance: str) -> List[str]:
        return self._lsf(self._remote_path(instance), dirs_only=True)

    def list_backups(self, instance: str, world: str) -> List[str]:
        files = self._lsf(self._remote_path(instance, world))
        return sorted((f for f in files if f.lower().endswith(".zip")), reverse=True)

    def upload_backup(
        self, local_zip: Path, instance: str, world: str,
        zip_name: str, log: Callable[[str], None],
        clear: Optional[Callable[[], None]] = None,
    ) -> None:
        dest = f"{self._remote_path(instance, world)}/{zip_name}"
        log(f"Uploading → {dest}")
        rc = stream_cmd([self.rclone, "copyto", "--progress", str(local_zip), dest], log, clear)
        if rc != 0:
            raise BackendError(f"rclone upload failed (exit code {rc})")

    def download_backup(
        self, instance: str, world: str, zip_name: str,
        local_zip: Path, log: Callable[[str], None],
        clear: Optional[Callable[[], None]] = None,
    ) -> None:
        src = f"{self._remote_path(instance, world)}/{sanitize_path_component(zip_name)}"
        log(f"Downloading {src} → {local_zip}")
        safe_unlink(local_zip)
        rc = stream_cmd([self.rclone, "copyto", "--progress", src, str(local_zip)], log, clear)
        if rc != 0:
            raise BackendError(f"rclone download failed (exit code {rc})")

    def prune_backups(
        self, instance: str, world: str, keep: int,
        log: Callable[[str], None],
    ) -> None:
        if keep <= 0:
            return
        backups = self.list_backups(instance, world)
        if len(backups) <= keep:
            return
        remote_dir = self._remote_path(instance, world)
        log(f"Pruning old backups (keeping {keep})")
        for old in backups[keep:]:
            target = f"{remote_dir}/{old}"
            log(f"  Deleting {old}")
            run_cmd([self.rclone, "deletefile", target], capture=True)

    # ---- Config sync ----

    def config_sync_supported(self) -> bool:
        return True

    def _cfg_remote(self) -> str:
        return f"{self.remote}/_config/config.json"

    def remote_config_exists(self) -> bool:
        res = run_cmd([self.rclone, "lsf", self._cfg_remote()], capture=True)
        return res.returncode == 0 and bool((res.stdout or "").strip())

    def download_remote_config(self, dest: Path, log: Callable[[str], None]) -> bool:
        tmp = Path(tempfile.gettempdir()) / f"mcvault_rcfg_{uuid.uuid4().hex}.json"
        safe_unlink(tmp)
        rc = stream_cmd([self.rclone, "copyto", self._cfg_remote(), str(tmp)], log)
        if rc != 0 or not tmp.exists():
            safe_unlink(tmp)
            return False
        try:
            ensure_dir(dest.parent)
            tmp.replace(dest)
            return True
        except OSError:
            safe_unlink(tmp)
            return False

    def upload_remote_config(self, src: Path, log: Callable[[str], None]) -> bool:
        try:
            src = src.expanduser().resolve()
        except OSError:
            pass
        if not src.exists():
            log(f"Config sync: local config missing at {src}")
            return False
        normalized = os.path.normpath(str(src))
        log(f"Config sync: uploading {normalized} → {self._cfg_remote()}")
        rc = stream_cmd([self.rclone, "copyto", normalized, self._cfg_remote()], log)
        return rc == 0


class UsbBackend(BackendBase):
    """Removable drive backend using plain filesystem copy."""
    name = "usb"

    def __init__(self, usb_root: str, vault_name: str = "MinecraftVault"):
        if not usb_root:
            raise BackendError(
                "USB root path is not configured. "
                "Use Settings → Select USB drive or Set USB root path."
            )
        self.root = Path(usb_root)
        self.vault_name = vault_name or "MinecraftVault"

    def _base(self) -> Path:
        return self.root / self.vault_name

    def _ensure_writable(self) -> None:
        if not self.root.exists():
            raise BackendError(f"USB path does not exist: {self.root}")
        ensure_dir(self._base())

    def _world_dir(self, instance: str, world: str) -> Path:
        return self._base() / sanitize_path_component(instance) / sanitize_path_component(world)

    def list_instances(self) -> List[str]:
        self._ensure_writable()
        return sorted(
            p.name for p in self._base().iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def list_worlds(self, instance: str) -> List[str]:
        self._ensure_writable()
        inst_dir = self._base() / sanitize_path_component(instance)
        if not inst_dir.is_dir():
            return []
        return sorted(
            p.name for p in inst_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def list_backups(self, instance: str, world: str) -> List[str]:
        self._ensure_writable()
        d = self._world_dir(instance, world)
        if not d.is_dir():
            return []
        return sorted((z.name for z in d.glob("*.zip")), reverse=True)

    def upload_backup(
        self, local_zip: Path, instance: str, world: str,
        zip_name: str, log: Callable[[str], None],
    ) -> None:
        self._ensure_writable()
        dest_dir = self._world_dir(instance, world)
        ensure_dir(dest_dir)
        dest = dest_dir / zip_name
        log(f"Copying to USB → {dest}")
        shutil.copy2(str(local_zip), str(dest))

    def download_backup(
        self, instance: str, world: str, zip_name: str,
        local_zip: Path, log: Callable[[str], None],
    ) -> None:
        self._ensure_writable()
        src = self._world_dir(instance, world) / sanitize_path_component(zip_name)
        if not src.exists():
            raise BackendError(f"Backup not found on USB: {src}")
        safe_unlink(local_zip)
        log(f"Copying from USB: {src} → {local_zip}")
        shutil.copy2(str(src), str(local_zip))

    def prune_backups(
        self, instance: str, world: str, keep: int,
        log: Callable[[str], None],
    ) -> None:
        if keep <= 0:
            return
        backups = self.list_backups(instance, world)
        if len(backups) <= keep:
            return
        d = self._world_dir(instance, world)
        log(f"Pruning old USB backups (keeping {keep})")
        for old in backups[keep:]:
            try:
                (d / old).unlink(missing_ok=True)
                log(f"  Deleted {old}")
            except OSError as exc:
                log(f"  WARN: could not delete {old}: {exc}")


class LocalBackend(BackendBase):
    """Stub for future local-folder backend."""
    name = "local"


def build_backend(cfg: Dict) -> BackendBase:
    """Construct the appropriate backend from config."""
    kind = cfg.get("default_backend", "rclone")
    if kind == "usb":
        return UsbBackend(
            cfg.get("usb_root", ""),
            cfg.get("usb_vault_name", "MinecraftVault"),
        )
    if kind == "local":
        return LocalBackend()
    return RcloneBackend(
        cfg.get("remote_root", REMOTE_DEFAULT),
        cfg.get("rclone_cmd", RCLONE_DEFAULT),
    )


# =============================================================================
# Zip / Packaging Logic
# =============================================================================

def dh_detect(world_path: Path) -> Tuple[bool, int, List[Path]]:
    """
    Detect Distant Horizons files in world/data/.
    Returns (found, total_bytes, list_of_file_paths).
    """
    data_dir = world_path / "data"
    found: List[Path] = []
    total = 0
    for name in DH_FILES:
        p = data_dir / name
        if p.is_file():
            found.append(p)
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return (len(found) > 0, total, found)


def zip_world_folder(
    world_path: Path, out_zip: Path,
    exclude_relpaths: Optional[List[str]] = None,
) -> None:
    """
    Create a zip of a world folder. The zip contains exactly one top-level
    directory named after the world, e.g. MyWorld/level.dat, MyWorld/region/...
    """
    exclude = set()
    if exclude_relpaths:
        exclude = {p.replace("\\", "/").lstrip("/") for p in exclude_relpaths}

    safe_unlink(out_zip)
    world_name = world_path.name

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(world_path):
            root_p = Path(root)
            rel_root = root_p.relative_to(world_path).as_posix()
            for fn in files:
                rel_in_world = f"{rel_root}/{fn}" if rel_root != "." else fn
                rel_in_world = rel_in_world.replace("\\", "/")
                if rel_in_world in exclude:
                    continue
                arcname = f"{world_name}/{rel_in_world}"
                zf.write(root_p / fn, arcname)


def zip_contains_level_dat(zip_path: Path) -> bool:
    """Validate that a backup zip contains a level.dat file."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return any(
                n.replace("\\", "/").endswith("level.dat")
                for n in zf.namelist()
            )
    except (zipfile.BadZipFile, OSError):
        return False


def extract_restore_world(zip_path: Path, dest_world_dir: Path) -> None:
    """
    Extract a validated backup zip. Finds level.dat inside the extracted
    tree and moves its parent directory to dest_world_dir.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="mcvault-restore-"))
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmpdir)

        # Locate the world root (the directory containing level.dat)
        for candidate in tmpdir.rglob("level.dat"):
            if candidate.is_file():
                shutil.move(str(candidate.parent), str(dest_world_dir))
                return

        raise RuntimeError("level.dat not found after extraction — corrupted backup?")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# =============================================================================
# Core Operations (Backup / Restore / Prune)
# =============================================================================

def backup_operation(
    prism_root: Path,
    local_inst: str,
    local_world: str,
    remote_instance: str,
    backend: BackendBase,
    cfg: Dict,
    dh_choice: Optional[str],
    log: Callable[[str], None],
    clear: Optional[Callable[[], None]] = None,
) -> None:
    """
    Worker-thread operation: zip world → upload → prune.
    No GUI interaction — only calls log() / clear() for output.
    """
    world_path = (
        instance_mcdir(prism_root / "instances" / local_inst) / "saves" / local_world
    )
    if not world_path.is_dir():
        raise RuntimeError(f"World folder not found: {world_path}")

    # Distant Horizons handling
    exclude: List[str] = []
    has_dh, _sz, dh_files = dh_detect(world_path)
    if has_dh:
        effective = dh_choice or cfg.get("dh_policy", "exclude")
        if effective == "delete":
            log("DH policy: deleting local files then excluding from backup")
            for f in dh_files:
                try:
                    f.unlink()
                    log(f"  Deleted {f.name}")
                except OSError as exc:
                    log(f"  WARN: could not delete {f.name}: {exc}")
            exclude = [f"data/{n}" for n in DH_FILES]
        elif effective == "exclude":
            log("DH policy: excluding from backup (recommended)")
            exclude = [f"data/{n}" for n in DH_FILES]
        else:
            log("DH policy: including in backup")

    # Create zip
    ts = local_timestamp()
    zip_name = f"{local_world}_{ts}.zip"
    tmp_zip = Path(tempfile.gettempdir()) / zip_name
    log(f"Zipping {world_path.name}...")
    zip_world_folder(world_path, tmp_zip, exclude_relpaths=exclude)

    # Upload
    backend.upload_backup(tmp_zip, remote_instance, local_world, zip_name, log, clear)

    # Prune old backups
    keep = int(cfg.get("keep_backups", KEEP_DEFAULT))
    backend.prune_backups(remote_instance, local_world, keep, log)

    safe_unlink(tmp_zip)
    log("✓ Backup complete.")


def restore_operation(
    prism_root: Path,
    local_inst: str,
    remote_instance: str,
    remote_world: str,
    backup_zip: str,
    backend: BackendBase,
    log: Callable[[str], None],
    clear: Optional[Callable[[], None]] = None,
) -> None:
    """
    Worker-thread operation: download → validate → safe rename → extract.
    No GUI interaction — only calls log() / clear() for output.

    Safety rules enforced:
      1. Validate backup BEFORE touching any local files.
      2. Never overwrite — rename existing world first.
    """
    tmp_zip = Path(tempfile.gettempdir()) / f"mcvault_restore_{uuid.uuid4().hex}.zip"
    safe_unlink(tmp_zip)

    # Download
    backend.download_backup(remote_instance, remote_world, backup_zip, tmp_zip, log, clear)

    # SAFETY: Validate BEFORE modifying local filesystem
    log("Validating backup...")
    if not zip_contains_level_dat(tmp_zip):
        safe_unlink(tmp_zip)
        raise RuntimeError(
            "Backup is invalid — no level.dat found. Restore aborted; no local files were changed."
        )

    saves = instance_mcdir(prism_root / "instances" / local_inst) / "saves"
    ensure_dir(saves)

    target = saves / remote_world
    if target.exists():
        # SAFETY: Rename, never overwrite
        safe_name = f"{remote_world}.before_restore_{local_timestamp()}"
        renamed = saves / safe_name
        target.rename(renamed)
        log(f"Existing world renamed → {safe_name}")

    log("Extracting backup...")
    extract_restore_world(tmp_zip, target)

    safe_unlink(tmp_zip)
    log("✓ Restore complete.")


# =============================================================================
# GUI Layer (Tkinter)
# =============================================================================

# Color palette
DARK = {
    "bg": "#1e1e2e",
    "fg": "#cdd6f4",
    "surface": "#181825",
    "accent": "#89b4fa",
    "border": "#313244",
    "select_bg": "#313244",
    "btn_bg": "#313244",
    "btn_active": "#45475a",
}
LIGHT = {
    "bg": "#eff1f5",
    "fg": "#4c4f69",
    "surface": "#ffffff",
    "accent": "#1e66f5",
    "border": "#ccd0da",
    "select_bg": "#bcc0cc",
    "btn_bg": "#ccd0da",
    "btn_active": "#bcc0cc",
}


class VaultGUI:
    """Main application window. All heavy work runs on daemon threads."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("920x620")
        self.root.minsize(640, 400)

        # Thread-safe log queue
        self._log_q: "queue.Queue[str]" = queue.Queue()

        # Config
        self.cfg_path = config_local_path()
        self.cfg = normalize_config(read_json(self.cfg_path) or default_config())

        # Backend
        self.backend: BackendBase = build_backend(self.cfg)

        # Build UI
        self._init_style()
        self._build_ui()
        self._apply_theme()

        # Start log pump
        self.root.after(50, self._pump_log)

        # Startup message
        self.log(f"{APP_NAME} v{APP_VERSION} ready.")
        self.log(f"Backend: {self.cfg.get('default_backend')}  |  "
                 f"KEEP: {self.cfg.get('keep_backups')}  |  "
                 f"DH: {self.cfg.get('dh_policy')}")

        # Non-blocking config sync on launch
        self._run_threaded(self._config_sync_on_launch)

    # ------------------------------------------------------------------ log
    def log(self, msg: str) -> None:
        """Thread-safe logging — messages are queued and flushed on the UI thread."""
        self._log_q.put_nowait(msg)

    def clear_log(self) -> None:
        """Thread-safe log clear — sentinel None clears the widget on the UI thread."""
        self._log_q.put_nowait(None)

    def _pump_log(self) -> None:
        """Drain the log queue into the text widget (runs on UI thread)."""
        dirty = False
        try:
            while True:
                msg = self._log_q.get_nowait()
                if msg is None:
                    self.log_text.delete("1.0", "end")
                else:
                    self.log_text.insert("end", msg + "\n")
                dirty = True
        except queue.Empty:
            pass
        if dirty:
            self.log_text.see("end")
        self.root.after(50, self._pump_log)

    # ------------------------------------------------------------ threading
    def _run_threaded(self, fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()

    # --------------------------------------------------------------- style
    def _init_style(self) -> None:
        self.style = ttk.Style()
        for theme in ("clam", "alt", "default"):
            if theme in self.style.theme_names():
                self.style.theme_use(theme)
                break

    def _palette(self) -> Dict[str, str]:
        return DARK if self.cfg.get("dark_mode") else LIGHT

    def _apply_theme(self) -> None:
        p = self._palette()
        self.root.configure(bg=p["bg"])

        self.style.configure(".", background=p["bg"], foreground=p["fg"])
        self.style.configure("TFrame", background=p["bg"])
        self.style.configure("TLabel", background=p["bg"], foreground=p["fg"])
        self.style.configure("TButton", padding=6, background=p["btn_bg"])
        self.style.configure("TLabelframe", background=p["bg"], foreground=p["fg"])
        self.style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        self.style.map("TButton",
                       background=[("active", p["btn_active"])],
                       foreground=[("active", p["fg"])])

        # Log text is a plain tk.Text — style it manually
        try:
            self.log_text.configure(
                bg=p["surface"], fg=p["fg"],
                insertbackground=p["fg"],
                selectbackground=p["select_bg"],
                selectforeground=p["fg"],
                relief="flat", borderwidth=0,
            )
        except AttributeError:
            pass  # widget not created yet

    def _apply_popup_theme(self, win: tk.Toplevel, widgets: List[tk.Widget]) -> None:
        """Apply the current color palette to a popup dialog's plain tk widgets."""
        p = self._palette()
        try:
            win.configure(bg=p["bg"])
        except tk.TclError:
            pass

        for w in widgets:
            cls = w.winfo_class()
            try:
                if cls == "Frame":
                    w.configure(bg=p["bg"])
                elif cls == "Label":
                    w.configure(bg=p["bg"], fg=p["fg"])
                elif cls == "Listbox":
                    w.configure(
                        bg=p["surface"], fg=p["fg"],
                        selectbackground=p["select_bg"], selectforeground=p["fg"],
                        highlightbackground=p["border"], relief="flat",
                    )
                elif cls == "Entry":
                    w.configure(
                        bg=p["surface"], fg=p["fg"],
                        insertbackground=p["fg"],
                        highlightbackground=p["border"], relief="flat",
                    )
                elif cls == "Button":
                    w.configure(
                        bg=p["btn_bg"], fg=p["fg"],
                        activebackground=p["btn_active"], activeforeground=p["fg"],
                        relief="flat", borderwidth=1,
                    )
            except tk.TclError:
                pass

    # ------------------------------------------------------------- build UI
    def _build_ui(self) -> None:
        # Button bar
        bar = ttk.Frame(self.root, padding=10)
        bar.pack(fill="x")

        ttk.Button(bar, text="⬆ Backup", command=self._on_backup).pack(side="left")
        ttk.Button(bar, text="⬇ Restore", command=self._on_restore).pack(side="left", padx=6)
        ttk.Button(bar, text="📋 List Remote", command=self._on_list_remote).pack(side="left")
        ttk.Button(bar, text="⚙ Settings", command=self._on_settings).pack(side="left", padx=6)
        ttk.Button(bar, text="Quit", command=self.root.destroy).pack(side="right")

        # Info strip
        self._info_var = tk.StringVar(value=self._info_text())
        ttk.Label(self.root, textvariable=self._info_var, font=("", 9)).pack(
            anchor="w", padx=12,
        )

        # Log area
        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self.log_text = tk.Text(log_frame, wrap="word", font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _info_text(self) -> str:
        c = self.cfg
        usb = c.get("usb_root", "") or "—"
        return (
            f"Backend: {c.get('default_backend')}  |  "
            f"Remote: {c.get('remote_root')}  |  "
            f"USB: {usb}  |  "
            f"KEEP: {c.get('keep_backups')}  |  "
            f"Dark: {'on' if c.get('dark_mode') else 'off'}  |  "
            f"DH: {c.get('dh_policy')}"
        )

    def _refresh_info(self) -> None:
        self._info_var.set(self._info_text())

    # ---------------------------------------- Gamescope-safe modal dialogs
    def _open_dialog(self, title: str, geometry: str = "720x540") -> tk.Toplevel:
        """Create a Toplevel dialog that's Steam Deck / Gamescope friendly."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry(geometry)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        win.lift()
        win.focus_force()
        # Drop topmost after a short delay so it doesn't fight the WM
        win.after(300, lambda: _safe_attr(win, "-topmost", False))
        return win

    def pick(self, title: str, prompt: str, items: List[str]) -> Optional[str]:
        """Modal list picker — avoids grab_set for Gamescope compatibility."""
        if not items:
            return None

        win = self._open_dialog(title)
        frm = tk.Frame(win, padx=12, pady=12)
        frm.pack(fill="both", expand=True)

        lbl = tk.Label(frm, text=prompt, anchor="w", justify="left", wraplength=690)
        lbl.pack(fill="x")

        lb = tk.Listbox(frm, activestyle="dotbox", exportselection=False, font=("", 11))
        lb.pack(fill="both", expand=True, pady=(8, 8))
        for item in items:
            lb.insert("end", item)
        lb.selection_set(0)
        lb.activate(0)
        lb.focus_set()

        result: Dict[str, Optional[str]] = {"value": None}

        def ok():
            sel = lb.curselection()
            if sel:
                result["value"] = lb.get(sel[0])
            win.destroy()

        def cancel():
            win.destroy()

        btn_frame = tk.Frame(frm)
        btn_frame.pack(fill="x")
        tk.Button(btn_frame, text="OK", command=ok, width=12).pack(side="right", padx=4, pady=4)
        tk.Button(btn_frame, text="Cancel", command=cancel, width=12).pack(side="right", padx=4, pady=4)

        self._apply_popup_theme(win, [win, frm, lbl, lb, btn_frame,
                                       *btn_frame.winfo_children()])

        lb.bind("<Double-Button-1>", lambda _: ok())
        win.bind("<Return>", lambda _: ok())
        win.bind("<Escape>", lambda _: cancel())

        # Keep focus on the listbox
        win.bind("<FocusOut>", lambda _: win.after(30, lb.focus_set))

        self.root.wait_window(win)
        return result["value"]

    def enter_text(self, title: str, prompt: str, initial: str = "") -> Optional[str]:
        """Modal text entry dialog — Gamescope safe."""
        win = self._open_dialog(title, "720x220")
        frm = tk.Frame(win, padx=12, pady=12)
        frm.pack(fill="both", expand=True)

        lbl = tk.Label(frm, text=prompt, anchor="w", justify="left", wraplength=690)
        lbl.pack(fill="x")

        var = tk.StringVar(value=initial)
        entry = tk.Entry(frm, textvariable=var, font=("", 11))
        entry.pack(fill="x", pady=(8, 8))
        entry.focus_set()
        entry.icursor("end")

        result: Dict[str, Optional[str]] = {"value": None}

        def ok():
            s = var.get().strip()
            if s:
                result["value"] = s
            win.destroy()

        def cancel():
            win.destroy()

        btn_frame = tk.Frame(frm)
        btn_frame.pack(fill="x")
        tk.Button(btn_frame, text="OK", command=ok, width=12).pack(side="right", padx=4, pady=4)
        tk.Button(btn_frame, text="Cancel", command=cancel, width=12).pack(side="right", padx=4, pady=4)

        self._apply_popup_theme(win, [win, frm, lbl, entry, btn_frame,
                                       *btn_frame.winfo_children()])
        win.bind("<Return>", lambda _: ok())
        win.bind("<Escape>", lambda _: cancel())

        self.root.wait_window(win)
        return result["value"]

    # -------------------------------------------------- config persistence
    def _save_config_local(self) -> None:
        try:
            write_json(self.cfg_path, self.cfg)
        except OSError as exc:
            self.log(f"ERROR saving config: {exc}")

    def _attempt_remote_config_upload(self) -> None:
        if not self.backend.config_sync_supported():
            return
        tmp_remote = Path(tempfile.gettempdir()) / f"mcvault_upload_cfg_{uuid.uuid4().hex}.json"
        try:
            # Always save the full config locally (device-local keys included)
            write_json(self.cfg_path, self.cfg)
            # Upload a sanitised copy — rclone_cmd and usb_root must not
            # overwrite the same settings on other devices
            write_json(tmp_remote, strip_device_local_keys(self.cfg))
            ok = self.backend.upload_remote_config(tmp_remote, self.log)
            self.log("Config sync: remote upload " + ("succeeded." if ok else "failed (offline?)."))
        except Exception as exc:
            self.log(f"Config sync: upload error — {exc}")
        finally:
            safe_unlink(tmp_remote)

    def _config_sync_on_launch(self) -> None:
        """On-launch config sync algorithm (rclone only, best-effort)."""
        if not self.backend.config_sync_supported():
            return

        self.log("Config sync: checking remote...")
        self.cfg = normalize_config(self.cfg)
        write_json(self.cfg_path, self.cfg)

        try:
            remote_exists = self.backend.remote_config_exists()
        except Exception:
            remote_exists = False

        if not remote_exists:
            self.log("Config sync: no remote config found — uploading local.")
            self._attempt_remote_config_upload()
            return

        # Download remote to temp and compare timestamps
        tmp = Path(tempfile.gettempdir()) / f"mcvault_rcfg_{uuid.uuid4().hex}.json"
        safe_unlink(tmp)
        try:
            downloaded = self.backend.download_remote_config(tmp, self.log)
        except Exception:
            downloaded = False

        if not downloaded or not tmp.exists():
            self.log("Config sync: could not fetch remote (offline?) — using local.")
            safe_unlink(tmp)
            return

        remote_cfg = normalize_config(read_json(tmp) or {})
        local_cfg = normalize_config(read_json(self.cfg_path) or {})
        safe_unlink(tmp)

        r_dt = parse_iso_utc(remote_cfg.get("last_modified_utc", ""))
        l_dt = parse_iso_utc(local_cfg.get("last_modified_utc", ""))
        epoch = datetime.min.replace(tzinfo=timezone.utc)

        if (r_dt or epoch) > (l_dt or epoch):
            self.log("Config sync: remote is newer — applying.")
            # Merge: take remote settings but keep this device's local paths
            self.cfg = normalize_config(merge_remote_config(local_cfg, remote_cfg))
            self._save_config_local()
        else:
            self.log("Config sync: local is newer — uploading.")
            self.cfg = local_cfg
            self._save_config_local()
            self._attempt_remote_config_upload()

        # Re-apply settings that may have changed
        self.backend = build_backend(self.cfg)
        self._apply_theme()
        self._refresh_info()

    # --------------------------------------------------- USB drive selector
    def _select_usb_drive(self) -> None:
        candidates = list_usb_candidates()
        items = candidates + ["[Enter path manually...]"]
        choice = self.pick("Select USB Drive", "Choose the USB mount point:", items)
        if not choice:
            return
        if choice == "[Enter path manually...]":
            choice = self.enter_text(
                "USB Root Path",
                "Enter USB root path (e.g. E:\\ or /run/media/deck/USBNAME):",
            )
            if not choice:
                return
        self.cfg["usb_root"] = choice.strip()
        self.cfg["usb_vault_name"] = "MinecraftVault"

    # --------------------------------------------------------- GUI actions
    def _on_settings(self) -> None:
        c = self.cfg
        options = [
            f"Set backend (current: {c.get('default_backend')})",
            f"Select USB drive (current: {c.get('usb_root') or '(not set)'})",
            f"Set USB root path manually",
            f"Toggle dark mode (current: {'on' if c.get('dark_mode') else 'off'})",
            f"Set KEEP backups (current: {c.get('keep_backups')})",
            f"Set Distant Horizons policy (current: {c.get('dh_policy')})",
            f"Toggle DH remember choice (current: {'on' if c.get('dh_remember_choice') else 'off'})",
            f"Set REMOTE root (current: {c.get('remote_root')})",
            f"Set RCLONE command (current: {c.get('rclone_cmd')})",
        ]
        choice = self.pick("Settings", "Choose a setting to change:", options)
        if not choice:
            return

        try:
            if choice.startswith("Set backend"):
                b = self.pick("Backend", "Choose backup storage:", [
                    "rclone (cloud)", "usb (removable drive)",
                ])
                if not b:
                    return
                self.cfg["default_backend"] = "rclone" if b.startswith("rclone") else "usb"
                if self.cfg["default_backend"] == "usb" and not (self.cfg.get("usb_root") or "").strip():
                    self._select_usb_drive()

            elif choice.startswith("Select USB"):
                self._select_usb_drive()

            elif choice.startswith("Set USB root"):
                s = self.enter_text(
                    "USB Root Path",
                    "Enter USB root path (e.g. E:\\ or /run/media/deck/USBNAME):",
                    self.cfg.get("usb_root", ""),
                )
                if s is not None:
                    self.cfg["usb_root"] = s.strip()

            elif choice.startswith("Toggle dark"):
                self.cfg["dark_mode"] = not self.cfg.get("dark_mode", False)

            elif choice.startswith("Set KEEP"):
                s = self.enter_text(
                    "Keep Backups", "How many backups to keep per world (0 = no pruning):",
                    str(self.cfg.get("keep_backups", KEEP_DEFAULT)),
                )
                if s is not None:
                    try:
                        self.cfg["keep_backups"] = max(0, int(s))
                    except ValueError:
                        self.log("Invalid number — setting not changed.")
                        return

            elif choice.startswith("Set Distant"):
                p = self.pick("DH Policy", "What should backup do with DistantHorizons.sqlite?", [
                    "exclude (recommended)",
                    "include",
                    "delete locally (then exclude)",
                ])
                if p:
                    mapping = {
                        "exclude (recommended)": "exclude",
                        "include": "include",
                        "delete locally (then exclude)": "delete",
                    }
                    self.cfg["dh_policy"] = mapping[p]

            elif choice.startswith("Toggle DH"):
                self.cfg["dh_remember_choice"] = not self.cfg.get("dh_remember_choice", False)

            elif choice.startswith("Set REMOTE"):
                s = self.enter_text(
                    "Remote Root", "Rclone remote root (e.g. gdrive:MinecraftVault):",
                    self.cfg.get("remote_root", REMOTE_DEFAULT),
                )
                if s is not None:
                    self.cfg["remote_root"] = s.strip()

            elif choice.startswith("Set RCLONE"):
                s = self.enter_text(
                    "Rclone Command", "Rclone executable path or command:",
                    self.cfg.get("rclone_cmd", RCLONE_DEFAULT),
                )
                if s is not None:
                    self.cfg["rclone_cmd"] = s.strip()

            touch_config(self.cfg)
            self._save_config_local()
            self.backend = build_backend(self.cfg)
            self._apply_theme()
            self._refresh_info()
            self._run_threaded(self._attempt_remote_config_upload)
            self.log("Settings updated.")

        except Exception as exc:
            self.log(f"ERROR in settings: {exc}")

    def _on_backup(self) -> None:
        """Gather user selections on UI thread, then run backup on worker thread."""
        try:
            prism_root = find_prism_root()
            instances = list_local_instances(prism_root)
            if not instances:
                self.log("ERROR: No PrismLauncher instances found.")
                return

            local_inst = self.pick("Choose Instance", "Select the local instance to back up from:", instances)
            if not local_inst:
                return

            worlds = list_local_worlds(prism_root, local_inst)
            if not worlds:
                self.log("ERROR: No worlds found in this instance.")
                return

            world = self.pick("Choose World", "Select the world to back up:", worlds)
            if not world:
                return

            # Choose destination instance folder on the backend
            remote_insts = []
            try:
                remote_insts = self.backend.list_instances()
            except Exception:
                pass

            ordered = [local_inst] + [r for r in remote_insts if r != local_inst]
            ordered.append("[Create new folder...]")
            remote_inst = self.pick(
                "Destination Folder",
                "Choose the remote instance folder to upload into:",
                ordered,
            )
            if not remote_inst:
                return
            if remote_inst == "[Create new folder...]":
                remote_inst = self.enter_text(
                    "New Folder", "Enter remote instance folder name:", local_inst,
                )
                if not remote_inst:
                    return

            # Distant Horizons prompt
            dh_choice: Optional[str] = None
            inst_path = prism_root / "instances" / local_inst
            world_path = instance_mcdir(inst_path) / "saves" / world
            has_dh, sz, _ = dh_detect(world_path)

            if has_dh:
                if self.cfg.get("dh_remember_choice"):
                    dh_choice = self.cfg.get("dh_policy", "exclude")
                else:
                    raw = self.pick(
                        "Distant Horizons Detected",
                        f"DistantHorizons.sqlite found (~{format_size(sz)}).\n"
                        f"What should this backup do?\n\n"
                        f"Default: exclude (recommended).",
                        ["exclude", "include", "delete locally"],
                    )
                    if not raw:
                        return
                    dh_choice = {"exclude": "exclude", "include": "include",
                                 "delete locally": "delete"}[raw]

            def worker():
                self.clear_log()
                try:
                    backup_operation(
                        prism_root=prism_root,
                        local_inst=local_inst,
                        local_world=world,
                        remote_instance=remote_inst,
                        backend=self.backend,
                        cfg=self.cfg,
                        dh_choice=dh_choice,
                        log=self.log,
                        clear=self.clear_log,
                    )
                except Exception as exc:
                    self.log(f"ERROR: {exc}")

            self._run_threaded(worker)

        except Exception as exc:
            self.log(f"ERROR: {exc}")

    def _on_restore(self) -> None:
        """Gather user selections on UI thread, then run restore on worker thread."""
        try:
            prism_root = find_prism_root()
            instances = list_local_instances(prism_root)
            if not instances:
                self.log("ERROR: No PrismLauncher instances found.")
                return

            local_inst = self.pick(
                "Restore Target", "Select the LOCAL instance to restore INTO:", instances,
            )
            if not local_inst:
                return

            remote_insts = self.backend.list_instances()
            if not remote_insts:
                self.log("ERROR: No remote instances found.")
                return

            remote_inst = self.pick(
                "Backup Source", "Select the remote instance to restore FROM:", remote_insts,
            )
            if not remote_inst:
                return

            worlds = self.backend.list_worlds(remote_inst)
            if not worlds:
                self.log("ERROR: No worlds found in that remote instance.")
                return

            world = self.pick("Choose World", "Select the remote world to restore:", worlds)
            if not world:
                return

            backups = self.backend.list_backups(remote_inst, world)
            if not backups:
                self.log("ERROR: No backups found for that world.")
                return

            backup = self.pick("Choose Backup", "Select the backup zip to restore:", backups)
            if not backup:
                return

            def worker():
                self.clear_log()
                try:
                    restore_operation(
                        prism_root=prism_root,
                        local_inst=local_inst,
                        remote_instance=remote_inst,
                        remote_world=world,
                        backup_zip=backup,
                        backend=self.backend,
                        log=self.log,
                        clear=self.clear_log,
                    )
                except Exception as exc:
                    self.log(f"ERROR: {exc}")

            self._run_threaded(worker)

        except Exception as exc:
            self.log(f"ERROR: {exc}")

    def _on_list_remote(self) -> None:
        def worker():
            try:
                insts = self.backend.list_instances()
                if not insts:
                    self.log("No remote instances found.")
                    return
                self.log("Remote instances:")
                for name in insts:
                    worlds = self.backend.list_worlds(name)
                    world_str = ", ".join(worlds) if worlds else "(empty)"
                    self.log(f"  {name}/  →  {world_str}")
            except Exception as exc:
                self.log(f"ERROR: {exc}")

        self._run_threaded(worker)


def _safe_attr(win: tk.Toplevel, attr: str, value) -> None:
    """Set a Toplevel attribute, ignoring errors if the window was destroyed."""
    try:
        win.attributes(attr, value)
    except tk.TclError:
        pass


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    app = VaultGUI()
    app.root.mainloop()

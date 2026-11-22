"""工作目录状态持久化。

- 源资料（inputs/）只读，所有应用产物放在独立工作目录；
- 不同工作目录的状态文件互不污染；
- 全部为本地 JSON，重启后可完整恢复筹备工作。
"""

import json
import os
import tempfile
from pathlib import Path

from .timeutil import iso, now_utc

STATE_FILENAME = "state.json"
STATE_VERSION = 1


class StoreError(RuntimeError):
    pass


def init_workspace(work_dir: str | Path, input_dir: str | Path, force: bool = False) -> Path:
    """初始化工作目录，写入状态骨架。已存在且非空时拒绝，除非 force。"""
    work_dir = Path(work_dir).resolve()
    input_dir = Path(input_dir).resolve()
    if not input_dir.exists():
        raise StoreError(f"输入目录不存在：{input_dir}")
    if work_dir.exists() and any(work_dir.iterdir()) and not force:
        raise StoreError(f"工作目录已存在且非空：{work_dir}（可用 force 覆盖）")
    work_dir.mkdir(parents=True, exist_ok=True)
    state = empty_state(str(input_dir))
    write_state(work_dir, state)
    return work_dir


def empty_state(input_dir: str) -> dict:
    return {
        "state_version": STATE_VERSION,
        "created_at": iso(now_utc()),
        "updated_at": iso(now_utc()),
        "input_dir": input_dir,
        "imports": [],
        "observations": [],
        "arrangements": [],
        "drills": [],
        "action_items": [],
        "counters": {"arrangement": 0, "drill": 0, "action": 0, "observation": 0},
    }


def state_path(work_dir: str | Path) -> Path:
    return Path(work_dir) / STATE_FILENAME


def read_state(work_dir: str | Path) -> dict:
    path = state_path(work_dir)
    if not path.exists():
        raise StoreError(
            f"工作目录尚未初始化：{path}（先运行 init）"
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"状态文件损坏：{exc}") from exc
    if state.get("state_version") != STATE_VERSION:
        raise StoreError(
            f"状态版本 {state.get('state_version')} 与应用版本 {STATE_VERSION} 不兼容"
        )
    return state


def write_state(work_dir: str | Path, state: dict) -> None:
    """原子写入：先写临时文件再替换，避免中途中断造成损坏。"""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = iso(now_utc())
    path = state_path(work_dir)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".state-", suffix=".tmp", dir=str(work_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def next_id(state: dict, kind: str, prefix: str) -> str:
    state["counters"][kind] = state["counters"].get(kind, 0) + 1
    return f"{prefix}{state['counters'][kind]}"

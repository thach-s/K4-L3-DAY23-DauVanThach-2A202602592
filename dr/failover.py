"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped event and return the emitted record."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm the target, cutting traffic over only after readiness."""
    if target not in URL or backend not in {"fs", "minio"} or wait < 0:
        raise ValueError("invalid target/backend/wait")
    primary = "b" if target == "a" else "a"
    try:
        before = state_of(target)
    except Exception as exc:
        before = {"region": target, "error": type(exc).__name__}
    emit(step="1_verify_target", target=target, state=before)

    try:
        restored = snapshot.get(target, backend)
        loss = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                            pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    except BaseException as exc:
        emit(step="2_restore_snapshot", target=target, ok=False,
             error=type(exc).__name__, detail=str(exc))
        return {"ok": False, "target": target, "failed_step": "2_restore_snapshot"}
    restore_event = emit(step="2_restore_snapshot", target=target, ok=True,
                         backend=backend, rpo_seconds=loss["rpo_seconds"],
                         docs_lost=loss["docs_lost"],
                         embed_model_version=restored.get("embed_model_version"),
                         snapshot_at=restored.get("snapshot_at"))

    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full\n", encoding="utf-8")
    emit(step="3_scale_pool", target=target, pool_state="full")

    started = time.monotonic()
    deadline = started + wait
    ready_state = None
    while True:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, max(0.1, wait)))
            ready_state = response.json()
            if response.status_code == 200 and ready_state.get("ready", True):
                break
        except Exception as exc:
            ready_state = {"error": type(exc).__name__}
        if time.monotonic() >= deadline:
            waited = round(time.monotonic() - started, 3)
            emit(step="4_wait_ready", target=target, ok=False, waited_s=waited,
                 state=ready_state, error="readiness_timeout")
            return {"ok": False, "target": target, "failed_step": "4_wait_ready",
                    "waited_s": waited, "restore": restore_event}
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    waited = round(time.monotonic() - started, 3)
    emit(step="4_wait_ready", target=target, ok=True, waited_s=waited, state=ready_state)
    pathlib.Path("edge/active_region").write_text(target + "\n", encoding="utf-8")
    cutover = emit(step="5_dns_cutover", target=target, ok=True, active_region=target)
    return {"ok": True, "target": target, "state": ready_state,
            "restore": restore_event, "cutover": cutover, "waited_s": waited}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))

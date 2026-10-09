"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Write one runbook timeline event."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit y unless automation was requested."""
    return True if auto else input(f"{msg} [y/N] ").strip().lower() == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, semi-automated disaster-recovery runbook."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different known regions")
    started = time.time()
    checks = []
    for _ in range(3):
        try:
            checks.append(httpx.get(f"{URL[primary]}/readyz", timeout=0.5).status_code == 200)
        except httpx.HTTPError:
            checks.append(False)
        time.sleep(0.1)
    try:
        target_alive = httpx.get(f"{URL[target]}/healthz", timeout=0.5).status_code == 200
    except httpx.HTTPError:
        target_alive = False
    outage_confirmed = not any(checks)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         primary_ready_checks=checks, outage_confirmed=outage_confirmed,
         target_alive=target_alive)
    if not outage_confirmed or not target_alive:
        return {"ok": False, "failed_step": 1, "reason": "outage_not_confirmed_or_target_dead"}

    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    outage_ts = None
    if chaos_file.exists():
        events = [json.loads(line) for line in chaos_file.read_text().splitlines() if line.strip()]
        matches = [e for e in events if e.get("action") == "kill" and e.get("region") == primary]
        outage_ts = matches[-1].get("ts") if matches else None
    if not confirm(auto, f"Xác nhận failover region-{primary} sang region-{target}?"):
        step(2, "thong_bao_incident", confirmed=False, outage_ts=outage_ts)
        return {"ok": False, "failed_step": 2, "reason": "operator_cancelled"}
    incident = step(2, "thong_bao_incident", confirmed=True, outage_ts=outage_ts,
                    notification_delay_s=None if outage_ts is None else round(time.time() - outage_ts, 3))

    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_ok=result.get("ok"), waited_s=result.get("waited_s"))
    if not result.get("ok"):
        return {"ok": False, "failed_step": 3, "failover": result}
    state = result.get("state", {})
    step(4, "verify_state_replica", vector_count=state.get("vectors", {}).get("count"),
         weights=not any("model_weights_missing" in str(x) for x in state.get("reasons", [])),
         embed_model_version=result.get("restore", {}).get("embed_model_version"),
         docs_lost=result.get("restore", {}).get("docs_lost"))
    step(5, "dns_cutover", ok=result.get("cutover", {}).get("ok", False),
         active_region=result.get("cutover", {}).get("active_region"))

    latencies, errors = [], 0
    for i in range(10):
        t0 = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", params={"q": f"golden-{i}"}, timeout=3.0)
            if response.status_code != 200:
                errors += 1
        except httpx.HTTPError:
            errors += 1
        latencies.append((time.monotonic() - t0) * 1000)
    ordered = sorted(latencies)
    p95 = round(ordered[max(0, int(0.95 * len(ordered) + 0.9999) - 1)], 1)
    error_rate = round(errors / len(latencies), 3)
    step(6, "verify_golden_signals", requests=len(latencies), p95_latency_ms=p95,
         errors=errors, error_rate=error_rate, ok=errors == 0)
    elapsed = round(time.time() - started, 3)
    step(7, "post_incident", elapsed_s=elapsed,
         measure_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
         incident_ts=incident["ts"])
    return {"ok": errors == 0, "primary": primary, "target": target,
            "elapsed_s": elapsed, "p95_latency_ms": p95, "error_rate": error_rate,
            "failover": result}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))

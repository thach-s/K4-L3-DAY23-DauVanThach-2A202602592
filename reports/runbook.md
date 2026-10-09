# Runbook — Region chính down

Phạm vi: bare mode local, primary `a`, DR target `b`, snapshot backend `fs`.
Incident Commander (IC) giữ quyền phê duyệt cutover và rollback. Không dùng
`--i-really-want-both`.

## Checklist vận hành

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `/readyz` của A fail 3 lần liên tiếp, B vẫn `alive:true` | on-call SRE |
| 2 | Mở incident, bắt đầu đồng hồ | `python3 dr/runbook.py --primary a --target b --backend fs` | terminal hỏi `y/N`; sau khi nhập `y`, `reports/runbook-run.jsonl` có `thong_bao_incident` | Incident Commander |
| 3 | Restore state | Tiếp tục cùng lệnh runbook ở bước 2 | `reports/failover-events.jsonl` có `2_restore_snapshot`, `ok:true`, RPO/docs lost/model version không null | storage on-call |
| 4 | Scale pool warm → full | Tiếp tục cùng lệnh runbook ở bước 2 | `4_wait_ready` có `ok:true`; `curl -sf localhost:8002/readyz` trả `ready:true` | ML platform on-call |
| 5 | DNS/LB cutover | Tiếp tục cùng lệnh runbook ở bước 2 | `5_dns_cutover` xuất hiện sau bước 4; `curl -s localhost:8080/edge/state` trả `active_region:b` sau TTL | network on-call |
| 6 | Verify golden signals | `curl -sf localhost:8002/v1/infer` | runbook thực hiện 10 request: error rate `0.0`, p95 dưới 500ms; xem `reports/runbook-run.jsonl` bước 6 | service owner |
| 7 | Đo RTO/RPO và mở postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:PASS`, RPO và docs_lost không null | IC + service owner |

Trong CI/drill đã phê duyệt trước, thêm `--auto` vào lệnh bước 2. Trong incident
thật phải giữ xác nhận `y/N` để tránh failover hai chiều do tín hiệu chập chờn.

## Abort và rollback

- Abort trước cutover nếu B không alive, restore lỗi, model version thiếu/không
  khớp, vector count bằng 0, hoặc `/readyz` không đạt trong 60 giây. `failover.py`
  không được ghi `edge/active_region` trong mọi trường hợp này.
- Sau cutover, rollback về A nếu B có error rate trên 1% trong 5 phút, p95 trên
  500ms trong 5 phút, state validation sai, hoặc phát hiện lỗi dữ liệu nghiêm trọng.
- Chỉ Incident Commander được quyết định rollback sau khi primary A đã ổn định và
  `/readyz` thành công 3 lần liên tiếp. Thao tác: `printf a > edge/active_region`;
  đợi ít nhất 5 giây TTL; kiểm tra `curl -sf localhost:8080/v1/infer` có
  `edge_region:a`. Ghi quyết định và timestamp vào incident log.
- Nếu A bị `netblock` trong drill, khôi phục bằng
  `python3 chaos/kill_region.py restore --region a --backend bare` trước khi cân
  nhắc rollback. Không rollback về một region chưa ready.

# RTO/RPO Evidence — Lab 23

Tất cả thời gian dưới đây được tính từ timestamp máy trong log của drill ngày
2026-10-09. Lệnh đo chuẩn: `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-10-09T02:54:41Z | chaos `kill` Region A | `chaos/chaos-events.jsonl:3` |
| Request fail đầu tiên | +0.0s | request bắt đầu ngay trước kill và hoàn tất bằng timeout sau kill | `reports/drill-1-nodr.jsonl:17` |
| Số request lỗi sau outage | 15 | đếm các dòng `ok:false` từ request lỗi đầu tiên đến cuối log | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau lỗi | Không có | không có `ok:true` sau lỗi đầu tiên | `reports/drill-1-nodr.jsonl:32` |
| RTO | NO_RECOVERY | từ dòng lỗi đầu tiên đến dòng cuối không xuất hiện `ok:true` | `reports/drill-1-nodr.jsonl:32` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | `action:kill`, Region B còn alive và không ép double outage | `chaos/chaos-events.jsonl:5` |
| User thấy lỗi đầu tiên | 0.0s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 14.9s | Region A chuyển sang `UNHEALTHY` sau 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 17.6s | `2_restore_snapshot`, có RPO và model version | `reports/failover-events.jsonl:2` |
| Region B ready | 23.7s | `/readyz` trả 200 sau warm-up | `reports/failover-events.jsonl:4` |
| DNS cutover | 23.7s | chỉ ghi active region sau khi B ready | `reports/failover-events.jsonl:5` |
| **Request đầu tiên phục hồi từ B** | **28.4s** | `ok:true`, `served_by:b` đầu tiên sau chuỗi lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | 28.4s | 300s | PASS, dư địa 271.6s |
| RPO — Vector DB | 4.0s / 2 docs | 300s | PASS, dư địa 296.0s |

RPO là chênh lệch timestamp document mới nhất giữa primary và bản restore, đồng
thời đếm chính xác hai document có ở primary nhưng chưa có trong snapshot.
Evidence: `reports/failover-events.jsonl:2`; snapshot nguồn gần nhất ở
`reports/replication.jsonl:2`.

## 3. Phân rã RTO

| Thành phần | Giây | Nguồn | Cách giảm |
|---|---:|---|---|
| Health-check detect floor | 15.0s | `interval_s=5.0 × threshold=3` tại `reports/health-events.jsonl:2` (quan sát thực tế 14.9s) | giảm interval có kiểm soát, giữ threshold chống flapping |
| Xác nhận + restore snapshot | 2.6s | từ detect tới `3_scale_pool`; restore hoàn tất tại `reports/failover-events.jsonl:2` | tự động hoá phê duyệt có circuit breaker, snapshot nhỏ/gần hơn |
| GPU pool warm-up | 6.1s | `waited_s=6.12` tại `reports/failover-events.jsonl:4` | duy trì capacity warm hoặc pre-warm |
| DNS/LB TTL cache | 4.7s | cutover `reports/failover-events.jsonl:5` tới request B `reports/drill-2-withdr.jsonl:39` | giảm TTL hoặc dùng global LB health routing |
| **Tổng** | **28.4s** | bằng RTO đo từ trải nghiệm user | — |

Sai số làm tròn từng thành phần là 0.1s. Công cụ đo đối chiếu trực tiếp bốn nguồn:
`reports/drill-2-withdr.jsonl`, `chaos/chaos-events.jsonl`,
`reports/health-events.jsonl` và `reports/failover-events.jsonl`; kết quả xác nhận
`valid:true`, `warnings:[]`.

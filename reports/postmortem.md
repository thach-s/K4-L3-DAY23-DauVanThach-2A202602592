# Postmortem — DR Drill Lab 23

Ngày drill: 2026-10-09. Timestamp evidence dùng UTC; giờ Bangkok = UTC+7.
Đây là postmortem blameless: nguyên nhân và action item tập trung vào hệ thống,
không quy lỗi cá nhân.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T02:55:51Z | Region A bị netblock; Region B vẫn alive | `chaos/chaos-events.jsonl:5` |
| 2026-10-09T02:55:51Z | Request đầu tiên bị timeout | `reports/drill-2-withdr.jsonl:25` |
| 2026-10-09T02:56:06Z | Health checker đánh dấu A `UNHEALTHY` sau 3 fail | `reports/health-events.jsonl:2` |
| 2026-10-09T02:56:08Z | Operator xác nhận incident, trễ 17.526s từ outage | `reports/runbook-run.jsonl:2` |
| 2026-10-09T02:56:08Z | Restore snapshot xong: RPO 4.0s, mất 2 docs | `reports/failover-events.jsonl:2` |
| 2026-10-09T02:56:14Z | B ready rồi DNS mới cutover | `reports/failover-events.jsonl:4` và `reports/failover-events.jsonl:5` |
| 2026-10-09T02:56:19Z | Request đầu tiên thành công từ Region B | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO và gap so với mục tiêu

- RTO mục tiêu 300s; đo được 28.4s; gap/dư địa 271.6s. Verdict PASS.
- RPO mục tiêu 300s; đo được 4.0s và 2 document bị mất; gap/dư địa 296.0s.
- Bước tốn nhiều thời gian nhất là health-check detection: 15.0s, tương đương
  52.8% RTO. GPU warm-up đứng thứ hai với 6.12s.
- Golden signals sau cutover: 10/10 request thành công, error rate 0.0, p95
  90.5ms tại `reports/runbook-run.jsonl:6`.

## 3. Root cause — 5 whys

1. User lỗi vì edge vẫn trỏ vào Region A đang bị netblock.
2. Edge không tự đổi ngay vì cần một quyết định cutover an toàn và còn TTL cache.
3. Quyết định chưa thể xảy ra trước khi health checker có 3 failure liên tiếp.
4. Sau detection, B vẫn phải restore state và chuyển pool warm sang full.
5. Kiến trúc active-passive ưu tiên chống flapping và chi phí thấp, nên chấp nhận
   detection floor, snapshot lag và warm-up nằm trong RTO/RPO.

Điểm dễ thất bại trong incident thật là phụ thuộc vào snapshot hợp lệ gần nhất.
Nếu replication chưa chạy hoặc model version không đi cùng index, runbook phải abort
trước DNS cutover; đây là hành vi an toàn nhưng sẽ khiến RTO vượt mục tiêu nếu không
có giám sát replication trước sự cố.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Cảnh báo snapshot age > 30s và kiểm tra restore/model version hằng ngày | Storage SRE | 2026-10-16 | giữ RPO dưới 30s; tránh restore failure kéo dài RTO |
| 2 | Thử interval 3s, threshold 3 trong game day và theo dõi false positive | Platform SRE | 2026-10-23 | detection floor giảm từ 15s xuống 9s, RTO giảm khoảng 6s |
| 3 | Duy trì một worker B pre-warmed và đo chi phí | ML Platform | 2026-10-30 | warm-up giảm tối đa khoảng 6.1s |

## 5. Câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`; chiếm khoảng 52.8% của RTO 28.4s.
2. Nếu interval xuống 1s và vẫn giữ threshold 3, detection floor còn 3s, nên RTO
   lý tưởng giảm khoảng 12s. Đổi lại là lưu lượng probe tăng 5 lần, nhạy hơn với
   timeout ngắn và nguy cơ false failover/flapping cao hơn; vẫn cần circuit breaker.
3. Với outage 6 giờ và primary mất vĩnh viễn, `docs_lost` là số tài liệu/ticket đã
   được nhận ở primary sau mốc snapshot nhưng không tồn tại trong bản khôi phục.
   Chúng cần được replay từ nguồn sự kiện hoặc đối soát và nhập lại; nếu không,
   câu trả lời inference có thể thiếu dữ liệu khách hàng mới nhất.

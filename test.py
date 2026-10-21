import json
from datetime import date

import httpx
from scripts.sync import sync_sales_orders as sales

# 查询 2026 年 10 月 SG、KK 两个来源，使用脚本默认产品层次。
prodh = ["00100"]
start_date = date(2026, 9, 1)
end_date = date(2026, 10, 31)
window_days = int(sales.env("FULL_WINDOW_DAYS", "7"))
orders = []

with httpx.Client(
    timeout=float(sales.env("HTTP_TIMEOUT", str(sales.DEFAULT_TIMEOUT))),
    trust_env=False,
) as client:
    for source, url in sales.SOURCES.items():
        for window_start, window_end in sales.date_windows(
            start_date, end_date, window_days
        ):
            response = client.post(
                url,
                json=sales.payload(window_start, window_end, prodh),
                headers=sales.request_headers(),
            )
            response.raise_for_status()
            rows = sales.validate_records(response.json(), source)
            orders.extend({"source": source, **row} for row in rows)

print(json.dumps(orders, ensure_ascii=False, indent=2, default=str))
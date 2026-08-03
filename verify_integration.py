"""Live integration check: 验证 /report 的 campaign_id 参数与 history_baseline 拉取。

前置：.env 已配置 TESS_DATA_API_BASE_URL 与 TESS_SYSTEM_TOKEN。
运行：
  /Users/menlinda.meng/.workbuddy/binaries/python/envs/default/bin/python verify_integration.py

脚本会：
  1. 拦截并打印实际发往 GET /report 的参数（确认是 campaign_id=... 而非 campaign=...）；
  2. 取一条真实 campaign_id，调用 fetch_campaign_time_series；
  3. 打印返回的 history_baseline（时间点数量 / 是否非空）。
这是本次改动里唯一需要真接口才能验证的部分（字段名、dimensions 拼接）。
"""
import os
import sys
import json


def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from tess_backend.data_connector import TeensingDataConnector  # noqa: E402

base = os.getenv("TESS_DATA_API_BASE_URL")
tok = os.getenv("TESS_SYSTEM_TOKEN")
if not base or not tok:
    print("✗ 缺少 TESS_DATA_API_BASE_URL / TESS_SYSTEM_TOKEN，无法跑真接口验证。")
    sys.exit(1)

c = TeensingDataConnector(base_url=base)

# 拦截 /report 调用，打印真实发出去的参数
orig = TeensingDataConnector._http_get


def spy(self, path, params=None, token=None):
    if path == "/report":
        print("▶ 实际发往 GET /report 的参数：")
        print("  ", params)
    return orig(self, path, params=params, token=token)


TeensingDataConnector._http_get = spy

# 1) 取一个真实 campaign_id
evs = c.fetch_recent_anomalies(limit=20, token=tok)
cid = None
for e in evs:
    c2 = e.get("campaign_id") or e.get("cid")
    if c2:
        cid = c2
        break
print(f"\n从 anomaly-warning 取到 campaign_id = {cid!r}")
if not cid:
    print("✗ 没有可用的 campaign_id，无法继续。")
    sys.exit(1)

# 2) 拉取历史时间序列
ts = c.fetch_campaign_time_series(str(cid), token=tok)
print("\n◀ history_baseline 返回（截断）：")
print(json.dumps(ts, ensure_ascii=False, indent=2)[:1600])
print("\n时间点数:", ts.get("data_points_count"), "| 是否非空:", bool(ts.get("time_series")))

# 3) 诊断：campaign_id 过滤是否真正生效 + 维度拆分形态
series = ts.get("time_series") or []
dates = sorted({p.get("timestamp") for p in series})
nonzero_rev = [p for p in series if p.get("revenue", 0) > 0]
# 若 dimensions=campaign 生效，每行应带 campaign 标识字段
camp_keys = {k for p in series[:20] for k in p.keys() if "camp" in k.lower()}
print("\n— 诊断 —")
print("  去重后 date 列表:", dates)
print("  revenue>0 的点数:", len(nonzero_rev), "/", len(series))
print("  行内含 'camp' 的字段:", camp_keys)
if camp_keys:
    samp = series[0]
    print("  首行 camp 字段值:", {k: samp.get(k) for k in camp_keys})
print("  => 实际日期跨度 %d 天、点数 %d：过滤%s生效。" % (
    len(dates), len(series), "已" if (len(dates) > 1 or nonzero_rev) else "未"))

# 4) 对照实验：确认 campaign_id 是否真被当作过滤条件
def _probe(cid_value):
    p = {
        "dimensions": "date,campaign",
        "campaign_ids": str(cid_value),
        "date_start": "2026-07-27",
        "date_end": "2026-08-03",
        "page": 1,
        "page_size": 5,
        "sort_by": "date",
        "sort_order": "asc",
        "page_size": 5,
    }
    resp = orig(c, "/report", params=p, token=tok)
    data = c._unwrap(resp)
    items = data.get("items") if isinstance(data, dict) else None
    return len(items or []), sorted({it.get("date") for it in (items or [])})

real_cnt, real_dates = _probe(7051990)
fake_cnt, fake_dates = _probe(999999999)
print("\n— 对照实验 (page_size=5) —")
print(f"  campaign_id=7051990(真) -> 行数 {real_cnt}, 日期 {real_dates}")
print(f"  campaign_id=999999999(假) -> 行数 {fake_cnt}, 日期 {fake_dates}")
if real_cnt == fake_cnt and real_dates == fake_dates:
    print("  => 两者结果完全一致：campaign_id 过滤【未生效】，/report 很可能忽略该参数（或需别的参数名/传参方式）。")
else:
    print("  => 真/假 campaign_id 结果不同：过滤生效。")

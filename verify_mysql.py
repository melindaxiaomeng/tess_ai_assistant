"""Tess advertiser_deepdive 对拍：API 路径 vs 直连 MySQL 路径（同一份 analytics 代码）。

用法（容器内）：  docker exec tess-diagnose python3 /verify_mysql.py
依赖：容器 env 已有 TESS_SYSTEM_TOKEN（API 路径鉴权）/ TESS_DATA_API_BASE_URL /
      TESS_MYSQL_*（MySQL 路径）；容器需能连 172.22.0.6（已挂 bridge 网卡）。
"""

import os
import sys
import traceback

sys.path.insert(0, "/app")  # tess_backend 包位于 /app/tess_backend

from tess_backend.analytics import (  # noqa: E402
    fetch_bi_analysis_context,
    _advertiser_deepdive_prompt_view,
)
from tess_backend.data_connector import (  # noqa: E402
    TeensingDataConnector,
    get_platform_base_url,
)
from tess_backend.mysql_connector import MysqlDataConnector  # noqa: E402

AID = 1000853
DS, DE = "2026-07-01", "2026-10-31"
TOKEN = os.getenv("TESS_SYSTEM_TOKEN")


def _run(conn):
    ctx = fetch_bi_analysis_context(
        conn, "advertiser_deepdive", token=TOKEN,
        params={"advertiser_id": AID, "date_start": DS, "date_end": DE},
    )
    view = _advertiser_deepdive_prompt_view(ctx)
    return ctx, view


def _metrics(view):
    snap = {str(s.get("campaign_id")): s for s in view.get("campaigns_snapshot", [])}
    nulls = [c for c, s in snap.items() if s.get("status") is None or s.get("status") == ""]
    dist = {}
    for s in view.get("campaigns_snapshot", []):
        st = s.get("status")
        dist[st] = dist.get(st, 0) + 1
    return snap, nulls, dist, view.get("campaign_trends_monthly", {})


def _safe(label):
    try:
        return _run(_CONNS[label])
    except Exception as e:  # noqa: BLE001
        print(f"!! {label} 路径异常：{type(e).__name__}: {e}")
        traceback.print_exc()
        return None, None


print(f"advertiser_id={AID}  window={DS}~{DE}")
print(f"TESS_DATA_API_BASE_URL={os.getenv('TESS_DATA_API_BASE_URL')}")
print(f"TESS_MYSQL_HOST={os.getenv('TESS_MYSQL_HOST')} db={os.getenv('TESS_MYSQL_DATABASES')}")

_CONNS = {
    "API": TeensingDataConnector(base_url=get_platform_base_url("Melodong")),
    "MYSQL": MysqlDataConnector(platform_id="Melodong"),
}

# ---- 先跑 MySQL 自检：一眼看清连没连上 / 数据在哪个库 / 表行数 ----
print("\n=== MYSQL SELF-CHECK (advertiser_id=%d, %s~%s) ===" % (AID, DS, DE))
try:
    sc = _CONNS["MYSQL"].self_check(AID, DS, DE)
    print("host=%s:%s user=%s default_db=%s cross_db=%s" % (
        sc["host"], sc["port"], sc["user"], sc["default_db"], sc["cross_db"]))
    for name, e in sc["dbs"].items():
        if e["reachable"]:
            print("  [%s] db=%s  report_month行数=%s  report_hour_new窗口内行数=%s" % (
                name, e["db"], e["report_month_rows"], e["report_hour_new_rows"]))
        else:
            print("  [%s] db=%s  不可达: %s" % (name, e["db"], e["error"]))
except Exception as e:  # noqa: BLE001
    print("  !! self_check 异常：%s: %s" % (type(e).__name__, e))

api_ctx, api_view = _safe("API")
mysql_ctx, mysql_view = _safe("MYSQL")

if api_view and mysql_view:
    api_snap, api_nulls, api_dist, api_trends = _metrics(api_view)
    mysql_snap, mysql_nulls, mysql_dist, mysql_trends = _metrics(mysql_view)

    print("\n=== ERRORS ===")
    print("API  :", api_ctx.get("errors"))
    print("MYSQL:", mysql_ctx.get("errors"))

    print("\n=== TRENDS_COUNT（月度趋势 campaign 数）===")
    print("API  :", len(api_trends))
    print("MYSQL:", len(mysql_trends))

    print("\n=== NULL_STATUS_COUNT ===")
    print("API  :", len(api_nulls))
    print("MYSQL:", len(mysql_nulls))

    print("\n=== STATUS_DIST ===")
    print("API  :", api_dist)
    print("MYSQL:", mysql_dist)

    api_ids, mysql_ids = set(api_snap), set(mysql_snap)
    print("\n=== campaign id 差集 ===")
    print("API only (前20):", sorted(api_ids - mysql_ids)[:20])
    print("MYSQL only (前20):", sorted(mysql_ids - api_ids)[:20])

    print("\n=== 营收/payout 差异（相对差 > 0.5% 才列出）===")
    diffs = []
    for cid in (api_ids & mysql_ids):
        a, m = api_snap[cid], mysql_snap[cid]
        ar, mr = a.get("revenue") or 0, m.get("revenue") or 0
        if ar or mr:
            rel = abs(ar - mr) / max(abs(ar), abs(mr), 1e-9)
            if rel > 0.005:
                diffs.append((cid, round(ar, 2), round(mr, 2), round(rel * 100, 2)))
    print("差异 campaign 数:", len(diffs))
    for d in diffs[:40]:
        print("   ", d)

    # 名称一致性抽检
    name_mismatch = [cid for cid in (api_ids & mysql_ids)
                    if (api_snap[cid].get("name") or "") != (mysql_snap[cid].get("name") or "")]
    print("\n=== 名称不一致 campaign 数 ===", len(name_mismatch))
    for cid in name_mismatch[:10]:
        print("   ", cid, "| API:", api_snap[cid].get("name"), "| MYSQL:", mysql_snap[cid].get("name"))

    print("\n=== 结论 ===")
    print("两路头部 campaign 数:", len(api_snap), "vs", len(mysql_snap))
    print("两路 null status:", len(api_nulls), "vs", len(mysql_nulls))
    print("两路营收差异(>0.5%):", len(diffs))
else:
    print("\n!! 至少一路未成功，无法对拍（见上方异常）")

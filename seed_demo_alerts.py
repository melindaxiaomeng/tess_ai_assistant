"""往 Tess 预警库灌一批演示数据，供前端（抽屉 / 列表页）联调使用。

用法：
    python seed_demo_alerts.py            # 清空 alerts 表后重新灌入演示批次
    python seed_demo_alerts.py --keep     # 不清空，直接追加（避免覆盖真实预警）

数据来源：tess_backend.dev_seed.build_demo_results（与运行时 API 端点共用，避免漂移）。

落点：默认（TESS_DATABASE_URL 为空）→ tess_alerts.db；生产填了 Postgres → 直接写生产库。
"""

import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 手动注入 .env（与后端运行环境一致）
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from tess_backend.alerts_store import AlertStore, Alert
from tess_backend.dev_seed import build_demo_results, ANOMALY_WARNINGS
from sqlalchemy import select, text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="不清空 alerts 表，直接追加演示数据")
    args = parser.parse_args()

    store = AlertStore()
    engine = store.engine

    if not args.keep:
        with store.Session() as s:
            s.execute(text("DELETE FROM alerts"))
            s.commit()
        print("🧹 已清空 alerts 表（演示刷新模式）")
    else:
        print("➕ 追加模式：保留现有数据，仅新增演示批次")

    run_time = time.strftime("%Y-%m-%d %H:%M:%S")
    results = build_demo_results()
    written = store.save_batch(results, run_time=run_time)
    print(f"✅ 已写入 {written} 条演示预警（批次 run_time={run_time}）")

    # 处理需要预置「已确认」态的条目
    acked = 0
    with store.Session() as s:
        rows = s.execute(select(Alert).where(Alert.run_time == run_time)).scalars().all()
        for a in rows:
            for r in ANOMALY_WARNINGS:
                if r["event_id"] == a.event_id and "ack" in r:
                    ack = r["ack"]
                    a.acked_at = time.strftime("%Y-%m-%d %H:%M:%S")
                    a.resolution = ack["resolution"]
                    a.acked_by = ack.get("acked_by")
                    a.ack_note = ack.get("note")
                    acked += 1
            s.commit()
    if acked:
        print(f"📌 已预置 {acked} 条为「已处理(resolved)」态（include_acked=false 时不再返回）")

    print("\n=== 演示数据分布 ===")
    print(f"  anomaly-warning : {sum(1 for r in results if r['meta']['source']=='anomaly-warning')} 条")
    print(f"  realtime-kpi    : {sum(1 for r in results if r['meta']['source']=='realtime-kpi')} 条")
    print("  状态覆盖        : DIAGNOSED / DIAGNOSED_SUSPECT / INCONCLUSIVE")
    print("  严重度覆盖      : HIGH / MEDIUM / LOW")
    print("\n下一步：起本地服务后，前端即可拉取：")
    print("  GET /tess/alerts")
    print("  GET /tess/realtime-kpi/alerts")


if __name__ == "__main__":
    main()

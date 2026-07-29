"""P1 规则引擎单测：Severity 四档 + max 聚合边界。"""

from tess_backend.rule_engine import compute_severity, calculate_loss_per_hour
from tess_backend.contracts import (
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
)


def test_severity_critical():
    # 倒贴 + 高损失
    assert compute_severity(margin_pct=-1.0, loss_per_hour_usd=600) == SEVERITY_CRITICAL


def test_severity_high_r6_example():
    # PRD §4.3 数据一致性自检示例：Margin 3.8% / Loss $350 -> HIGH（不再误标 CRITICAL）
    assert compute_severity(margin_pct=3.8, loss_per_hour_usd=350) == SEVERITY_HIGH


def test_severity_medium():
    assert compute_severity(margin_pct=12.0, loss_per_hour_usd=50) == SEVERITY_MEDIUM


def test_severity_low():
    assert compute_severity(margin_pct=20.0, loss_per_hour_usd=5) == SEVERITY_LOW


def test_severity_max_aggregates_margin_high_loss_low():
    # margin 命中 HIGH 但 loss 仅 LOW -> 取最严重 HIGH
    assert compute_severity(margin_pct=5.0, loss_per_hour_usd=5) == SEVERITY_HIGH


def test_severity_max_aggregates_loss_critical_margin_high():
    # margin 仅 HIGH 但 loss 达 CRITICAL -> 取 CRITICAL
    assert compute_severity(margin_pct=5.0, loss_per_hour_usd=600) == SEVERITY_CRITICAL


def test_severity_boundaries():
    # 边界：margin 恰好 0 -> HIGH；恰好 10 -> MEDIUM；恰好 15 -> LOW
    assert compute_severity(margin_pct=0.0, loss_per_hour_usd=0) == SEVERITY_HIGH
    assert compute_severity(margin_pct=10.0, loss_per_hour_usd=0) == SEVERITY_MEDIUM
    assert compute_severity(margin_pct=15.0, loss_per_hour_usd=0) == SEVERITY_LOW


def test_calculate_loss_per_hour():
    # 损耗 = 仍在燃烧的消耗 + 缺失收益
    assert calculate_loss_per_hour(300.0, 50.0) == 350.0
    # 负值防御：不为负
    assert calculate_loss_per_hour(-100.0, 0.0) == 0.0
    # None 容错
    assert calculate_loss_per_hour(None, None) == 0.0

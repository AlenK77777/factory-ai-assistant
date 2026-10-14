"""days_late is computed in SQL, not in Python and not by the model."""

from mcp_server.server import get_order_status


def test_days_late_is_computed_in_sql_with_current_date(monkeypatch):
    captured = []

    def fake_fetch(query, params=()):
        captured.append(query)
        if "defect_type" in query:
            return [{"defect_type": "Open", "count": 1, "units": 14}]
        return [
            {
                "order_id": "PO-10049",
                "customer_sector": "Medical",
                "product_type": "Rigid-Flex",
                "layers": 8,
                "quantity": 110,
                "status": "In QA",
                "production_line": "Line 2",
                "created_date": "2026-06-06",
                "due_date": "2026-07-03",
                "is_late": True,
                "days_late": 103,
                "defect_count": 1,
                "defective_units": 14,
            }
        ]

    monkeypatch.setattr("mcp_server.server.fetch_all", fake_fetch)
    result = get_order_status("po-10049")
    assert result.days_late == 103
    assert result.is_late is True
    sql = captured[0]
    assert "CURRENT_DATE - o.due_date" in sql
    assert "AS days_late" in sql
    assert "CURRENT_DATE" in sql

"""纯卡片渲染；不访问网络、账本或策略状态。"""
from __future__ import annotations


def message_card(title: str, color: str, lines: list[str], tag: str = "") -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
            "text_tag_list": [
                {"tag": "text_tag", "text": {"tag": "plain_text", "content": tag}, "color": color}
            ] if tag else [],
        },
        "body": {
            "direction": "vertical", "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": [{"tag": "markdown", "content": line} for line in lines],
        },
    }


def signal_button(action: dict) -> dict:
    label = "买入" if action["side"] == "buy" else "卖出"
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": f"登记实际{label} {action['name']}({action['code']})"},
        "type": "default",
        "behaviors": [{"type": "callback", "value": {
            "action": f"confirm_{action['side']}", "code": action["code"],
            "signal_id": action["notification_fingerprint"],
        }}],
    }


def metric_column(text: str, color: str = "grey") -> dict:
    return {
        "tag": "column", "width": "weighted", "weight": 1,
        "background_style": f"{color}-50", "padding": "12px", "vertical_spacing": "2px",
        "elements": [{"tag": "markdown", "content": text, "text_align": "center"}],
    }


def scheduled_card(report: dict, message: str) -> dict:
    analysis = report["analysis"]
    market = analysis.get("market", {})
    color = {"red": "red", "green": "green"}.get(market.get("state"), "yellow")
    card = message_card(f"A股{report.get('label', '收盘总结')}", color, [], market.get("label", "未核验"))
    card["header"]["subtitle"] = {
        "tag": "plain_text",
        "content": f"{analysis.get('trade_date', '')} · 数据截至 {analysis.get('data_as_of', '')}",
    }
    card["header"]["icon"] = {"tag": "standard_icon", "token": "calendar_colorful"}
    columns = []
    for index in analysis.get("indices", [])[:4]:
        change = index.get("change_pct")
        pct = "—" if change is None else f"{'+' if change > 0 else ''}{change:.2f}%"
        index_color = "green" if change is not None and change > 0 else ("red" if change is not None and change < 0 else "grey")
        column = metric_column("", index_color)
        column["elements"] = [
            {"tag": "markdown", "content": f"**{index.get('name', '')}**", "text_align": "center"},
            {"tag": "markdown", "content": f"## <font color='{index_color}'>{index.get('price', '—')}</font>", "text_align": "center"},
            {"tag": "markdown", "content": f"<font color='grey'>{pct}</font>", "text_align": "center", "text_size": "notation"},
        ]
        columns.append(column)
    elements = card["body"]["elements"]
    if columns:
        elements.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px", "columns": columns})
    breadth = analysis.get("breadth", {})
    average = breadth.get("average_change_pct")
    pct = "—" if average is None else f"{'+' if average > 0 else ''}{average:.2f}%"
    elements.append({
        "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
        "columns": [
            metric_column(f"上涨 {breadth.get('up', 0)}", "green"),
            metric_column(f"下跌 {breadth.get('down', 0)}", "red"),
            metric_column(f"平盘 {breadth.get('flat', 0)}"),
            metric_column(f"**平均 {pct}**"),
        ],
    })
    elements.append({"tag": "markdown", "content": message})
    elements.append({
        "tag": "markdown",
        "content": f"<font color='grey'>{market.get('summary', '')} · 仅供研究参考，不构成个性化投资建议</font>",
        "text_size": "notation",
    })
    return card

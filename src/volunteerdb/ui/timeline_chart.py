"""Gantt-style membership timeline (ECharts custom series, no extra deps).

Colors are concrete hexes because an ECharts option can't read CSS custom
properties; both role ramps are ordinal one-hue terracotta scales validated
for monotone lightness and step separation against the matching surface.
Like the cytoscape graph, the chart re-tints on the next page load after a
dark-mode toggle.
"""

import html
from datetime import date, datetime

from nicegui import ui

from ..models import ROLE_LABELS, TeamRole

ROLE_COLORS_LIGHT = {
    TeamRole.leader: "#6f3322",
    TeamRole.second: "#a5573e",
    TeamRole.core: "#c97b5d",
    TeamRole.member: "#e5a184",
}
# most responsibility = strongest step; the anchor flips on the dark surface
ROLE_COLORS_DARK = {
    TeamRole.leader: "#f0b695",
    TeamRole.second: "#d18d6b",
    TeamRole.core: "#b16a4c",
    TeamRole.member: "#8f4e35",
}

_LIGHT = {"surface": "#fffbee", "ink": "#333333", "muted": "#6b6255", "rule": "#d8c9a3"}
_DARK = {"surface": "#2a2622", "ink": "#e8e0ce", "muted": "#a89e8f", "rule": "#4a443c"}

_SERIF = 'Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif'

# the echarts global isn't reachable from NiceGUI-converted functions, so the
# bar is clipped to the grid by hand instead of echarts.graphic.clipRectByRect
_RENDER_ITEM = """
function (params, api) {
  const row = api.value(0);
  const start = api.coord([api.value(1), row]);
  const end = api.coord([api.value(2), row]);
  const height = Math.min(api.size([0, 1])[1] * 0.6, 22);
  const cs = params.coordSys;
  const x0 = Math.max(start[0], cs.x);
  const x1 = Math.min(end[0], cs.x + cs.width);
  if (x1 <= x0) return null;
  return {
    type: "rect",
    shape: {x: x0, y: start[1] - height / 2, width: Math.max(x1 - x0, 2), height: height, r: 2},
    style: api.style(),
  };
}
"""


def _duration_text(start: date, end: date | None) -> str:
    days = ((end or date.today()) - start).days
    if days < 60:
        days = max(days, 1)
        return f"{days} day{'s' if days != 1 else ''}"
    years, months = divmod(round(days / 30.44), 12)
    if years == 0:
        return f"{months} mo"
    return f"{years} yr {months} mo" if months else f"{years} yr"


def _tip(row_label: str, role: TeamRole, start: date, end: date | None) -> str:
    when = f"{start.isoformat()} → {end.isoformat() if end else 'ongoing'}"
    return (
        f"<b>{html.escape(row_label)}</b><br>"
        f"{ROLE_LABELS[role]}<br>"
        f"{when} · {_duration_text(start, end)}"
    )


def timeline_chart(spells, paths: dict[int, str], dark: bool) -> None:
    """One bar per membership spell, segmented by role, teams on the y-axis."""
    if not spells:
        ui.label("No membership history recorded yet.").classes("text-gray-500")
        return

    chrome = _DARK if dark else _LIGHT
    palette = ROLE_COLORS_DARK if dark else ROLE_COLORS_LIGHT

    row_labels: list[str] = []
    row_of: dict[int, int] = {}
    for spell in spells:  # already sorted by first start
        if spell.team_id not in row_of:
            row_of[spell.team_id] = len(row_labels)
            label = paths.get(spell.team_id, f"{spell.team_name} (deleted)")
            row_labels.append(label)

    now_ms = int(datetime.now().astimezone().timestamp() * 1000)
    data: dict[TeamRole, list[dict]] = {role: [] for role in TeamRole}
    for spell in spells:
        label = row_labels[row_of[spell.team_id]]
        for seg in spell.segments:
            seg_start = seg.start.astimezone().date()
            seg_end = seg.end.astimezone().date() if seg.end else None
            data[seg.role].append(
                {
                    "value": [
                        row_of[spell.team_id],
                        int(seg.start.timestamp() * 1000),
                        int(seg.end.timestamp() * 1000) if seg.end else now_ms,
                    ],
                    "tip": _tip(label, seg.role, seg_start, seg_end),
                }
            )

    axis_line = {"lineStyle": {"color": chrome["rule"]}}
    option = {
        "animation": False,
        "textStyle": {"fontFamily": _SERIF, "color": chrome["ink"]},
        "legend": {
            "top": 0,
            "data": [ROLE_LABELS[role] for role in TeamRole if data[role]],
            "textStyle": {"color": chrome["ink"], "fontFamily": _SERIF},
            "itemWidth": 14,
            "itemHeight": 8,
            "icon": "roundRect",
        },
        "grid": {"left": 8, "right": 16, "top": 40, "bottom": 8, "containLabel": True},
        "xAxis": {
            "type": "time",
            "axisLabel": {"color": chrome["muted"], "fontFamily": _SERIF},
            "axisLine": axis_line,
            "splitLine": {
                "show": True,
                "lineStyle": {"color": chrome["rule"], "opacity": 0.5},
            },
        },
        "yAxis": {
            "type": "category",
            "data": row_labels,
            "inverse": True,
            "axisLabel": {"color": chrome["ink"], "fontFamily": _SERIF},
            "axisLine": axis_line,
            "axisTick": {"show": False},
        },
        "tooltip": {
            "trigger": "item",
            "backgroundColor": "#4a443c",  # matches .q-tooltip in theme.css
            "borderColor": "#4a443c",
            "textStyle": {"color": "#f5efdd", "fontFamily": _SERIF},
            ":formatter": "(p) => p.data.tip",
        },
        "series": [
            {
                "name": ROLE_LABELS[role],
                "type": "custom",
                ":renderItem": _RENDER_ITEM,
                "encode": {"x": [1, 2], "y": 0},
                # 2px surface-colored border keeps abutting segments/spells distinct
                "itemStyle": {
                    "color": palette[role],
                    "borderColor": chrome["surface"],
                    "borderWidth": 2,
                },
                "data": data[role],
            }
            for role in TeamRole
            if data[role]
        ],
    }
    height = max(160, 96 + 34 * len(row_labels))
    ui.echart(option).classes("w-full").style(f"height:{height}px")

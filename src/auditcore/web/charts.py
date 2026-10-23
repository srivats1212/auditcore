"""Server-rendered SVG bar chart for per-mode annual trends.

Two series (483 citations, warning-letter citations), grouped by year. Colors
are CSS variables (categorical slots 1 and 2, validated for light and dark
surfaces) so the page theme controls them. Each bar carries a <title> for
hover; the page also renders the same numbers as a table.
"""

from __future__ import annotations

from html import escape

SERIES = (("483", "Form 483", "var(--series-1)"), ("wl", "Warning letter", "var(--series-2)"))


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Bar with rounded top corners only; the baseline end stays square."""
    if h <= 0:
        return ""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def trend_svg(trend: dict[int, dict[str, int]], *, width: int = 480, height: int = 200) -> str:
    if not trend:
        return ""
    years = sorted(trend)
    peak = max(max(v.values()) for v in trend.values()) or 1
    top = max(1, _nice(peak))
    left, right, top_pad, bottom = 36, 8, 12, 28
    plot_w, plot_h = width - left - right, height - top_pad - bottom
    group_w = plot_w / len(years)
    bar_w = max(4.0, min(22.0, (group_w - 10) / 2))
    gap = 2.0  # surface gap between adjacent bars

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Citations per year" class="chart">']
    for frac in (0, 0.5, 1):
        y = top_pad + plot_h * (1 - frac)
        parts.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="tick">{int(top * frac)}</text>')
    for i, year in enumerate(years):
        cx = left + group_w * i + group_w / 2
        x0 = cx - bar_w - gap / 2
        for j, (key, label, color) in enumerate(SERIES):
            v = trend[year].get(key, 0)
            h = plot_h * v / top
            x = x0 + j * (bar_w + gap)
            d = _bar_path(x, top_pad + plot_h - h, bar_w, h)
            # Invisible hit target spans the full plot height so small bars are hoverable.
            parts.append(f'<g class="bar"><title>{year}, {escape(label)}: {v} citations</title>'
                         f'<rect x="{x:.1f}" y="{top_pad}" width="{bar_w:.1f}" height="{plot_h}" fill="transparent"/>')
            if d:
                parts.append(f'<path d="{d}" fill="{color}"/>')
            parts.append("</g>")
        parts.append(f'<text x="{cx:.1f}" y="{height - 8}" text-anchor="middle" class="tick">{year}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _nice(n: int) -> int:
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if n <= step * 4:
            return step * -(-n // step)
    return n

"""知识区关系星图静态自检（不启动浏览器）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASS = 0
FAILS: list[str] = []


def ok(name, condition, extra=""):
    global PASS
    if condition:
        PASS += 1
        print("PASS ", name)
    else:
        FAILS.append(name)
        print("FAIL ", name, "|", str(extra)[:300])


def main():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    checks = [
        ("星图数据构建", "function kgBuildGraph" in html and "kind: 'knowledge'" in html and "kind: 'document'" in html),
        ("星图节点类型", all(x in html for x in ["kind: 'note'", "kind: 'mindmap'", "kind: 'zone'"])),
        ("星图加载接口", "/documents?zone_id=" in html and "apiGet('/notes')" in html and "/mindmaps?zone_id=" in html),
        ("星图绘制", "function kgDrawScene" in html and "kg-link-hierarchy" in html and "'kg-node kg-node-' + node.kind" in html),
        ("搜索与筛选", "data-kg-filter" in html and "libraryGraphFilters" in html and "kgSearch" in html),
        ("缩放拖拽", "data-kg-zoom" in html and "kgScreenToGraph" in html and "pointermove" in html),
        ("节点详情", "kgDetailHtml" in html and "查看该节点资料" in html and "打开资料列表" in html),
        ("视图切换", "data-lib-view=\"graph\"" in html and "data-lib-view=\"list\"" in html),
        ("持续力导向", "function kgStep" in html and "requestAnimationFrame" in html and "kgStartTicker" in html),
        ("列表树排布", "function libraryDocTreeHtml" in html and "kg-doc-group" in html and "documentListItemHtml" in html),
        ("保留列表管理", "data-doc-reindex" in html and "data-doc-node-select" in html),
        ("不引入图谱库", all(x not in html for x in ["d3.min.js", "vis-network.min.js", "cytoscape.min.js", "cdn.jsdelivr.net/npm/d3"])),
        ("DocLib 归因", "DocLib 2.0" in html and "MIT" in html),
    ]
    for name, condition in checks:
        ok(name, condition)
    print(f"\n== {PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败项：", FAILS)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
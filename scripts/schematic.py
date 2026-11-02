#!/usr/bin/env python3
"""原理图页 → IR → 网络。

IR 是解析结果的中间表示，字段：
  project / page{uuid,name,docType} / components[] / wires[] / nets[] / warnings[]

连通性判定只认三种几何接触：
  1. 点与点重合（容差 0.02，即 TOL）
  2. 线段端点与另一线段端点重合
  3. 点严格落在线段内部（EDA 的 junction 语义）

不做 ±1 以上的宽松匹配，否则平行引线会被误短接。
"""
import math

import epru
import library

TOL = 0.02


class UnionFind(object):
    def __init__(self):
        self.parent = {}

    def add(self, key):
        if key not in self.parent:
            self.parent[key] = key

    def find(self, key):
        self.add(key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self):
        out = {}
        for key in list(self.parent):
            out.setdefault(self.find(key), []).append(key)
        return out


def point_key(x, y):
    return "%.2f,%.2f" % (round(float(x), 2), round(float(y), 2))


def _on_segment(px, py, x1, y1, x2, y2, eps=TOL):
    dx, dy = x2 - x1, y2 - y1
    sq = dx * dx + dy * dy
    if sq == 0:
        return False
    cross = dx * (py - y1) - dy * (px - x1)
    if abs(cross) > eps * math.sqrt(sq):
        return False
    t = ((px - x1) * dx + (py - y1) * dy) / sq
    return 0.001 < t < 0.999


class _SegmentGrid(object):
    """线段的空间索引：按包围盒登记进固定尺寸网格桶。

    只用来**筛掉**不可能接触的线段，接触判定仍然交给 `_on_segment`，
    所以连通性语义与逐条遍历完全一致。

    - 线段跨格时登记它覆盖的所有格子，不只登记质心格；
    - 引脚查本格 + 邻格（3×3）：格边长 ≥ 10×TOL，落在格边界 tol 内的引脚
      最多只差一格，查邻格才不会漏；
    - 超长线段跨格数太多（一条线登记上万个桶反而更慢），降级到 8 倍粗网格，
      粗网格仍超限则放进 loose 线性表。
    """

    MAX_CELLS = 64

    def __init__(self, segments, tol):
        self.segments = segments
        self.buckets = {}
        self.coarse = {}
        self.loose = []
        if not segments:
            self.cell = max(tol * 10.0, 1.0)
            self.coarse_cell = self.cell * 8.0
            return
        xs, ys = [], []
        for x1, y1, x2, y2 in segments:
            xs.extend((x1, x2))
            ys.extend((y1, y2))
        span = max(max(xs) - min(xs), max(ys) - min(ys), 0.0)
        # 自适应边长：让网格大约是 sqrt(线段数) × sqrt(线段数) 的规模
        self.cell = max(tol * 10.0, span / max(1.0, math.sqrt(len(segments))))
        self.coarse_cell = self.cell * 8.0
        for index, seg in enumerate(segments):
            if self._insert(index, seg, self.cell, self.buckets):
                continue
            if self._insert(index, seg, self.coarse_cell, self.coarse):
                continue
            self.loose.append(index)

    def _cells(self, seg, cell):
        x1, y1, x2, y2 = seg
        i0 = int(math.floor(min(x1, x2) / cell))
        i1 = int(math.floor(max(x1, x2) / cell))
        j0 = int(math.floor(min(y1, y2) / cell))
        j1 = int(math.floor(max(y1, y2) / cell))
        return i0, i1, j0, j1

    def _insert(self, index, seg, cell, buckets):
        """登记成功返回 True；跨格数超过上限则放弃（交给更粗的层）。"""
        i0, i1, j0, j1 = self._cells(seg, cell)
        if (i1 - i0 + 1) * (j1 - j0 + 1) > self.MAX_CELLS:
            return False
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                buckets.setdefault((i, j), []).append(index)
        return True

    def query(self, px, py):
        """返回可能与 (px, py) 接触的线段下标（含少量假阳性，由调用方精确判定）。"""
        if not self.segments:
            return ()
        out = list(self.loose)
        ci = int(math.floor(px / self.cell))
        cj = int(math.floor(py / self.cell))
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                out.extend(self.buckets.get((ci + di, cj + dj), ()))
        if self.coarse:
            ii = int(math.floor(px / self.coarse_cell))
            jj = int(math.floor(py / self.coarse_cell))
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    out.extend(self.coarse.get((ii + di, jj + dj), ()))
        return out


def page_to_ir(doc, symbols, devices, project=None, page_name=None):
    by_type, meta = epru.doc_elements(doc)
    attrs_by_parent = epru.attr_map(by_type)

    ir = {
        "project": project,
        "page": {
            "uuid": doc["uuid"],
            "name": page_name or meta.get("title") or "",
            "docType": doc["docType"],
        },
        "components": [],
        "wires": [],
        "nets": [],
        "warnings": [],
    }

    for cid, comp in by_type.get("COMPONENT", []):
        if not isinstance(comp, dict):
            continue
        inline = comp.get("attrs") or {}
        merged = dict(attrs_by_parent.get(cid, {}))
        for key, value in inline.items():
            merged.setdefault(key, value)

        dev_uuid = library.device_uuid_of(inline, merged)
        device = devices.get(dev_uuid) or {}
        dev_attrs = device.get("attributes") or {}
        sym_uuid = merged.get("Symbol") or dev_attrs.get("Symbol")
        symbol = symbols.get(sym_uuid) or {}
        ctype = library.component_type(symbol, device)

        item = {
            "id": cid,
            "componentType": ctype,
            "designator": merged.get("Designator") or dev_attrs.get("Designator"),
            "deviceUuid": dev_uuid,
            "deviceTitle": device.get("title") or merged.get("Name"),
            "manufacturerPart": (merged.get("Manufacturer Part")
                                 or dev_attrs.get("Manufacturer Part")
                                 or dev_attrs.get("Name")),
            "value": (merged.get("Value") or dev_attrs.get("Value")
                      or library.value_like(merged.get("Name"))
                      or library.value_like(dev_attrs.get("Name"))),
            "x": round(float(comp.get("x") or 0.0), 2),
            "y": round(library.Y_SIGN * float(comp.get("y") or 0.0), 2),
            "rotation": comp.get("rotation") or 0,
            "mirror": bool(comp.get("isMirror")),
            "pins": [],
            "attrs": {k: v for k, v in merged.items() if v is not None},
        }
        # 记录的摆放字段名（isMirror）与变换函数的入参名（mirror）不同，
        # 归一集中由 library.placement_of() 负责；不归一镜像分支就读不到，
        # 旋转+镜像的元件会被当成没镜像（引脚左右颠倒）。
        placement = library.placement_of(comp)
        for _pid, pin in (symbol.get("pins") or {}).items():
            ax, ay = library.transform_pin(pin["x"], pin["y"], placement)
            item["pins"].append({
                "number": pin.get("number"),
                "name": pin.get("name"),
                "x": round(ax, 2),
                "y": round(library.Y_SIGN * ay, 2),
            })
        if ctype in ("netflag", "netport"):
            item["net"] = (merged.get("Global Net Name")
                           or symbol.get("globalNetName")
                           or merged.get("Name")
                           or dev_attrs.get("Name"))
        ir["components"].append(item)

    wire_net = {}
    for wid, wire in by_type.get("WIRE", []):
        wire_net[wid] = (attrs_by_parent.get(wid, {}) or {}).get("NET") or ""

    for lid, line in by_type.get("LINE", []):
        if not isinstance(line, dict):
            continue
        group = line.get("lineGroup")
        if group not in wire_net:
            continue
        x1 = float(line.get("startX") or 0.0)
        y1 = library.Y_SIGN * float(line.get("startY") or 0.0)
        x2 = float(line.get("endX") or 0.0)
        y2 = library.Y_SIGN * float(line.get("endY") or 0.0)
        if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
            continue
        ir["wires"].append({
            "id": lid,
            "line": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
            "net": wire_net.get(group) or "",
        })
    return ir


def build_nets(ir, tol=TOL):
    """填 ir["nets"]，返回 {(designator, pinNumber): netName}。"""
    uf = UnionFind()
    pins = []
    for comp in ir.get("components", []):
        for pin in comp.get("pins") or []:
            key = point_key(pin["x"], pin["y"])
            uf.add(key)
            pins.append((key, comp, pin))

    segments = []
    for wire in ir.get("wires", []):
        x1, y1, x2, y2 = wire["line"]
        a, b = point_key(x1, y1), point_key(x2, y2)
        uf.union(a, b)
        segments.append((x1, y1, x2, y2))

    grid = _SegmentGrid(segments, tol)
    for key, _comp, pin in pins:
        px, py = pin["x"], pin["y"]
        seen = set()
        for index in grid.query(px, py):
            if index in seen:
                continue
            seen.add(index)
            x1, y1, x2, y2 = segments[index]
            if min(x1, x2) - tol <= px <= max(x1, x2) + tol and \
               min(y1, y2) - tol <= py <= max(y1, y2) + tol:
                if _on_segment(px, py, x1, y1, x2, y2):
                    uf.union(key, point_key(x1, y1))

    # 显式网络名：导线 NET 属性 + 网络标识/端口元件
    names = {}
    for wire in ir.get("wires", []):
        name = (wire.get("net") or "").strip()
        if not name:
            continue
        x1, y1, x2, y2 = wire["line"]
        for key in (point_key(x1, y1), point_key(x2, y2)):
            names.setdefault(key, set()).add(name)

    for comp in ir.get("components", []):
        if comp.get("componentType") not in ("netflag", "netport"):
            continue
        name = (comp.get("net") or "").strip()
        if not name:
            continue
        for pin in comp.get("pins") or []:
            names.setdefault(point_key(pin["x"], pin["y"]), set()).add(name)

    # 同名网络全局合并
    root_names = {}
    for key, ns in names.items():
        root_names.setdefault(uf.find(key), set()).update(ns)

    by_name = {}
    for root, ns in root_names.items():
        for name in ns:
            by_name.setdefault(name, []).append(root)
    for roots in by_name.values():
        for root in roots[1:]:
            uf.union(roots[0], root)

    # 合并后重算，根节点可能已经变了
    root_names = {}
    for key, ns in names.items():
        root_names.setdefault(uf.find(key), set()).update(ns)

    groups = uf.groups()
    # 一次遍历把引脚按根节点分好组：原来是 groups × pins 的双重循环，
    # 大工程下是 O(网络数×引脚数)。分组顺序沿用 groups 的顺序，
    # 未命名网络的 N$n 编号因此保持不变。
    members_by_root = {}
    for key, comp, pin in pins:
        members_by_root.setdefault(uf.find(key), []).append({
            "designator": comp.get("designator"),
            "pinNumber": pin.get("number"),
            "pinName": pin.get("name"),
            "componentType": comp.get("componentType"),
        })

    pin_net = {}
    nets = []
    for root in groups:
        members = members_by_root.get(root) or []
        if not members:
            continue
        ns = sorted(root_names.get(root, set()))
        name = ns[0] if ns else "N$%d" % len(nets)
        nets.append({
            "name": name,
            "named": bool(ns),
            "conflicts": ns[1:],
            "pins": members,
        })
        for m in members:
            pin_net[(m["designator"], m["pinNumber"])] = name

    nets.sort(key=lambda n: (not n["named"], n["name"]))
    ir["nets"] = nets

    conflicts = [n for n in nets if n["conflicts"]]
    if conflicts:
        ir["warnings"].append(
            "%d 个网络存在多个名字（可能是真短路或历史残留线段）" % len(conflicts))
    if not ir.get("wires"):
        ir["warnings"].append("本页没有导线记录，连通性仅依赖引脚坐标重合")
    return pin_net


def validate(ir):
    warns = list(ir.get("warnings") or [])
    parts = [c for c in ir.get("components", []) if c.get("componentType") == "part"]
    if not parts:
        warns.append("没有识别到任何元件")
    elif not any(c.get("pins") for c in parts):
        warns.append("元件都没有引脚数据，符号表可能没解析到")
    missing = [c for c in parts if not c.get("designator")]
    if missing:
        warns.append("%d 个元件没有位号" % len(missing))
    return warns

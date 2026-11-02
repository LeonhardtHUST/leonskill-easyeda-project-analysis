#!/usr/bin/env python3
"""原理图页 → IR → 网络。

IR 是解析结果的中间表示，字段：
  project / page{uuid,name,docType} / components[] / wires[] / nets[] / warnings[]

连通性判定只认三种几何接触：
  1. 点与点重合（容差 0.01）
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
        for _pid, pin in (symbol.get("pins") or {}).items():
            ax, ay = library.transform_pin(pin["x"], pin["y"], comp)
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

    for key, _comp, pin in pins:
        px, py = pin["x"], pin["y"]
        for x1, y1, x2, y2 in segments:
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
    pin_net = {}
    nets = []
    for root, keys in groups.items():
        members = []
        for key, comp, pin in pins:
            if uf.find(key) == root:
                members.append({
                    "designator": comp.get("designator"),
                    "pinNumber": pin.get("number"),
                    "pinName": pin.get("name"),
                    "componentType": comp.get("componentType"),
                })
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

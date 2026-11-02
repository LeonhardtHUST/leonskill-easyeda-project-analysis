#!/usr/bin/env python3
"""PCB 文档解析：权威网表 + 几何。

权威网表规则:
  - 只有带 Designator 属性的 COMPONENT 是当前元件，无位号的是删除残留
  - PAD_NET 的 id 形如 ["PAD_NET", 元件id, 焊盘号, 引脚id]，同一 (元件, 焊盘)
    有多条历史记录，只取 ticket 最大的那条
  - 引脚号必须经元件所属符号解析：引脚 id 只在符号文档内唯一

几何单位:
  - PCB 坐标为 mil，与 CANVAS.unit 写的 mm 无关
  - 焊盘绝对坐标 = 元件坐标 + 元件角度旋转后的封装本地坐标
"""
import json
import math

import epru
import library

MIL_TO_MM = 0.0254
BOARD_OUTLINE_LAYER = 11          # 板框所在层 id（实测）
COPPER_TYPES = {"TOP", "BOTTOM", "INNER", "INNER1", "INNER2", "PLANE"}


def _as_id(rid):
    if isinstance(rid, str):
        try:
            parsed = json.loads(rid)
            return parsed if isinstance(parsed, list) else rid
        except ValueError:
            return rid
    return rid


def pcb_docs(docs):
    return [d for d in docs if d["docType"] == "PCB"]


def _component_meta(by_type, devices=None):
    """{compId: {designator, footprint, symbol, device, layerId, x, y, angle}}

    PCB 侧元件通常只有 Device 属性，没有 Symbol；符号必须经 DEVICE 文档的
    attributes.Symbol 反查，否则焊盘号无法映射到引脚号。
    """
    attrs = epru.attr_map(by_type)
    devices = devices or {}
    out = {}
    for cid, comp in by_type.get("COMPONENT", []):
        if not isinstance(comp, dict):
            continue
        merged = dict(attrs.get(cid, {}))
        for key, value in (comp.get("attrs") or {}).items():
            merged.setdefault(key, value)
        device_uuid = merged.get("Device")
        symbol_uuid = merged.get("Symbol")
        if not symbol_uuid and device_uuid:
            device = devices.get(device_uuid) or {}
            symbol_uuid = (device.get("attributes") or {}).get("Symbol")
        # 不能用 or 链：x / y / angle 为 0 是合法值（原点上的元件、0 度摆放），
        # 用 or 会掉到 centerX / rotation 上，读出错位的坐标。
        x = comp.get("x")
        if x is None:
            x = comp.get("centerX")
        y = comp.get("y")
        if y is None:
            y = comp.get("centerY")
        angle = comp.get("angle")
        if angle is None:
            angle = comp.get("rotation")
        out[cid] = {
            "designator": merged.get("Designator"),
            "footprint": merged.get("Footprint"),
            "symbol": symbol_uuid,
            "device": device_uuid,
            "layerId": comp.get("layerId"),
            "x": 0.0 if x is None else x,
            "y": 0.0 if y is None else y,
            "angle": 0 if angle is None else angle,
        }
    return out


def pad_net_map(doc):
    """{(compId, padNum): (ticket, {padId, net})}，同键取最大 ticket。"""
    best = {}
    for (rtype, rid), (ticket, payload) in doc["elems"].items():
        if rtype != "PAD_NET" or not isinstance(payload, dict):
            continue
        parts = _as_id(rid)
        if not isinstance(parts, list) or len(parts) < 3:
            continue
        comp_id = str(parts[1])
        pad_num = str(parts[2])
        pin_id = str(parts[3]) if len(parts) > 3 else None
        key = (comp_id, pad_num)
        prev = best.get(key)
        if prev is None or ticket >= prev[0]:
            best[key] = (ticket, {"padId": pin_id, "net": payload.get("padNet")})
    return best


def build_netlist(docs, symbols, devices=None):
    """返回 (rows, warnings)。rows: [{designator, pad, pinNumber, pinName, net, ticket}]"""
    rows = []
    warnings = []
    unresolved = 0
    for doc in pcb_docs(docs):
        by_type, _meta = epru.doc_elements(doc)
        comps = _component_meta(by_type, devices)
        current = {cid: meta for cid, meta in comps.items() if meta.get("designator")}
        dropped = len(comps) - len(current)
        if dropped:
            warnings.append("PCB 侧剔除 %d 个无位号元件（删除残留）" % dropped)
        nets = pad_net_map(doc)
        for (comp_id, pad_num), (ticket, info) in nets.items():
            comp = current.get(comp_id)
            if not comp:
                continue
            pin_number = pin_name = None
            symbol = symbols.get(comp.get("symbol") or "") or {}
            pins = symbol.get("pins") or {}
            # 物理引脚号 = 封装焊盘号，这是唯一可靠的匹配键。
            # PAD_NET 第 4 个字段是封装焊盘 id，可能与符号引脚 id 撞名但指向别的脚，
            # 所以只能在没有同号引脚时才拿它兜底。
            pin = None
            for candidate in pins.values():
                if str(candidate.get("number")) == str(pad_num):
                    pin = candidate
                    break
            if pin is None:
                pin = pins.get(info.get("padId") or "")
            if pin:
                pin_number = pin.get("number")
                pin_name = pin.get("name")
            else:
                unresolved += 1
            rows.append({
                "designator": comp["designator"],
                "component": comp_id,
                "pad": pad_num,
                "pinNumber": pin_number,
                "pinName": pin_name,
                "net": info.get("net") or "",
                "ticket": ticket,
            })
    rows.sort(key=lambda r: (_natural(r["designator"]), _natural(r["pad"])))
    if unresolved:
        warnings.append("%d 个焊盘无法映射到引脚号（符号 Pin Number 缺失），"
                        "两侧网表比对会因此不完整" % unresolved)
    return rows, warnings


def _natural(text):
    text = str(text or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    prefix = "".join(ch for ch in text if not ch.isdigit())
    return (prefix, int(digits) if digits else -1)


def _rotate(x, y, deg):
    """封装本地坐标 → 元件坐标系。PCB 的 angle 是逆时针正方向（实测）。"""
    if not deg:
        return x, y
    theta = math.radians(deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return x * cos_t - y * sin_t, x * sin_t + y * cos_t


def _poly_points(payload):
    """板框路径 → [(x, y)]。支持 ["R",x,y,w,h,rot,r] 与点序列两种写法。"""
    path = payload.get("path") or payload.get("points") or payload.get("polygon")
    if not isinstance(path, list):
        return []
    points = []
    i = 0
    while i < len(path):
        item = path[i]
        if isinstance(item, str) and item.upper() == "R":
            try:
                x, y, w, h = (float(path[i + 1]), float(path[i + 2]),
                              float(path[i + 3]), float(path[i + 4]))
            except (IndexError, TypeError, ValueError):
                return points
            points.extend([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])
            i += 5
            # R 形式末尾跟着 rotation 与 radius 两个数字，它们不是坐标点。
            # 不跳过的话，任何不落在原点的矩形都会凭空多出一个点，
            # 包围盒被撑到 (0,0)～角点，板框尺寸能差出一个数量级。
            if i + 2 == len(path) and all(
                    isinstance(v, (int, float)) for v in path[i:i + 2]):
                i += 2
            continue
        if isinstance(item, (int, float)) and i + 1 < len(path) and \
                isinstance(path[i + 1], (int, float)):
            points.append((float(item), float(path[i + 1])))
            i += 2
            continue
        if isinstance(item, list) and len(item) >= 2:
            try:
                points.append((float(item[0]), float(item[1])))
            except (TypeError, ValueError):
                pass
        i += 1
    return points


def build_geometry(docs):
    """返回几何字典。所有长度同时给原始 mil 与换算后的 mm。

    工程可能有多个 PCB 文档（多板工程），走线/过孔/铜层计数必须跨文档累加：
    放在循环内会被后一个文档覆盖，只剩最后一个文档的数据。
    板框则逐板记在 `outlines` 里，`outline` 给所有板框的并集范围。

    `segments` 与 `pads` 是**逐图元**的原始记录（不做拓扑归并），供下游
    寄生提参 / 全波 coupon 之类需要端点坐标的消费者使用；`traces` 仍是
    按网络汇总的统计口径，两者不要混用。
    """
    geo = {
        "unit": "mil",
        "outline": None,
        "outlines": [],
        "layers": [],
        "stackup": [],
        "traces": [],
        "segments": [],
        "vias": [],
        "pads": [],
        "warnings": [],
    }
    footprints = library.build_footprints(docs)
    used_layers = set()
    net_len = {}
    via_sizes = {}
    outline_boxes = []
    for doc in pcb_docs(docs):
        by_type, _meta = epru.doc_elements(doc)

        for rid, layer in by_type.get("LAYER", []):
            parts = _as_id(rid)
            layer_id = parts[1] if isinstance(parts, list) and len(parts) > 1 else rid
            if isinstance(layer, dict):
                geo["layers"].append({
                    "id": layer_id,
                    "type": layer.get("layerType"),
                    "name": layer.get("layerName"),
                })

        for _lid, line in by_type.get("LINE", []):
            if not isinstance(line, dict):
                continue
            net = line.get("netName") or ""
            x1, y1 = float(line.get("startX") or 0.0), float(line.get("startY") or 0.0)
            x2, y2 = float(line.get("endX") or 0.0), float(line.get("endY") or 0.0)
            length = math.hypot(x2 - x1, y2 - y1)
            used_layers.add(line.get("layerId"))
            entry = net_len.setdefault(net, {"net": net, "segments": 0, "length_mil": 0.0,
                                             "widths": {}})
            entry["segments"] += 1
            entry["length_mil"] += length
            width = line.get("width")
            if width is not None:
                entry["widths"][str(width)] = entry["widths"].get(str(width), 0) + 1
            # 逐段原始记录：端点保留 mil，下游自己换算
            if length > 0:
                geo["segments"].append({
                    "net": net,
                    "layer_id": line.get("layerId"),
                    "x1": round(x1, 3), "y1": round(y1, 3),
                    "x2": round(x2, 3), "y2": round(y2, 3),
                    "width": width,
                })

        for _vid, via in by_type.get("VIA", []):
            if not isinstance(via, dict):
                continue
            hole = via.get("holeDiameter")
            via_sizes[str(hole)] = via_sizes.get(str(hole), 0) + 1
            geo["vias"].append({
                "net": via.get("netName") or "",
                "x": via.get("centerX"),
                "y": via.get("centerY"),
                "hole": hole,
                "diameter": via.get("viaDiameter"),
                "type": via.get("viaType"),
            })

        # 焊盘绝对坐标 = 元件坐标 + 旋转后的封装本地坐标。
        # 只保留有位号的元件：无位号的是删除残留，它们的焊盘不是当前设计。
        comps = _component_meta(by_type)
        pad_nets = pad_net_map(doc)
        for comp_id, comp in comps.items():
            designator = comp.get("designator")
            if not designator:
                continue
            footprint = footprints.get(comp.get("footprint") or "") or {}
            for pad in footprint.get("pads") or []:
                num = str(pad.get("num"))
                ax, ay = _rotate(pad["x"], pad["y"], comp.get("angle") or 0.0)
                layer_id = pad.get("layer_id") or 12
                if int(layer_id) == 12:            # MULTI = 通孔盘，落在元件所在层
                    layer_id = comp.get("layerId") or 1
                hit = pad_nets.get((comp_id, num))
                geo["pads"].append({
                    "component": designator,
                    "pin": num,
                    "net": (hit[1].get("net") if hit else None) or "",
                    "x": round(comp.get("x") + ax, 3),
                    "y": round(comp.get("y") + ay, 3),
                    "layer_id": int(layer_id),
                    "w": pad.get("w"),
                    "h": pad.get("h"),
                })
                used_layers.add(int(layer_id))

        points = []
        for _pid, poly in by_type.get("POLY", []):
            if not isinstance(poly, dict):
                continue
            if poly.get("polyType") == "BOARD_OUTLINE" or poly.get("layerId") == BOARD_OUTLINE_LAYER:
                points.extend(_poly_points(poly))
        if points:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            width_mil = max(xs) - min(xs)
            height_mil = max(ys) - min(ys)
            box = {
                "min_x": min(xs), "max_x": max(xs),
                "min_y": min(ys), "max_y": max(ys),
                "width_mil": round(width_mil, 2),
                "height_mil": round(height_mil, 2),
                "width_mm": round(width_mil * MIL_TO_MM, 2),
                "height_mm": round(height_mil * MIL_TO_MM, 2),
            }
            outline_boxes.append(box)
            # 逐板框既要宽高，也要绝对范围：下游按 min/max 算板级 extent，
            # 只给宽高它没法定位（只能退回用走线范围冒充板框）。
            listed = {k: box[k] for k in ("min_x", "max_x", "min_y", "max_y",
                                          "width_mil", "height_mil",
                                          "width_mm", "height_mm")}
            listed["doc"] = doc["uuid"]
            geo["outlines"].append(listed)

        phys = by_type.get("LAYER_PHYS", [])
        for rid, rec in phys:
            if isinstance(rec, dict):
                geo["stackup"].append({"id": _as_id(rid), "raw": rec})

    # ---- 跨文档汇总：这里只算一次，避免多 PCB 文档互相覆盖 ----
    if not geo["stackup"]:
        # 叠层通常是工程级信息，某个 PCB 文档里没写不等于拿不到
        geo["warnings"].append("没有 LAYER_PHYS 记录，叠层无法确认")

    for net, entry in net_len.items():
        widths = entry.pop("widths")
        entry["width_mil_mode"] = _mode(widths)
        entry["length_mm"] = round(entry["length_mil"] * MIL_TO_MM, 2)
        entry["length_mil"] = round(entry["length_mil"], 2)
        geo["traces"].append(entry)
    geo["traces"].sort(key=lambda t: -t["length_mil"])

    geo["via_hole_histogram"] = dict(sorted(via_sizes.items(), key=lambda kv: -kv[1]))

    if outline_boxes:
        min_x = min(b["min_x"] for b in outline_boxes)
        max_x = max(b["max_x"] for b in outline_boxes)
        min_y = min(b["min_y"] for b in outline_boxes)
        max_y = max(b["max_y"] for b in outline_boxes)
        width_mil, height_mil = max_x - min_x, max_y - min_y
        # 单板时这就是唯一那块板的尺寸；多板时是各板框的并集范围，
        # 逐板尺寸看 outlines，报告里也会逐板列一行。
        geo["outline"] = {
            "min_x": min_x, "max_x": max_x,
            "min_y": min_y, "max_y": max_y,
            "width_mil": round(width_mil, 2),
            "height_mil": round(height_mil, 2),
            "width_mm": round(width_mil * MIL_TO_MM, 2),
            "height_mm": round(height_mil * MIL_TO_MM, 2),
            "boards": len(outline_boxes),
        }
        if len(outline_boxes) > 1:
            geo["warnings"].append(
                "工程含 %d 个 PCB 文档（多板）：板框给的是各板并集尺寸"
                "（逐板尺寸见 outlines），走线与过孔按网络名跨板合并，未按板拆分"
                % len(outline_boxes))

    copper = [l for l in geo["layers"] if (l.get("type") or "").upper() in COPPER_TYPES]
    used = [l for l in copper if l.get("id") in used_layers]
    geo["copper_layers_used"] = len(used)
    geo["copper_layers_declared"] = len(copper)
    geo["segment_count"] = len(geo["segments"])
    geo["pad_count"] = len(geo["pads"])

    return geo


def _mode(counts):
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def format_netlist_csv(rows):
    header = "位号,焊盘号,引脚号,引脚名,网络\n"
    body = "".join(
        '%s,%s,%s,%s,%s\n' % (
            _csv(r["designator"]), _csv(r["pad"]), _csv(r["pinNumber"]),
            _csv(r["pinName"]), _csv(r["net"]))
        for r in rows)
    return header + body


def _csv(value):
    if value is None:
        return ""
    s = str(value)
    if any(ch in s for ch in ',"\n'):
        return '"%s"' % s.replace('"', '""')
    return s

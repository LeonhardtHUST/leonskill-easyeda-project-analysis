#!/usr/bin/env python3
"""库表构建：SYMBOL（符号 + 引脚）、DEVICE（器件 + 属性）、FOOTPRINT（封装 + 焊盘）。

坐标约定（实测）:
  - 符号引脚坐标 (x, y) 是**连接端**的本地坐标，不是画布中心
  - 文件里的 rotation 与 EDA 显示值不同向，变换时取 -rotation
  - 文件里的 y 是 EDA 显示坐标的负值，所以最终 y 要取负
  - 变换顺序固定为：旋转 → 镜像 → 平移，顺序反了引脚会左右颠倒
"""
import math

import epru

Y_SIGN = -1.0
ROT_SIGN = -1.0


def build_symbols(docs):
    """{uuid: {pins{pid: {...}}, globalNetName, title, docType}}"""
    symbols = {}
    for doc in epru.docs_of(docs, "SYMBOL"):
        by_type, meta = epru.doc_elements(doc)
        pins = {}
        for pid, payload in by_type.get("PIN", []):
            if not isinstance(payload, dict):
                continue
            pins[pid] = {
                "x": payload.get("x") or 0.0,
                "y": payload.get("y") or 0.0,
                "rotation": payload.get("rotation") or 0,
                "length": payload.get("length") or 0,
                "number": None,
                "name": None,
            }
        global_net = None
        for _rid, attr in by_type.get("ATTR", []):
            if not isinstance(attr, dict):
                continue
            parent = attr.get("parentId")
            key = attr.get("key")
            if parent and parent in pins:
                if key == "Pin Number":
                    pins[parent]["number"] = _text(attr.get("value"))
                elif key == "Pin Name":
                    pins[parent]["name"] = _text(attr.get("value"))
            elif not parent and key == "Global Net Name":
                global_net = _text(attr.get("value"))
        symbols[doc["uuid"]] = {
            "title": meta.get("title") or "",
            "docType": meta.get("docType"),
            "pins": pins,
            "globalNetName": global_net,
        }
    return symbols


def build_devices(docs):
    """{uuid: {title, attributes{}}}"""
    devices = {}
    for doc in epru.docs_of(docs, "DEVICE"):
        _by_type, meta = epru.doc_elements(doc)
        if meta:
            devices[doc["uuid"]] = {
                "title": meta.get("title") or "",
                "attributes": meta.get("attributes") or {},
            }
    return devices


def build_footprints(docs):
    """{uuid: {pads[{num, x, y}], title}}，坐标为封装本地坐标。"""
    footprints = {}
    for doc in epru.docs_of(docs, "FOOTPRINT"):
        by_type, meta = epru.doc_elements(doc)
        pads = []
        for _pid, pad in by_type.get("PAD", []):
            if not isinstance(pad, dict):
                continue
            pads.append({
                "num": _text(pad.get("num") or pad.get("number") or pad.get("padNumber")),
                "x": _num(pad.get("centerX"), pad.get("x")),
                "y": _num(pad.get("centerY"), pad.get("y")),
            })
        footprints[doc["uuid"]] = {"pads": pads, "title": meta.get("title") or ""}
    return footprints


def _num(*candidates):
    for value in candidates:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def _text(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    return str(value).strip()


def transform_pin(px, py, component):
    """符号本地坐标 → 元件绝对坐标（文件坐标系，y 尚未取负）。

    `component` 需要是 `placement_of()` 归一后的摆放字典
    （键：x / y / rotation / mirror）。直接传原始 COMPONENT 记录也能跑，
    但记录的镜像字段叫 `isMirror`、这里读的是 `mirror`，不归一就会静默丢掉镜像。
    """
    theta = math.radians((component.get("rotation") or 0) * ROT_SIGN)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    x = px * cos_t - py * sin_t
    y = px * sin_t + py * cos_t
    if component.get("mirror"):
        x = -x
    return x + (component.get("x") or 0.0), y + (component.get("y") or 0.0)


def placement_of(record):
    """COMPONENT 记录 → 摆放字典，只做字段名归一，不改数值。

    记录里是 `isMirror`，变换函数按约定读 `mirror`。这层映射集中在这里，
    调用方不必各自记住哪边是哪个名字；两个名字都认，缺省按未镜像处理。
    """
    if not isinstance(record, dict):
        return {"x": 0.0, "y": 0.0, "rotation": 0, "mirror": False}
    mirror = record.get("isMirror")
    if mirror is None:
        mirror = record.get("mirror")
    return {
        "x": record.get("x"),
        "y": record.get("y"),
        "rotation": record.get("rotation"),
        "mirror": bool(mirror),
    }


def component_type(symbol, device):
    """part / netflag / netport / sheet。"""
    doc_type = (symbol or {}).get("docType")
    title = "%s %s" % ((symbol or {}).get("title") or "", (device or {}).get("title") or "")
    if doc_type == 18 or "NetFlag" in title or "网络标识" in title:
        return "netflag"
    if doc_type == 19 or "NetPort" in title or "网络端口" in title:
        return "netport"
    if doc_type == 20 or "Drawing-Symbol" in title or "Sheet" in title:
        return "sheet"
    return "part"


def device_uuid_of(inline_attrs, merged_attrs):
    """COMPONENT.attrs.DeviceName 是 JSON 字符串，里面才有真正的器件 uuid。"""
    raw = (inline_attrs or {}).get("DeviceName")
    if raw:
        parsed = None
        if isinstance(raw, str):
            import json
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        elif isinstance(raw, dict):
            parsed = raw
        if isinstance(parsed, dict) and parsed.get("uuid"):
            return parsed["uuid"]
    return (merged_attrs or {}).get("Device")


def value_like(text):
    """Name 字段若是具体值（如 100nF、4.7k）就当 Value 兜底；公式类以 = 开头则忽略。"""
    if not text:
        return None
    s = str(text).strip()
    if not s or s.startswith("="):
        return None
    first = s[0]
    return s if (first.isdigit() or first == ".") else None

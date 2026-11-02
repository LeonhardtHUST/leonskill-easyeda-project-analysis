#!/usr/bin/env python3
"""两版工程语义差异：元素级 added / removed / changed，附噪声过滤。

`.epru` 每次保存都会重排 ticket、换客户端 GUID、重灌铺铜，这些不是设计变更。
不先过滤会得到几千条"改动"，真实变更通常只有几条。

用法:
  python epru_diff.py old.epro2 new.epro2 [--json out.json]
"""
import argparse
import json
import sys

import epru
import epro2
import pcbnet

VOLATILE_KEYS = {
    "ticket", "firstTicket", "lastTicket", "client", "updateTime", "update_time",
    "version", "zIndex", "dataType", "checksum", "hash", "revision",
}
# 注意 max / min 曾经在剔除名单里，但它们可能是焊盘/铺铜的真实几何字段
# （包围盒尺寸），剔掉会把真实的焊盘尺寸变更吞进"无变化"。
# 判定原则：只有确实与设计无关的元数据才能进 VOLATILE_KEYS。

NOISE_TYPES = {
    "ELE_PLACEHOLDER",   # 保存时的批处理重编号
    "LAYER_FILL",        # 铺铜重灌
    "POURED",
    "BLOB",
}


def strip_volatile(value, depth=0):
    """递归剔除每次保存都会变、但没有设计含义的字段。"""
    if depth > 12:
        return value
    if isinstance(value, dict):
        return {k: strip_volatile(v, depth + 1)
                for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v, depth + 1) for v in value]
    return value


def _type_of(key):
    parts = key.split("|")
    return parts[1] if len(parts) > 1 else ""


def diff_records(text_old, text_new):
    old = epru.flat_map(text_old)
    new = epru.flat_map(text_new)

    added, removed, changed = [], [], []
    noise = 0
    for key in new:
        if key not in old:
            if _type_of(key) in NOISE_TYPES:
                noise += 1
                continue
            added.append({"key": key, "type": _type_of(key),
                          "payload": strip_volatile(new[key][1])})
    for key in old:
        if key not in new:
            if _type_of(key) in NOISE_TYPES:
                noise += 1
                continue
            removed.append({"key": key, "type": _type_of(key)})
    for key in new:
        if key not in old:
            continue
        before = json.dumps(strip_volatile(old[key][1]), sort_keys=True, ensure_ascii=False)
        after = json.dumps(strip_volatile(new[key][1]), sort_keys=True, ensure_ascii=False)
        if before != after:
            if _type_of(key) in NOISE_TYPES:
                noise += 1
                continue
            changed.append({"key": key, "type": _type_of(key),
                            "before": strip_volatile(old[key][1]),
                            "after": strip_volatile(new[key][1])})

    return {
        "records_old": len(old),
        "records_new": len(new),
        "added": added,
        "removed": removed,
        "changed": changed,
        "noise_filtered": noise,
    }


def diff_netlist(rows_old, rows_new):
    """按 (位号, 焊盘号) 逐行比对网络与引脚映射。

    只比网络会漏掉"符号改版"——焊盘号还是那些，但引到的引脚换了。
    所以行的语义签名是 (网络, 引脚号, 引脚名)，任一不同都算变更。
    """
    def index(rows):
        return {(r["designator"], r["pad"]): r for r in rows}

    a, b = index(rows_old), index(rows_new)
    added = [b[k] for k in b if k not in a]
    removed = [a[k] for k in a if k not in b]
    changed = []
    for key in b:
        if key not in a:
            continue
        ra, rb = a[key], b[key]
        fields = []
        if (ra.get("net") or "") != (rb.get("net") or ""):
            fields.append("net")
        if str(ra.get("pinNumber")) != str(rb.get("pinNumber")):
            fields.append("pinNumber")
        if str(ra.get("pinName")) != str(rb.get("pinName")):
            fields.append("pinName")
        if not fields:
            continue
        changed.append({
            "designator": key[0], "pad": key[1],
            "fields": fields,
            "before": ra.get("net") or "",
            "after": rb.get("net") or "",
            "pin_before": {"number": ra.get("pinNumber"), "name": ra.get("pinName")},
            "pin_after": {"number": rb.get("pinNumber"), "name": rb.get("pinName")},
        })
    return {"added": added, "removed": removed, "changed": changed}


def compare(old_path, new_path):
    proj_old = epro2.open_project(old_path)
    proj_new = epro2.open_project(new_path)

    result = {
        "old": {"path": old_path, "records": proj_old.line_count()},
        "new": {"path": new_path, "records": proj_new.line_count()},
        "elements": diff_records(proj_old.epru_text, proj_new.epru_text),
    }

    import library
    docs_old = epru.parse_records(proj_old.epru_text)
    docs_new = epru.parse_records(proj_new.epru_text)
    # 库表必须按版本各自构建：符号/器件在版本间变了（引脚改号、换符号），
    # 两版就不能共用一张表，否则旧版网表会被新版的符号重算一遍，差异被抹平。
    symbols_old, symbols_new = library.build_symbols(docs_old), library.build_symbols(docs_new)
    devices_old, devices_new = library.build_devices(docs_old), library.build_devices(docs_new)
    rows_old, _ = pcbnet.build_netlist(docs_old, symbols_old, devices_old)
    rows_new, _ = pcbnet.build_netlist(docs_new, symbols_new, devices_new)
    result["netlist"] = diff_netlist(rows_old, rows_new)
    result["netlist_rows"] = {"old": len(rows_old), "new": len(rows_new)}
    return result


def summarize(result):
    el = result["elements"]
    nl = result["netlist"]
    pin_only = sum(1 for c in nl["changed"] if "net" not in (c.get("fields") or ["net"]))
    detail = "（其中 %d 条仅引脚映射变化，网名未变）" % pin_only if pin_only else ""
    return "\n".join([
        "记录数: %d → %d" % (el["records_old"], el["records_new"]),
        "元素级: 新增 %d / 删除 %d / 变更 %d（已过滤噪声 %d）"
        % (len(el["added"]), len(el["removed"]), len(el["changed"]), el["noise_filtered"]),
        "网表: %d → %d 行；新增 %d / 删除 %d / 换网 %d%s"
        % (result["netlist_rows"]["old"], result["netlist_rows"]["new"],
           len(nl["added"]), len(nl["removed"]), len(nl["changed"]), detail),
    ])


def main(argv=None):
    ap = argparse.ArgumentParser(description="两版 .epro2 语义差异")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--json", help="写出差异 JSON")
    args = ap.parse_args(argv)

    try:
        result = compare(args.old, args.new)
    except epro2.ProjectError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    print(summarize(result))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        print("差异已写入: %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())

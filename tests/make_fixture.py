#!/usr/bin/env python3
"""合成一个最小 .epro2 工程，供测试使用。

工程里包含：3 个符号（电阻 + 三脚测试件 + 地标识）、2 个器件、1 个封装、
1 张原理图页、1 个 PCB 文档。原理图页上除 R1 与 GND 标识外，
还有 rotation=90 且镜像的 U3、rotation=180 不镜像的 U4，用来覆盖坐标变换分支。
刻意塞进三种真实状况：

  - 同一焊盘有两条 PAD_NET 记录（旧记录是空网络，新记录接了 VCC）
  - 一个没有位号的残留元件
  - 板框用 ["R", x, y, w, h] 形式

用法（测试内部调用，也可单独生成到指定路径）:
  python make_fixture.py out.epro2 [--net-vcc2 X] [--swap-pins]
                         [--two-pcb] [--duplicate-page] [--pad-max N]

变体开关（默认全关，保持既有测试的工程形态不变）:
  --swap-pins       互换符号引脚号，模拟符号改版（网表 diff 回归用）
  --two-pcb         追加第二个 PCB 文档（几何跨文档累加回归用）
  --duplicate-page  追加一张同名原理图页 P1（同名页误报回归用）
  --pad-max N       给焊盘加 max 字段（diff 噪声过滤回归用）
"""
import argparse
import json
import os
import sys
import zipfile


def _rec(rtype, rid, ticket, payload):
    head = json.dumps({"type": rtype, "id": rid, "ticket": ticket},
                      ensure_ascii=False)
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
    return "%s||%s|" % (head, body)


def _doc(doc_type, uuid, title, edit_version=1):
    return _rec("DOCHEAD", uuid, 1,
                {"docType": doc_type, "uuid": uuid, "editVersion": edit_version,
                 "title": title})


def build_epru(net_for_pad2="VCC", swap_pins=False, two_pcb=False,
               duplicate_page=False, pad_max=None):
    lines = []
    # 符号引脚号：swap_pins 时把两个引脚的 Pin Number 互换（Pin Name 不动），
    # 模拟"符号改版后引脚编号挪位"。焊盘号仍按 Pin Number 匹配，
    # 所以两版网表的行不会增减，但每行映射到的引脚名会变。
    pin1_number, pin2_number = ("2", "1") if swap_pins else ("1", "2")

    # ---- 符号：电阻 ----
    lines.append(_doc("SYMBOL", "s1", "R_0603"))
    lines.append(_rec("META", "m1", 1, {"title": "R_0603", "docType": 2}))
    lines.append(_rec("PIN", "p1", 1, {"x": -20, "y": 0, "rotation": 0, "length": 10}))
    lines.append(_rec("PIN", "p2", 1, {"x": 20, "y": 0, "rotation": 180, "length": 10}))
    lines.append(_rec("ATTR", "a1", 1, {"parentId": "p1", "key": "Pin Number", "value": pin1_number}))
    lines.append(_rec("ATTR", "a2", 1, {"parentId": "p1", "key": "Pin Name", "value": "1"}))
    lines.append(_rec("ATTR", "a3", 1, {"parentId": "p2", "key": "Pin Number", "value": pin2_number}))
    lines.append(_rec("ATTR", "a4", 1, {"parentId": "p2", "key": "Pin Name", "value": "2"}))

    # ---- 符号：三脚测试件（引脚不在坐标轴上，旋转与镜像都看得出效果） ----
    lines.append(_doc("SYMBOL", "s3", "TRI_3PIN"))
    lines.append(_rec("META", "m2b", 1, {"title": "TRI_3PIN", "docType": 2}))
    lines.append(_rec("PIN", "u1", 1, {"x": -30, "y": -10, "rotation": 0, "length": 5}))
    lines.append(_rec("PIN", "u2", 1, {"x": 30, "y": -10, "rotation": 180, "length": 5}))
    lines.append(_rec("PIN", "u3", 1, {"x": 0, "y": 20, "rotation": 90, "length": 5}))
    lines.append(_rec("ATTR", "a10", 1, {"parentId": "u1", "key": "Pin Number", "value": "1"}))
    lines.append(_rec("ATTR", "a11", 1, {"parentId": "u1", "key": "Pin Name", "value": "A"}))
    lines.append(_rec("ATTR", "a12", 1, {"parentId": "u2", "key": "Pin Number", "value": "2"}))
    lines.append(_rec("ATTR", "a13", 1, {"parentId": "u2", "key": "Pin Name", "value": "B"}))
    lines.append(_rec("ATTR", "a14", 1, {"parentId": "u3", "key": "Pin Number", "value": "3"}))
    lines.append(_rec("ATTR", "a15", 1, {"parentId": "u3", "key": "Pin Name", "value": "C"}))

    # ---- 符号：地标识 ----
    lines.append(_doc("SYMBOL", "s2", "GND"))
    lines.append(_rec("META", "m2", 1, {"title": "GND", "docType": 18}))
    lines.append(_rec("PIN", "g1", 1, {"x": 0, "y": 0, "rotation": 0, "length": 0}))
    lines.append(_rec("ATTR", "a5", 1, {"parentId": "g1", "key": "Pin Number", "value": "1"}))
    lines.append(_rec("ATTR", "a6", 1, {"key": "Global Net Name", "value": "GND"}))

    # ---- 器件 ----
    lines.append(_doc("DEVICE", "d1", "R_0603"))
    lines.append(_rec("META", "m3", 1,
                      {"title": "R_0603", "attributes": {"Symbol": "s1", "Value": "10k"}}))
    lines.append(_doc("DEVICE", "d3", "TRI_3PIN"))
    lines.append(_rec("META", "m3b", 1,
                      {"title": "TRI_3PIN", "attributes": {"Symbol": "s3"}}))

    # ---- 封装 ----
    lines.append(_doc("FOOTPRINT", "f1", "R0603"))
    lines.append(_rec("META", "m4", 1, {"title": "R0603"}))
    # pad_max 用来制造"两版只有 max 字段不同"的焊盘几何记录（diff 噪声过滤回归）
    _pad1 = {"num": "1", "centerX": -30, "centerY": 0}
    if pad_max is not None:
        _pad1["max"] = pad_max
    lines.append(_rec("PAD", "pd1", 1, _pad1))
    lines.append(_rec("PAD", "pd2", 1, {"num": "2", "centerX": 30, "centerY": 0}))

    # ---- 原理图页 ----
    lines.append(_doc("SCH_PAGE", "pg1", "P1"))
    lines.append(_rec("META", "m5", 1, {"title": "P1", "docType": 2}))
    lines.append(_rec("COMPONENT", "c1", 1, {
        "x": 100, "y": 200, "rotation": 0, "isMirror": False,
        "attrs": {"DeviceName": json.dumps({"name": "R_0603", "uuid": "d1"})},
    }))
    lines.append(_rec("ATTR", "a7", 1, {"parentId": "c1", "key": "Designator", "value": "R1"}))
    lines.append(_rec("COMPONENT", "c2", 2, {"x": 80, "y": 200, "rotation": 0}))
    lines.append(_rec("ATTR", "a8", 1, {"parentId": "c2", "key": "Symbol", "value": "s2"}))
    # 旋转 + 镜像用例：rotation=90 且 isMirror=True
    lines.append(_rec("COMPONENT", "c3", 1, {
        "x": 400, "y": 300, "rotation": 90, "isMirror": True,
        "attrs": {"DeviceName": json.dumps({"name": "TRI_3PIN", "uuid": "d3"})},
    }))
    lines.append(_rec("ATTR", "a16", 1, {"parentId": "c3", "key": "Designator", "value": "U3"}))
    # 纯旋转用例：rotation=180，不镜像
    lines.append(_rec("COMPONENT", "c4", 1, {
        "x": 600, "y": 400, "rotation": 180, "isMirror": False,
        "attrs": {"DeviceName": json.dumps({"name": "TRI_3PIN", "uuid": "d3"})},
    }))
    lines.append(_rec("ATTR", "a17", 1, {"parentId": "c4", "key": "Designator", "value": "U4"}))
    lines.append(_rec("WIRE", "w1", 3, {}))
    lines.append(_rec("ATTR", "a9", 1, {"parentId": "w1", "key": "NET", "value": "VCC"}))
    lines.append(_rec("LINE", "l1", 3, {
        "lineGroup": "w1", "startX": 120, "startY": 200, "endX": 200, "endY": 200,
    }))

    if duplicate_page:
        # 同名页：第二页标题也叫 P1，标签会变成 P1#2，但 name 仍是 P1
        lines.append(_doc("SCH_PAGE", "pg2", "P1"))
        lines.append(_rec("META", "m5b", 1, {"title": "P1", "docType": 2}))

    # ---- PCB ----
    lines.append(_doc("PCB", "pb1", "PCB"))
    lines.append(_rec("META", "m6", 1, {"title": "PCB"}))
    lines.append(_rec("LAYER", ["LAYER", 1], 1, {"layerType": "TOP", "layerName": "Top Layer"}))
    lines.append(_rec("LAYER", ["LAYER", 2], 1, {"layerType": "BOTTOM", "layerName": "Bottom Layer"}))
    lines.append(_rec("LAYER_PHYS", ["LAYER_PHYS"], 1,
                      {"thickness": 1.6, "er": 4.4, "lossTangent": 0.02}))
    lines.append(_rec("COMPONENT", "pc1", 1, {"x": 0, "y": 0, "angle": 0, "layerId": 1}))
    lines.append(_rec("ATTR", "b1", 1, {"parentId": "pc1", "key": "Designator", "value": "R1"}))
    lines.append(_rec("ATTR", "b2", 1, {"parentId": "pc1", "key": "Device", "value": "d1"}))
    lines.append(_rec("PAD_NET", ["PAD_NET", "pc1", "1", "pd1"], 10, {"padNet": "GND"}))
    lines.append(_rec("PAD_NET", ["PAD_NET", "pc1", "2", "pd2"], 5, {"padNet": ""}))
    lines.append(_rec("PAD_NET", ["PAD_NET", "pc1", "2", "pd2"], 20,
                      {"padNet": net_for_pad2}))

    # 残留元件：没有位号
    lines.append(_rec("COMPONENT", "pc9", 2, {"x": 500, "y": 500, "angle": 0, "layerId": 1}))
    lines.append(_rec("PAD_NET", ["PAD_NET", "pc9", "1", "px"], 7, {"padNet": "GHOST"}))

    lines.append(_rec("LINE", "t1", 4, {
        "netName": "VCC", "layerId": 1, "startX": 0, "startY": 0,
        "endX": 1000, "endY": 0, "width": 10,
    }))
    lines.append(_rec("VIA", "v1", 4, {
        "netName": "VCC", "centerX": 500, "centerY": 0,
        "holeDiameter": 11.811, "viaDiameter": 23.622, "viaType": "THROUGH",
    }))
    lines.append(_rec("POLY", "o1", 4, {
        "layerId": 11, "polyType": "BOARD_OUTLINE",
        "path": ["R", 0, 0, 1000, 500, 0, 0],
    }))

    if two_pcb:
        # 第二个 PCB 文档：只有底层走线和另一种孔径的过孔。
        # 几何统计必须跨文档累加（过孔孔径分布合并、铜层取并集），
        # 否则后一个文档会把前一个的统计覆盖掉。
        lines.append(_doc("PCB", "pb2", "PCB_2"))
        lines.append(_rec("META", "m7", 1, {"title": "PCB_2"}))
        lines.append(_rec("LINE", "t2", 4, {
            "netName": "GND", "layerId": 2, "startX": 0, "startY": 0,
            "endX": 300, "endY": 400, "width": 8,
        }))
        lines.append(_rec("VIA", "v2", 4, {
            "netName": "GND", "centerX": 100, "centerY": 100,
            "holeDiameter": 20.0, "viaDiameter": 40.0, "viaType": "THROUGH",
        }))
        lines.append(_rec("POLY", "o2", 4, {
            "layerId": 11, "polyType": "BOARD_OUTLINE",
            "path": ["R", 0, 0, 1000, 500, 0, 0],
        }))
    return "\n".join(lines) + "\n"


def write_project(path, net_for_pad2="VCC", epru_name="TestProj.epru", swap_pins=False,
                  two_pcb=False, duplicate_page=False, pad_max=None):
    text = build_epru(net_for_pad2, swap_pins, two_pcb, duplicate_page, pad_max)
    project_meta = {"title": "TestProj", "editorVersion": "4.1.0"}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project2.json", json.dumps(project_meta, ensure_ascii=False))
        zf.writestr(epru_name, text)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成合成测试工程")
    ap.add_argument("out")
    ap.add_argument("--net-vcc2", default="VCC", help="第二个焊盘的网络")
    ap.add_argument("--swap-pins", action="store_true",
                    help="互换符号两个引脚的 Pin Number（模拟符号改版）")
    ap.add_argument("--two-pcb", action="store_true", help="追加第二个 PCB 文档")
    ap.add_argument("--duplicate-page", action="store_true", help="追加一张同名原理图页")
    ap.add_argument("--pad-max", type=float, default=None, help="给焊盘加 max 字段")
    args = ap.parse_args(argv)
    write_project(args.out, args.net_vcc2, swap_pins=args.swap_pins,
                  two_pcb=args.two_pcb, duplicate_page=args.duplicate_page,
                  pad_max=args.pad_max)
    print("written: %s (%d bytes)" % (args.out, os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

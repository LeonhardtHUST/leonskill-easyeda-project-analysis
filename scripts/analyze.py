#!/usr/bin/env python3
"""统一入口：把一个 .epro2 解析成结构化产物 + 可读报告。

用法:
  python analyze.py design.epro2 --out ./eda-out [--page 页名]
  python analyze.py --diff old.epro2 new.epro2 --out ./eda-diff
"""
import argparse
import json
import os
import re
import sys

import epru
import epro2
import library
import pcbnet
import schematic


def _slug(text, fallback="page"):
    s = re.sub(r"[^\w\-.]+", "_", str(text or "")).strip("_")
    return s or fallback


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def _write_text(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="\n") as f:
        f.write(text)
    return path


def collect(project_path, page_key=None):
    """解析一个工程，返回 (artifacts, docs, project)。"""
    proj = epro2.open_project(project_path)
    stats = {}
    docs = epru.parse_records(proj.epru_text, stats)
    if not docs:
        raise epro2.ProjectError("记录流里没有文档段（DOCHEAD 缺失）")
    ratio = (stats["unparsed"] / float(stats["total"])) if stats.get("total") else 0.0
    if ratio > 0.05:
        raise epro2.ProjectError(
            "记录行解析失败 %.1f%%（%d/%d），解析规则与文件版本不匹配"
            % (ratio * 100, stats["unparsed"], stats["total"]))

    symbols = library.build_symbols(docs)
    devices = library.build_devices(docs)
    footprints = library.build_footprints(docs)

    pages = [d for d in docs if d["docType"] in ("SCH_PAGE", "SIMULATION")]
    page_list = [{"uuid": d["uuid"], "name": epru.doc_title(d) or "(未命名)",
                  "docType": d["docType"]} for d in pages]

    if page_key:
        targets = [d for d in pages
                   if d["uuid"] == page_key or (epru.doc_title(d) or "") == page_key]
        if not targets:
            raise epro2.ProjectError(
                "找不到页「%s」。可用页：%s"
                % (page_key, "、".join(p["name"] for p in page_list) or "无"))
    else:
        targets = [d for d in pages if d["docType"] == "SCH_PAGE"] or pages[:1]

    pages_ir = []
    nets_by_key = {}
    for doc in targets:
        ir = schematic.page_to_ir(doc, symbols, devices, proj.title)
        for key, value in schematic.build_nets(ir).items():
            nets_by_key.setdefault(key, set()).add(value)
        pages_ir.append(ir)

    # 同名页去重：多页可能都叫 P1
    seen_titles = {}
    for ir in pages_ir:
        base = ir["page"]["name"] or "(未命名)"
        seen_titles[base] = seen_titles.get(base, 0) + 1
        count = seen_titles[base]
        ir["page"]["label"] = base if count == 1 else "%s#%d" % (base, count)

    # 跨页同位号会把不同网络叠在一起，只保留各页一致的引脚
    pin_net = {}
    ambiguous = {}
    for key, values in nets_by_key.items():
        if len(values) == 1:
            pin_net[key] = next(iter(values))
        else:
            ambiguous[key] = sorted(values)

    # 同一位号出现在多个页时，无法判断 PCB 上那颗属于哪一页，整体排除比对
    des_pages = {}
    for ir in pages_ir:
        label = ir["page"].get("label") or ir["page"]["name"]
        for comp in ir["components"]:
            des = comp.get("designator")
            if des and comp.get("componentType") == "part":
                des_pages.setdefault(des, set()).add(label)
    multi_page = {des: sorted(p) for des, p in des_pages.items() if len(p) > 1}

    rows, warnings = pcbnet.build_netlist(docs, symbols, devices)
    geo = pcbnet.build_geometry(docs)

    meta = {
        "project": proj.title,
        "source": os.path.basename(project_path),
        "epru": proj.epru_name,
        "record_lines": stats.get("total", 0),
        "record_unparsed": stats.get("unparsed", 0),
        "project_meta": proj.project_meta,
        "documents": [{"docType": d["docType"], "uuid": d["uuid"],
                       "title": epru.doc_title(d)} for d in docs],
        "library": {"symbols": len(symbols), "devices": len(devices),
                    "footprints": len(footprints)},
        "pages": page_list,
        "pages_analyzed": [ir["page"].get("label") or ir["page"]["name"]
                           for ir in pages_ir],
        # 按 uuid 记录真正解析过的页：同名页会有 P1 / P1#2 两个 label，
        # 用 label（或 name）比对会把同名页判错，uuid 才是唯一键。
        "pages_analyzed_uuids": [ir["page"]["uuid"] for ir in pages_ir],
    }

    artifacts = {
        "meta": meta,
        "pages_ir": pages_ir,
        "pin_net": pin_net,
        "ambiguous": ambiguous,
        "multi_page_designators": multi_page,
        "pcb_rows": rows,
        "pcb_warnings": warnings,
        "geometry": geo,
    }
    return artifacts, docs, proj


def duplicate_designators(rows):
    """PCB 上同一位号挂在多个元件上的情况：这类位号的引脚无法唯一对应，不能参与比对。"""
    seen = {}
    for r in rows:
        seen.setdefault(r["designator"], set()).add(r.get("component"))
    return {des: len(comps) for des, comps in seen.items() if len(comps) > 1}


def check_consistency(artifacts):
    """原理图侧与 PCB 侧逐脚比对，返回 (mismatches, stats)。"""
    rows = artifacts["pcb_rows"]
    pin_net = artifacts["pin_net"] or {}
    dup = duplicate_designators(rows)
    multi_page = artifacts.get("multi_page_designators") or {}
    pcb_map = {}
    duplicate_pin_keys = set()
    duplicate_pin_conflicts = set()
    for r in rows:
        key = (r["designator"], r["pinNumber"])
        if key[1] is None:
            continue
        if r["designator"] in dup or r["designator"] in multi_page:
            continue
        net = r.get("net") or ""
        if key in pcb_map:
            # 同一 (位号, 引脚号) 出现多行：不再静默留首行。
            duplicate_pin_keys.add(key)
            if pcb_map[key] != net:
                duplicate_pin_conflicts.add(key)
            continue
        pcb_map[key] = net
    # 网络互相矛盾的键连"信哪条"都定不了，只能排除比对并报出来
    for key in duplicate_pin_conflicts:
        pcb_map.pop(key, None)

    mismatches = []
    compared = 0
    skipped = 0
    for (des, num), net in pin_net.items():
        if not des or num is None:
            continue
        if (des, num) not in pcb_map:
            continue
        pcb_net = pcb_map[(des, num)]
        if net.startswith("N$") or not pcb_net:
            skipped += 1
            continue
        compared += 1
        if net != pcb_net:
            mismatches.append({"designator": des, "pin": num,
                               "schematic": net, "pcb": pcb_net})
    stats = {"compared": compared, "mismatch": len(mismatches),
             "skipped_unnamed": skipped,
             "ambiguous": len(artifacts.get("ambiguous") or {}),
             "duplicate_designators": len(dup),
             "duplicate_pin_keys": len(duplicate_pin_keys),
             "duplicate_pin_conflicts": len(duplicate_pin_conflicts),
             "multi_page": len(multi_page),
             "pcb_rows": len(rows), "schematic_nets": len(pin_net)}
    return mismatches, stats


def find_floating(pages_ir, limit=40):
    """只连了一个实体引脚的命名网络，通常是悬空或绘图残留。

    返回 (列表, 是否被截断)。截断时报告里必须注明"仅列前 N 条"，
    否则会被当成全部结果。
    """
    out = []
    for ir in pages_ir:
        for net in ir.get("nets", []):
            if not net.get("named"):
                continue
            real = [p for p in net["pins"] if p.get("componentType") == "part"]
            if len(real) <= 1:
                out.append({"net": net["name"], "page": ir["page"]["name"],
                            "pins": real})
            if len(out) >= limit:
                return out, True
    return out, False


def render_report(artifacts, mismatches, stats, floating, floating_truncated=False):
    meta = artifacts["meta"]
    geo = artifacts["geometry"]
    pages_ir = artifacts["pages_ir"]

    doc_counts = {}
    for d in meta["documents"]:
        doc_counts[d["docType"]] = doc_counts.get(d["docType"], 0) + 1

    # 未解析的页按 uuid 求差集：页名会重名，label 又会加 #2 后缀，都不可靠
    analyzed_uuids = set(meta.get("pages_analyzed_uuids") or [])
    pending_pages = [p for p in meta["pages"] if p["uuid"] not in analyzed_uuids]

    lines = []
    add = lines.append
    add("# %s 解析报告" % meta["project"])
    add("")
    add("## 一、工程概况")
    add("")
    add("- 源文件：`%s`（记录流 `%s`，%d 行，解析失败 %d 行）"
        % (meta["source"], meta["epru"], meta["record_lines"], meta["record_unparsed"]))
    add("- 文档：" + "，".join("%s×%d" % (k, v) for k, v in sorted(doc_counts.items())))
    add("- 库表：符号 %d / 器件 %d / 封装 %d"
        % (meta["library"]["symbols"], meta["library"]["devices"],
           meta["library"]["footprints"]))
    if pages_ir:
        for ir in pages_ir:
            parts = [c for c in ir["components"] if c.get("componentType") == "part"]
            add("- 原理图页 `%s`：元件 %d（实体件 %d），导线 %d，网络 %d"
                % (ir["page"].get("label") or ir["page"]["name"],
                   len(ir["components"]), len(parts),
                   len(ir["wires"]), len(ir["nets"])))
        if pending_pages:
            add("- **未解析的页**：%s"
                % "、".join(p["name"] + "（%s）" % p["uuid"] for p in pending_pages))
    else:
        add("- 原理图页：无（工程内没有原理图页文档）")
    add("- PCB：网表 %d 行，走线网络 %d 个，过孔 %d 个"
        % (len(artifacts["pcb_rows"]), len(geo.get("traces") or []),
           len(geo.get("vias") or [])))
    add("")

    add("## 二、网表一致性")
    add("")
    add("- 可比对引脚 %d 个，不一致 %d 个" % (stats["compared"], stats["mismatch"]))
    if stats.get("skipped_unnamed"):
        add("- 另有 %d 个引脚所在网络在原理图侧未命名，无法按名比对"
            % stats["skipped_unnamed"])
    if stats.get("ambiguous"):
        add("- 另有 %d 个引脚在不同页上落在不同网络（位号重复），已排除比对"
            % stats["ambiguous"])
    if stats.get("duplicate_designators"):
        add("- 另有 %d 个位号在 PCB 上对应多个元件，已排除比对"
            % stats["duplicate_designators"])
    if stats.get("duplicate_pin_keys"):
        add("- 另有 %d 个（位号, 引脚号）在 PCB 网表里重复出现，其中 %d 个网络互相矛盾已排除比对"
            % (stats["duplicate_pin_keys"], stats.get("duplicate_pin_conflicts") or 0))
    if stats.get("multi_page"):
        add("- 另有 %d 个位号出现在多个原理图页，已排除比对"
            % stats["multi_page"])
    if mismatches:
        add("")
        add("| 位号 | 引脚 | 原理图网络 | PCB 网络 |")
        add("|---|---|---|---|")
        for m in mismatches[:50]:
            add("| %s | %s | %s | %s |"
                % (m["designator"], m["pin"], m["schematic"], m["pcb"]))
        if len(mismatches) > 50:
            add("")
            add("（仅列前 50 条）")
    elif stats["compared"]:
        add("- 逐脚一致。")
    else:
        add("- **未完成比对**：两侧引脚号没有交集，见数据缺失清单。")
    add("")

    add("## 三、PCB 几何")
    add("")
    if geo.get("outline"):
        o = geo["outline"]
        if (o.get("boards") or 1) > 1:
            add("- 板框：%d 块板，并集 %.2f × %.2f mm"
                % (o["boards"], o["width_mm"], o["height_mm"]))
            for i, board in enumerate(geo.get("outlines") or [], 1):
                add("  - 板 %d（`%s`）：%.2f × %.2f mm"
                    % (i, board.get("doc"), board["width_mm"], board["height_mm"]))
        else:
            add("- 板框：%.2f × %.2f mm" % (o["width_mm"], o["height_mm"]))
    else:
        add("- 板框：**数据缺失**（未找到 BOARD_OUTLINE 记录）")
    add("- 铜层：声明 %d 条，被实际引用 %d 条"
        % (geo.get("copper_layers_declared") or 0, geo.get("copper_layers_used") or 0))
    if geo.get("stackup"):
        add("- 叠层：%d 条 LAYER_PHYS 记录（原始字段见 `pcb_geometry.json`）"
            % len(geo["stackup"]))
    else:
        add("- 叠层：**数据缺失**")
    traces = geo.get("traces") or []
    if traces:
        total = sum(t["length_mm"] for t in traces)
        add("- 走线：%d 个网络，总长 %.1f mm" % (len(traces), total))
        for t in traces[:8]:
            add("  - `%s`：%d 段，%.1f mm，线宽 %s mil"
                % (t["net"] or "(未命名)", t["segments"], t["length_mm"],
                   t.get("width_mil_mode") or "?"))
    else:
        add("- 走线：**数据缺失**")
    if geo.get("via_hole_histogram"):
        add("- 过孔孔径分布：%s"
            % "，".join("%s mil×%d" % (k, v)
                        for k, v in geo["via_hole_histogram"].items()))
    add("")

    add("## 四、自动检查项")
    add("")
    findings = []
    for ir in pages_ir:
        for net in ir.get("nets", []):
            if net.get("conflicts"):
                findings.append(("🔴", "网络 `%s` 同时出现名字 %s（页 `%s`）"
                                 % (net["name"], "、".join(net["conflicts"]),
                                    ir["page"].get("label") or ir["page"]["name"]),
                                 "可能是真短路，也可能是历史残留线段；需人工确认"))
    for item in floating:
        findings.append(("🟡", "命名网络 `%s` 只连了 1 个实体引脚（页 `%s`）"
                         % (item["net"], item["page"]),
                         "悬空或绘图残留，需确认是否有意为之"))
    if stats.get("ambiguous"):
        findings.append(("🟠", "%d 个引脚在不同原理图页上落在不同网络" % stats["ambiguous"],
                         "多个页存在同一位号，未编号或重复；先确认后再做网表比对"))
    if stats.get("duplicate_designators"):
        findings.append(("🟠", "%d 个位号在 PCB 上对应多个元件"
                         % stats["duplicate_designators"],
                         "位号重复会让焊盘无法唯一归属，需先重新编号"))
    if stats.get("multi_page"):
        findings.append(("🟠", "%d 个位号出现在多个原理图页" % stats["multi_page"],
                         "多页共用位号通常是未重新编号的副本页，需确认哪一页有效"))
    if stats.get("duplicate_pin_conflicts"):
        findings.append(("🟠", "%d 个引脚在 PCB 网表里对应多个网络"
                         % stats["duplicate_pin_conflicts"],
                         "同一（位号, 引脚号）有多条网络矛盾的 PAD_NET，"
                         "需确认焊盘归属后再比对"))
    for w in dict.fromkeys((artifacts.get("pcb_warnings") or [])
                           + (geo.get("warnings") or [])):
        findings.append(("🟡", w, "解析限制，不是设计缺陷"))
    if mismatches:
        findings.append(("🟠", "%d 个引脚的两侧网络不一致" % len(mismatches),
                         "原理图与 PCB 不同步，或一侧存在历史残留"))

    if findings:
        add("| 级别 | 发现 | 说明 |")
        add("|---|---|---|")
        for level, what, why in findings[:60]:
            add("| %s | %s | %s |" % (level, what, why))
    else:
        add("- 自动检查未发现问题。这只代表解析层面一致，不等于设计正确。")
    add("")
    if floating_truncated:
        add("> 悬空网络清单已截断：**仅列前 %d 条**，完整结果见 `nets.json`，"
            "未列出的部分同样需要人工核对。" % len(floating))
        add("")
    add("> 自动检查只覆盖可机械判定的项（网络冲突、悬空、两侧不一致、解析缺项）。"
        "引脚功能、封装选型、电流余量、器件极性等需要对照数据手册逐项核对，"
        "见 `references/review-playbook.md`。")
    add("")

    add("## 五、数据缺失清单")
    add("")
    missing = []
    if not pages_ir:
        missing.append("工程内没有原理图页，无法做原理图侧分析")
    if not artifacts["pcb_rows"]:
        missing.append("PCB 网表为空：可能没有 PCB 文档，或 PAD_NET 记录缺失")
    if not geo.get("outline"):
        missing.append("缺少板框记录，无法判断外形尺寸")
    if not geo.get("stackup"):
        missing.append("缺少 LAYER_PHYS，叠层与阻抗无法确认")
    if stats["compared"] == 0:
        missing.append("两侧引脚号没有交集，网表一致性未验证")
    if pending_pages:
        missing.append("有 %d 个原理图页未被解析（%s），跨页网络可能不完整"
                       % (len(pending_pages),
                          "、".join(p["name"] for p in pending_pages)))
    if missing:
        for m in missing:
            add("- %s" % m)
    else:
        add("- 无")
    add("")

    add("## 六、结论")
    add("")
    if mismatches or any(f[0] == "🔴" for f in findings):
        add("**需修改**：存在网络冲突或两侧不一致，先解决再讨论打样。")
    elif missing:
        add("**无法完整判定**：解析已完成，但上述数据缺失项会限制结论范围。")
    else:
        add("**解析通过**：自动检查项无异常。设计正确性仍需按检查清单人工核对。")
    add("")
    add("---")
    add("")
    add("本报告由工程文件机械解析生成。所有数字可在同目录产物中复查：")
    add("`meta.json` / `schematic_*.json` / `nets.json` / `pcb_netlist.csv` / "
        "`pcb_geometry.json`。")
    return "\n".join(lines)


def run_single(project_path, out_dir, page_key=None):
    artifacts, _docs, _proj = collect(project_path, page_key)
    os.makedirs(out_dir, exist_ok=True)
    written = []

    written.append(_write_json(os.path.join(out_dir, "meta.json"), artifacts["meta"]))

    nets_dump = {"pages": []}
    used_names = set()
    for ir in artifacts["pages_ir"]:
        base = _slug(ir["page"]["name"])
        name = base
        suffix = 2
        while name in used_names:
            name = "%s_%d" % (base, suffix)
            suffix += 1
        used_names.add(name)
        written.append(_write_json(
            os.path.join(out_dir, "schematic_%s.json" % name), ir))
        nets_dump["pages"].append({"page": ir["page"], "nets": ir["nets"]})
    written.append(_write_json(os.path.join(out_dir, "nets.json"), nets_dump))

    written.append(_write_text(os.path.join(out_dir, "pcb_netlist.csv"),
                               pcbnet.format_netlist_csv(artifacts["pcb_rows"]),
                               encoding="utf-8-sig"))
    written.append(_write_json(os.path.join(out_dir, "pcb_geometry.json"),
                               artifacts["geometry"]))

    mismatches, stats = check_consistency(artifacts)
    floating, floating_truncated = find_floating(artifacts["pages_ir"])
    report = render_report(artifacts, mismatches, stats, floating, floating_truncated)
    written.append(_write_text(os.path.join(out_dir, "report.md"), report))

    print("工程: %s" % artifacts["meta"]["project"])
    print("文档: %d，解析页: %s，PCB 网表: %d 行"
          % (len(artifacts["meta"]["documents"]),
             "、".join(artifacts["meta"]["pages_analyzed"]) or "无",
             len(artifacts["pcb_rows"])))
    print("网表比对: 可比 %d，不一致 %d，未命名跳过 %d"
          % (stats["compared"], stats["mismatch"], stats["skipped_unnamed"]))
    print("产物:")
    for path in written:
        print("  %s" % path)
    return 0


def run_diff(old_path, new_path, out_dir):
    import epru_diff
    result = epru_diff.compare(old_path, new_path)
    os.makedirs(out_dir, exist_ok=True)
    written = [_write_json(os.path.join(out_dir, "diff.json"), result)]
    summary = epru_diff.summarize(result)
    written.append(_write_text(os.path.join(out_dir, "diff.md"),
                               "# 版本差异\n\n```\n" + summary + "\n```\n"))
    print(summary)
    print("产物:")
    for path in written:
        print("  %s" % path)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="嘉立创EDA专业版工程解析")
    ap.add_argument("project", nargs="?", help=".epro2 文件")
    ap.add_argument("--out", default="eda-out", help="产物目录（默认 ./eda-out）")
    ap.add_argument("--page", help="只解析指定页（页名或 uuid），默认解析全部原理图页")
    ap.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), help="对比两版工程")
    args = ap.parse_args(argv)

    try:
        if args.diff:
            return run_diff(args.diff[0], args.diff[1], args.out)
        if not args.project:
            ap.error("需要给出 .epro2 文件，或使用 --diff OLD NEW")
        return run_single(args.project, args.out, args.page)
    except epro2.ProjectError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

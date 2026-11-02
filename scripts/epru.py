#!/usr/bin/env python3
"""`.epru` 记录流解析：逐行 `{record}||{payload}`，按 (type, id) 取最大 ticket。

核心语义:
  - 一行一条记录，`||` 前是头部 JSON（type/id/ticket），后面是可选的载荷 JSON
  - 同一 (type, id) 会有多条记录，**ticket 最大的才是当前状态**
  - `DOCHEAD` 以载荷里的 docType/uuid 划分文档段；之后的记录归属该段
  - 载荷为空字符串表示该元素已被删除

用法:
  python epru.py <project.epro2> --docs      # 文档清单
  python epru.py <project.epro2> --types     # 各文档的记录类型统计
"""
import argparse
import json
import sys

import epro2


def norm_id(rid):
    """元素 id 归一化为字符串：可能是字符串、数字，也可能是 JSON 字符串。"""
    if isinstance(rid, str):
        return rid
    if rid is None:
        return ""
    try:
        return json.dumps(rid, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(rid)


def _loads(raw):
    if raw is None:
        return None
    s = raw.strip()
    if s in ("", '""'):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def parse_records(text, stats=None):
    """返回文档列表：[{docType, uuid, editVersion, elems{(type, id): (ticket, payload)}}]。"""
    docs = []
    cur = None
    unparsed = 0
    total = 0
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        total += 1
        if "||" not in s:
            unparsed += 1
            continue
        if s.endswith("|"):
            s = s[:-1]
        head_raw, _, payload_raw = s.partition("||")
        try:
            head = json.loads(head_raw)
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(head, dict):
            unparsed += 1
            continue

        rtype = head.get("type")
        rid = head.get("id")
        ticket = head.get("ticket")
        ticket = ticket if isinstance(ticket, (int, float)) else 0

        if rtype == "DOCHEAD":
            payload = _loads(payload_raw) or {}
            cur = {
                "docType": payload.get("docType"),
                "uuid": payload.get("uuid"),
                "editVersion": payload.get("editVersion"),
                "title": "",
                "elems": {},
            }
            docs.append(cur)
            continue

        if cur is None:
            unparsed += 1
            continue

        key = (rtype, norm_id(rid))
        prev = cur["elems"].get(key)
        if prev is None or ticket >= prev[0]:
            cur["elems"][key] = (ticket, _loads(payload_raw))

    if stats is not None:
        stats["total"] = total
        stats["unparsed"] = unparsed
    return docs


def doc_elements(doc):
    """返回 (by_type{type: [(id, payload)]}, meta)。已删除（payload 为 None）的记录跳过。"""
    by_type = {}
    meta = {}
    for (rtype, rid), (_ticket, payload) in doc["elems"].items():
        if payload is None:
            continue
        if rtype == "META":
            if isinstance(payload, dict):
                meta = payload
            continue
        by_type.setdefault(rtype, []).append((rid, payload))
    return by_type, meta


def attr_map(by_type):
    """{parentId: {key: value}}，用于把 ATTR 挂回所属元素。"""
    out = {}
    for rid, attr in by_type.get("ATTR", []):
        if not isinstance(attr, dict):
            continue
        parent = attr.get("parentId")
        key = attr.get("key")
        if parent and key:
            out.setdefault(parent, {})[key] = attr.get("value")
    return out


def flat_map(text):
    """文档内唯一的扁平映射 {(uuid, type, id): (ticket, payload)}，用于两版对比。"""
    out = {}
    for doc in parse_records(text):
        uuid = doc["uuid"] or "?"
        for (rtype, rid), val in doc["elems"].items():
            out["%s|%s|%s" % (uuid, rtype, rid)] = val
    return out


def docs_of(docs, doc_type):
    return [d for d in docs if d["docType"] == doc_type]


def doc_title(doc):
    _by_type, meta = doc_elements(doc)
    return meta.get("title") or ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="检查 .epru 记录流")
    ap.add_argument("project")
    ap.add_argument("--docs", action="store_true", help="列出文档")
    ap.add_argument("--types", action="store_true", help="统计各文档的记录类型")
    args = ap.parse_args(argv)

    try:
        proj = epro2.open_project(args.project)
    except epro2.ProjectError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    stats = {}
    docs = parse_records(proj.epru_text, stats)
    ratio = (stats["unparsed"] / float(stats["total"])) if stats.get("total") else 0.0
    print("记录行: %d，解析失败: %d（%.1f%%）"
          % (stats.get("total", 0), stats.get("unparsed", 0), ratio * 100))
    print("文档段: %d" % len(docs))

    if args.docs or not args.types:
        for doc in docs:
            print("  %-12s %-38s %s" % (doc["docType"], doc["uuid"], doc_title(doc)))

    if args.types:
        for doc in docs:
            by_type, _meta = doc_elements(doc)
            counts = ", ".join("%s×%d" % (t, len(v)) for t, v in sorted(by_type.items()))
            print("[%s] %s\n  %s" % (doc["docType"], doc["uuid"], counts or "(空)"))
    return 0 if ratio <= 0.05 else 3


if __name__ == "__main__":
    sys.exit(main())

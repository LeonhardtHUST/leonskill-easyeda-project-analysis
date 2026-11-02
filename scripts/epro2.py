#!/usr/bin/env python3
"""嘉立创EDA专业版工程容器（.epro2）解包。

.epro2 是 ZIP 归档，内含：
  - project2.json        项目元数据
  - <工程名>.epru        逐行编辑记录流（核心数据）
  - IMAGE/*.webp         预览图（解析不需要）

ZIP 成员名以 GBK 编码写入，Python 按 cp437 解码后需再转码。

用法:
  python epro2.py <project.epro2> --list
"""
import argparse
import json
import os
import sys
import zipfile


class ProjectError(Exception):
    """工程容器不可解析。"""


def decode_member_name(name):
    """ZIP 成员名 cp437 → GBK 还原，失败回退 UTF-8，最后回退原名。"""
    for enc in ("gbk", "utf-8"):
        try:
            return name.encode("cp437").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return name


def _looks_like_zip(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError as exc:
        raise ProjectError("无法读取文件：%s" % exc)


class Project(object):
    """解包后的工程。字段：path / epru_name / epru_text / project_meta / members。"""

    def __init__(self, path, epru_name, epru_text, project_meta, members):
        self.path = path
        self.epru_name = epru_name
        self.epru_text = epru_text
        self.project_meta = project_meta
        self.members = members

    @property
    def title(self):
        meta = self.project_meta or {}
        for key in ("title", "name"):
            if meta.get(key):
                return meta[key]
        return os.path.splitext(os.path.basename(self.path))[0]

    def line_count(self):
        return len([ln for ln in self.epru_text.split("\n") if ln.strip()])


def open_project(path):
    """打开 .epro2，返回 Project。失败抛 ProjectError（带可读原因）。"""
    if not os.path.isfile(path):
        raise ProjectError("文件不存在：%s" % path)
    if not _looks_like_zip(path):
        if os.path.isdir(path):
            raise ProjectError("这是目录，不是 .epro2：旧版 .epro 为目录式包，"
                               "请在 EDA 中另存为 .epro2")
        raise ProjectError("文件头不是 ZIP（PK\\x03\\x04）："
                           "可能是旧版 .epro 或被截断的下载文件")
    if not zipfile.is_zipfile(path):
        raise ProjectError("ZIP 结构损坏，无法读取目录")

    try:
        with zipfile.ZipFile(path) as zf:
            members = []
            epru_info = None
            for info in zf.infolist():
                name = decode_member_name(info.filename)
                members.append({"name": name, "size": info.file_size})
                if name.lower().endswith(".epru") and epru_info is None:
                    epru_info = info
            if epru_info is None:
                raise ProjectError("ZIP 内没有 .epru 工程日志："
                                   "不是嘉立创EDA专业版工程，或文件不完整")
            raw = zf.read(epru_info)
            text = raw.decode("utf-8", "replace")
            meta = {}
            for info in zf.infolist():
                if decode_member_name(info.filename) == "project2.json":
                    try:
                        meta = json.loads(zf.read(info).decode("utf-8", "replace"))
                    except (UnicodeDecodeError, ValueError):
                        meta = {}
                    break
    except zipfile.BadZipFile as exc:
        # 中央目录能读、成员读不出来（传输截断 / 写入中断）时走到这里。
        # 转成 ProjectError，让调用方按"主动报错不出半成品"处理。
        raise ProjectError("ZIP 数据损坏，解压失败（%s）：文件可能在传输或保存时被截断，"
                           "请在 EDA 里重新导出 .epro2" % exc)
    except NotImplementedError as exc:
        raise ProjectError("ZIP 用了不支持的压缩方式（%s）："
                           "请用 Python 3.8+ 与标准库解压" % exc)

    return Project(path, decode_member_name(epru_info.filename), text, meta, members)


def main(argv=None):
    ap = argparse.ArgumentParser(description="列出 .epro2 容器内容")
    ap.add_argument("project")
    ap.add_argument("--list", action="store_true", help="列出 ZIP 成员与工程元数据")
    args = ap.parse_args(argv)

    try:
        proj = open_project(args.project)
    except ProjectError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    print("工程: %s" % proj.title)
    print("记录流: %s（%d 行）" % (proj.epru_name, proj.line_count()))
    if args.list:
        print("成员:")
        for m in sorted(proj.members, key=lambda x: x["name"]):
            print("  %-40s %10d B" % (m["name"], m["size"]))
        if proj.project_meta:
            print("project2.json:")
            print(json.dumps(proj.project_meta, ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())

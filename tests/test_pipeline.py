#!/usr/bin/env python3
"""端到端测试：合成工程 → 解析 → 断言。

运行:
  python -m unittest discover -s tests -v
  或  python tests/test_pipeline.py
"""
import contextlib
import csv
import io
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import analyze          # noqa: E402
import epro2            # noqa: E402
import epru             # noqa: E402
import epru_diff        # noqa: E402
import pcbnet           # noqa: E402
import schematic        # noqa: E402
import make_fixture     # noqa: E402


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "TestProj.epro2")
        make_fixture.write_project(self.path)
        self.proj = epro2.open_project(self.path)
        self.docs = epru.parse_records(self.proj.epru_text)

    def tearDown(self):
        self.tmp.cleanup()


class TestContainer(FixtureCase):
    def test_members_and_title(self):
        self.assertEqual(self.proj.title, "TestProj")
        names = [m["name"] for m in self.proj.members]
        self.assertIn("project2.json", names)
        self.assertTrue(self.proj.epru_name.endswith(".epru"))

    def test_gbk_member_name_roundtrip(self):
        gbk = "工程文件.epru".encode("gbk").decode("cp437")
        self.assertEqual(epro2.decode_member_name(gbk), "工程文件.epru")

    def test_not_a_zip(self):
        bad = os.path.join(self.tmp.name, "bad.epro2")
        with open(bad, "wb") as f:
            f.write(b"not a zip")
        with self.assertRaises(epro2.ProjectError):
            epro2.open_project(bad)

    def test_missing_file(self):
        with self.assertRaises(epro2.ProjectError):
            epro2.open_project(os.path.join(self.tmp.name, "nope.epro2"))


class TestRecords(FixtureCase):
    def test_documents(self):
        types = [d["docType"] for d in self.docs]
        for expected in ("SYMBOL", "DEVICE", "FOOTPRINT", "SCH_PAGE", "PCB"):
            self.assertIn(expected, types)

    def test_ticket_reduction_keeps_latest(self):
        pcb = [d for d in self.docs if d["docType"] == "PCB"][0]
        key = ("PAD_NET", '["PAD_NET", "pc1", "2", "pd2"]')
        ticket, payload = pcb["elems"][key]
        self.assertEqual(ticket, 20)
        self.assertEqual(payload["padNet"], "VCC")


class TestNetlist(FixtureCase):
    def _rows(self):
        symbols = analyze.library.build_symbols(self.docs)
        devices = analyze.library.build_devices(self.docs)
        rows, _warnings = pcbnet.build_netlist(self.docs, symbols, devices)
        return rows

    def test_latest_record_wins(self):
        rows = self._rows()
        by_pad = {r["pad"]: r["net"] for r in rows}
        self.assertEqual(by_pad["1"], "GND")
        self.assertEqual(by_pad["2"], "VCC")

    def test_residue_component_dropped(self):
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["designator"] == "R1" for r in rows))
        self.assertNotIn("GHOST", [r["net"] for r in rows])

    def test_pad_number_maps_to_pin(self):
        rows = {r["pad"]: r for r in self._rows()}
        self.assertEqual(rows["1"]["pinNumber"], "1")
        self.assertEqual(rows["2"]["pinNumber"], "2")

    def test_csv_has_bom(self):
        text = pcbnet.format_netlist_csv(self._rows())
        self.assertTrue(text.startswith("位号,焊盘号,引脚号,引脚名,网络"))
        parsed = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(parsed), 2)


class TestSchematic(FixtureCase):
    def _ir(self):
        symbols = analyze.library.build_symbols(self.docs)
        devices = analyze.library.build_devices(self.docs)
        page = [d for d in self.docs if d["docType"] == "SCH_PAGE"][0]
        ir = schematic.page_to_ir(page, symbols, devices, "TestProj")
        schematic.build_nets(ir)
        return ir

    def test_pin_coordinates(self):
        ir = self._ir()
        r1 = [c for c in ir["components"] if c.get("designator") == "R1"][0]
        coords = sorted((p["x"], p["y"]) for p in r1["pins"])
        self.assertEqual(coords, [(80.0, -200.0), (120.0, -200.0)])

    def test_pin_coordinates_rot90_mirror(self):
        """rotation=90 + isMirror：按 library.py 文档头的变换顺序手算。

        符号 TRI_3PIN 的引脚本地坐标（文件坐标系，y 未取负）：
            引脚1 (-30, -10)   引脚2 (30, -10)   引脚3 (0, 20)
        变换顺序（library.transform_pin）：-rotation 旋转 → x=-x 镜像 → 平移 → 最终 y 取负。

        theta = radians(-90)，cos=0、sin=-1：
            x' = px*cos - py*sin = py
            y' = px*sin + py*cos = -px
        引脚1: (-10, 30)  引脚2: (-10, -30)  引脚3: (20, 0)
        镜像 x = -x：
            引脚1: (10, 30)  引脚2: (10, -30)  引脚3: (-20, 0)
        平移元件位置 (400, 300)：
            引脚1: (410, 330)  引脚2: (410, 270)  引脚3: (380, 300)
        最终 y 取负（Y_SIGN=-1）：330 → -330，270 → -270，300 → -300
        若镜像没生效，引脚1/2 会落在 390 而不是 410；若先平移再镜像，
        引脚1/2 会落在 -390（左右颠倒），两者都会被下面的断言挡住。
        """
        ir = self._ir()
        u3 = [c for c in ir["components"] if c.get("designator") == "U3"][0]
        self.assertEqual(u3["rotation"], 90)
        self.assertTrue(u3["mirror"])
        coords = {(p["number"], p["x"], p["y"]) for p in u3["pins"]}
        self.assertEqual(coords, {("1", 410.0, -330.0),
                                  ("2", 410.0, -270.0),
                                  ("3", 380.0, -300.0)})

    def test_pin_coordinates_rot180(self):
        """rotation=180 不镜像：纯旋转分支。

        theta = radians(-180)，cos=-1、sin≈0：
            x' = px*(-1) - py*0 = -px
            y' = px*0 + py*(-1) = -py
        引脚1 (-30, -10) → (30, 10)
        引脚2 (30, -10)  → (-30, 10)
        引脚3 (0, 20)    → (0, -20)
        平移 (600, 400)：引脚1 (630, 410)、引脚2 (570, 410)、引脚3 (600, 380)
        最终 y 取负：410 → -410，380 → -380
        """
        ir = self._ir()
        u4 = [c for c in ir["components"] if c.get("designator") == "U4"][0]
        self.assertEqual(u4["rotation"], 180)
        self.assertFalse(u4["mirror"])
        coords = {(p["number"], p["x"], p["y"]) for p in u4["pins"]}
        self.assertEqual(coords, {("1", 630.0, -410.0),
                                  ("2", 570.0, -410.0),
                                  ("3", 600.0, -380.0)})

    def test_nets_from_wire_and_netflag(self):
        ir = self._ir()
        names = {n["name"]: n for n in ir["nets"]}
        self.assertIn("GND", names)
        self.assertIn("VCC", names)
        gnd_pins = {(p["designator"], p["pinNumber"]) for p in names["GND"]["pins"]}
        self.assertIn(("R1", "1"), gnd_pins)
        vcc_pins = {(p["designator"], p["pinNumber"]) for p in names["VCC"]["pins"]}
        self.assertIn(("R1", "2"), vcc_pins)


class TestGeometry(FixtureCase):
    def test_outline_trace_via(self):
        geo = pcbnet.build_geometry(self.docs)
        self.assertAlmostEqual(geo["outline"]["width_mm"], 25.4, places=2)
        self.assertAlmostEqual(geo["outline"]["height_mm"], 12.7, places=2)
        self.assertEqual(len(geo["traces"]), 1)
        self.assertAlmostEqual(geo["traces"][0]["length_mm"], 25.4, places=2)
        self.assertEqual(len(geo["vias"]), 1)
        self.assertEqual(geo["stackup"][0]["raw"]["er"], 4.4)

    def test_segments_keep_endpoints(self):
        """聚合后的 traces 只有总长，下游提参/EM 需要逐段端点。"""
        geo = pcbnet.build_geometry(self.docs)
        self.assertEqual(geo["segment_count"], 1)
        seg = geo["segments"][0]
        self.assertEqual((seg["net"], seg["layer_id"]), ("VCC", 1))
        self.assertEqual((seg["x1"], seg["y1"], seg["x2"], seg["y2"]),
                         (0.0, 0.0, 1000.0, 0.0))
        self.assertEqual(seg["width"], 10)

    def test_pads_have_absolute_coordinates_and_net(self):
        """焊盘要能落到 (位号, 焊盘号, 网络, 层)，且不能被残留元件污染。"""
        geo = pcbnet.build_geometry(self.docs)
        pads = {(p["component"], p["pin"]): p for p in geo["pads"]}
        self.assertEqual(set(pads), {("R1", "1"), ("R1", "2")})
        self.assertEqual((pads[("R1", "1")]["x"], pads[("R1", "1")]["y"]), (-30.0, 0.0))
        self.assertEqual((pads[("R1", "2")]["x"], pads[("R1", "2")]["y"]), (30.0, 0.0))
        self.assertEqual(pads[("R1", "1")]["net"], "GND")
        # 同一焊盘有两条 PAD_NET，必须取 ticket 最大的一条（旧记录是空网络）
        self.assertEqual(pads[("R1", "2")]["net"], "VCC")
        # MULTI(12) 通孔盘落到元件所在层
        self.assertEqual(pads[("R1", "1")]["layer_id"], 1)

    def test_pad_rotation_is_applied(self):
        x, y = pcbnet._rotate(30, 0, 90)
        self.assertAlmostEqual(x, 0.0, places=9)
        self.assertAlmostEqual(y, 30.0, places=9)


class TestPolyPoints(unittest.TestCase):
    def test_rect_tail_is_not_a_point(self):
        """R 形式末尾的 rotation / radius 不是坐标点，不能算进包围盒。"""
        pts = pcbnet._poly_points({"path": ["R", 2000, 0, 500, 500, 0, 0]})
        self.assertEqual(len(pts), 4)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        self.assertEqual((min(xs), max(xs)), (2000.0, 2500.0))
        self.assertEqual((min(ys), max(ys)), (0.0, 500.0))

    def test_negative_size_uses_bbox(self):
        """w/h 为负表示反方向画，包围盒要按端点算，不能直接相加。"""
        pts = pcbnet._poly_points({"path": ["R", 1000, 500, -1000, -500, 0, 0]})
        xs = sorted(p[0] for p in pts)
        ys = sorted(p[1] for p in pts)
        self.assertEqual((xs[0], xs[-1]), (0.0, 1000.0))
        self.assertEqual((ys[0], ys[-1]), (0.0, 500.0))


class TestMultiPcbDocument(unittest.TestCase):
    def test_geometry_accumulates_across_docs(self):
        """两个 PCB 文档：过孔孔径分布要合并，铜层引用数要取并集。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Two.epro2")
            make_fixture.write_project(path, two_pcb=True)
            docs = epru.parse_records(epro2.open_project(path).epru_text)
            geo = pcbnet.build_geometry(docs)

            self.assertEqual(len(geo["vias"]), 2)
            self.assertEqual(set(geo["via_hole_histogram"]), {"11.811", "20.0"})
            self.assertEqual(geo["via_hole_histogram"]["11.811"], 1)
            self.assertEqual(geo["via_hole_histogram"]["20.0"], 1)
            # 层1 在文档一被走线引用，层2 在文档二被引用 → 两条铜层都被用到
            self.assertEqual(geo["copper_layers_declared"], 2)
            self.assertEqual(geo["copper_layers_used"], 2)
            # 走线也要两份都在：VCC 25.4mm + GND (300,400)mil → 500mil=12.7mm
            nets = {t["net"]: t for t in geo["traces"]}
            self.assertEqual(set(nets), {"VCC", "GND"})
            self.assertAlmostEqual(nets["GND"]["length_mm"], 12.7, places=2)
            # 板框：逐板各一条，outline 给并集（板1 0..1000mil，板2 2000..2500mil）
            self.assertEqual(len(geo["outlines"]), 2)
            self.assertEqual(geo["outline"]["boards"], 2)
            self.assertAlmostEqual(geo["outlines"][0]["width_mm"], 25.4, places=2)
            self.assertAlmostEqual(geo["outlines"][1]["width_mm"], 12.7, places=2)
            self.assertAlmostEqual(geo["outline"]["width_mm"], 63.5, places=2)
            self.assertAlmostEqual(geo["outline"]["height_mm"], 12.7, places=2)
            self.assertTrue(any("多板" in w for w in geo["warnings"]))


class TestBadZipBody(unittest.TestCase):
    def test_corrupt_member_raises_project_error(self):
        """中央目录完整但成员数据损坏（CRC 不对）：必须转成 ProjectError。"""
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.epro2")
            make_fixture.write_project(good)
            with open(good, "rb") as f:
                blob = bytearray(f.read())

            # 中央目录里的 CRC 才是读取时用来校验的（本地头那份被忽略），
            # 所以改这里才能造出"目录正常、成员数据对不上"的损坏文件。
            # 中央头布局：签名(4) 版本(2+2) 标志(2) 压缩法(2) 时间(2+2) → CRC 在 +16
            idx = bytes(blob).find(b"PK\x01\x02")
            self.assertGreater(idx, 0)
            blob[idx + 16:idx + 20] = b"\xde\xad\xbe\xef"
            bad = os.path.join(tmp, "bad.epro2")
            with open(bad, "wb") as f:
                f.write(bytes(blob))

            self.assertTrue(epro2.zipfile.is_zipfile(bad))  # 目录还在 → 不会被文件头检查挡掉
            with self.assertRaises(epro2.ProjectError) as ctx:
                epro2.open_project(bad)
            self.assertIn("ZIP", str(ctx.exception))


class TestDiff(unittest.TestCase):
    def test_net_rename_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "old.epro2")
            new = os.path.join(tmp, "new.epro2")
            make_fixture.write_project(old, "VCC")
            make_fixture.write_project(new, "VCC2")
            result = epru_diff.compare(old, new)
            changed = result["netlist"]["changed"]
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0]["before"], "VCC")
            self.assertEqual(changed[0]["after"], "VCC2")

    def test_max_field_change_detected(self):
        """只差 max 字段的焊盘记录必须出现在 changed 里（max 不是噪声）。

        曾经 max/min 在 VOLATILE_KEYS 里，被整字段剔除，
        焊盘包围盒尺寸变了却什么都看不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "old.epro2")
            new = os.path.join(tmp, "new.epro2")
            make_fixture.write_project(old, pad_max=60)
            make_fixture.write_project(new, pad_max=80)
            result = epru_diff.compare(old, new)
            changed = result["elements"]["changed"]
            self.assertEqual(len(changed), 1)
            self.assertEqual(changed[0]["type"], "PAD")
            self.assertEqual(changed[0]["before"]["max"], 60)
            self.assertEqual(changed[0]["after"]["max"], 80)
            # 几何确实变了，不能被算成噪声
            self.assertEqual(result["elements"]["noise_filtered"], 0)

    def test_identical_projects_have_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a.epro2")
            b = os.path.join(tmp, "b.epro2")
            make_fixture.write_project(a)
            make_fixture.write_project(b)
            result = epru_diff.compare(a, b)
            self.assertEqual(result["netlist"]["changed"], [])
            self.assertEqual(result["elements"]["changed"], [])

    def test_symbol_pin_swap_detected_in_netlist(self):
        """符号引脚号互换必须在网表 diff 里被捕到。

        两版只有 SYMBOL 的 Pin Number 互换，PCB 侧 PAD_NET 一字未改。
        若用"新旧符号表合并（新版覆盖旧版）"再重建两版网表，
        两版算出来的引脚映射完全相同，changed 会是空 —— 符号改版就这样漏掉了。
        """
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "old.epro2")
            new = os.path.join(tmp, "new.epro2")
            make_fixture.write_project(old)
            make_fixture.write_project(new, swap_pins=True)
            result = epru_diff.compare(old, new)
            changed = result["netlist"]["changed"]
            self.assertEqual({(c["designator"], c["pad"]) for c in changed},
                             {("R1", "1"), ("R1", "2")})
            # 网络没变，变的是焊盘号映射到的引脚名：pad1 由引脚 1 变成引脚 2
            by_pad = {c["pad"]: c for c in changed}
            self.assertEqual(by_pad["1"]["before"], "GND")
            self.assertEqual(by_pad["1"]["after"], "GND")
            self.assertIn("pinName", by_pad["1"]["fields"])
            self.assertEqual(by_pad["1"]["pin_before"]["name"], "1")
            self.assertEqual(by_pad["1"]["pin_after"]["name"], "2")
            self.assertEqual(by_pad["2"]["pin_before"]["name"], "2")
            self.assertEqual(by_pad["2"]["pin_after"]["name"], "1")


class TestPlacement(unittest.TestCase):
    def test_placement_of_normalizes_mirror_key(self):
        """记录用 isMirror、变换函数读 mirror：归一必须两种写法都认，缺省不镜像。"""
        placement_of = analyze.library.placement_of
        p = placement_of({"x": 1, "y": 2, "rotation": 90, "isMirror": True})
        self.assertEqual((p["x"], p["y"], p["rotation"]), (1, 2, 90))
        self.assertTrue(p["mirror"])
        self.assertTrue(placement_of({"mirror": True})["mirror"])
        self.assertFalse(placement_of({"x": 0, "y": 0})["mirror"])
        self.assertFalse(placement_of(None)["mirror"])


class TestBuildNetsSemantics(unittest.TestCase):
    """直接喂 IR 验证连通性语义，不经过 fixture。"""

    def test_pin_on_segment_interior_connects(self):
        """点在导线内部（EDA junction 语义）必须连通——分桶索引不能把它筛掉。"""
        ir = {
            "components": [
                {"designator": "R1", "componentType": "part",
                 "pins": [{"number": "1", "name": "1", "x": 0.0, "y": 0.0}]},
                {"designator": "R2", "componentType": "part",
                 "pins": [{"number": "1", "name": "1", "x": 300.0, "y": 0.0}]},
            ],
            "wires": [{"line": [0.0, 0.0, 400.0, 0.0], "net": "MID"}],
            "nets": [],
            "warnings": [],
        }
        pin_net = schematic.build_nets(ir)
        self.assertEqual(pin_net[("R1", "1")], "MID")
        self.assertEqual(pin_net[("R2", "1")], "MID")

    def test_near_but_not_touching_stays_separate(self):
        """容差内的平行引线不能误短接：0.2 的间距远大于 TOL=0.02。"""
        ir = {
            "components": [
                {"designator": "R1", "componentType": "part",
                 "pins": [{"number": "1", "name": "1", "x": 100.0, "y": 0.2}]},
            ],
            "wires": [{"line": [0.0, 0.0, 400.0, 0.0], "net": "MID"}],
            "nets": [],
            "warnings": [],
        }
        pin_net = schematic.build_nets(ir)
        # 孤立引脚会自成一个网络（未命名 → N$n），关键是它不能并到 MID 上
        self.assertTrue(pin_net[("R1", "1")].startswith("N$"))


class TestBuildNetsPerformance(unittest.TestCase):
    def test_large_ir_under_budget(self):
        """性能冒烟：1000 引脚 × 2000 线段必须秒级完成（上限放宽到 5s 防机器抖动）。

        逐条遍历的旧实现在这个规模上是 2×10^6 次 _on_segment，纯 Python 下要十几秒。
        """
        pin_count, seg_count = 1000, 2000
        comps = []
        for i in range(pin_count):
            comps.append({
                "designator": "U%d" % i,
                "componentType": "part",
                "pins": [{"number": "1", "name": "1",
                          "x": float((i % 50) * 200), "y": float((i // 50) * 200)}],
            })
        wires = []
        for j in range(seg_count):
            x1 = float((j % 50) * 200)
            y1 = float((j // 50) * 100)
            wires.append({"line": [x1, y1, x1 + 150.0, y1], "net": "N%d" % (j % 10)})
        ir = {"components": comps, "wires": wires, "nets": [], "warnings": []}

        start = time.perf_counter()
        schematic.build_nets(ir)
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 5.0, "build_nets 用时 %.2fs，超出冒烟上限" % elapsed)
        # 顺带确认不是"什么都没算"才算得快
        self.assertTrue(any(len(n["pins"]) > 1 for n in ir["nets"]))


class TestReportInternals(unittest.TestCase):
    """报告与比对里的边界行为，直接喂结构体，不经过 fixture。"""

    def test_component_at_origin_keeps_zero(self):
        """元件摆在 (0,0)：x/y/angle 为 0 不能被 or 链吃掉，落到 centerX/rotation 上。"""
        text = "\n".join([
            make_fixture._doc("PCB", "pb1", "PCB"),
            make_fixture._rec("COMPONENT", "c1", 1, {
                "x": 0, "y": 0, "centerX": 999, "centerY": 999,
                "angle": 0, "rotation": 90,
            }),
        ]) + "\n"
        docs = epru.parse_records(text)
        by_type, _meta = epru.doc_elements(docs[0])
        meta = pcbnet._component_meta(by_type)["c1"]
        self.assertEqual(meta["x"], 0)
        self.assertEqual(meta["y"], 0)
        self.assertEqual(meta["angle"], 0)

    def test_duplicate_pin_key_excluded_and_counted(self):
        """同一 (位号, 引脚号) 出现两行且网络矛盾：排除比对并计数，不能静默留首行。"""
        artifacts = {
            "pcb_rows": [
                {"designator": "R1", "component": "c1", "pad": "1",
                 "pinNumber": "1", "pinName": "1", "net": "GND"},
                {"designator": "R1", "component": "c1", "pad": "2",
                 "pinNumber": "1", "pinName": "1", "net": "VCC"},
            ],
            "pin_net": {("R1", "1"): "GND"},
            "ambiguous": {},
            "multi_page_designators": {},
        }
        mismatches, stats = analyze.check_consistency(artifacts)
        self.assertEqual(stats["duplicate_pin_keys"], 1)
        self.assertEqual(stats["duplicate_pin_conflicts"], 1)
        self.assertEqual(stats["compared"], 0)
        self.assertEqual(mismatches, [])

    def test_find_floating_marks_truncation(self):
        pages = [{"page": {"name": "P1"}, "nets": [
            {"name": "N%d" % i, "named": True, "pins": []} for i in range(45)]}]
        items, truncated = analyze.find_floating(pages, limit=40)
        self.assertEqual(len(items), 40)
        self.assertTrue(truncated)
        items, truncated = analyze.find_floating(pages, limit=100)
        self.assertEqual(len(items), 45)
        self.assertFalse(truncated)


class TestAnalyzeEndToEnd(unittest.TestCase):
    def test_products_written_and_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "TestProj.epro2")
            out = os.path.join(tmp, "out")
            make_fixture.write_project(project)
            with contextlib.redirect_stdout(io.StringIO()):
                code = analyze.run_single(project, out)
            self.assertEqual(code, 0)
            for name in ("meta.json", "nets.json", "pcb_netlist.csv",
                         "pcb_geometry.json", "report.md", "schematic_P1.json"):
                self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
            with open(os.path.join(out, "report.md"), encoding="utf-8") as f:
                report = f.read()
            self.assertIn("## 二、网表一致性", report)
            self.assertIn("不一致 0", report)
            self.assertNotIn("ERROR", report)

    def test_unknown_page_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "TestProj.epro2")
            make_fixture.write_project(project)
            with self.assertRaises(epro2.ProjectError):
                analyze.collect(project, "NotExist")

    def test_same_named_page_reported_by_uuid(self):
        """同名页（都叫 P1）只解析其中一张时，未解析的页必须按 uuid 指出来。

        旧实现拿 name 去比 pages_analyzed（存的是 label），两张同名页会被
        判成"都解析过了"，报告里只剩一行空的"未解析的页"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "Two.epro2")
            out = os.path.join(tmp, "out")
            make_fixture.write_project(project, duplicate_page=True)
            with contextlib.redirect_stdout(io.StringIO()):
                code = analyze.run_single(project, out, page_key="pg1")
            self.assertEqual(code, 0)
            with open(os.path.join(out, "report.md"), encoding="utf-8") as f:
                report = f.read()
            self.assertIn("未解析的页**：P1（pg2）", report)
            self.assertIn("有 1 个原理图页未被解析", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)

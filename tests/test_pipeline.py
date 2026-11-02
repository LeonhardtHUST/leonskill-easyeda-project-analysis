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

    def test_identical_projects_have_no_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = os.path.join(tmp, "a.epro2")
            b = os.path.join(tmp, "b.epro2")
            make_fixture.write_project(a)
            make_fixture.write_project(b)
            result = epru_diff.compare(a, b)
            self.assertEqual(result["netlist"]["changed"], [])
            self.assertEqual(result["elements"]["changed"], [])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

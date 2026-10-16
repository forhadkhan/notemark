#!/usr/bin/env python3
"""Tests for the Excel export (notemark.build_xlsx).

Pure Python: no window is shown. The workbook is read back with the standard library and, when
LibreOffice is installed, opened by it too, so the file is checked by a real spreadsheet app.
Needs the GTK bindings to import notemark, so run it under a display (xvfb-run) like the others.
"""
import csv
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import notemark  # noqa: E402

NS = {"m": notemark._NS_MAIN}


def read_sheet(data):
    """Map of cell reference to text, plus the sheet root, from the workbook bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = ElementTree.fromstring(zf.read("xl/worksheets/sheet1.xml"))
    cells = {}
    for c in root.iterfind(".//m:c", NS):
        cells[c.get("r")] = "".join(t.text or "" for t in c.iterfind(".//m:t", NS))
    return cells, root


class ParseTests(unittest.TestCase):
    def test_quotes_delimiters_and_newlines(self):
        text = 'a,b\n"x,1","he said ""hi""\nthere"\n'
        self.assertEqual(notemark.parse_csv(text), [["a", "b"], ["x,1", 'he said "hi"\nthere']])

    def test_delimiter_detection_matches_the_table(self):
        cases = {"a;b;c": ";", "a\tb\tc": "\t", "a|b|c": "|", "a,b": ",", "": ","}
        for text, want in cases.items():
            self.assertEqual(notemark.detect_delimiter(text), want, repr(text))

    def test_bom_crlf_and_ragged_rows(self):
        rows = notemark.parse_csv("﻿a,b,c\r\n1\r\n2,3,4,5\r\n")
        self.assertEqual(rows, [["a", "b", "c", ""], ["1", "", "", ""], ["2", "3", "4", "5"]])

    def test_empty_text_gives_no_rows(self):
        self.assertEqual(notemark.parse_csv(""), [])


class WorkbookTests(unittest.TestCase):
    def test_parts_and_cell_values(self):
        data, notes = notemark.build_xlsx("name,age\nAda,36\nLin,41\n", "people")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual(zf.testzip(), None)
            names = set(zf.namelist())
        self.assertTrue({"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                         "xl/styles.xml", "xl/worksheets/sheet1.xml"} <= names)
        cells, _ = read_sheet(data)
        self.assertEqual(cells, {"A1": "name", "B1": "age", "A2": "Ada", "B2": "36", "A3": "Lin", "B3": "41"})
        self.assertEqual(notes, {"cut": 0, "cleaned": 0})

    def test_header_is_bold_and_frozen(self):
        data, _ = notemark.build_xlsx("a,b\n1,2\n")
        _, root = read_sheet(data)
        self.assertEqual(root.find(".//m:c[@r='A1']", NS).get("s"), "1")
        self.assertIsNone(root.find(".//m:c[@r='A2']", NS).get("s"))
        pane = root.find(".//m:pane", NS)
        self.assertEqual((pane.get("state"), pane.get("ySplit")), ("frozen", "1"))

    def test_values_stay_text(self):
        data, _ = notemark.build_xlsx('=1+1,007,"1e3",@x\n')
        raw = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
        self.assertNotIn("<f>", raw)
        self.assertNotIn("<f ", raw)
        cells, _ = read_sheet(data)
        self.assertEqual([cells["A1"], cells["B1"], cells["C1"], cells["D1"]], ["=1+1", "007", "1e3", "@x"])

    def test_xml_special_characters_are_escaped(self):
        data, _ = notemark.build_xlsx('a & b,<tag>"q"\n')
        cells, _ = read_sheet(data)
        self.assertEqual(cells["A1"], "a & b")
        self.assertEqual(cells["B1"], '<tag>"q"')

    def test_control_characters_are_removed_and_counted(self):
        data, notes = notemark.build_xlsx("a\x01b,c\x0bd\n")
        cells, _ = read_sheet(data)
        self.assertEqual((cells["A1"], cells["B1"]), ("ab", "cd"))
        self.assertEqual(notes["cleaned"], 2)

    def test_line_breaks_inside_a_cell_are_kept(self):
        data, _ = notemark.build_xlsx('"one\r\ntwo"\n')
        cells, _ = read_sheet(data)
        self.assertEqual(cells["A1"], "one\ntwo")

    def test_long_cells_are_cut_and_counted(self):
        data, notes = notemark.build_xlsx("x" * (notemark.XLSX_MAX_CELL + 10) + ",ok\n")
        cells, _ = read_sheet(data)
        self.assertEqual(len(cells["A1"]), notemark.XLSX_MAX_CELL)
        self.assertEqual(notes["cut"], 1)

    def test_widths_follow_the_longest_cell_and_are_capped(self):
        long_word = "w" * 200
        data, _ = notemark.build_xlsx("short,%s\n1,2\n" % long_word)
        _, root = read_sheet(data)
        widths = [float(c.get("width")) for c in root.iterfind(".//m:col", NS)]
        self.assertEqual(widths, [notemark.XLSX_MIN_WIDTH, notemark.XLSX_MAX_WIDTH])

    def test_wide_characters_count_double(self):
        self.assertEqual(notemark._display_width("中文"), 4)
        self.assertEqual(notemark._display_width("ab\nabcd"), 4)

    def test_empty_table_is_still_a_workbook(self):
        data, _ = notemark.build_xlsx("")
        cells, _ = read_sheet(data)
        self.assertEqual(cells, {})

    def test_ragged_rows_fill_the_grid(self):
        data, _ = notemark.build_xlsx("a,b,c\n1\n")
        cells, root = read_sheet(data)
        self.assertEqual(len(root.findall(".//m:col", NS)), 3)
        self.assertEqual(cells, {"A1": "a", "B1": "b", "C1": "c", "A2": "1"})

    def test_too_many_columns_is_refused(self):
        with self.assertRaises(notemark.ExportError):
            notemark.build_xlsx(",".join(["a"] * (notemark.XLSX_MAX_COLS + 1)) + "\n")

    def test_too_many_rows_is_refused(self):
        with self.assertRaises(notemark.ExportError):
            notemark.build_xlsx("\n" * notemark.XLSX_MAX_ROWS + "x\n")


class SheetNameTests(unittest.TestCase):
    def test_forbidden_characters_are_replaced(self):
        self.assertEqual(notemark._sheet_name("sales [2026]: q1/q2?*"), "sales  2026   q1 q2")

    def test_length_and_reserved_names(self):
        self.assertEqual(len(notemark._sheet_name("x" * 60)), 31)
        self.assertEqual(notemark._sheet_name("History"), "Table")
        self.assertEqual(notemark._sheet_name(""), "Sheet1")
        self.assertEqual(notemark._sheet_name("'quoted'"), "quoted")


class LibreOfficeTests(unittest.TestCase):
    """Opens the workbook in LibreOffice and reads it back. Skipped when soffice is not installed."""

    @unittest.skipUnless(shutil.which("soffice"), "LibreOffice is not installed")
    def test_libreoffice_reads_the_table(self):
        text = 'name,note,code\nAda,"two\nlines, and a comma",007\nLin,=1+1,\n'
        data, _ = notemark.build_xlsx(text, "people")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "people.xlsx"
            src.write_bytes(data)
            profile = "file://" + str(Path(tmp) / "profile")
            subprocess.run(["soffice", "-env:UserInstallation=" + profile, "--headless", "--convert-to", "csv",
                            "--outdir", tmp, str(src)], check=True, capture_output=True, timeout=180)
            got = list(csv.reader(io.StringIO((Path(tmp) / "people.csv").read_text(encoding="utf-8"))))
        self.assertEqual(got[0], ["name", "note", "code"])
        self.assertEqual(got[1], ["Ada", "two\nlines, and a comma", "007"])
        self.assertEqual(got[2][:2], ["Lin", "=1+1"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

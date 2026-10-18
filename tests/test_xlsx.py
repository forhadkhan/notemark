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
import threading
import types
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import notemark  # noqa: E402
from gi.repository import GLib  # noqa: E402

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


class ThreadedExportTests(unittest.TestCase):
    """The export runs on a worker thread and reports back on the GTK main loop."""

    @staticmethod
    def run_loop(start):
        """Call start(done) and run a main loop until done(...) runs; returns what done got, or None on timeout."""
        loop, got = GLib.MainLoop(), []

        def done(*args):
            got.append((args, threading.current_thread() is threading.main_thread()))
            loop.quit()
        start(done)
        GLib.timeout_add(20000, loop.quit)
        loop.run()
        return got[0] if got else None

    def test_threaded_export_gives_the_same_bytes(self):
        text = "name,note\n" + "".join("row%d,\"a, b\nc\"\n" % i for i in range(300))
        want, want_notes = notemark.build_xlsx(text, "people")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "out.xlsx")
            worker = []
            (args, on_main) = self.run_loop(lambda done: notemark.run_in_thread(
                lambda: (worker.append(threading.current_thread()), notemark.write_xlsx_file(path, text, "people"))[1], done, "tag"))
            got = Path(path).read_bytes()
        self.assertEqual(args, ("tag", want_notes, None))
        self.assertTrue(on_main)
        self.assertIsNot(worker[0], threading.main_thread())
        self.assertEqual(got, want)

    def test_a_refused_export_comes_back_as_the_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.xlsx"
            wide = ",".join(["a"] * (notemark.XLSX_MAX_COLS + 1)) + "\n"
            (args, _) = self.run_loop(lambda done: notemark.run_in_thread(
                lambda: notemark.write_xlsx_file(str(path), wide, "t"), done))
            self.assertFalse(path.exists())
        self.assertIsNone(args[0])
        self.assertIsInstance(args[1], notemark.ExportError)

    def test_an_unwritable_path_comes_back_as_an_oserror(self):
        (args, _) = self.run_loop(lambda done: notemark.run_in_thread(
            lambda: notemark.write_xlsx_file("/nonexistent-dir/out.xlsx", "a\n", "t"), done))
        self.assertIsInstance(args[1], OSError)

    def window_stub(self):
        shown = []
        win = types.SimpleNamespace(
            _exporting=False, _closed=False, toast=types.SimpleNamespace(set_reveal_child=lambda *_: None),
            notify=lambda text, ms=0: shown.append(("notify", text)),
            error=lambda text, secondary: shown.append(("error", secondary)), seen=shown)
        win._xlsx_done = types.MethodType(notemark.NotemarkWindow._xlsx_done, win)
        return win

    def finish(self, win):
        """Run the main loop until the stub's export has reported back."""
        loop = GLib.MainLoop()
        GLib.timeout_add(20000, loop.quit)
        GLib.timeout_add(20, lambda: loop.quit() if not win._exporting else True)
        loop.run()

    def test_window_shows_the_same_messages_and_blocks_a_second_export(self):
        win = self.window_stub()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.xlsx")
            text = "a,b\n" + "1,2\n" * 20000
            notemark.NotemarkWindow._write_xlsx(win, path, text, "t")
            self.assertTrue(win._exporting)
            notemark.NotemarkWindow._write_xlsx(win, path, text, "t")      # refused while the first runs
            self.finish(win)
            self.assertEqual(Path(path).read_bytes(), notemark.build_xlsx(text, "t")[0])
        self.assertEqual([k for k, _ in win.seen], ["notify", "notify", "notify"])
        self.assertIn("already running", win.seen[1][1])
        self.assertEqual(win.seen[2][1], "Exported t.xlsx")

    def test_window_reports_an_export_error_in_its_dialog(self):
        win = self.window_stub()
        wide = ",".join(["a"] * (notemark.XLSX_MAX_COLS + 1)) + "\n"
        notemark.NotemarkWindow._write_xlsx(win, "/tmp/never-written.xlsx", wide, "t")
        self.finish(win)
        self.assertEqual(win.seen[-1][0], "error")
        self.assertIn("columns", win.seen[-1][1])
        self.assertFalse(win._exporting)

    def test_a_window_closed_during_the_export_is_left_alone(self):
        win = self.window_stub()
        with tempfile.TemporaryDirectory() as tmp:
            notemark.NotemarkWindow._write_xlsx(win, str(Path(tmp) / "t.xlsx"), "a,b\n1,2\n", "t")
            win._closed = True
            self.finish(win)
        self.assertEqual([k for k, _ in win.seen], ["notify"])      # only the "Exporting..." toast from before the close
        self.assertFalse(win._exporting)


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

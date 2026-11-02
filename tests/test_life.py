"""The socket lot is the contract. Costs are checked against a hand count."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from life_tables_for_lasers.life import load_lot, run_lot, survival_at

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "lot.json"
RESULT = (
    "hot-screen costs USD 3900 on laser-socket-105c (1 escape, 1 false scrap); "
    "hut-table costs USD 10740 (3 escapes, 1 false scrap); "
    "both-gates costs USD 3980 and still misses the overlap escape."
)


class LifeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.lot = load_lot(FIXTURE)
        self.report = run_lot(self.lot)

    def test_hut_table_does_not_cover_the_socket_warranty(self) -> None:
        projection = self.report.projection
        self.assertEqual(projection.af_text, "12.81")
        self.assertEqual(projection.last_socket_h, 12491)
        self.assertEqual(projection.knots[-1].survive_pct, 70)
        self.assertEqual(projection.warranty_hut_h, 512357)
        self.assertTrue(projection.unwritten)
        self.assertEqual(projection.naive_survive_pct, 94)
        self.assertEqual(survival_at(self.lot.table, 30000), 96)
        self.assertIsNone(survival_at(self.lot.table, 512357))

    def test_screen_costs(self) -> None:
        hut = self.report.policy("hut-table")
        hot = self.report.policy("hot-screen")
        both = self.report.policy("both-gates")
        self.assertEqual(self.report.limit_ppm, 30)
        self.assertEqual(self.report.cal_survivors, 4)
        self.assertEqual((hut.die_scrap, hut.escapes, hut.false_scrap, hut.cost_usd), (3, 3, 1, 10740))
        self.assertEqual((hot.die_scrap, hot.escapes, hot.false_scrap, hot.cost_usd), (5, 1, 1, 3900))
        self.assertEqual((both.die_scrap, both.escapes, both.false_scrap, both.cost_usd), (6, 1, 2, 3980))
        self.assertLess(hot.cost_usd, both.cost_usd)
        self.assertLess(both.cost_usd, hut.cost_usd)

    def test_overlap_escape_and_gates_do_not_read_each_other(self) -> None:
        by_id = {item.part.id: item for item in self.report.decisions}
        self.assertFalse(by_id["p9"].hut_scrap or by_id["p9"].hot_scrap)
        self.assertTrue(by_id["p9"].part.fails)
        self.assertTrue(by_id["p4"].hot_scrap and not by_id["p4"].hut_scrap)
        self.assertTrue(by_id["p7"].hut_scrap and not by_id["p7"].hot_scrap)
        self.assertTrue(by_id["p10"].hot_scrap and not by_id["p10"].part.fails)
        self.assertFalse(by_id["p7"].part.fails)

    def test_limit_ignores_lot_outcomes(self) -> None:
        from life_tables_for_lasers.life import drift_limit

        self.assertEqual(drift_limit(self.lot.calibration), 30)
        flipped = tuple(
            type(part)(part.id, part.split, part.mttf_h, part.drift_ppm, not part.fails)
            if part.split == "lot"
            else part
            for part in self.lot.parts
        )
        changed = type(self.lot)(
            program=self.lot.program,
            warranty_h=self.lot.warranty_h,
            hut_k=self.lot.hut_k,
            socket_k=self.lot.socket_k,
            ea_mev=self.lot.ea_mev,
            die_usd=self.lot.die_usd,
            surgery_usd=self.lot.surgery_usd,
            table=self.lot.table,
            parts=flipped,
            source=self.lot.source,
        )
        self.assertEqual(drift_limit(changed.calibration), 30)

    def test_result_line(self) -> None:
        self.assertEqual(self.report.result_line, RESULT)

    def test_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "life_tables_for_lasers", str(FIXTURE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(RESULT, proc.stdout)
        self.assertIn("AF 12.81", proc.stdout)
        self.assertIn("past the table", proc.stdout)

    def test_cli_json(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "life_tables_for_lasers", "--json", str(FIXTURE)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"], RESULT)
        self.assertEqual(payload["af"], "12.81")
        self.assertTrue(payload["unwritten"])
        self.assertEqual(payload["policies"][1]["cost_usd"], 3900)

    def test_missing_file(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "life_tables_for_lasers", "fixtures/missing.json"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(proc.stderr.strip())

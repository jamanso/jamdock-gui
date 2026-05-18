"""Tests for the pure-function helpers in ``jamdock_gui.core.results``.

These functions reproduce ``jamrank``'s ranking and ZINC-ID extraction
byte-for-byte, so a regression here means the Python pipeline diverges
from the bash one. Keep them green.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jamdock_gui.core.qvina_log import DockingMode
from jamdock_gui.core.results import (
    ResultRow,
    compute_sim_score,
    extract_zinc_id,
    ro5_violation_details,
    zinc_link,
)


# ---------------------------------------------------------------------------
# compute_sim_score — reproduces jamrank's Option 2 ranking exactly.
# ---------------------------------------------------------------------------
def _mode(mode: int, affinity: float, rmsd_lb: float, rmsd_ub: float) -> DockingMode:
    return DockingMode(mode=mode, affinity=affinity, rmsd_lb=rmsd_lb, rmsd_ub=rmsd_ub)


class TestComputeSimScore:
    def test_returns_none_for_empty_input(self) -> None:
        assert compute_sim_score([]) is None
        assert compute_sim_score(()) is None

    def test_single_mode_is_zero(self) -> None:
        # Only mode 1 (rmsd_lb=rmsd_ub=0 by definition). cnt_lb = cnt_ub = 1,
        # so pct = (1-1)*100/1 = 0 on both axes => SimScore 0.
        score = compute_sim_score([_mode(1, -8.0, 0.0, 0.0)])
        assert score == 0

    def test_all_modes_tightly_clustered(self) -> None:
        # 5 modes, all under the 1.6 / 3.2 Å cutoffs except mode 1 (which
        # is always under since lb=ub=0). Both percentages = 100*(5-1)/5 = 80.
        modes = [
            _mode(1, -8.0, 0.0, 0.0),
            _mode(2, -7.8, 0.5, 1.0),
            _mode(3, -7.5, 1.0, 2.0),
            _mode(4, -7.3, 1.2, 2.4),
            _mode(5, -7.0, 1.5, 3.0),
        ]
        assert compute_sim_score(modes) == 80

    def test_all_secondary_modes_above_cutoffs(self) -> None:
        # All non-best modes exceed both RMSD cutoffs. cnt_lb = cnt_ub = 1
        # (just mode 1), so pct = (1-1)*100/N = 0 => SimScore 0.
        modes = [
            _mode(1, -8.0, 0.0, 0.0),
            _mode(2, -7.0, 4.0, 6.0),
            _mode(3, -6.5, 5.0, 7.0),
        ]
        assert compute_sim_score(modes) == 0

    def test_negative_rmsd_is_ignored(self) -> None:
        # Defensive: qvina can emit negative RMSDs occasionally. The
        # function explicitly requires 0 <= rmsd to count toward cnt_*,
        # so a negative-RMSD row drops out of both counts.
        modes = [
            _mode(1, -8.0, 0.0, 0.0),
            _mode(2, -7.0, -1.0, -1.0),   # should NOT be counted
        ]
        # cnt_lb = cnt_ub = 1 (just mode 1) => SimScore 0.
        assert compute_sim_score(modes) == 0


# ---------------------------------------------------------------------------
# extract_zinc_id — matches jamrank's two-strategy heuristic.
# ---------------------------------------------------------------------------
class TestExtractZincId:
    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert extract_zinc_id(tmp_path / "does_not_exist.sdf") is None

    def test_finds_id_in_tag_block(self, tmp_path: Path) -> None:
        sdf = tmp_path / "412.sdf"
        sdf.write_text(
            "412\n"
            "  Mrv2014 01010000002D\n"
            "\n"
            "  0  0  0  0  0  0            999 V2000\n"
            "M  END\n"
            "> <ZINC_ID>\n"
            "ZINC000408682836\n"
            "\n"
            "$$$$\n",
            encoding="utf-8",
        )
        assert extract_zinc_id(sdf) == "ZINC000408682836"

    def test_finds_id_when_first_line_is_bare_zinc(self, tmp_path: Path) -> None:
        # jamlib sometimes emits SDFs whose title (first) line *is* the
        # ZINC ID — extract_zinc_id falls back to that when no tag is
        # present.
        sdf = tmp_path / "bare.sdf"
        sdf.write_text(
            "ZINC000123456789\n"
            "  some-toolkit 01010000002D\n"
            "\n"
            "M  END\n"
            "$$$$\n",
            encoding="utf-8",
        )
        assert extract_zinc_id(sdf) == "ZINC000123456789"

    def test_returns_none_for_sdf_without_zinc(self, tmp_path: Path) -> None:
        sdf = tmp_path / "unknown.sdf"
        sdf.write_text(
            "compound_42\n"
            "  some-toolkit 01010000002D\n"
            "\n"
            "M  END\n"
            "$$$$\n",
            encoding="utf-8",
        )
        assert extract_zinc_id(sdf) is None


# ---------------------------------------------------------------------------
# zinc_link — trivial URL builder, but cheap to lock down.
# ---------------------------------------------------------------------------
class TestZincLink:
    def test_returns_none_for_missing_id(self) -> None:
        assert zinc_link(None) is None
        assert zinc_link("") is None

    def test_builds_canonical_url(self) -> None:
        url = zinc_link("ZINC000408682836")
        # Trailing slash matters: the substance landing page on ZINC
        # responds with a redirect-or-500 without it.
        assert url == "https://zinc.docking.org/substances/ZINC000408682836/"


# ---------------------------------------------------------------------------
# ro5_violation_details — human-readable Rule-of-Five breakdown.
# ---------------------------------------------------------------------------
def _row(**descriptors: float | int) -> ResultRow:
    """Build a minimal ResultRow with the descriptor fields under test."""
    return ResultRow(
        ligand="dummy",
        log_path=Path("/tmp/dummy.log"),
        pose_path=Path("/tmp/dummy.pdbqt"),
        **descriptors,
    )


class TestRo5ViolationDetails:
    def test_empty_for_passing_compound(self) -> None:
        # MW 400, LogP 3, HBD 2, HBA 6 — all within bounds.
        row = _row(mw=400.0, logp=3.0, hbd=2, hba=6)
        assert ro5_violation_details(row) == []

    def test_flags_each_violated_rule(self) -> None:
        # Every descriptor breaks its rule.
        row = _row(mw=750.0, logp=6.5, hbd=8, hba=12)
        details = ro5_violation_details(row)
        joined = " / ".join(details)
        # Order matters (MW, LogP, HBD, HBA), but we just check each rule
        # surfaces — keeps the test robust to formatting tweaks.
        assert "MW" in joined
        assert "LogP" in joined
        assert "HBD" in joined
        assert "HBA" in joined
        assert len(details) == 4

    def test_skips_descriptors_that_are_none(self) -> None:
        # RDKit may be missing; descriptors stay None. The helper must
        # not raise on those rows and must not invent violations.
        row = _row(mw=None, logp=None, hbd=None, hba=None)
        assert ro5_violation_details(row) == []

    @pytest.mark.parametrize(
        "descriptor, value, expected_label",
        [
            ("mw", 600.0, "MW"),
            ("logp", 7.0, "LogP"),
            ("hbd", 6, "HBD"),
            ("hba", 12, "HBA"),
        ],
    )
    def test_single_violation_per_descriptor(
        self,
        descriptor: str,
        value: float | int,
        expected_label: str,
    ) -> None:
        row = _row(**{descriptor: value})
        details = ro5_violation_details(row)
        assert len(details) == 1
        assert details[0].startswith(expected_label)

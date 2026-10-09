# -*- coding: utf-8 -*-
"""Phase A: pack / cache / adaptive concurrency unit tests."""
from __future__ import annotations

import asyncio
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from app.pipeline.cache import DiskCache, content_hash
from app.pipeline.extractor import (
    LlmExtractor,
    RawFpEntry,
    entries_from_cache,
    entries_to_cache,
    pack_segments,
)
from app.pipeline.segmenter import Segment
from app.schemas.contract import FpType

_CACHE_ROOT = Path(__file__).resolve().parents[1] / "data" / "_test_cache"


def _seg(name: str, content: str, system: str = "S", config: str = "C") -> Segment:
    return Segment(system, config, name, content, path=name)


def test_pack_segments_groups_short_and_splits_long():
    short = [_seg(f"a{i}", "x" * 100) for i in range(5)]
    packs = pack_segments(short, max_chars=1000, max_segments=3)
    assert len(packs) == 2
    assert [len(p) for p in packs] == [3, 2]

    long = _seg("big", "y" * 5000)
    packs2 = pack_segments([long, _seg("s", "z" * 10)], max_chars=1000, max_segments=8)
    assert packs2[0] == [long]
    assert len(packs2[1]) == 1

    assert pack_segments(short, max_chars=0, max_segments=8) == [[s] for s in short]
    assert pack_segments(short, max_chars=1000, max_segments=1) == [[s] for s in short]


def test_pack_keeps_same_config_together():
    a = [_seg("a1", "x" * 50, config="A"), _seg("a2", "x" * 50, config="A"),
         _seg("b1", "x" * 50, config="B")]
    packs = pack_segments(a, max_chars=10000, max_segments=8)
    assert len(packs) == 2
    assert [s.moduleConfig for s in packs[0]] == ["A", "A"]
    assert [s.moduleConfig for s in packs[1]] == ["B"]


def test_disk_cache_roundtrip():
    root = _CACHE_ROOT / "roundtrip"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    cache = DiskCache(str(root), enabled=True, ttl_hours=24)
    assert cache.enabled is True
    assert cache.get("ns", "k") is None
    cache.set("ns", "k", {"a": 1})
    assert cache.get("ns", "k") == {"a": 1}
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_entries_cache_codec():
    rows = entries_to_cache([
        RawFpEntry("s", "c", "o", "r", FpType.ILF, Decimal("1.5"), "d", 1)
    ])
    back = entries_from_cache(rows)
    assert back[0].requirementName == "r"
    assert back[0].fpType == FpType.ILF
    assert back[0].fpCount == Decimal("1.5")
    assert back[0].auditResult == 1


@pytest.mark.asyncio
async def test_extract_pack_requires_seg_id_and_maps_back():
    segs = [_seg("o1", "c1"), _seg("o2", "c2")]

    class FakeClient:
        async def chat_json(self, system, user, *, max_tokens=None):
            assert "segId" in system
            assert max_tokens == 1234
            return [
                {"segId": 2, "requirementName": "B", "fpType": "EI", "description": "R10|2"},
                {"segId": 1, "requirementName": "A", "fpType": "ILF", "description": "R10|1"},
            ]

    skills = Path(__file__).resolve().parent.parent / "app" / "skills"
    ext = LlmExtractor(FakeClient(), skills_dir=str(skills), max_tokens=1234)
    out = await ext.extract_pack(segs, "NO4_QUOTATION", mode="spec")
    assert [e.requirementName for e in out] == ["B", "A"]
    assert out[0].softwareObject == "o2"
    assert out[1].softwareObject == "o1"


@pytest.mark.asyncio
async def test_extract_parallel_uses_pack_and_cache():
    from app.agents.orchestrator import _extract_parallel

    calls = {"n": 0}

    class FakeExt:
        async def extract_pack(self, segs, tool, mode="spec"):
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return [
                RawFpEntry(s.moduleSystem, s.moduleConfig, s.softwareObject,
                           f"{s.softwareObject}-r", FpType.EI, Decimal(1), "x")
                for s in segs
            ]

    segs = [_seg(f"n{i}", "body" * 20) for i in range(6)]
    root = _CACHE_ROOT / "parallel"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    cache = DiskCache(str(root), enabled=True, ttl_hours=24)
    assert cache.enabled is True
    entries, failures = await _extract_parallel(
        FakeExt(), segs, "NO4_QUOTATION", mode="spec",
        conc=2, pack_max_chars=500, pack_max_segments=3,
        cache=cache, skill_ver="v1", model="m",
    )
    assert failures == []
    assert len(entries) == 6
    assert calls["n"] >= 1
    first_calls = calls["n"]

    entries2, failures2 = await _extract_parallel(
        FakeExt(), segs, "NO4_QUOTATION", mode="spec",
        conc=2, pack_max_chars=500, pack_max_segments=3,
        cache=cache, skill_ver="v1", model="m",
    )
    assert failures2 == []
    assert len(entries2) == 6
    assert calls["n"] == first_calls


@pytest.mark.asyncio
async def test_extract_parallel_reports_failures_with_backoff(monkeypatch):
    from app.agents import orchestrator as orch

    async def fast_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(orch, "_backoff_sleep", fast_sleep)

    class Boom:
        async def extract_pack(self, segs, tool, mode="spec"):
            raise RuntimeError("rate_limit 429")

    seg = _seg("x", "c")
    entries, failures = await orch._extract_parallel(
        Boom(), [seg], "NO4_AUDIT", conc=1,
        pack_max_chars=1, pack_max_segments=1,
    )
    assert entries == []
    assert len(failures) == 1

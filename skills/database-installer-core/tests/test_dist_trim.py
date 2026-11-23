from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENDOR_CORE = ROOT / "vendor" / "memory_core"


def test_vendor_runtime_excludes_historical_pipeline_modules() -> None:
    forbidden = {
        "answer.py",
        "synthesis.py",
        "step3_candidates.py",
        "step4_groups.py",
        "step4_review.py",
        "step4b1a.py",
        "step4b1b.py",
        "step5a.py",
        "step5b.py",
        "step5c.py",
        "step6a.py",
        "step7a.py",
        "step7c.py",
        "step7d.py",
    }
    present = sorted(name for name in forbidden if (VENDOR_CORE / name).exists())
    assert present == []
    assert not any(path.match("step[3-7]*.py") for path in VENDOR_CORE.iterdir())

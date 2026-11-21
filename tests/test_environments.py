"""Whether a benchmark's tasks can run here at all: the images they name must be public, checked right after fetching."""
import json

from harbor_master.tasks import environments


def test_the_images_a_benchmark_names_are_found_and_looked_up_one_per_repository(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    rows = [{"environment": {"docker_image": f"someorg/pyscf_m_pyscf:pr-{n}"}} for n in range(5)]
    rows += [{"environment": {"docker_image": "public/toolkit"}} for _ in range(9)]
    (tmp_path / "data" / "tasks.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "task1" / "environment").mkdir(parents=True)
    pinned = "denoland/deno:2@sha256:" + "a" * 64                  # pinned by digest: a real image, not a placeholder
    (tmp_path / "task1" / "environment" / "Dockerfile").write_text(f"FROM python:3.12 AS build\nFROM build\nFROM {{{{IMAGE}}}}\nFROM {pinned}\n")
    found = environments.references(tmp_path)
    assert found["public/toolkit"] == 9 and found["python:3.12"] == 1 and "build" not in found and "{{IMAGE}}" not in found
    assert found[pinned] == 1
    looked = []
    monkeypatch.setattr(environments, "public", lambda image: looked.append(image) or not image.startswith("someorg/"))
    result = environments.check(tmp_path)
    assert len(looked) == 4                                        # one tag of the one-image-per-task repository, not five
    assert result["private_uses"] == 5 and result["public_uses"] == 11
    assert environments.verdict(result) == "" and "leave such tasks out" in environments.partly(result)
    (tmp_path / "data" / "built.jsonl").write_text(json.dumps({"docker_image": "lab/x:pr-1", "needs_build": True}) + "\n")
    monkeypatch.setattr(environments, "public", lambda image: False)                    # no registry has it: refused...
    assert "lab/x:pr-1" in environments.check(tmp_path / "data")["unknown"]           # ...unless its harness builds it

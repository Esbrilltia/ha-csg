"""M7 HACS package and pinned brand assets; PNG validation needs only stdlib."""

import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import struct
import tomllib
import zlib

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "csg_plus"


def test_hacs_discovers_exactly_one_standalone_integration():
    manifests = sorted((ROOT / "custom_components").glob("*/manifest.json"))
    assert manifests == [PACKAGE / "manifest.json"]
    assert not (ROOT / "custom_components" / "csg").exists()
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == PACKAGE.name == "csg_plus"
    assert manifest["name"] == hacs["name"] == "CSG Statistics Plus"
    assert manifest["version"] == "3.0.0-beta.1"
    assert manifest["codeowners"] == ["@Esbrilltia"]
    assert manifest["config_flow"] is True
    assert manifest["documentation"] == "https://github.com/Esbrilltia/ha-csg-plus/"
    assert manifest["issue_tracker"] == "https://github.com/Esbrilltia/ha-csg-plus/issues"
    assert hacs["homeassistant"] == "2026.9.3"
    assert hacs.get("content_in_root", False) is False
    assert hacs.get("zip_release", False) is False


def test_packaging_preserves_locked_runtime_and_candidate_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["source"] == {"virtual": "."})
    assert project["name"] == package["name"] == "ha-csg-plus"
    assert project["version"] == package["version"] == "3.0.0-beta.1"
    assert project["requires-python"] == ">=3.14.2,<3.15"
    assert project["dependencies"] == ["homeassistant==2026.9.3", "pycryptodome", "brotli"]
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.14.2"
    assert version("homeassistant") == "2026.9.3"


@pytest.mark.parametrize("name,size,blob_sha", [
    ("icon.png", (256, 256), "e62fa5453c7119bd7b0987b101558c8a2f2c564b"),
    ("icon@2x.png", (512, 512), "3ce6c56969aa36f96f0a82bce4808201662cbe7d"),
])
def test_brand_png_header_and_exact_pinned_blob(name, size, blob_sha):
    data = (PACKAGE / "brand" / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">I", data[8:12])[0] == 13
    assert data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == size
    assert struct.unpack(">I", data[29:33])[0] == zlib.crc32(data[12:29])
    assert hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() == blob_sha

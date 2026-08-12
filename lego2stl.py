#!/usr/bin/env python3
"""
lego2stl.py v1.0

Simple Rebrickable + LDraw workflow.

Example:
  lego2stl_output/<SET>/
    stl/
      Black/
        3004__QTY-16.stl
        3024__QTY-14.stl
        ...
      Red/
      ...
    3mf/
      Black.3mf
      Red.3mf
      Light_Bluish_Gray.3mf
      Multicolor.3mf
    inventory.csv
    report.json
    3mf_manifest.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import zipfile
import itertools
from io import BytesIO
from urllib.parse import urljoin
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import numpy as np
import requests
import trimesh
from PIL import Image

REBRICKABLE_API = "https://rebrickable.com/api/v3"
LDRAW_COMPLETE_URL = "https://library.ldraw.org/library/updates/complete.zip"
LDU_TO_MM = 0.4

DEFAULT_CACHE = Path.home() / ".cache" / "lego2stl"
DEFAULT_OUTPUT = Path.cwd() / "lego2stl_output"

SAFE_RE = re.compile(r"[^A-Za-z0-9._+-]+")
HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def safe_name(text: str) -> str:
    text = str(text or "").strip()
    text = SAFE_RE.sub("_", text)
    return text.strip("._") or "unknown"


def normalize_part_ref(ref: str) -> str:
    return ref.replace("\\", "/").strip().lower()


def normalize_hex(rgb: str, fallback="808080") -> str:
    s = str(rgb or "").strip().lstrip("#")
    return s.upper() if HEX_RE.match(s) else fallback


@dataclass
class InventoryItem:
    part_num: str
    part_name: str
    color_id: Optional[int]
    color_name: str
    color_rgb: str
    quantity: int
    ldraw_candidates: List[str]
    is_spare: bool = False


@dataclass
class ColorMesh:
    color_name: str
    rgb: str
    mesh: trimesh.Trimesh


@dataclass
class PreparedPart:
    item: InventoryItem
    ldraw_id: str
    color_meshes: List[ColorMesh]
    bounds: np.ndarray
    source_parent: str = ""
    exploded_minifig: bool = False

    @property
    def is_multicolor(self) -> bool:
        total_faces = sum(len(cm.mesh.faces) for cm in self.color_meshes)
        meaningful = []
        for cm in self.color_meshes:
            if total_faces and len(cm.mesh.faces) / total_faces >= 0.0005:
                meaningful.append(cm)
        return len(meaningful) > 1

    @property
    def width(self) -> float:
        return float(self.bounds[1, 0] - self.bounds[0, 0])

    @property
    def depth(self) -> float:
        return float(self.bounds[1, 1] - self.bounds[0, 1])



def cube_rotation_matrices():
    mats = []
    eye = np.eye(3)
    for perm in itertools.permutations(range(3)):
        p = eye[:, perm]
        for signs in itertools.product((-1.0, 1.0), repeat=3):
            m = p @ np.diag(signs)
            if np.linalg.det(m) > 0.999:
                out = np.eye(4)
                out[:3, :3] = m
                mats.append(out)
    uniq, seen = [], set()
    for m in mats:
        key = tuple(np.round(m[:3, :3].ravel(), 6))
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    return uniq


CUBE_ROTATIONS = cube_rotation_matrices()


def recalc_bounds(part):
    part.bounds = np.asarray([
        np.min([cm.mesh.bounds[0] for cm in part.color_meshes], axis=0),
        np.max([cm.mesh.bounds[1] for cm in part.color_meshes], axis=0),
    ])


def auto_orient_part(part):
    merged = trimesh.util.concatenate([cm.mesh.copy() for cm in part.color_meshes])
    best = None
    for rot in CUBE_ROTATIONS:
        test = merged.copy()
        test.apply_transform(rot)
        b = test.bounds
        ext = b[1] - b[0]
        height = float(ext[2])
        footprint = float(ext[0] * ext[1])
        z0 = float(b[0, 2])
        bottomish = int(np.count_nonzero(test.vertices[:, 2] <= z0 + 0.35))
        score = (round(height, 5), -round(footprint, 5), -bottomish)
        if best is None or score < best[0]:
            best = (score, rot)

    rot = best[1]
    for cm in part.color_meshes:
        cm.mesh.apply_transform(rot)
    recalc_bounds(part)

    b = part.bounds
    shift = np.array([
        -((b[0, 0] + b[1, 0]) / 2.0),
        -((b[0, 1] + b[1, 1]) / 2.0),
        -b[0, 2],
    ])
    for cm in part.color_meshes:
        cm.mesh.apply_translation(shift)
    recalc_bounds(part)


def ldraw_header(path):
    info = {"description": "", "org": "", "name": path.name}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return info

    if lines and lines[0].startswith("0 "):
        info["description"] = lines[0][2:].strip()

    for line in lines[:40]:
        s = line.strip()
        if s.startswith("0 !LDRAW_ORG "):
            info["org"] = s[len("0 !LDRAW_ORG "):].strip()
        elif s.startswith("0 Name:"):
            info["name"] = s.split(":", 1)[1].strip()
    return info


def direct_ldraw_refs(path):
    result = []
    try:
        data = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return result

    for raw in data.splitlines():
        tok = raw.strip().split()
        if len(tok) >= 15 and tok[0] == "1":
            result.append((tok[1], " ".join(tok[14:])))
    return result


def is_minifig_assembly(path):
    h = ldraw_header(path)
    desc = h["description"].lower()
    org = h["org"].lower()
    return (
        "minifig" in desc
        and any(x in desc for x in (
            "torso with arms",
            "with arms and hands",
            "hips and legs",
            "body with arms",
        ))
        and ("shortcut" in org or "assembly" in org)
    )


def resolve_ldraw_color_token(lib, token, parent_name, parent_rgb):
    try:
        code = int(token, 0)
    except Exception:
        return parent_name, parent_rgb
    if code in (16, 24):
        return parent_name, parent_rgb
    return lib.color_for_code(code)


def explode_minifig_shortcut(lib, path, parent_item, depth=0):
    if depth > 4 or not is_minifig_assembly(path):
        return []

    result = []
    for color_token, child_ref in direct_ldraw_refs(path):
        child_path = lib.resolve(child_ref)
        if child_path is None:
            continue

        cname, crgb = resolve_ldraw_color_token(
            lib, color_token, parent_item.color_name, parent_item.color_rgb
        )

        child_item = InventoryItem(
            part_num=Path(child_ref).stem,
            part_name=ldraw_header(child_path)["description"] or Path(child_ref).stem,
            color_id=parent_item.color_id,
            color_name=cname,
            color_rgb=crgb,
            quantity=parent_item.quantity,
            ldraw_candidates=[Path(child_ref).stem],
            is_spare=parent_item.is_spare,
        )

        if is_minifig_assembly(child_path):
            result.extend(explode_minifig_shortcut(lib, child_path, child_item, depth + 1))
        else:
            result.append((child_item, child_path))

    return result


def download_binary(url, path, session=None):
    if not url:
        return False
    sess = session or requests.Session()
    try:
        r = sess.get(url, timeout=45, headers={"User-Agent": "Mozilla/5.0 lego2stl/1.0"})
        r.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return True
    except Exception:
        return False


def save_images_as_pdf(image_blobs, output):
    images = []
    try:
        for blob in image_blobs:
            im = Image.open(BytesIO(blob))
            im.load()
            if im.mode != "RGB":
                im = im.convert("RGB")
            images.append(im)
        if not images:
            return False
        images[0].save(
            output,
            "PDF",
            resolution=150.0,
            save_all=True,
            append_images=images[1:],
        )
        return True
    except Exception:
        return False
    finally:
        for im in images:
            try:
                im.close()
            except Exception:
                pass


def download_set_assets(info, set_num, set_name, root):
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 lego2stl/1.0"})

    result = {
        "set_image": None,
        "instructions_pdf": None,
        "instruction_sources": [],
    }

    img_url = info.get("set_img_url") or ""
    if img_url:
        ext = Path(img_url.split("?", 1)[0]).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            ext = ".jpg"
        target = assets / ("set_image" + ext)
        if download_binary(img_url, target, session):
            result["set_image"] = str(target.relative_to(root))

    base_num = set_num.split("-", 1)[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", set_name).strip("_")
    sources = [
        f"https://lego.brickinstructions.com/lego_instructions/set/{base_num}/{slug}",
        f"https://brickset.com/sets/{set_num}",
        f"https://rebrickable.com/sets/{set_num}/",
    ]
    result["instruction_sources"] = sources
    (assets / "instructions_sources.txt").write_text(
        "\n".join(sources) + "\n",
        encoding="utf-8",
    )

    page_url = sources[0]
    blobs = []
    try:
        r = session.get(page_url, timeout=45)
        if r.ok:
            html = r.text
            pattern = r"(?i)(?:src|data-src|data-original)\s*=\s*[\"']([^\"']+)[\"']"
            urls = re.findall(pattern, html)
            seen = set()
            for u in urls:
                full = urljoin(page_url, u)
                if full in seen:
                    continue
                seen.add(full)
                low = full.lower()
                if not any(x in low for x in (base_num, "instruction", "manual")):
                    continue
                try:
                    rr = session.get(full, timeout=30)
                    if not rr.ok or "image" not in rr.headers.get("content-type", "").lower():
                        continue
                    if len(rr.content) < 8000:
                        continue
                    im = Image.open(BytesIO(rr.content))
                    w, h = im.size
                    im.close()
                    if w >= 500 and h >= 300:
                        blobs.append(rr.content)
                except Exception:
                    pass
    except Exception:
        pass

    if blobs:
        pdf = assets / "instructions.pdf"
        if save_images_as_pdf(blobs, pdf):
            result["instructions_pdf"] = str(pdf.relative_to(root))

    return result


def bambu_project_settings(colors):
    if not colors:
        colors = [("Default", "808080")]
    n = len(colors)
    return {
        "name": "project_settings",
        "from": "project",
        "enable_support": "1",
        "support_type": "normal(auto)",
        "support_style": "default",
        "support_on_build_plate_only": "0",
        "detect_overhang_wall": "1",
        "filament_colour": [f"#{rgb}" for _, rgb in colors],
        "default_filament_colour": [f"#{rgb}" for _, rgb in colors],
        "filament_type": ["PLA"] * n,
        "filament_settings_id": ["Generic PLA"] * n,
        "filament_diameter": ["1.75"] * n,
        "filament_flow_ratio": ["1"] * n,
        "filament_density": ["1.24"] * n,
        "filament_is_support": ["0"] * n,
        "filament_soluble": ["0"] * n,
        "enable_prime_tower": "1" if n > 1 else "0",
    }

class Rebrickable:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"key {api_key.strip()}",
            "User-Agent": "lego2print/3.0",
        })

    def _get(self, path_or_url: str, params=None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{REBRICKABLE_API}{path_or_url}"
        while True:
            r = self.session.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(max(1, int(r.headers.get("Retry-After", "2"))))
                continue
            if r.status_code == 401:
                raise RuntimeError("Rebrickable API key is invalid.")
            if r.status_code == 404:
                raise RuntimeError(f"Rebrickable: not found: {url}")
            r.raise_for_status()
            return r.json()

    def set_info(self, set_num: str) -> dict:
        return self._get(f"/lego/sets/{set_num}/")

    @staticmethod
    def _ldraw_candidates(part: dict) -> List[str]:
        ext = part.get("external_ids") or {}
        raw = ext.get("LDraw", [])
        if isinstance(raw, str):
            raw = [raw]
        elif isinstance(raw, dict):
            raw = raw.get("ext_ids") or raw.get("ids") or []
        result = [str(x).strip() for x in raw if str(x).strip()]
        pnum = str(part.get("part_num") or "").strip()
        if pnum and pnum not in result:
            result.append(pnum)
        return result

    def set_parts(self, set_num: str, include_spares=False) -> List[InventoryItem]:
        params = {
            "page_size": 1000,
            "inc_part_details": 1,
            "inc_color_details": 1,
            "inc_minifig_parts": 1,
        }
        data = self._get(f"/lego/sets/{set_num}/parts/", params=params)
        items = []

        while True:
            for row in data.get("results", []):
                if row.get("is_spare") and not include_spares:
                    continue
                part = row.get("part") or {}
                color = row.get("color") or {}

                items.append(InventoryItem(
                    part_num=str(part.get("part_num") or ""),
                    part_name=str(part.get("name") or ""),
                    color_id=color.get("id"),
                    color_name=str(color.get("name") or "Unknown"),
                    color_rgb=normalize_hex(color.get("rgb"), "808080"),
                    quantity=int(row.get("quantity") or 1),
                    ldraw_candidates=self._ldraw_candidates(part),
                    is_spare=bool(row.get("is_spare")),
                ))

            nxt = data.get("next")
            if not nxt:
                break
            data = self._get(nxt)

        return items

    def part_info(self, part_num: str, rgb="808080", color_name="Unspecified") -> InventoryItem:
        data = self._get(f"/lego/parts/{part_num}/", params={"inc_part_details": 1})
        return InventoryItem(
            part_num=str(data.get("part_num") or part_num),
            part_name=str(data.get("name") or ""),
            color_id=None,
            color_name=color_name,
            color_rgb=normalize_hex(rgb),
            quantity=1,
            ldraw_candidates=self._ldraw_candidates(data),
        )


class LDrawLibrary:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.zip_path = cache_dir / "complete.zip"
        self.root = cache_dir / "ldraw"
        self.file_index: Dict[str, Path] = {}
        self.colors: Dict[int, Tuple[str, str]] = {}

    def ensure(self, force_update=False):
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if force_update:
            shutil.rmtree(self.root, ignore_errors=True)
            self.zip_path.unlink(missing_ok=True)

        if not self.root.exists():
            if not self.zip_path.exists():
                print(f"[LDraw] Downloading {LDRAW_COMPLETE_URL}")
                with requests.get(LDRAW_COMPLETE_URL, stream=True, timeout=180) as r:
                    r.raise_for_status()
                    with self.zip_path.open("wb") as f:
                        for chunk in r.iter_content(1024 * 1024):
                            if chunk:
                                f.write(chunk)

            temp = self.cache_dir / "_extract"
            shutil.rmtree(temp, ignore_errors=True)
            temp.mkdir(parents=True)
            with zipfile.ZipFile(self.zip_path) as z:
                z.extractall(temp)

            candidates = [
                p.parent for p in temp.rglob("parts")
                if p.is_dir() and (p.parent / "p").exists()
            ]
            if not candidates:
                raise RuntimeError("Could not locate LDraw parts/ and p/ folders.")
            shutil.move(str(candidates[0]), str(self.root))
            shutil.rmtree(temp, ignore_errors=True)

        self._build_index()
        self._load_colors()

    def _build_index(self):
        self.file_index.clear()
        for base in ("parts", "p"):
            folder = self.root / base
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(folder).as_posix().lower()
                    self.file_index[rel] = path
                    self.file_index[path.name.lower()] = path

    def _load_colors(self):
        self.colors.clear()
        cfgs = list(self.root.glob("LDConfig.ldr")) + list(self.root.rglob("LDConfig.ldr"))
        if not cfgs:
            return
        rx = re.compile(
            r"^0\s+!COLOUR\s+(.+?)\s+CODE\s+(-?\d+)\s+VALUE\s+#([0-9A-Fa-f]{6})",
            re.I,
        )
        for line in cfgs[0].read_text(encoding="utf-8", errors="ignore").splitlines():
            m = rx.match(line.strip())
            if m:
                name, code, rgb = m.group(1), int(m.group(2)), m.group(3).upper()
                self.colors[code] = (name.replace("_", " "), rgb)

    def color_for_code(self, code: int) -> Tuple[str, str]:
        if code in self.colors:
            return self.colors[code]
        if code >= 0x2000000:
            rgb = f"{code & 0xFFFFFF:06X}"
            return (f"Direct {rgb}", rgb)
        return (f"LDraw {code}", "808080")

    def resolve(self, ref: str) -> Optional[Path]:
        ref = normalize_part_ref(ref)
        candidates = [ref]
        if not ref.endswith((".dat", ".ldr", ".mpd")):
            candidates.extend([ref + ".dat", ref + ".ldr"])
        for c in candidates:
            if c in self.file_index:
                return self.file_index[c]
        return None

    def resolve_top_part(self, candidates: Iterable[str]):
        for c in candidates:
            p = self.resolve(c)
            if p is not None:
                return c, p
        return None, None


class LDrawMesher:
    def __init__(self, library: LDrawLibrary):
        self.library = library

    def mesh_part(self, path: Path, base_name: str, base_rgb: str) -> List[ColorMesh]:
        buckets: Dict[str, Tuple[str, List[List[float]], List[List[int]]]] = {}
        self._load_recursive(
            path=path,
            transform=np.eye(4),
            current_color=("__CURRENT__", normalize_hex(base_rgb)),
            buckets=buckets,
            stack=[],
        )

        result = []
        all_bounds = []
        for rgb, (name, verts, faces) in buckets.items():
            if not faces:
                continue
            v = np.asarray(verts, dtype=float) * LDU_TO_MM
            f = np.asarray(faces, dtype=np.int64)
            mesh = trimesh.Trimesh(vertices=v, faces=f, process=True)
            try:
                mesh.remove_unreferenced_vertices()
                mesh.merge_vertices()
            except Exception:
                pass
            result.append(ColorMesh(
                color_name=base_name if name == "__CURRENT__" else name,
                rgb=rgb,
                mesh=mesh,
            ))
            all_bounds.append(mesh.bounds)

        if not result:
            raise RuntimeError(f"No triangle geometry in {path}")

        low = np.min([b[0] for b in all_bounds], axis=0)
        high = np.max([b[1] for b in all_bounds], axis=0)
        shift = np.array([
            -((low[0] + high[0]) / 2.0),
            -((low[1] + high[1]) / 2.0),
            -low[2],
        ])
        for cm in result:
            cm.mesh.apply_translation(shift)

        return result

    @staticmethod
    def _resolve_color(token: str, current: Tuple[str, str], lib: LDrawLibrary) -> Tuple[str, str]:
        try:
            code = int(token, 0)
        except ValueError:
            return current
        if code in (16, 24):
            return current
        return lib.color_for_code(code)

    @staticmethod
    def _add_poly(buckets, color, pts):
        name, rgb = color
        if rgb not in buckets:
            buckets[rgb] = (name, [], [])
        _, verts, faces = buckets[rgb]
        base = len(verts)
        verts.extend(pts.tolist())
        if len(pts) == 3:
            faces.append([base, base + 1, base + 2])
        elif len(pts) == 4:
            faces.append([base, base + 1, base + 2])
            faces.append([base, base + 2, base + 3])

    def _load_recursive(self, path, transform, current_color, buckets, stack):
        real = str(path.resolve())
        if real in stack:
            raise RuntimeError(f"LDraw recursion loop: {path}")

        text = path.read_text(encoding="utf-8", errors="ignore")
        for raw in text.splitlines():
            tok = raw.strip().split()
            if not tok:
                continue
            kind = tok[0]

            if kind == "1" and len(tok) >= 15:
                child_color = self._resolve_color(tok[1], current_color, self.library)
                try:
                    x, y, z = map(float, tok[2:5])
                    a, b, c, d, e, f, g, h, i = map(float, tok[5:14])
                except ValueError:
                    continue
                child_m = np.array([
                    [a, b, c, x],
                    [d, e, f, y],
                    [g, h, i, z],
                    [0, 0, 0, 1],
                ], dtype=float)
                child_path = self.library.resolve(" ".join(tok[14:]))
                if child_path is None:
                    raise FileNotFoundError(
                        f"Missing LDraw subfile {' '.join(tok[14:])} in {path.name}"
                    )
                self._load_recursive(
                    child_path,
                    transform @ child_m,
                    child_color,
                    buckets,
                    stack + [real],
                )

            elif kind in ("3", "4"):
                n = 3 if kind == "3" else 4
                needed = 2 + n * 3
                if len(tok) < needed:
                    continue
                color = self._resolve_color(tok[1], current_color, self.library)
                try:
                    pts = np.asarray(
                        list(map(float, tok[2:needed])),
                        dtype=float
                    ).reshape(n, 3)
                except ValueError:
                    continue
                pts_h = np.column_stack([pts, np.ones(n)])
                tpts = (transform @ pts_h.T).T[:, :3]
                self._add_poly(buckets, color, tpts)


class ThreeMFWriter:
    """
    Plain standards-based 3MF writer.

    Contains only:
      - mesh geometry
      - standard 3MF base-material colors
      - object instances / positions

    Contains NO:
      - Bambu printer preset
      - Bambu filament preset
      - Bambu process preset
      - supports
      - nozzle settings
      - G-code
      - private Bambu Metadata/*.config
    """
    NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

    def __init__(self):
        self.next_id = 2
        self.mesh_objects = []
        self.component_objects = []
        self.build_items = []
        self.materials = []
        self.material_index = {}

    def _mat_index(self, name, rgb):
        rgb = normalize_hex(rgb)
        key = (name, rgb)
        if key not in self.material_index:
            self.material_index[key] = len(self.materials)
            self.materials.append((name, rgb))
        return self.material_index[key]

    def add_part_definition(self, prepared):
        child_ids = []

        for cm in prepared.color_meshes:
            oid = self.next_id
            self.next_id += 1
            pi = self._mat_index(cm.color_name, cm.rgb)
            obj_name = f"{prepared.item.part_num}_{cm.color_name}"
            self.mesh_objects.append((oid, cm.mesh, pi, obj_name))
            child_ids.append(oid)

        if len(child_ids) == 1:
            return child_ids[0]

        parent = self.next_id
        self.next_id += 1
        self.component_objects.append(
            (parent, child_ids, prepared.item.part_num)
        )
        return parent

    def add_instance(self, object_id, x, y, z=0.0):
        transform = (
            f"1 0 0 0 1 0 0 0 1 "
            f"{x:.6f} {y:.6f} {z:.6f}"
        )
        self.build_items.append((object_id, transform))

    def write(self, output, title="LEGO parts"):
        output.parent.mkdir(parents=True, exist_ok=True)

        mats = [
            f'<base name="{xml_escape(name)}" displaycolor="#{rgb}FF"/>'
            for name, rgb in self.materials
        ]
        materials_xml = (
            f'<basematerials id="1">{"".join(mats)}</basematerials>'
        )

        object_xml = []

        for oid, mesh, pi, name in self.mesh_objects:
            verts = "".join(
                f'<vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>'
                for v in mesh.vertices
            )
            tris = "".join(
                f'<triangle v1="{int(f[0])}" v2="{int(f[1])}" '
                f'v3="{int(f[2])}" pid="1" p1="{pi}"/>'
                for f in mesh.faces
            )
            object_xml.append(
                f'<object id="{oid}" type="model" name="{xml_escape(name)}">'
                f'<mesh><vertices>{verts}</vertices>'
                f'<triangles>{tris}</triangles></mesh></object>'
            )

        for oid, child_ids, name in self.component_objects:
            comps = "".join(
                f'<component objectid="{cid}"/>'
                for cid in child_ids
            )
            object_xml.append(
                f'<object id="{oid}" type="model" name="{xml_escape(name)}">'
                f'<components>{comps}</components></object>'
            )

        build = "".join(
            f'<item objectid="{oid}" transform="{tr}"/>'
            for oid, tr in self.build_items
        )

        model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="{self.NS}">
  <metadata name="Title">{xml_escape(title)}</metadata>
  <metadata name="Application">lego2stl v1.0</metadata>
  <resources>
    {materials_xml}
    {"".join(object_xml)}
  </resources>
  <build>{build}</build>
</model>"""

        content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

        rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0"
    Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED
        ) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", rels)
            z.writestr("3D/3dmodel.model", model)


class ShelfPacker:
    def __init__(self, bed_x=256.0, bed_y=256.0, margin=8.0, spacing=5.0):
        self.bed_x = bed_x
        self.bed_y = bed_y
        self.margin = margin
        self.spacing = spacing

    def pack(self, instances: List[PreparedPart]):
        placements = []
        x = self.margin
        y = self.margin
        row_h = 0.0

        for p in instances:
            w = max(0.1, p.width)
            d = max(0.1, p.depth)

            if x + w + self.margin > self.bed_x:
                x = self.margin
                y += row_h + self.spacing
                row_h = 0.0

            if y + d + self.margin > self.bed_y:
                return None

            placements.append((p, x + w / 2.0, y + d / 2.0))
            x += w + self.spacing
            row_h = max(row_h, d)

        return placements


def build_plate_files(root, prepared_parts, bed_x, bed_y, margin, spacing):
    """
    v1.0:
    exactly ONE plain 3MF for each LEGO inventory color.

    Example:
      3mf/Black.3mf
      3mf/Red.3mf
      3mf/Light_Bluish_Gray.3mf
      3mf/Multicolor.3mf

    Each 3MF contains every required physical instance for that color.
    Placement is only a simple non-overlapping layout. It is NOT printer-aware
    and is NOT restricted to a Bambu bed size; the user can rearrange later.

    STL remains separate: many files inside stl/<Color>/.
    """
    out_root = root / "3mf"
    shutil.rmtree(out_root, ignore_errors=True)
    out_root.mkdir(parents=True, exist_ok=True)

    groups = {}
    for p in prepared_parts:
        group = "Multicolor" if p.is_multicolor else p.item.color_name
        groups.setdefault(group, []).append(p)

    manifest = []

    # Virtual layout only; no printer/bed profile.
    virtual_row_width = max(200.0, float(bed_x))
    gap = max(2.0, float(spacing))

    for group_name in sorted(groups, key=lambda x: (x == "Multicolor", x)):
        parts = groups[group_name]
        writer = ThreeMFWriter()
        object_cache = {}

        x = 0.0
        y = 0.0
        row_h = 0.0
        total_pieces = 0

        # Larger items first makes the simple layout less messy.
        parts_sorted = sorted(
            parts,
            key=lambda p: max(0.1, p.width) * max(0.1, p.depth),
            reverse=True,
        )

        for p in parts_sorted:
            cache_key = (
                p.item.part_num,
                p.item.color_name,
                tuple((cm.color_name, cm.rgb) for cm in p.color_meshes),
            )

            if cache_key not in object_cache:
                object_cache[cache_key] = writer.add_part_definition(p)

            oid = object_cache[cache_key]

            w = max(0.1, float(p.width))
            d = max(0.1, float(p.depth))

            for _ in range(max(1, int(p.item.quantity))):
                if x > 0.0 and x + w > virtual_row_width:
                    x = 0.0
                    y += row_h + gap
                    row_h = 0.0

                # Original LDraw orientation is preserved.
                # Translation accounts for the part's original bounds.
                tx = x - float(p.bounds[0][0])
                ty = y - float(p.bounds[0][1])
                tz = -float(p.bounds[0][2])

                writer.add_instance(oid, tx, ty, tz)

                x += w + gap
                row_h = max(row_h, d)
                total_pieces += 1

        filename = f"{safe_name(group_name)}.3mf"
        writer.write(
            out_root / filename,
            title=f"{group_name} - {total_pieces} pieces",
        )

        manifest.append({
            "file": f"3mf/{filename}",
            "color": group_name,
            "pieces": total_pieces,
            "unique_rows": len(parts),
        })

    (root / "3mf_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def export_stls(root: Path, parts: List[PreparedPart]):
    sroot = root / "stl"
    shutil.rmtree(sroot, ignore_errors=True)
    for p in parts:
        if len(p.color_meshes) == 1:
            pdir = sroot / safe_name(p.item.color_name)
            pdir.mkdir(parents=True, exist_ok=True)
            p.color_meshes[0].mesh.export(
                pdir / f"{safe_name(p.item.part_num)}__QTY-{p.item.quantity}.stl"
            )
        else:
            mdir = sroot / "Multicolor" / safe_name(p.item.part_num)
            mdir.mkdir(parents=True, exist_ok=True)
            for cm in p.color_meshes:
                cm.mesh.export(
                    mdir / f"{safe_name(cm.color_name)}_{cm.rgb}.stl"
                )


def write_inventory_csv(path: Path, rows: List[dict]):
    fields = [
        "part_num", "part_name", "color_name", "color_rgb", "quantity",
        "ldraw_id", "multicolor", "mesh_colors", "status",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def parse_args():
    p = argparse.ArgumentParser(
        description="LEGO set/part -> simple STL + plain 3MF from Rebrickable/LDraw"
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--set", dest="set_num", help="Set number, e.g. 6835-1")
    mode.add_argument("--part", dest="part_num", help="Part number, e.g. 3475b")

    p.add_argument("--api-key", default=os.environ.get("REBRICKABLE_API_KEY", ""))
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    p.add_argument("--include-spares", action="store_true")
    p.add_argument("--update-ldraw", action="store_true")
    p.add_argument("--stl", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--bed-x", type=float, default=256.0)
    p.add_argument("--bed-y", type=float, default=256.0)
    p.add_argument("--margin", type=float, default=8.0)
    p.add_argument("--spacing", type=float, default=5.0)
    p.add_argument("--part-color", default="808080")
    p.add_argument("--part-color-name", default="Unspecified")
    return p.parse_args()


def main():
    args = parse_args()
    if not args.api_key:
        eprint(
            "ERROR: Rebrickable API key required.\n"
            'Run: export REBRICKABLE_API_KEY="YOUR_KEY"'
        )
        return 2

    rb = Rebrickable(args.api_key)
    lib = LDrawLibrary(args.cache)
    lib.ensure(args.update_ldraw)
    mesher = LDrawMesher(lib)

    if args.set_num:
        set_num = args.set_num.strip()
        if "-" not in set_num:
            set_num += "-1"
        info = rb.set_info(set_num)
        set_name = str(info.get("name") or set_num)
        root = args.output / f"{safe_name(set_num)}_{safe_name(set_name)}"
        root.mkdir(parents=True, exist_ok=True)

        print("[Assets] Downloading set image and instruction resources...")
        assets_info = download_set_assets(info, set_num, set_name, root)

        items = rb.set_parts(set_num, args.include_spares)

        merged = {}
        for it in items:
            k = (it.part_num, it.color_id)
            if k in merged:
                merged[k].quantity += it.quantity
                for c in it.ldraw_candidates:
                    if c not in merged[k].ldraw_candidates:
                        merged[k].ldraw_candidates.append(c)
            else:
                merged[k] = it

        prepared = []
        rows = []
        failed = []

        print(f"[Set] {set_num}: {set_name}")
        print(f"[Set] {sum(x.quantity for x in merged.values())} physical pieces")

        for idx, item in enumerate(merged.values(), 1):
            print(
                f"[{idx:>3}/{len(merged)}] "
                f"{item.part_num:<14} x{item.quantity:<3} {item.color_name}"
            )
            ldraw_id, path = lib.resolve_top_part(item.ldraw_candidates)

            if path is None:
                status = "MISSING_LDRAW"
                failed.append((item.part_num, status))
                rows.append({
                    "part_num": item.part_num,
                    "part_name": item.part_name,
                    "color_name": item.color_name,
                    "color_rgb": item.color_rgb,
                    "quantity": item.quantity,
                    "ldraw_id": "",
                    "multicolor": "",
                    "mesh_colors": "",
                    "status": status,
                })
                continue

            try:
                exploded = explode_minifig_shortcut(lib, path, item)

                if exploded:
                    print(
                        f"    -> minifig assembly exploded into "
                        f"{len(exploded)} printable components"
                    )
                    for child_item, child_path in exploded:
                        cms = mesher.mesh_part(
                            child_path,
                            child_item.color_name,
                            child_item.color_rgb,
                        )
                        bounds = np.asarray([
                            np.min([cm.mesh.bounds[0] for cm in cms], axis=0),
                            np.max([cm.mesh.bounds[1] for cm in cms], axis=0),
                        ])
                        pp = PreparedPart(
                            item=child_item,
                            ldraw_id=child_path.stem,
                            color_meshes=cms,
                            bounds=bounds,
                            source_parent=item.part_num,
                            exploded_minifig=True,
                        )
                        prepared.append(pp)
                        rows.append({
                            "part_num": child_item.part_num,
                            "part_name": child_item.part_name,
                            "color_name": child_item.color_name,
                            "color_rgb": child_item.color_rgb,
                            "quantity": child_item.quantity,
                            "ldraw_id": child_path.stem,
                            "multicolor": "yes" if pp.is_multicolor else "no",
                            "mesh_colors": "; ".join(
                                f"{c.color_name} #{c.rgb}" for c in cms
                            ),
                            "status": f"OK_EXPLODED_FROM_{item.part_num}",
                        })
                    continue

                cms = mesher.mesh_part(
                    path,
                    item.color_name,
                    item.color_rgb
                )
                bounds = np.asarray([
                    np.min([cm.mesh.bounds[0] for cm in cms], axis=0),
                    np.max([cm.mesh.bounds[1] for cm in cms], axis=0),
                ])
                pp = PreparedPart(
                    item=item,
                    ldraw_id=str(ldraw_id),
                    color_meshes=cms,
                    bounds=bounds,
                )
                prepared.append(pp)
                rows.append({
                    "part_num": item.part_num,
                    "part_name": item.part_name,
                    "color_name": item.color_name,
                    "color_rgb": item.color_rgb,
                    "quantity": item.quantity,
                    "ldraw_id": ldraw_id,
                    "multicolor": "yes" if pp.is_multicolor else "no",
                    "mesh_colors": "; ".join(
                        f"{c.color_name} #{c.rgb}" for c in cms
                    ),
                    "status": "OK",
                })
            except Exception as exc:
                status = f"ERROR: {type(exc).__name__}: {exc}"
                failed.append((item.part_num, status))
                eprint("    ->", status)
                rows.append({
                    "part_num": item.part_num,
                    "part_name": item.part_name,
                    "color_name": item.color_name,
                    "color_rgb": item.color_rgb,
                    "quantity": item.quantity,
                    "ldraw_id": ldraw_id or "",
                    "multicolor": "",
                    "mesh_colors": "",
                    "status": status,
                })

        plates = build_plate_files(
            root,
            prepared,
            args.bed_x,
            args.bed_y,
            args.margin,
            args.spacing,
        )

        export_stls(root, prepared)

        write_inventory_csv(root / "inventory.csv", rows)

        report = {
            "set_num": set_num,
            "set_name": set_name,
            "physical_pieces": sum(x.quantity for x in merged.values()),
            "unique_part_color_rows": len(merged),
            "prepared_rows": len(prepared),
            "failed_rows": len(failed),
            "plates": plates,
            "assets": assets_info,
            "export_mode": {
                "version": "1.0",
                "geometry_source": "LDraw only",
                "stl_grouped_by_color": True,
                "one_3mf_per_color": True,
                "printer_profile_written": False,
                "filament_profile_written": False,
                "support_settings_written": False,
                "auto_orientation": False
            },
            "failed": [{"part": p, "reason": r} for p, r in failed],
        }
        (root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        print()
        print(f"DONE: {root}")
        print("3MF files:")
        for p in plates:
            print(f"  - {p['file']} ({p['pieces']} pcs)")
        if failed:
            print(
                f"WARNING: {len(failed)} inventory rows could not be converted. "
                "See report.json"
            )

    else:
        item = rb.part_info(
            args.part_num.strip(),
            args.part_color,
            args.part_color_name,
        )
        root = args.output / f"part_{safe_name(item.part_num)}"
        root.mkdir(parents=True, exist_ok=True)

        ldraw_id, path = lib.resolve_top_part(item.ldraw_candidates)
        if path is None:
            raise RuntimeError(f"No LDraw mapping found for {item.part_num}")

        cms = mesher.mesh_part(
            path,
            item.color_name,
            item.color_rgb
        )
        bounds = np.asarray([
            np.min([c.mesh.bounds[0] for c in cms], axis=0),
            np.max([c.mesh.bounds[1] for c in cms], axis=0),
        ])
        pp = PreparedPart(item, str(ldraw_id), cms, bounds)

        writer = ThreeMFWriter()
        oid = writer.add_part_definition(pp)
        writer.add_instance(
            oid,
            pp.width / 2 + 5,
            pp.depth / 2 + 5
        )
        out = root / f"{safe_name(item.part_num)}.3mf"
        writer.write(out, title=f"{item.part_num} {item.part_name}")

        if args.stl:
            export_stls(root, [pp])

        print(f"DONE: {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

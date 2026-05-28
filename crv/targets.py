"""crv/targets.py — Target pool management for blinded CRV sessions.

Solo-research blinding scaffold. The honest framing is "lockbox you have
to deliberately open" not "cryptographically tamper-proof": you own the
keys, so you *can* peek if you want to. But the workflow makes peeking
deliberate rather than accidental, which is the actual goal.

Storage layout (under <root>/targets/):

    pool/<coord>.enc        — Fernet-encrypted JSON: target content
    pool/<coord>.image      — encrypted image bytes (if any)
    keys/<coord>.key        — Fernet key (same filename = same target)
    pool/manifest.json      — list of coordinates + content SHA-256 hashes
                              (visible without unlocking)
    revealed/<coord>.json   — plaintext content after session reveal

Workflow:

    # Pool building (do this once, ideally before knowing what sessions
    # you'll run; the more targets in the pool the better):
    $ python -m crv.targets fetch --count 50

    # During a session run.py picks a target coordinate from the pool
    # and shows only the coordinate to the viewer.

    # After the viewer has finished and saved their notes, run.py asks
    # for a decoy ranking, then reveals the actual target:
    $ python -m crv.targets reveal <session-id>

CLI commands:
    fetch       Pull N new targets from the configured source
    list        Show pool stats (count, coverage)
    peek        Decrypt one target (for debugging only — taints blinding!)
    reveal      Unseal target for a finished session, write to revealed/
    seal        Re-seal a revealed target (rebuilds pool after reveal)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------------------------------------------------------
# Coordinate generation — SRI-style 8-digit numeric
# ----------------------------------------------------------------------------

def generate_coordinate() -> str:
    """Generate an SRI-style 8-digit coordinate, formatted as XXX-XXXX X."""
    digits = ''.join(secrets.choice(string.digits) for _ in range(8))
    return f"{digits[:3]}-{digits[3:7]} {digits[7]}"


def coord_to_filename(coord: str) -> str:
    """Strip a coordinate of formatting characters for use as a filename."""
    return ''.join(c for c in coord if c.isalnum())


def is_valid_coord(coord: str) -> bool:
    """Quick validity check — alphanumeric content of length 6-12."""
    clean = coord_to_filename(coord)
    return 6 <= len(clean) <= 12 and clean.isalnum()


# ----------------------------------------------------------------------------
# Lightweight crypto wrapper using only the Python stdlib.
#
# This is NOT meant to defeat a determined attacker; the keys live on the
# same filesystem as the ciphertext. The goal is to make accidental peeking
# inconvenient — opening the .enc file in a text editor won't reveal
# anything. For real adversarial security you'd want to keep the keys
# on a separate machine.
# ----------------------------------------------------------------------------

def _derive_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate a keystream from key + nonce using repeated SHA-256.
    This is essentially a hash-based stream cipher."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt bytes with the given 32-byte key. Output is `nonce || ciphertext`."""
    nonce = secrets.token_bytes(16)
    keystream = _derive_keystream(key, nonce, len(plaintext))
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, keystream))
    return nonce + ciphertext


def decrypt(blob: bytes, key: bytes) -> bytes:
    """Decrypt the output of encrypt()."""
    nonce, ciphertext = blob[:16], blob[16:]
    keystream = _derive_keystream(key, nonce, len(ciphertext))
    return bytes(c ^ k for c, k in zip(ciphertext, keystream))


def generate_key() -> bytes:
    return secrets.token_bytes(32)


def key_to_b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def key_from_b64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


# ----------------------------------------------------------------------------
# Target sources — plug-in interface
# ----------------------------------------------------------------------------

@dataclass
class FetchedTarget:
    """One target's worth of content as pulled from a source."""
    title:       str
    description: str
    image_url:   Optional[str] = None
    image_bytes: Optional[bytes] = None
    source:      str = ""
    license:     str = ""
    attribution: str = ""
    extra:       Dict[str, Any] = None


class WikimediaFeaturedSource:
    """Pull random featured pictures from Wikimedia Commons.

    Featured Pictures are a curated set on Commons (~14k+ images, all CC-
    licensed) chosen for visual quality. Variety is naturally high: places,
    objects, animals, technical drawings, art reproductions, photographs.
    Perfect for distinguishable target pools.

    No API key required. The MediaWiki API returns JSON; we parse it.

    Image fetching uses the MediaWiki thumbnail rendering pipeline
    (`iiurlwidth` parameter) so we get a JPEG-resized version (~200-800 KB
    for typical photos) instead of the multi-megabyte camera original.
    This is the polite way to consume Featured Pictures programmatically.

    Wikimedia's rate-limiting policy: requests should be slow (~1/sec
    typical, much slower for bulk). We retry on HTTP 429 with exponential
    backoff and respect `Retry-After` headers. The User-Agent identifies
    us per their UA policy (https://meta.wikimedia.org/wiki/User-Agent_policy).
    """

    API   = "https://commons.wikimedia.org/w/api.php"
    CATEGORY = "Featured_pictures_on_Wikimedia_Commons"
    # Wikimedia's UA policy requires a real identifier + contact method.
    # Set this to your real contact / project URL if you publish.
    USER_AGENT = (
        "crv-research-tool/0.1 "
        "(personal-research; via local Python urllib) "
        "Python-urllib"
    )

    def __init__(self,
                  fetch_images:    bool = True,
                  thumb_width_px:  int  = 1024,    # ~200-800 KB JPEGs typically
                  max_image_bytes: int  = 8_000_000,
                  request_delay:   float = 2.0,
                  max_retries:     int  = 5):
        self.fetch_images    = fetch_images
        self.thumb_width_px  = thumb_width_px
        self.max_image_bytes = max_image_bytes
        self.request_delay   = request_delay
        self.max_retries     = max_retries

    def fetch_one(self, rng: random.Random) -> Optional[FetchedTarget]:
        """Pick one random featured picture. May return None on transient
        network failure — caller should retry."""
        # Step 1: get a random sample of category members
        params = {
            "action":      "query",
            "list":        "categorymembers",
            "cmtitle":     f"Category:{self.CATEGORY}",
            "cmtype":      "file",
            "cmlimit":     "500",
            "format":      "json",
            "cmstartsortkey": rng.choice(string.ascii_uppercase),
        }
        data = self._api(params)
        if not data:
            return None
        members = data.get("query", {}).get("categorymembers", [])
        if not members:
            return None
        chosen = rng.choice(members)
        file_title = chosen["title"]   # e.g. "File:Foo.jpg"

        # Step 2: fetch image info + description, requesting a thumbnail URL
        # via iiurlwidth so we get a sensibly-sized JPEG instead of the
        # multi-megabyte camera original.
        info_params = {
            "action":      "query",
            "titles":      file_title,
            "prop":        "imageinfo|info",
            "iiprop":      "url|size|extmetadata",
            "iiurlwidth":  str(self.thumb_width_px),
            "format":      "json",
        }
        info = self._api(info_params)
        if not info:
            return None
        pages = info.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        imageinfo = page.get("imageinfo", [{}])[0]
        # `thumburl` is the resized JPEG. Fall back to original URL if
        # thumbnail rendering isn't available (rare).
        thumb_url   = imageinfo.get("thumburl") or imageinfo.get("url")
        original_url = imageinfo.get("url")
        meta        = imageinfo.get("extmetadata", {})

        description = self._strip_html(meta.get("ImageDescription", {}).get("value", ""))
        license_short = meta.get("LicenseShortName", {}).get("value", "")
        artist      = self._strip_html(meta.get("Artist", {}).get("value", ""))
        credit      = self._strip_html(meta.get("Credit", {}).get("value", ""))
        attribution = f"{artist} ({credit})" if artist or credit else file_title

        # Use the file title (without "File:" prefix and extension) as
        # the human-readable title.
        clean_title = file_title
        if clean_title.startswith("File:"):
            clean_title = clean_title[5:]
        clean_title = clean_title.rsplit(".", 1)[0].replace("_", " ")

        # Step 3: optionally fetch image bytes from the thumbnail URL
        image_bytes = None
        if self.fetch_images and thumb_url:
            image_bytes = self._download(thumb_url, max_bytes=self.max_image_bytes)

        return FetchedTarget(
            title=clean_title,
            description=description[:2000] if description else f"Featured Picture: {clean_title}",
            image_url=original_url,            # store ORIGINAL URL for attribution
            image_bytes=image_bytes,           # but cache the thumbnail bytes
            source="Wikimedia Commons Featured Pictures",
            license=license_short,
            attribution=attribution,
            extra={"file_title": file_title, "thumb_url": thumb_url},
        )

    def _api(self, params: dict) -> Optional[dict]:
        url = f"{self.API}?{urllib.parse.urlencode(params)}"
        body = self._request_with_backoff(url)
        if body is None:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            print(f"[targets] API JSON decode failed: {e}")
            return None

    def _download(self, url: str, max_bytes: int) -> Optional[bytes]:
        body = self._request_with_backoff(url, max_bytes=max_bytes)
        return body

    def _request_with_backoff(self, url: str,
                                max_bytes: Optional[int] = None) -> Optional[bytes]:
        """HTTP GET with proper Wikimedia citizenship:
        - User-Agent that identifies us
        - Honors Retry-After header on 429
        - Exponential backoff between retries
        - Length check before reading body"""
        req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
        delay = self.request_delay
        for attempt in range(1, self.max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if max_bytes is not None:
                        content_length = int(resp.headers.get("Content-Length") or 0)
                        if content_length > max_bytes:
                            print(f"[targets] response too large "
                                  f"({content_length} bytes); skipping")
                            return None
                        return resp.read(max_bytes)
                    return resp.read()

            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Honor Retry-After if present
                    retry_after = e.headers.get("Retry-After")
                    try:
                        wait = float(retry_after) if retry_after else delay
                    except ValueError:
                        wait = delay
                    wait = max(wait, delay)
                    print(f"[targets] HTTP 429 rate-limited (attempt {attempt}/"
                          f"{self.max_retries}). Backing off {wait:.1f}s ...")
                    time.sleep(wait)
                    delay = min(delay * 2, 60.0)   # cap backoff at 60 s
                    continue
                # Other HTTP error — give up
                print(f"[targets] HTTP {e.code}: {e.reason}")
                return None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                print(f"[targets] network error (attempt {attempt}/"
                      f"{self.max_retries}): {e}")
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
        print(f"[targets] giving up on {url} after {self.max_retries} attempts")
        return None

    @staticmethod
    def _strip_html(text: str) -> str:
        """Crude HTML stripper for Wikimedia metadata fields."""
        if not text:
            return ""
        out = []
        depth = 0
        for ch in text:
            if ch == "<":
                depth += 1
            elif ch == ">":
                depth -= 1
            elif depth == 0:
                out.append(ch)
        return "".join(out).strip()


# ----------------------------------------------------------------------------
# TargetPool — manages the sealed pool on disk
# ----------------------------------------------------------------------------

class TargetPool:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.pool_dir     = self.root / "pool"
        self.keys_dir     = self.root / "keys"
        self.revealed_dir = self.root / "revealed"
        for d in (self.pool_dir, self.keys_dir, self.revealed_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.pool_dir / "manifest.json"

    # ---- manifest helpers ----

    def manifest(self) -> List[dict]:
        if not self.manifest_path.exists():
            return []
        return json.loads(self.manifest_path.read_text())

    def _save_manifest(self, entries: List[dict]):
        self.manifest_path.write_text(json.dumps(entries, indent=2))

    def coordinates(self) -> List[str]:
        return [e["coord"] for e in self.manifest()]

    def available_coordinates(self) -> List[str]:
        """Coordinates not yet used in any session — pool minus revealed."""
        revealed = {p.stem for p in self.revealed_dir.glob("*.json")}
        return [c for c in self.coordinates()
                if coord_to_filename(c) not in revealed]

    # ---- adding targets ----

    def add(self, target: FetchedTarget) -> str:
        """Seal a fetched target and add it to the pool. Returns the
        coordinate assigned."""
        coord = generate_coordinate()
        # Guard against (astronomically unlikely) collision
        existing = set(self.coordinates())
        while coord in existing:
            coord = generate_coordinate()

        coord_fn = coord_to_filename(coord)
        content = {
            "coord":       coord,
            "title":       target.title,
            "description": target.description,
            "image_url":   target.image_url,
            "source":      target.source,
            "license":     target.license,
            "attribution": target.attribution,
            "extra":       target.extra or {},
            "fetched_at":  time.strftime("%Y-%m-%dT%H:%M:%S"),
            "has_image":   target.image_bytes is not None,
        }

        # Encrypt the JSON
        key = generate_key()
        plaintext = json.dumps(content, indent=2).encode("utf-8")
        ciphertext = encrypt(plaintext, key)

        # Hash for manifest integrity
        sha = hashlib.sha256(plaintext).hexdigest()

        # Save sealed content
        enc_path = self.pool_dir / f"{coord_fn}.enc"
        enc_path.write_bytes(ciphertext)

        # Save image bytes separately, also encrypted (if any)
        if target.image_bytes:
            img_path = self.pool_dir / f"{coord_fn}.image"
            img_path.write_bytes(encrypt(target.image_bytes, key))

        # Save key
        key_path = self.keys_dir / f"{coord_fn}.key"
        key_path.write_text(key_to_b64(key))

        # Update manifest
        entries = self.manifest()
        entries.append({
            "coord":       coord,
            "sha256":      sha,
            "has_image":   target.image_bytes is not None,
            "source":      target.source,
            "fetched_at":  content["fetched_at"],
        })
        self._save_manifest(entries)
        return coord

    # ---- picking targets ----

    def pick_random(self, rng: Optional[random.Random] = None) -> Optional[str]:
        """Pick a random available (un-revealed) coordinate.
        Returns None if the pool is empty / fully used."""
        rng = rng or random.SystemRandom()
        available = self.available_coordinates()
        if not available:
            return None
        return rng.choice(available)

    def get_manifest_entry(self, coord: str) -> Optional[dict]:
        for e in self.manifest():
            if e["coord"] == coord:
                return e
        return None

    # ---- backfilling images for no-image targets ----

    def refetch_image(self, coord: str, source: "WikimediaFeaturedSource") -> bool:
        """For a target that's already in the pool but has no image
        (e.g. it was fetched when Wikimedia was rate-limiting us), pull
        the image now using the same source. Returns True on success."""
        coord_fn = coord_to_filename(coord)
        enc_path = self.pool_dir / f"{coord_fn}.enc"
        key_path = self.keys_dir / f"{coord_fn}.key"
        img_path = self.pool_dir / f"{coord_fn}.image"
        if not enc_path.exists() or not key_path.exists():
            return False
        if img_path.exists():
            return True   # already has an image

        key = key_from_b64(key_path.read_text())
        plaintext = decrypt(enc_path.read_bytes(), key)
        content = json.loads(plaintext.decode("utf-8"))

        # Re-derive the thumbnail URL from the stored file title
        file_title = (content.get("extra", {}) or {}).get("file_title")
        if not file_title:
            return False
        info_params = {
            "action":      "query",
            "titles":      file_title,
            "prop":        "imageinfo",
            "iiprop":      "url",
            "iiurlwidth":  str(source.thumb_width_px),
            "format":      "json",
        }
        info = source._api(info_params)
        if not info:
            return False
        pages = info.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        imageinfo = page.get("imageinfo", [{}])[0]
        thumb_url = imageinfo.get("thumburl") or imageinfo.get("url")
        if not thumb_url:
            return False

        image_bytes = source._download(thumb_url, max_bytes=source.max_image_bytes)
        if not image_bytes:
            return False

        # Encrypt and save
        img_path.write_bytes(encrypt(image_bytes, key))

        # Update manifest entry
        entries = self.manifest()
        for e in entries:
            if e["coord"] == coord:
                e["has_image"] = True
                break
        self._save_manifest(entries)

        # Update encrypted content too (has_image flag)
        content["has_image"] = True
        enc_path.write_bytes(encrypt(
            json.dumps(content, indent=2).encode("utf-8"), key))
        return True

    # ---- revealing ----

    def reveal(self, coord: str) -> Optional[dict]:
        """Decrypt and return a target's content. Side effect: writes the
        plaintext to revealed/<coord>.json (so we know it's been used)."""
        coord_fn = coord_to_filename(coord)
        enc_path = self.pool_dir / f"{coord_fn}.enc"
        key_path = self.keys_dir / f"{coord_fn}.key"
        if not enc_path.exists() or not key_path.exists():
            return None
        key = key_from_b64(key_path.read_text())
        plaintext = decrypt(enc_path.read_bytes(), key)
        content = json.loads(plaintext.decode("utf-8"))

        # Decrypt image if present
        img_path = self.pool_dir / f"{coord_fn}.image"
        if img_path.exists():
            try:
                img_bytes = decrypt(img_path.read_bytes(), key)
                out_img = self.revealed_dir / f"{coord_fn}.jpg"
                out_img.write_bytes(img_bytes)
                content["revealed_image_path"] = str(out_img)
            except Exception as e:
                content["image_decrypt_error"] = str(e)

        # Save plaintext
        revealed_path = self.revealed_dir / f"{coord_fn}.json"
        revealed_path.write_text(json.dumps(content, indent=2))
        return content

    def is_revealed(self, coord: str) -> bool:
        coord_fn = coord_to_filename(coord)
        return (self.revealed_dir / f"{coord_fn}.json").exists()

    # ---- decoy ranking support ----

    def pick_decoys(self, exclude: str, count: int = 3,
                      rng: Optional[random.Random] = None) -> List[str]:
        """Return `count` decoy coordinates other than `exclude`, for
        post-session blinded ranking."""
        rng = rng or random.SystemRandom()
        candidates = [c for c in self.coordinates() if c != exclude]
        if len(candidates) <= count:
            return candidates
        return rng.sample(candidates, count)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_refetch_images(args):
    """Backfill images for pool entries that don't have them (e.g. due to
    rate-limiting during the original fetch)."""
    pool = TargetPool(Path(args.root))
    source = WikimediaFeaturedSource(
        fetch_images=True,
        thumb_width_px=args.thumb_width,
        max_image_bytes=args.max_image_bytes,
        request_delay=args.delay,
        max_retries=args.max_retries,
    )
    missing = [e for e in pool.manifest() if not e.get("has_image")]
    if not missing:
        print("No targets missing images. Pool is complete.")
        return 0
    print(f"Found {len(missing)} targets without images. Refetching ...")
    done = 0
    failed = 0
    for i, entry in enumerate(missing, 1):
        coord = entry["coord"]
        ok = pool.refetch_image(coord, source)
        if ok:
            done += 1
            print(f"[{i}/{len(missing)}] ✓ {coord}")
        else:
            failed += 1
            print(f"[{i}/{len(missing)}] ✗ {coord} (still no image)")
        time.sleep(args.delay)
    print(f"\nBackfilled {done} images. {failed} still without.")
    return 0


def cmd_fetch(args):
    pool = TargetPool(Path(args.root))
    source = WikimediaFeaturedSource(
        fetch_images=not args.no_images,
        thumb_width_px=args.thumb_width,
        max_image_bytes=args.max_image_bytes,
        request_delay=args.delay,
        max_retries=args.max_retries,
    )
    rng = random.SystemRandom()
    added = 0
    attempts = 0
    while added < args.count and attempts < args.count * 4:
        attempts += 1
        target = source.fetch_one(rng)
        if target is None:
            print(f"[{attempts}] fetch failed, retrying after {args.delay}s ...")
            time.sleep(args.delay)
            continue
        coord = pool.add(target)
        size = "no image" if target.image_bytes is None else f"{len(target.image_bytes)//1024} KB"
        print(f"[{added+1}/{args.count}] {coord}  {target.title[:60]}  ({size})")
        added += 1
        time.sleep(args.delay)
    print(f"\nFetched {added} new targets. Pool now has "
          f"{len(pool.coordinates())} total, "
          f"{len(pool.available_coordinates())} available.")


def cmd_list(args):
    pool = TargetPool(Path(args.root))
    entries = pool.manifest()
    available = set(pool.available_coordinates())
    print(f"Pool: {args.root}")
    print(f"Total: {len(entries)}    Available: {len(available)}    "
          f"Revealed: {len(entries) - len(available)}\n")
    for e in entries:
        marker = " " if e["coord"] in available else "✓"
        img = "📷" if e.get("has_image") else "  "
        print(f"  {marker} {img} {e['coord']}   {e['source']}  ({e['fetched_at']})")


def cmd_peek(args):
    """Decrypts a target without marking it revealed. Use sparingly —
    seeing the content taints the blinding for any future session that
    might pick that coordinate."""
    pool = TargetPool(Path(args.root))
    coord_fn = coord_to_filename(args.coord)
    enc_path = pool.pool_dir / f"{coord_fn}.enc"
    key_path = pool.keys_dir / f"{coord_fn}.key"
    if not enc_path.exists():
        print(f"No such coordinate: {args.coord}")
        return 1
    key = key_from_b64(key_path.read_text())
    plaintext = decrypt(enc_path.read_bytes(), key)
    content = json.loads(plaintext.decode("utf-8"))
    print(json.dumps(content, indent=2))
    print(f"\n⚠️  Peeking taints blinding. Consider this coordinate burned.")
    return 0


def cmd_reveal(args):
    pool = TargetPool(Path(args.root))
    content = pool.reveal(args.coord)
    if content is None:
        print(f"Could not reveal {args.coord}")
        return 1
    print(f"=== REVEAL: {args.coord} ===\n")
    print(f"Title:       {content.get('title')}")
    print(f"Source:      {content.get('source')}")
    print(f"License:     {content.get('license')}")
    print(f"Attribution: {content.get('attribution')}")
    print(f"\nDescription:\n  {content.get('description')}\n")
    if content.get("revealed_image_path"):
        print(f"Image: {content['revealed_image_path']}")
    return 0


def main(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(
        prog="crv.targets",
        description="Sealed target pool management for blinded CRV sessions")
    p.add_argument("--root", default="targets",
                   help="Pool root directory (default: targets)")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="Fetch new targets into the pool")
    f.add_argument("--count", type=int, default=20)
    f.add_argument("--delay", type=float, default=3.0,
                   help="Seconds between API requests (default 3.0; be polite "
                        "to Wikimedia — too fast triggers rate-limiting)")
    f.add_argument("--max-retries", type=int, default=5,
                   help="Max retries on HTTP 429 with exponential backoff")
    f.add_argument("--no-images", action="store_true",
                   help="Skip downloading image bytes (smaller pool, faster)")
    f.add_argument("--thumb-width", type=int, default=1024,
                   help="Thumbnail width in px (default 1024; smaller = faster "
                        "+ smaller pool; larger = better quality)")
    f.add_argument("--max-image-bytes", type=int, default=8_000_000,
                   help="Skip thumbnails larger than this (default 8MB)")
    f.set_defaults(func=cmd_fetch)

    l = sub.add_parser("list", help="Show pool contents")
    l.set_defaults(func=cmd_list)

    ri = sub.add_parser("refetch-images",
                          help="Backfill images for targets that don't have them")
    ri.add_argument("--delay", type=float, default=3.0)
    ri.add_argument("--max-retries", type=int, default=5)
    ri.add_argument("--thumb-width", type=int, default=1024)
    ri.add_argument("--max-image-bytes", type=int, default=8_000_000)
    ri.set_defaults(func=cmd_refetch_images)

    pk = sub.add_parser("peek", help="Decrypt a target (taints blinding!)")
    pk.add_argument("coord")
    pk.set_defaults(func=cmd_peek)

    r = sub.add_parser("reveal",
                        help="Unseal a target after a session (marks it used)")
    r.add_argument("coord")
    r.set_defaults(func=cmd_reveal)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

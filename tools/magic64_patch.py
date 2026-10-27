#!/usr/bin/env python3
"""
magic64_patch.py - build a signature-preserving runtime-patched image.

Wraps TomKing062's magic64 (chipram sign-check bypass) and injects this
repository's patch_data into the generated image.

What magic64 actually produces (byte-verified against a real build)
-------------------------------------------------------------------
magic64 does NOT duplicate the payload.  For a 1,262,792-byte stock LK it emits
a 1,263,048-byte image, i.e. +0x100 bytes, laid out as:

    0x000  DHTB header      mImgSize  0x133E10 -> 0x133F10  (+0x100)
    0x200  jump code        16 B: b <shellcode>
    0x210  payload          the ORIGINAL payload, byte-for-byte identical,
                            shifted forward by 0x10
    0x...  magic area       0x70 shellcode + 0x80 patch_data   (0xF0 total)
    0x...  footer           0x60: payload_offset = 0x210, cert_offset += 0x100
    0x...  cert / priv      unchanged, shifted by +0x100

The signed payload bytes are never touched, and the offsets that point at them
move to match, so the image still verifies.  At runtime the shellcode copies the
payload from 0x210 back to 0x200, applies patch_data, then branches back into
the restored payload.  (Compare magic64.cpp upstream: img_h->mImgSize +=
add_lenth, img_f->payload_offset = 0x210, img_f->cert_offset += add_lenth.)

This is NOT the bsp_sign_fxxker trick, which stores a second copy of the payload
and roughly doubles the image size.

Requirements
------------
* magic64.exe + aarch64-linux-android-{as,ld,objcopy}.exe in one directory
* run from a path WITHOUT parentheses or spaces (magic64 shells out to as/ld)

Usage
-----
    python magic64_patch.py <stock_image> <payload_base> <patches.json|inline> -o out.bin

Patch file format - both shapes are accepted
--------------------------------------------
    # object with metadata + guards (see patches/get_lock_status.json)
    {"target": {"image_sha256": "...", "payload_base": "0x9F000000"},
     "patches": [{"addr": "0x9F064540", "words": ["0x52800020", ...],
                  "original_words": ["0xA9BD7BFD", ...]}]}

    # bare array (no guards)
    [{"addr": 0x9F064540, "words": [0x52800020, ...]}]

Addresses and words may be ints or hex strings.  addr is the RUNTIME address of
the first word.  When original_words / target.image_sha256 are present they are
enforced: the tool refuses to emit an image for a different firmware instead of
silently patching the wrong function.

Example - T65 / UMS9230S get_lock_status:

    python tools/magic64_patch.py stock/lk-sign.bin 0x9F000000 \
        patches/get_lock_status.json -o lk-global1.bin \
        --magic64 downloads/chipram_exploit/magic64.exe

    stock  sha256 b286d8d3...  (1,262,792 B)
    output sha256 69145c92...  (1,263,048 B)
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

MAGIC_ADD_LEN = 0x100      # magic64 arm64 add_length: (0x10 + 0x70 + 0xFF) & ~0xFF
SHELLCODE_LEN = 0x70       # arm64 shellcode size
PATCH_SPACE = 0x80         # bytes available for patch_data
JUMP_LEN = 0x10
DHTB_MAGIC = 0x42544844    # "DHTB"
HEADER_SIZE = 0x200
MIMG_SIZE_OFF = 0x30


def die(msg):
    sys.exit("error: " + str(msg))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def as_int(v, what):
    """Accept ints and hex/decimal strings ('0x52800020')."""
    if isinstance(v, bool):
        die(what + ": unexpected boolean")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v.strip(), 0)
        except ValueError:
            die(what + ": cannot parse " + repr(v) + " as a number")
    die(what + ": expected int or hex string, got " + type(v).__name__)


def load_patch_file(path):
    """Return (patches, meta) from either shape; normalises numbers to ints."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        die("patch file not found: " + path)
    except json.JSONDecodeError as e:
        die("patch file is not valid JSON (" + path + "): " + str(e))

    if isinstance(raw, list):
        meta, entries = {}, raw
    elif isinstance(raw, dict):
        meta = raw
        entries = raw.get("patches")
        if entries is None:
            die("patch object has no 'patches' array")
    else:
        die("patch file must be a JSON array or an object containing 'patches'")

    if not isinstance(entries, list) or not entries:
        die("'patches' must be a non-empty array")

    patches = []
    for i, p in enumerate(entries):
        if not isinstance(p, dict):
            die("patches[%d] is not an object" % i)
        words = p.get("words")
        if not isinstance(words, list) or not words:
            die("patches[%d] has no 'words' array" % i)
        item = {
            "addr": as_int(p.get("addr"), "patches[%d].addr" % i),
            "words": [as_int(w, "patches[%d].words[%d]" % (i, k))
                      for k, w in enumerate(words)],
        }
        if p.get("original_words"):
            item["original_words"] = [
                as_int(w, "patches[%d].original_words[%d]" % (i, k))
                for k, w in enumerate(p["original_words"])
            ]
        if p.get("symbol"):
            item["symbol"] = p["symbol"]
        patches.append(item)
    return patches, meta


def read_mimg_size(img):
    if len(img) < 0x40:
        die("image too small")
    if struct.unpack_from("<I", img, 0)[0] != DHTB_MAGIC:
        die("not a SPRD signed image (magic=%r)" % (img[:4],))
    size = struct.unpack_from("<I", img, MIMG_SIZE_OFF)[0]
    if not size:
        die("broken image: mImgSize == 0")
    return size


def encode_patch_data(patches):
    """[addr][word_count][words...] ... terminated by a zero address."""
    buf = b""
    for p in patches:
        buf += struct.pack("<II", p["addr"], len(p["words"]))
        buf += b"".join(struct.pack("<I", w) for w in p["words"])
    buf += struct.pack("<I", 0)
    if len(buf) > PATCH_SPACE:
        die("patch_data too large: %d > %d bytes" % (len(buf), PATCH_SPACE))
    return buf


def main():
    ap = argparse.ArgumentParser(
        description="magic64 wrapper: signature-preserving runtime patch")
    ap.add_argument("image", help="stock signed image (e.g. lk-sign.bin)")
    ap.add_argument("payload_base", help="runtime base of the payload, e.g. 0x9F000000")
    ap.add_argument("patches", help="patch JSON file (object or array)")
    ap.add_argument("-o", "--output", default="patched_out.bin")
    ap.add_argument("--magic64", default="magic64.exe",
                    help="path to magic64.exe (its directory must also hold the "
                         "aarch64-linux-android-{as,ld,objcopy}.exe helpers)")
    ap.add_argument("--check", action="store_true",
                    help="validate the patch file against the image and exit "
                         "(no magic64 needed)")
    ap.add_argument("--force", action="store_true",
                    help="downgrade image_sha256 / original_words mismatches to warnings")
    args = ap.parse_args()

    base = as_int(args.payload_base, "payload_base")
    with open(args.image, "rb") as fh:
        src = fh.read()
    m_img_size = read_mimg_size(src)
    patches, meta = load_patch_file(args.patches)

    problems = []

    # guard 1: are we looking at the firmware this patch was made for?
    target = meta.get("target") or {}
    want_sha = target.get("image_sha256")
    if want_sha and want_sha.lower() != sha256(src):
        problems.append("image sha256 %s != %s (patch file was made for a "
                        "different firmware)" % (sha256(src), want_sha))
    if target.get("payload_base") is not None:
        want_base = as_int(target["payload_base"], "target.payload_base")
        if want_base != base:
            problems.append("payload_base 0x%X != 0x%X from patch file"
                            % (base, want_base))
    if target.get("stock_mimg_size") is not None:
        want_mimg = as_int(target["stock_mimg_size"], "target.stock_mimg_size")
        if want_mimg != m_img_size:
            problems.append("mImgSize 0x%X != 0x%X from patch file"
                            % (m_img_size, want_mimg))

    payload_lo, payload_hi = base, base + m_img_size
    for p in patches:
        span = 4 * len(p["words"])
        if not (payload_lo <= p["addr"] and p["addr"] + span <= payload_hi):
            die("patch at 0x%X (+%d) outside payload [0x%X, 0x%X)"
                % (p["addr"], span, payload_lo, payload_hi))

        # guard 2: are the original instructions really there?
        orig = p.get("original_words")
        if orig:
            file_off = HEADER_SIZE + (p["addr"] - base)   # payload starts at 0x200
            have = [struct.unpack_from("<I", src, file_off + 4 * k)[0]
                    for k in range(len(orig))]
            if have != orig:
                problems.append(
                    "0x%X: original instructions differ (file 0x%X: have %s, "
                    "expected %s)" % (p["addr"], file_off,
                                      ["0x%08X" % w for w in have],
                                      ["0x%08X" % w for w in orig]))

    if problems:
        for m in problems:
            print(("warning: " if args.force else "error: ") + m, file=sys.stderr)
        if not args.force:
            print("\nrefusing to build - pass --force only if you know the "
                  "offsets still apply to this image", file=sys.stderr)
            sys.exit(2)

    if args.check:
        print("image             : %s (%d bytes)" % (args.image, len(src)))
        print("sha256            : %s" % sha256(src))
        print("payload base      : 0x%X" % base)
        print("mImgSize (stock)  : 0x%X" % m_img_size)
        for p in patches:
            sym = " (%s)" % p["symbol"] if p.get("symbol") else ""
            print("patch             : 0x%X  %d words%s"
                  % (p["addr"], len(p["words"]), sym))
        print("patch_data bytes  : %d / %d" % (len(encode_patch_data(patches)),
                                               PATCH_SPACE))
        print("check             : OK")
        return

    # run magic64 in an isolated, bracket-free directory
    workdir = tempfile.mkdtemp(prefix="magic_")
    try:
        exe_name = os.path.basename(args.magic64)
        magic_dir = os.path.dirname(os.path.abspath(args.magic64))
        for f in (exe_name, "aarch64-linux-android-as.exe",
                  "aarch64-linux-android-ld.exe",
                  "aarch64-linux-android-objcopy.exe"):
            src_f = os.path.join(magic_dir, f)
            if not os.path.exists(src_f):
                die("missing helper: " + src_f)
            shutil.copy2(src_f, workdir)

        shutil.copy2(args.image, os.path.join(workdir, "img.bin"))
        r = subprocess.run([os.path.join(workdir, exe_name), "img.bin"],
                           cwd=workdir, capture_output=True, text=True)
        patched_path = os.path.join(workdir, "patched.bin")
        if r.returncode != 0:
            die("magic64 exited with %d\n%s\n%s"
                % (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()))
        if not os.path.exists(patched_path):
            die("magic64 did not produce patched.bin\n"
                + (r.stdout or "") + (r.stderr or ""))
        with open(patched_path, "rb") as fh:
            out = bytearray(fh.read())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # sanity on what magic64 produced
    if len(out) != len(src) + MAGIC_ADD_LEN:
        die("unexpected image growth: %d vs %d (expected +0x%X)"
            % (len(out), len(src), MAGIC_ADD_LEN))
    new_mimg = read_mimg_size(bytes(out))
    if new_mimg != m_img_size + MAGIC_ADD_LEN:
        die("unexpected mImgSize change: 0x%X (expected 0x%X)"
            % (new_mimg, m_img_size + MAGIC_ADD_LEN))

    # inject patch_data right after the shellcode
    pd_offset = HEADER_SIZE + JUMP_LEN + m_img_size + SHELLCODE_LEN
    blob = encode_patch_data(patches)
    if pd_offset + PATCH_SPACE > len(out):
        die("computed patch_data offset exceeds image; magic64 layout changed")
    out[pd_offset:pd_offset + len(blob)] = blob

    # every check runs BEFORE anything is written, so a failed build never
    # leaves a half-baked image on disk for someone to flash by mistake
    orig_payload = src[HEADER_SIZE:HEADER_SIZE + m_img_size]
    new_payload = bytes(out[HEADER_SIZE + JUMP_LEN:HEADER_SIZE + JUMP_LEN + m_img_size])
    if orig_payload != new_payload:
        die("original payload was modified - signature will NOT verify "
            "(nothing written)")

    expected = meta.get("expected_output") or {}
    digest = sha256(bytes(out))
    if expected.get("sha256") and digest != expected["sha256"].lower():
        die("output sha256 %s != expected %s (nothing written)"
            % (digest, expected["sha256"]))
    if expected.get("size") and expected["size"] != len(out):
        die("output size %d != expected %d (nothing written)"
            % (len(out), expected["size"]))

    with open(args.output, "wb") as fh:
        fh.write(out)

    print("payload base      : 0x%X" % base)
    print("mImgSize          : 0x%X -> 0x%X" % (m_img_size, new_mimg))
    print("patch_data offset : 0x%X  (%d/%d bytes)"
          % (pd_offset, len(blob), PATCH_SPACE))
    print("original payload intact (signature material unchanged): True")
    print("sha256            : %s" % digest)
    print("output            : %s  (%d bytes)" % (args.output, len(out)))


if __name__ == "__main__":
    main()
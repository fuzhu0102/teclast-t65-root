#!/usr/bin/env python3
"""
xref_scan.py — find AArch64 PC-relative references to an address or string.

Cheap alternative to spinning up Ghidra when you only need "who references this
string / this global?".

Why ADRP + <op>
---------------
AArch64 cannot materialise a 64-bit constant in one instruction, so the compiler
emits a pair:

    adrp xN, <page>              ; X = (PC & ~0xFFF) + (page_delta << 12)
    add  xN, xN, #<offset>       ; X += offset within the page
    ldr  wN, [xN, #<offset>]     ; 32-bit access to a global  <-- 32-bit form
    ldr  xN, [xN, #<offset>]     ; 64-bit access to a global

so a reference to target T is an ADRP whose target page == (T & ~0xFFF) followed
within a few instruction slots by an ADD/LDR/STR on the same register with an
offset of (T & 0xFFF).  Measured on this LK, the gap can be as large as four
slots — `FUN_9f0680b0` is `adrp x0, 0x9F13D000` / `stp` / `mov` / `mov` /
`ldr w1, [x0, #0x8C8]` — so the window covers six slots.

Scanned forms (64-bit *and* 32-bit — the 32-bit ones matter):
    ADD  Xd, Xn, #imm      0x91000000
    ADD  Wd, Wn, #imm      0x11000000
    LDR  Xt, [Xn, #imm]    0xF9400000
    LDR  Wt, [Xn, #imm]    0xB9400000
    LDRSW Xt, [Xn, #imm]   0xB9800000
    STR  Xt, [Xn, #imm]    0xF9000000
    STR  Wt, [Xn, #imm]    0xB9000000

Usage
-----
    python xref_scan.py <image.bin> <load_base> <target_addr|--string TEXT>

Examples
--------
    # where is androidboot.vbmeta.device_state referenced?
    python xref_scan.py lk-sign.bin 0x9EFFFE00 --string androidboot.vbmeta.device_state

    # who touches the global at 0x9F13D8C8?  (32-bit LDR W in this build)
    python xref_scan.py lk-sign.bin 0x9EFFFE00 0x9F13D8C8

Notes
-----
* For LK the file starts with a 0x200-byte DHTB header, so
  `runtime = load_base + file_offset` with load_base = 0x9EFFFE00.
* "no references found" is NOT proof that there are none: this scanner only
  recognises the ADRP-relative forms above.  Indirect references (a pointer
  loaded from a table, register-indirect addressing, code built at runtime) are
  invisible to it — confirm with a real disassembler before concluding.
"""

import struct
import sys

# (mask, value, kind, needs_same_dest, operand_shift)
# ADD is  Rd = Rn + imm   -> the destination must be the ADRP register itself.
# LDR/STR are Rt, [Rn, #imm] -> only the *base* register (Rn) is the ADRP one;
#   the data register Rt is unrelated, e.g. `adrp x0, ...; ldr w1, [x0, #0x8C8]`.
FORMS = [
    (0xFF800000, 0x91000000, "add  x", True, 0),
    (0xFF800000, 0x11000000, "add  w", True, 0),
    (0xFFC00000, 0xF9400000, "ldr  x", False, 3),
    (0xFFC00000, 0xB9400000, "ldr  w", False, 2),
    (0xFFC00000, 0xB9800000, "ldrsw", False, 2),
    (0xFFC00000, 0xF9000000, "str  x", False, 3),
    (0xFFC00000, 0xB9000000, "str  w", False, 2),
]


def find_string(data: bytes, needle: str) -> int:
    idx = data.find(needle.encode())
    if idx < 0:
        sys.exit(f"string not found in image: {needle!r}")
    return idx


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)

    path, base_s, target_s = sys.argv[1], sys.argv[2], sys.argv[3]
    data = open(path, "rb").read()
    base = int(base_s, 0)

    if target_s == "--string":
        if len(sys.argv) < 5:
            sys.exit("--string needs a value")
        file_off = find_string(data, sys.argv[4])
        target = base + file_off
        print(f"string found at file 0x{file_off:X} -> vaddr 0x{target:X}")
    else:
        target = int(target_s, 0)

    page = target & ~0xFFF
    off = target & 0xFFF
    print(f"target vaddr 0x{target:X}  page 0x{page:X}  offset 0x{off:X}")
    print(f"scanning {len(data)} bytes, load base 0x{base:X}, "
          f"forms: add x/w, ldr x/w, ldrsw, str x/w\n")

    hits = []
    for i in range(0, len(data) - 16, 4):
        w = struct.unpack_from("<I", data, i)[0]

        # ADRP: bits 28..24 == 10000
        if (w & 0x9F000000) != 0x90000000:
            continue
        rd = w & 0x1F
        immlo = (w >> 29) & 0x3
        immhi = (w >> 5) & 0x7FFFF
        imm = (immhi << 2) | immlo
        if imm & (1 << 20):
            imm -= 1 << 21

        pc = base + i
        if (pc & ~0xFFF) + (imm << 12) != page:
            continue

        for k in (4, 8, 12, 16, 20, 24):
            if i + k + 4 > len(data):
                break
            w2 = struct.unpack_from("<I", data, i + k)[0]
            rn = (w2 >> 5) & 0x1F
            rdd = w2 & 0x1F
            if rn != rd:                     # base/add register must be the ADRP one
                continue
            for mask, value, kind, same_dest, shift in FORMS:
                if (w2 & mask) != value:
                    continue
                if same_dest and rdd != rd:
                    break
                if (((w2 >> 10) & 0xFFF) << shift) == off:
                    hits.append((i, k, kind))
                break

    if not hits:
        print("no references found")
        print("(this form only — an indirect reference would still be invisible)")
        return

    print(f"{len(hits)} reference(s):")
    for i, k, kind in hits:
        print(f"  file 0x{i:06X}   vaddr 0x{base + i:X}   ({kind} at +{k})")


if __name__ == "__main__":
    main()

# NOTICE — third-party components

This repository contains **only original text and Python scripts**. It does **not**
redistribute any firmware image, any vendor binary, or any upstream project's code.

The MIT licence in [`LICENSE`](LICENSE) covers **this repository's own content only**.
Each component below keeps its own licence and terms; obtain it from its own upstream
and check that licence before redistributing or building on it.

> Licensing note: naming a *toolchain* ("the Android NDK") is not the same as naming the
> licence of the individual executables. GNU `as`/`ld`/`objcopy` are **GNU Binutils**
> under GPL-3.0-or-later, not Apache-2.0. Where in doubt, read the licence file that
> ships inside the package you actually downloaded, and treat each component on its own.

## Referenced / required to reproduce

| Component | Upstream | What it is | Licence / note |
|---|---|---|---|
| `magic64` / `magic32` | [TomKing062/unisoc_chipram_signcheck_exploit](https://github.com/TomKing062/unisoc_chipram_signcheck_exploit) | SPRD chipram sign-check bypass framework; this repo documents its byte layout and wraps it | See upstream repository |
| CVE-2022-38694 unlock flow | [TomKing062/CVE-2022-38694_unlock_bootloader](https://github.com/TomKing062/CVE-2022-38694_unlock_bootloader) | FDL1/FDL2/SPL patching tooling and RAM-exec flow | See upstream repository |
| `bsp_sign_fxxker` | [YC-nw/bsp_sign_fxxker](https://github.com/YC-nw/bsp_sign_fxxker) | Alternative signature wrapper (two payload copies). Documented here only as a rejected route | See upstream repository |
| UMS9230 / QOGIRL6 AVB analysis | [nec093/uis7861-uboot-avb-bypass](https://github.com/nec093/uis7861-uboot-avb-bypass) | Prior art for U-Boot + AVB bypass | See upstream repository |
| Magisk | [topjohnwu/Magisk](https://github.com/topjohnwu/Magisk) | Root solution used here (`v31.1`) | GPL-3.0; the APK is downloaded by the user, not redistributed here |
| `spd_dump` | [TomKing062/spd_dump](https://github.com/TomKing062/spd_dump) | Download-mode (BootROM/FDL) tool used to read and write partitions | See upstream repository |
| `aarch64-linux-android-{as,ld,objcopy}` | GNU Binutils (shipped with the Android NDK) | Assembler / linker / objcopy that `magic64` shells out to | **GPL-3.0-or-later**. Checked on the local binaries: version string `binutils-2.27-bf0c2e88` plus the notice *"GNU General Public License version 3 or (at your option) a later version"* |

## Not included at all

- **Firmware / PAC / partition images** (`Firmware.pac`, `lk-sign.bin`, `init_boot.img`, …):
  proprietary Teclast / UNISOC material. Supply your own, extracted from your own device
  or the vendor's upgrade package. `.gitignore` excludes `*.bin`, `*.img`, `*.pac`.
- **Device dumps and personal data** from the test unit (partition backups, logs, photos).

## Trademarks

Teclast, UNISOC (Spreadtrum), Android, Magisk and other names are trademarks of their
respective owners. They are used here descriptively only.

## Research context

This material documents reversing and modifying a device the author physically owns,
for the purpose of installing software on it. It is published for research and
educational use. GitHub permits vulnerability research material; see
[GitHub's acceptable use policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-active-malware-or-exploits).

# Teclast T65 (UMS9230S / Android 16) — Bootloader Unlock & Root

> **Verified on one device** (hardware ID `A7D5`, `T65_EEA`, board `ums9230_6h10`, Android 16 incremental `20260413`):
> by patching **LK at runtime**, the boot chain can be made to report unlocked and stop enforcing AVB, after which a Magisk-patched `init_boot` boots and gives root.
> This is **not** the vendor's permanent unlock: no vendor-recognised lock flag is written.

**Result on the test device**

```
ro.boot.verifiedbootstate    = orange
ro.boot.vbmeta.device_state  = unlocked
sys.boot_completed           = 1
$ adb shell su -c id
uid=0(root) gid=0(root) context=u:r:magisk:s0
```

---

## 摘要（中文）

台电 T65（A7D5），展锐 UMS9230S，Android 16。标准 fastboot 解锁在本机**完全不可用**（`get_unlock_ability` = Not implement，`flashing unlock` / `oem device-info` = unknown command，`oem unlock` = `Unlock bootloader fail.`），ROM 没有实现标准 OEM 解锁接口。

往 `splloader` 写改过的 SPL 也不行：本机做过对照实验 —— 只把一段**日志字符串里的 1 个字符**改掉，设备就起不来；写回原厂 SPL 立刻正常。这说明被修改的 SPL 无法通过启动前完整性检查。

成功的关键是两步：

1. **用 `magic64` 包装原厂 LK** —— payload 一个字节都没改，只在它前面插跳转、后面追加框架，镜像只长 0x100 字节；设备照常通过校验，而在运行时把补丁打进去。
2. **补丁必须写全局变量，不能只改返回值** —— `get_lock_status()` 除了返回锁状态，还维护全局变量 `_DAT_9f13d8c8`（`0x9F13D8C8`）；而 AVB 回调 `FUN_9f0680b0` **直接读这个全局变量**，根本不调 `get_lock_status()`。只改返回值 = 假解锁（`green` 不变）；连全局变量一起写 = 真解锁（`orange`）。

最后用 Magisk 修补 `init_boot_a`（AVB 已不拦），重启即得 root。

完整操作顺序：**[`docs/04-end-to-end-recipe.md`](docs/04-end-to-end-recipe.md)**。

---

## 这份方法到底是什么（以及不是什么）

| 说法 | 准确表述 |
|---|---|
| 「解锁了 bootloader」 | **通过修改 LK，在启动期间强制解锁状态并允许修改镜像启动**。没有写入厂商认可的永久解锁标志 |
| 「永久解锁」 | 不是。原厂 LK 回刷、整包固件回刷、OTA 替换 `uboot`/`trustos`/`vbmeta` 都可能让方法失效 |
| 「设备永远能救」 | 不是。本机几次故障都是靠下载模式恢复的，但分区表写坏、eMMC 故障等不在保证范围内 |
| 「数据和原来一样」 | 不是。第一次刷入补丁 LK 后 boot 状态变化，Android 会要求恢复出厂，数据会被抹掉 |

## Why the standard method fails

The upstream project [CVE-2022-38694_unlock_bootloader](https://github.com/TomKing062/CVE-2022-38694_unlock_bootloader) can flash a re-signed SPL (it also offers RAM-exec variants). On this device the flash route is impossible:

| Test | Result |
|---|---|
| Write stock SPL | boots ✅ |
| Write SPL with **one ASCII byte changed** (inside a log string, no logic impact) | **does not boot** ❌ |

→ **A modified SPL does not pass the pre-boot integrity check.** Both the v1 (5×NOP) and v2 (branch-rewrite) patches failed for this reason — not because the patch logic was wrong. (The check is not proven to be RSA signature verification specifically; what is proven is "modified → won't boot".)

## The two-piece solution

### 1. `magic64` — patch at runtime, signature material untouched

From [unisoc_chipram_signcheck_exploit](https://github.com/TomKing062/unisoc_chipram_signcheck_exploit). Byte-verified layout for this device:

```text
0x000  DHTB header      only mImgSize changes: 0x133E10 -> 0x133F10 (+0x100)
0x200  jump code        16 B: b <shellcode>
0x210  payload          the ORIGINAL payload, byte-for-byte identical, shifted +0x10
0x...  magic area       0x70 shellcode + 0x80 patch_data
0x...  footer           0x60: payload_offset = 0x210, cert_offset += 0x100
0x...  cert / priv      unchanged, shifted +0x100
```

Stock LK 1,262,792 B → patched 1,263,048 B. **There is exactly one payload copy, and it is unmodified** — the +0x100 is all framework.

> ⚠️ A different technique (`bsp_sign_fxxker`) *does* store a second copy and roughly doubles the image; that is not what this repository uses.

At runtime: jump → shellcode copies the payload from `0x210` back to `0x200` → applies `patch_data` → branches back into the restored payload.

⚠️ `magic64` needs the payload base to be **page-aligned**. `uboot` = `0x9F000000` ✅. `trustos` is explicitly unsupported upstream (`not trustos`), and we did not use it.

### 2. The global-variable patch — the actual breakthrough

Decompiled from stock `lk-sign.bin` (image base `0x9EFFFE00`, file offset = vaddr − 0x9EFFFE00):

```c
// FUN_9f064540 = get_lock_status()
undefined4 FUN_9f064540(void) {
    _DAT_9f13d8c8 = 0;                 // 0x9F064558  clear global
    ...
    if (verify_ok) _DAT_9f13d8c8 = 1;  // 0x9F0645D4  set global
    return _DAT_9f13d8c8;
}

// FUN_9f0680b0 = AVB "is device unlocked" callback, registered at avb_ops +0x48
undefined8 FUN_9f0680b0(long ops, char *out) {
    if (_DAT_9f13d8c8 == 1) { *out = 1; return 0; }   // reads the GLOBAL
    *out = 0; return 0;
}
```

Both ends are checkable on the stock image with `tools/xref_scan.py`:

```
file 0x064750   vaddr 0x9F064550   (str  w at +8)     get_lock_status writes the global
file 0x0682B0   vaddr 0x9F0680B0   (ldr  w at +16)    AVB callback reads the global
```

Earlier attempts replaced the function entry with `mov w0, #1; ret`. That only fakes the **return value**; the AVB callback still reads the global, which stays `0` → state splits:

| Consumer | Sees |
|---|---|
| Callers of `get_lock_status()` | unlocked |
| **AVB callback** | **locked** |

Hence `verifiedbootstate` stayed `green`.

**The working patch:**

```asm
mov  w0, #1
adrp x9, 0x9F13D000
str  w0, [x9, #0x8C8]    ; ← write the global _DAT_9f13d8c8
ret
```

Encoded: `52800020 B00006C9 B908C920 D65F03C0`, applied at runtime address `0x9F064540` through `magic64`'s `patch_data`. Ready-made: [`patches/get_lock_status.json`](patches/get_lock_status.json) + [`tools/magic64_patch.py`](tools/magic64_patch.py).

## Reproduce

Run from the repository root:

```powershell
python tools\magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json --check
python tools\magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json `
    -o work\lk-global1.bin --magic64 <dir>\magic64.exe
```

Expected (verified on the test device): 1,263,048 B, `mImgSize 0x133F10`,
sha256 `69145c92e72c9703b1fecce0631b9c4c43eeb9ca3f88f7b1bb169ce10b52ccdc`.

The script refuses to build when the image hash or the original instructions at the patch address don't match the patch file — that is deliberate: **do not point it at another firmware with `--force`** unless you have re-verified the offsets.

The full ordered procedure (素材 → 生成 → 备份 → 写入 → 验证 → Magisk → 恢复) is in
**[`docs/04-end-to-end-recipe.md`](docs/04-end-to-end-recipe.md)**.

## Result

```
ro.boot.verifiedbootstate   = orange
ro.boot.vbmeta.device_state = unlocked
sys.boot_completed          = 1
```

On first boot after flashing, Android asks to factory-reset. We **observed** this on the
test device; a plausible explanation is that FBE keys are bound to the verified-boot state.
The prompt by itself is not proof of success — the three properties above are.

## Root

1. Extract `Magisk-v31.1.apk`, keep the arm64 binaries, **copy `libmagisk.so` to a file named `magisk`** (the official `boot_patch.sh` looks for that exact name; without it the `xz`/`lz4` step fails). Use `cp`, not `mv` — the same binary is also needed as `magisk64`.
2. Push the binaries + stock `init_boot.img` to the device, run the official `boot_patch.sh` **on the device** (it needs Android's shell environment, not a PC).
3. Write the resulting 8 MiB image to `init_boot_<slot>` **through download mode** (`spd_dump`) — fastboot writes were unstable on this unit. Check `ro.boot.slot_suffix` first; this device is `_a`.
4. Reboot, install the Magisk app, finish "additional setup".
5. Grant superuser to shell in the Magisk app. `adb shell su` is denied until you do; the exact policy depends on the Magisk release notes for your version, so don't attribute it to a single build property.

Verified: `adb shell su -c id` → `uid=0(root) gid=0(root) context=u:r:magisk:s0`.

## Repository layout

```
README.md                             this file
LICENSE                               MIT — applies to this repository's own text and scripts only
NOTICE.md                             third-party components and their own licences
docs/01-recon-and-blocked-paths.md    device facts, address table, every route that failed
docs/02-the-breakthrough.md           the global-variable bug, in full
docs/03-root-recovery-toolchain.md    Magisk steps, recovery (two scenarios), Ghidra/magic64/spd_dump
docs/04-end-to-end-recipe.md          the full ordered procedure with hashes and pass criteria
patches/get_lock_status.json          the patch, with image hash + original-instruction guards
tools/magic64_patch.py                build a patched image with magic64 + patch_data
tools/xref_scan.py                    find ADRP-relative references (ADD/LDR/STR, 32- and 64-bit)
```

## Limitations

- Validated on **one** device, one firmware (`20260413`). Another T65 — even another A7D5 — needs its own `lk-sign.bin` re-checked: the function may move, or not exist.
- Restoring the stock LK / flashing a full stock image / an OTA that replaces `uboot`, `trustos` or `vbmeta` will likely revert the effect.
- The method changes what LK **reports and enforces**; it does not make the device "permanently unlocked" in the vendor sense.
- First boot after flashing wipes user data. That data cannot be recovered from firmware.
- Firmware images are **not** included in this repository (copyright); supply your own from your own device or the vendor's package.

## Safety

- BootROM lives in mask ROM, and the failures we hit were recovered through download mode (5+ times) — but **recovery is not guaranteed** for every possible failure (corrupted partition table, eMMC issues, hardware faults are out of scope).
- Back up `uboot_a/b`, `splloader`, `init_boot_a/b` and verify their hashes **before** writing anything.
- Restore order matters: when going back to stock from a rooted state, restore `init_boot` **before** `uboot` (see `docs/03`).
- Do not blind-flash another T65 without re-verifying the offsets against its own `lk-sign.bin`.

## License

MIT for the scripts and documentation **in this repository**. Third-party tools, upstream code and firmware are **not** covered by this licence and are not redistributed here — see [`NOTICE.md`](NOTICE.md). Public visibility of a project does not by itself grant redistribution rights.

## Credits

- [TomKing062](https://github.com/TomKing062) — CVE-2022-38694 and `unisoc_chipram_signcheck_exploit` (`magic32`/`magic64`)
- [YC-nw](https://github.com/YC-nw/bsp_sign_fxxker) — Pre-Verification signature bypass
- [nec093](https://github.com/nec093/uis7861-uboot-avb-bypass) — UMS9230/QOGIRL6 U-Boot + AVB analysis
- [topjohnwu](https://github.com/topjohnwu/Magisk) — Magisk
- [VR-25](https://github.com/VR-25/acc) / [MatteCarra](https://github.com/MatteCarra/AccA) — ACC / AccA (charge limiting)

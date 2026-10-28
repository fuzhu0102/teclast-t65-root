# Teclast T65（UMS9230S / Android 16）：运行时 LK 解锁与 Magisk Root

> **Summary.** The Teclast T65 (hardware ID `A7D5`, SoC UMS9230S, Android 16) has no usable OEM unlock, and a modified SPL is rejected at boot. This repository documents a route that works anyway: wrap the stock LK with `magic64` (payload byte-for-byte unchanged, signature material intact), patch `get_lock_status()` at runtime so it also writes the global variable the AVB callback reads, then boot a Magisk-patched `init_boot`. On the test unit this gives `verifiedbootstate=orange`, `vbmeta.device_state=unlocked` and `su -c id` → `uid=0`.

## 1. 目标与结果

目标：在台电 T65 上获得 root。

路线一句话：**不碰 SPL；用 `magic64` 包装原厂 LK，让它在运行时给自己打补丁，使 AVB 汇报"已解锁"；再把 Magisk 修补过的 `init_boot` 经下载模式写进去。**

测试设备上的结果：

```
ro.boot.verifiedbootstate    = orange
ro.boot.vbmeta.device_state  = unlocked
sys.boot_completed           = 1
$ adb shell su -c id
uid=0(root) gid=0(root) context=u:r:magisk:s0
```

## 2. 设备与固件基线

| 项 | 值 |
|---|---|
| 型号 | 台电 T65，硬件 ID `A7D5`，ADB 标识 `T65_EEA` |
| SoC / 板级 | UMS9230S / `ums9230_6h10` |
| 系统 | Android 16，增量 `20260413`，安全补丁 2026-03-05 |
| 分区方案 | 虚拟 A/B，当前槽位 `_a`，eMMC |
| 初始状态 | `verifiedbootstate=green`，`vbmeta.device_state=locked` |
| 启动链 | BootROM → SPL/FDL1 → TOS → UBOOT(LK) → Kernel |

完整的设备信息、分区尺寸和 LK 里的函数地址表见 [docs/01](docs/01-device-and-boot-chain.md)。

## 3. 路线总览

```
原厂 SPL（不改）
   └─ 补丁 LK（magic64 包装，写入 uboot_a / uboot_b）
        └─ 运行时：get_lock_status() 同时写返回值和全局变量
             └─ AVB 汇报 unlocked / orange，不再拦截 init_boot
                  └─ Magisk 修补的 init_boot（下载模式写入当前槽位）
                       └─ root
```

三个关键点：

1. **SPL 保持原厂。** 往 `splloader` 写任何被修改过的镜像，设备都不能启动，所以改动只放在 LK 一层。
2. **LK 用 `magic64` 包装，签名材料不动。** payload 逐字节不变，只在它前后追加跳转和补丁框架，镜像增长 0x100 字节，原厂 SPL 的校验照常通过，补丁在运行时才生效。
3. **补丁要写全局变量，不能只改返回值。** AVB 的"是否解锁"回调直接读全局变量 `0x9F13D8C8`，不经过 `get_lock_status()`。

## 4. 原理

### 4.1 为什么只能动 LK

这台设备上标准解锁入口都不存在，落盘改 SPL 也行不通：

| 入口 | 结果 |
|---|---|
| `fastboot getvar unlock_ability` | `Not implement` |
| `fastboot flashing unlock` / `oem device-info` | `unknown command` |
| `fastboot oem unlock` | `Unlock bootloader fail.`（LK 要求厂商签发的产品序列号令牌） |
| 往 `splloader` 写改过的 SPL | 设备起不来 |

最后一条做过对照实验：只把 SPL 里一条日志字符串的 1 个字符改掉，设备就无法启动，写回原厂 SPL 立刻正常。结论是被修改的 SPL 通不过启动前的完整性检查。这个实验证明的是"改了就不启动"，不能单独证明校验算法就是 RSA 验签，但足以排除"落盘改 SPL"。

### 4.2 `magic64`：补丁在运行时才生效

`magic64` 来自 [unisoc_chipram_signcheck_exploit](https://github.com/TomKing062/unisoc_chipram_signcheck_exploit)。下面是在本设备上逐字节核对过的布局（原厂 LK 1,262,792 B → 产物 1,263,048 B）：

```text
0x000  DHTB 头       只有 mImgSize 变：0x133E10 → 0x133F10
0x200  跳转码        16 B：b <shellcode>
0x210  payload       原厂 payload，逐字节相同，整体后移 0x10
...    shellcode     0x70 B
...    patch_data    0x80 B（实际用 28 B）
...    footer        0x60 B：payload_offset = 0x210，cert_offset += 0x100
...    cert / priv   原样，整体后移 0x100
```

运行时的流程：跳转 → shellcode 把 payload 从 `0x210` 搬回 `0x200` → 按 `patch_data` 逐条写入补丁并刷新指令缓存 → 跳回 payload 入口 `0x9F000000`。签名覆盖的是被指向的那段 payload 字节，它们没变，只有指向它们的偏移跟着挪了，所以没有重算任何哈希，也不需要私钥。

适用条件：shellcode 用 `ADRP` 寻址，要求 payload 基址页对齐。`uboot` 的 `0x9F000000` 满足；`splloader` 的 `0x65000A00` 不满足；`trustos` 上游标注为不支持，本项目没有使用。

### 4.3 补丁点：`get_lock_status()` 与它维护的全局变量

在原厂 `lk-sign.bin`（镜像基址 `0x9EFFFE00`，文件偏移 = 运行时地址 − `0x9EFFFE00`）里：

```c
// FUN_9f064540 = get_lock_status()
undefined4 FUN_9f064540(void) {
    _DAT_9f13d8c8 = 0;                 // 0x9F064558  先清全局
    ...
    if (verify_ok) _DAT_9f13d8c8 = 1;  // 0x9F0645D4  校验通过才置位
    return _DAT_9f13d8c8;
}

// FUN_9f0680b0 = AVB "is device unlocked" 回调，注册在 avb_ops +0x48
undefined8 FUN_9f0680b0(long ops, char *out) {
    if (_DAT_9f13d8c8 == 1) { *out = 1; return 0; }   // 直接读全局
    *out = 0; return 0;
}
```

所以补丁要同时覆盖两个消费方。只把入口换成 `mov w0,#1; ret` 的话，调用 `get_lock_status()` 的路径看到已解锁，AVB 回调读到的全局变量仍是 0，`verifiedbootstate` 停在 `green`（这个现象在设备上实测过）。实际使用的补丁：

```asm
mov  w0, #1
adrp x9, 0x9F13D000
str  w0, [x9, #0x8C8]    ; 写 _DAT_9f13d8c8
ret
```

编码 `52800020 B00006C9 B908C920 D65F03C0`，应用于运行时地址 `0x9F064540`，由 `patches/get_lock_status.json` 描述。写端和读端的引用都能用 `tools/xref_scan.py` 在原厂镜像上复核。完整推导见 [docs/02](docs/02-lock-state-patch.md)。

### 4.4 AVB 放行之后

AVB 回调返回已解锁后，`init_boot` 不再被强制校验。Magisk 的 `boot_patch.sh` 需要 Android 的 shell 环境，所以在平板上执行，产物 8 MiB 正好一个分区，经下载模式（`spd_dump`）写入 `init_boot_<当前槽位>`。重启后装 Magisk App 完成额外安装，并在 App 里给 `shell` 授予超级用户权限。

"AVB 不再拦截"的证据分两层：三个属性只证明锁状态翻转了；修补过的 `init_boot` 真的启动并拿到 `su`，才证明它不再拦改过的启动镜像。

## 5. 操作流程

完整命令、预期输出和哈希见 [docs/04](docs/04-end-to-end-recipe.md)，各环节细节见 [docs/03](docs/03-root-and-toolchain.md)。

| # | 步骤 | 通过判据 |
|---|---|---|
| 1 | 备齐原厂镜像（`lk-sign.bin`、`u-boot-spl-16k-emmc-sign.bin`、`init_boot.img`）和工具 | 长度与 SHA-256 同 docs/04 的清单 |
| 2 | 确认设备状态、槽位 | `green`，板级 `ums9230_6h10` |
| 3 | 生成补丁 LK | 退出码 0，payload intact，1,263,048 B，sha256 `69145c92…` |
| 4 | 进下载模式，备份 `uboot_a/b`、`splloader*`、`init_boot_a/b` | 回读哈希等于原厂值 |
| 5 | 写入 `uboot_a` 和 `uboot_b` | 两个槽位回读 sha256 = `69145c92…` |
| 6 | 开机（会要求恢复出厂）并验证 | `orange` / `unlocked` / `boot_completed=1` |
| 7 | 在平板上修补 `init_boot`，经下载模式写入当前槽位 | 回读哈希 = 本次产物哈希 |
| 8 | 装 Magisk App，给 shell 授权 | `su -c id` → `uid=0` |

生成补丁 LK 的命令（在仓库根目录执行）：

```powershell
python tools\magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json --check
python tools\magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json `
    -o work\lk-global1.bin --magic64 <magic64目录>\magic64.exe
```

脚本在镜像哈希或原始指令与补丁文件不符时拒绝生成，所有检查通过后才写盘。换到别的固件时请先自己核对地址，不要用 `--force` 绕过。

## 6. 方案取舍

评估过的路线和结论（证据见 [docs/01](docs/01-device-and-boot-chain.md#方案评估)）：

| 路线 | 结论 | 依据 |
|---|---|---|
| 标准 fastboot 解锁 | 不可用 | ROM 未实现解锁接口 |
| 落盘刷入改签 SPL | 不可用 | 改 1 个字符即不能启动 |
| 伪造 `miscdata` 锁标志 | 不可行 | 标志前 0x40 字节由 TEE 校验（反编译结论） |
| `bsp_sign_fxxker` 改 trustos | 不可用 | 产物 7,120,536 B，大于 6 MiB 分区 |
| `magic64` 改 trustos | 未采用 | 上游标注不支持，本项目未验证 |
| 只改 `vbmeta.device_state` 字符串 | 无效 | 只改了显示，启动状态不变 |
| 只改 `get_lock_status()` 返回值 | 无效 | AVB 回调读的是全局变量 |
| **`magic64` 包装 LK + 写全局变量** | **采用** | 实测 `orange` / `unlocked`，Magisk 启动并取得 root |

## 7. 适用范围与证据强度

- **验证范围**：一台设备、一个固件版本（硬件 ID `A7D5`，增量 `20260413`）。其他 T65，甚至其他 A7D5，需要用自己的 `lk-sign.bin` 重新核对：函数可能换了位置，也可能不存在。
- **解锁方式**：这是运行时解锁。补丁 LK 在每次启动时于内存里改写锁状态，不写入厂商认可的永久解锁标志。原厂 LK 回刷、整包固件回刷，以及替换 `uboot`、`trustos`、`vbmeta` 的 OTA，都会让这套效果失效。
- **固件**：仓库不含任何固件或分区镜像，请用自己设备读出的分区，或厂商升级包里的文件。
- **证据强度**：
  - 实测：SPL 单字符对照实验；只改返回值的结果；补丁 LK 的启动状态和 root；`magic64` 产物布局。
  - 反编译推断：TEE 对锁标志的校验、各函数的调用关系。
  - 尚未单独验证：首次开机要求恢复出厂的原因（可能是 FBE 密钥与 verified-boot 状态绑定）；SPL 校验算法的具体类型。

## 8. 风险点

| 风险 | 触发条件 | 后果 | 预防与恢复 |
|---|---|---|---|
| 用户数据被抹除 | 首次启动补丁 LK，boot 状态由 `green` 变 `orange` | Android 要求恢复出厂，数据不可恢复 | 操作前自行备份 |
| 补丁用在不同固件 | 把补丁用于别的固件或设备 | 地址错位，改到别的函数，可能起不来 | 脚本校验镜像哈希与原始指令；不要用 `--force` |
| 补丁 LK 启动失败 | LK 写入有误或补丁不对 | 无法开机或落回下载模式 | 先备份 `uboot_a/b`；按 docs/03 情况 A 在下载模式写回原厂 LK |
| 只写了一个 LK 槽位 | 只写 `uboot_a` 或 `uboot_b` | 下次切槽后"有时生效有时不生效" | `uboot_a` 和 `uboot_b` 都写；`init_boot` 写当前槽位 |
| 写入 SPL | 往 `splloader` 写任何被改动的镜像 | 无法启动 | 本路线全程不写 SPL；误写按 docs/03 恢复 |
| 恢复顺序颠倒 | 已刷 Magisk 时先恢复 LK | 原厂 LK 遇到被改的 `init_boot`，多半起不来 | 先恢复 `init_boot_a/b`，再恢复 `uboot_a/b` |
| 用 fastboot 写大镜像 | fastboot 写 8 MiB 的 `init_boot` | 写入阶段 `usb_read failed` 中断，有写坏分区的风险 | 一律用下载模式（`spd_dump`） |
| 混用通用包镜像 | 用通用解锁包里的 SPL/LK/FDL 替代本机镜像 | 其 `fdl1-dl.bin` 与本机原厂只有 12.84% 字节相同 | 只取其中的 ROP 载荷，镜像一律用本机的 |
| 下载模式通信不稳 | `spd_dump` 报 `find port failed` 等 | 写入中断，设备停在半连接状态 | 加 `--reconnect` 并重试；拔线后长按电源键 20 秒重进下载模式 |
| 以"能开机"判断恢复成功 | 恢复后只看是否开机 | `uboot` 可能仍是改过的 LK | 逐分区回读并比对哈希，比对范围是写入的有效字节 |
| 把恢复出厂提示当作成功 | 看到恢复出厂提示就认为补丁生效 | 启动失败等原因也可能触发类似提示 | 只认三个属性：`orange`、`unlocked`、`boot_completed=1` |
| 启动镜像校验失效 | 补丁生效后 | AVB 不再拦截被修改的启动镜像，设备失去 verified boot 的保护 | 使用前自行评估 |
| 恢复通道的边界 | 分区表损坏、eMMC 故障、硬件故障 | 下载模式也可能救不回 | 测试设备上的故障均经下载模式恢复，累计 5 次以上；上述几类情形不在验证范围内 |

## 9. 仓库结构

```
README.md                               本文：技术路线与风险
docs/01-device-and-boot-chain.md        设备基线、启动链、地址表、方案评估与证据
docs/02-lock-state-patch.md             全局变量问题与 magic64 布局的完整推导
docs/03-root-and-toolchain.md           Magisk、写分区、恢复、Ghidra / magic64 / xref 工具用法
docs/04-end-to-end-recipe.md            完整操作顺序，含哈希与通过判据
patches/get_lock_status.json            补丁，带镜像哈希与原始指令校验
tools/magic64_patch.py                  用 magic64 + patch_data 生成补丁镜像
tools/xref_scan.py                      查找 ADRP 相对引用（ADD/LDR/STR，32 位与 64 位）
```

## 10. 许可与致谢

仓库中的脚本与文档使用 MIT 协议。第三方工具、上游代码和固件不在其范围内，也不在此分发，详见 [NOTICE.md](NOTICE.md)。

- [TomKing062](https://github.com/TomKing062)：CVE-2022-38694 与 `unisoc_chipram_signcheck_exploit`（`magic32` / `magic64`）
- [YC-nw](https://github.com/YC-nw/bsp_sign_fxxker)：Pre-Verification 签名绕过
- [nec093](https://github.com/nec093/uis7861-uboot-avb-bypass)：UMS9230 / QOGIRL6 的 U-Boot + AVB 分析
- [topjohnwu](https://github.com/topjohnwu/Magisk)：Magisk

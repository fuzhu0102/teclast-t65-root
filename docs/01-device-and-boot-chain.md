# 01 — 设备基线、启动链与方案评估

> 本文的事实来自设备实测或对原厂镜像的反编译，每条都标明是哪一种。没有验证过的内容直接写"未验证"。

## 设备基线

| 项 | 值 |
|---|---|
| 型号 | 台电 T65（外壳硬件 ID `A7D5`，ADB 产品标识 `T65_EEA`，设备代号 `T65`） |
| SoC（实读） | `ro.soc.model = UMS9230S` |
| 板级 | `ro.product.board = ums9230_6h10` |
| 系统 | Android 16，增量 `20260413`，SDK 36，安全补丁 2026-03-05，指纹 `Teclast/T65_EEA/T65:16/BP2A.250605.031.A3/20260413:user/release-keys` |
| 分区方案 | 虚拟 A/B，槽位 `_a` |
| 存储 | eMMC，GPT 74 项（`mmcblk0p1..74`） |
| 初始状态 | `verifiedbootstate=green`、`vbmeta.device_state=locked`、SELinux Enforcing、`ro.debuggable=0` |

厂商页面标注 T7280，与硅片实读的 UMS9230S 是型号映射关系，两者不冲突。

### 分区尺寸（关键几项）

| 分区 | 大小 | 说明 |
|---|---|---|
| `splloader` / `splloader_bak` | 256 KiB | 对应 `mmcblk0boot0` / `boot1`，SPL 镜像 66,552 B |
| `uboot_a` / `uboot_b` | 3 MiB | LK 镜像 1,262,792 B |
| `trustos_a` / `trustos_b` | **6 MiB** | TOS 镜像 3,561,112 B |
| `init_boot_a` / `init_boot_b` | 8 MiB | Magisk 修补目标 |

## 启动链

```
BOOTROM → SPL/FDL1 → TOS → UBOOT(LK) → KERNEL
```

SPL 在载入 LK、sml、trustos 后，分别调用 `FUN_6500bd3c` 验签（基址 `0x65000800`）；返回值非 0 则走 `FUN_650018bc` 报错路径。LK 再通过 AVB 校验后续镜像，AVB 看到的锁状态由 LK 里的一个回调给出，这就是本项目的补丁点。

## 地址换算

本仓库全部文档里的偏移都按这条换算：

```
文件偏移 = 运行时地址 − 0x9EFFFE00
```

`0x9EFFFE00` 是 DHTB 镜像基址；LK payload 的第一条指令在 `0x9F000000`，对应文件偏移 `0x200`。如果用 `0x9F000000` 去减，每个结果都会少 `0x200`。

## LK 里与锁状态相关的函数

| 运行时地址 | 文件偏移 | 符号 |
|---|---|---|
| `0x9F064540` | `0x64740` | `get_lock_status()` |
| `0x9F06435C` | `0x6455C` | `set_lock_status(locked, token)` |
| `0x9F064238` | `0x64438` | `write_lock_flag` → `miscdata`@0x2000，0x1dc 字节 |
| `0x9F0620C0` | `0x622C0` | `read_lock_flag` |
| `0x9F0680B0` | `0x682B0` | **AVB 锁状态回调**（注册到 avb_ops `+0x48`） |
| `0x9F069374` | `0x69574` | 写 `androidboot.vbmeta.device_state` 的函数 |

符号名是反编译时起的名字。可以用 `tools/xref_scan.py` 在原厂镜像上复核全局变量 `0x9F13D8C8` 的引用：

```text
$ python tools/xref_scan.py stock/lk-sign.bin 0x9EFFFE00 0x9F13D8C8
  file 0x064750   vaddr 0x9F064550   (str  w at +8)    <- get_lock_status 写全局
  file 0x0682B0   vaddr 0x9F0680B0   (ldr  w at +16)   <- AVB 回调读全局
  ...（共 9 处，见 docs/02）
```

## 方案评估

评估过七条路线。每条标明证据类型：**实测**（在设备上做过）、**反编译**（读原厂镜像得出）、**上游声明**（来自上游项目文档）。

### 1. 标准 fastboot 解锁：不可用（实测）

```
fastboot getvar unlock_ability     → Not implement
fastboot flashing unlock           → unknown command
fastboot oem device-info           → unknown command
fastboot oem unlock                → FAILED (remote: 'Unlock bootloader fail.')
```

LK 里有 `verify_product_sn_signature`（512 字节令牌校验），解锁需要厂商签发的产品序列号令牌。ROM 没有实现标准 OEM 解锁接口。

### 2. 落盘刷入改签 SPL：不可用（实测）

对照实验：向 `splloader` 写入只改 1 个 ASCII 字符的 SPL（把日志字符串 `teecfg magic err!` 里的 `a` 改成 `Z`，不参与任何逻辑），设备无法启动；写回原厂 SPL 后立刻正常启动。两种 SPL 补丁（5×NOP、改分支）都在这一步失败，原因是被修改的 SPL 通不过启动前的完整性检查，与补丁逻辑无关。

这个实验证明的是"改了就不启动"，不能单独证明校验算法是 RSA 验签（也可能是哈希比对或证书链校验），但足以排除落盘路线。

上游 CVE-2022-38694 流程除了落盘，也提供从 RAM 执行改版 SPL/FDL 的用法。本项目用的是后者：通过 ROP 载荷让 BootROM 执行补丁版 FDL1/FDL2，用来读写分区（见 docs/03）。

### 3. 伪造 `miscdata` 锁标志：不可行（反编译）

`miscdata`@0x2000 的标志，前 0x40 字节由 TEE 校验（`FUN_9f0a8cb0`，TEE 命令 `0x32`）。没有厂商密钥就造不出合法标志。这是反编译结论，没有在设备上实测。

### 4. `bsp_sign_fxxker` 改 trustos：不可用（实测尺寸）

该工具把"修改后的 payload"和"原始签名 payload"各存一份，产物约为原镜像的两倍：

| 项 | 大小 |
|---|---|
| `trustos` 分区 | 6,291,456 B |
| 该工具产物 | **7,120,536 B**，装不下 |

它和 `magic64` 是两种不同的包装方式，`magic64` 只增长 0x100 B（见 docs/02）。

### 5. `magic64` 改 trustos：未采用（上游声明）

上游 README 对 `magic32`/`magic64` 的适用分区写的是 `sml, uboot`，并标注 `not trustos`。在本机上，`magic64` 能处理 TOS 镜像的格式（能生成产物，只增长 256 B），但这只说明结构兼容，不说明运行时可用。这条没有被证伪，只是没有采用。

### 6. 只改 `androidboot.vbmeta.device_state` 字符串：无效（实测）

改掉这个字符串只会让 Android 以为已解锁，AVB 的实际校验由 TOS 完成，因此不会放行修改过的镜像。用改末字符的诊断镜像实测，启动状态没有任何变化。

### 7. 只改 `get_lock_status()` 的返回值：无效（实测）

把函数入口替换成 `mov w0,#1; ret`，设备能启动，但 `verifiedbootstate` 仍是 `green`。原因是 AVB 回调读的是全局变量，不调用 `get_lock_status()`，详见 [docs/02](02-lock-state-patch.md)。

### 结论

七条路线里，只有"`magic64` 包装 LK，并写全局变量"同时满足三个条件：不碰 SPL，镜像放得进分区，AVB 回调读到的状态也被改写。

# 01 — 设备侦察与失效路径

> 本文只记录**在本机实测或反编译中确认**的事实，以及被实测排除的路线。推断与"看起来应该"的内容一律标注。

## 设备基线

| 项 | 值 |
|---|---|
| 型号 | 台电 T65（外壳硬件 ID `A7D5`，ADB 产品标识 `T65_EEA`，设备代号 `T65`） |
| SoC（实读） | `ro.soc.model = UMS9230S` |
| 板级 | `ro.product.board = ums9230_6h10` |
| 系统 | Android 16，增量 `20260413`，SDK 36，安全补丁 2026-03-05，指纹 `Teclast/T65_EEA/T65:16/BP2A.250605.031.A3/20260413:user/release-keys` |
| 分区方案 | 虚拟 A/B，槽位 `_a` |
| 存储 | eMMC，GPT 74 项（`mmcblk0p1..74`） |
| 状态（初始） | `verifiedbootstate=green`、`vbmeta.device_state=locked`、SELinux Enforcing、`ro.debuggable=0` |

厂商页面标注 T7280，与硅片实读的 UMS9230S 是型号映射关系，不是冲突。

### 分区尺寸（关键几项）

| 分区 | 大小 | 说明 |
|---|---|---|
| `splloader` / `splloader_bak` | 256 KiB | 对应 `mmcblk0boot0` / `boot1`，SPL 镜像 66,552 B |
| `uboot_a` / `uboot_b` | 3 MiB | LK 镜像 1,262,792 B |
| `trustos_a` / `trustos_b` | **6 MiB** | TOS 镜像 3,561,112 B |
| `init_boot_a` / `init_boot_b` | 8 MiB | Magisk 修补目标 |

## 启动链

正常：

```
BOOTROM → SPL/FDL1 → TOS → UBOOT(LK) → KERNEL
```

SPL 在载入 LK/sml/trustos 后，会分别调用 `FUN_6500bd3c` 验签（基址 `0x65000800`）；返回值非 0 则走 `FUN_650018bc` 报错路径。

### 地址换算（本文全部偏移的基准）

```
文件偏移 = 运行时地址 − 0x9EFFFE00
```

`0x9EFFFE00` 是 **DHTB 镜像基址**；LK payload 的第一条指令在 `0x9F000000`，对应文件 `0x200`。
**容易搞错的地方**：用 `0x9F000000` 去减，每条结果都会少 `0x200`（本文件早期版本正是这么错的，已修正）。

LK 里的锁状态相关函数：

| 运行时地址 | 文件偏移 | 符号 |
|---|---|---|
| `0x9F064540` | `0x64740` | `get_lock_status()` |
| `0x9F06435C` | `0x6455C` | `set_lock_status(locked, token)` |
| `0x9F064238` | `0x64438` | `write_lock_flag` → `miscdata`@0x2000，0x1dc 字节 |
| `0x9F0620C0` | `0x622C0` | `read_lock_flag` |
| `0x9F0680B0` | `0x682B0` | **AVB 锁状态回调**（注册到 avb_ops `+0x48`） |
| `0x9F069374` | `0x69574` | 写 `androidboot.vbmeta.device_state` 的函数 |

可用 `tools/xref_scan.py` 复核，例如全局变量 `0x9F13D8C8` 的全部引用：

```text
$ python tools/xref_scan.py stock/lk-sign.bin 0x9EFFFE00 0x9F13D8C8
  file 0x064750   vaddr 0x9F064550   (str  w at +8)    <- get_lock_status 写全局
  file 0x0682B0   vaddr 0x9F0680B0   (ldr  w at +16)   <- AVB 回调只读全局
  ...（共 9 处，见下篇）
```

## 所有走过并失败的路

### ① 标准 fastboot 解锁 —— ROM 未实现

```
fastboot getvar unlock_ability     → Not implement
fastboot flashing unlock           → unknown command
fastboot oem device-info           → unknown command
fastboot oem unlock                → FAILED (remote: 'Unlock bootloader fail.')
```

LK 里存在 `verify_product_sn_signature`（512 字节令牌校验），即厂商要求产品序列号签名令牌。

### ② 改签 SPL 落盘 —— 未能通过启动前完整性检查

**这是最关键的一次证伪实验**：向 `splloader` 写入一个只改 **1 个 ASCII 字符**的 SPL（把日志字符串 `teecfg magic err!` 里的 `a` 改成 `Z`，纯提示文本，不参与任何逻辑）。

结果：**设备无法启动**。写回原厂 SPL 后立刻正常启动 —— 对照组成立。

> **结论（保守表述）：被修改过的 SPL 无法通过启动前的完整性检查，因此"落盘刷入改签 SPL"在本机不可行。**
> 此前 v1（5×NOP）与 v2（改分支）两版补丁失败的根因就在这一步，与补丁逻辑无关。

需要说明的是，这个实验**不能单独证明**校验算法是 RSA 签名验证（也可能是哈希比对或证书链校验）。它证明的是"改了就不让启动"这一事实，足以排除落盘路线。

另外需要更正一处常见误解：上游 CVE-2022-38694 流程**并不只有"刷改签 SPL 落盘"一种用法**，它同样提供从 RAM 执行改版 SPL/FDL 的路径（本机也确实用 ROP 载荷跑通了 RAM exec，见 `03`）。所以"上游靠刷 SPL 落盘"这句简化说法是不准确的。

### ③ 伪造锁标志 —— TEE 保护

反编译显示：`miscdata`@0x2000 的标志前 0x40 字节由 TEE 校验（`FUN_9f0a8cb0`，TEE 命令 `0x32`）。没有厂商密钥就造不出合法标志 —— 这是**反编译结论**，不是实测。

### ④ `bsp_sign_fxxker` 改 trustos —— 分区装不下

该工具的原理是把"修改后的 payload"和"原始签名 payload"各存一份，产物约为原镜像两倍：

| 项 | 大小 |
|---|---|
| `trustos` 分区 | 6,291,456 B |
| 该工具产物 | **7,120,536 B** ❌ |

结构性装不下，此路不可用。（注意：这与 `magic64` 不是一回事，`magic64` 只增长 0x100 字节，见 `02`。）

### ⑤ `magic64` 改 trustos —— 上游明确不支持

上游 README 对 `magic32`/`magic64` 的适用分区写的是 `sml, uboot`，并显式标注 **`not trustos`**。

本机实测：格式上 magic64 确实能处理 TOS（能吐出产物，只长 256 B），但那只是"结构兼容"，不等于运行时可用 —— 作者在该平台上试过并留下了结论。**这条未被我们证伪，只是没有采用。**

### ⑥ 只改 `androidboot.vbmeta.device_state` 字符串 —— 表面功夫

把该字符串改掉只是让 Android"以为"解锁，AVB 的实际校验由 TOS 完成，改字符串换不来 root。本机实测（改末字符的诊断镜像）未改变任何启动状态。

### ⑦ 只改 `get_lock_status` 的返回值 —— 假解锁（见下一篇）

把函数入口替换成 `mov w0,#1; ret`，`verifiedbootstate` 仍然是 `green`。原因见 `02-the-breakthrough.md`。

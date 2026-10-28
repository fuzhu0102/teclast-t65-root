# 02 — 锁状态补丁与 magic64 布局

## 只改返回值的结果

用 `magic64` 把 LK 包装成"签名材料不变 + 运行时补丁"的形式，补丁内容是把 `get_lock_status()` 的入口换成：

```asm
mov w0, #1
ret
```

结果：

| 指标 | 期望 | 实际 |
|---|---|---|
| 设备能否启动 | 能 | ✅ 能 |
| `ro.boot.vbmeta.device_state` | `unlocked` | ❌ 仍 `locked` |
| `ro.boot.verifiedbootstate` | `orange` | ❌ 仍 `green` |

补丁显然执行了（magic64 的框架如果不跑，跳转码后面就是垃圾指令，设备根本起不来），但状态没变。

## 根因：状态分裂

反编译原厂 `lk-sign.bin`（基址 `0x9EFFFE00`）：

```c
// FUN_9f064540  = get_lock_status()
undefined4 FUN_9f064540(void)
{
  _DAT_9f13d8c8 = 0;                     // ← 0x9F064558  清全局
  ...
  iVar2 = FUN_9f0620c0(lVar3);           // 读 miscdata
  if (iVar2 == 0) {
      if (FUN_9f00b30c(lVar3, 0x40) != 0) {   // TEE 校验
          /* locked */
      } else {
          _DAT_9f13d8c8 = 1;             // ← 0x9F0645D4  置全局
      }
  }
  uVar1 = _DAT_9f13d8c8;                 // 从全局取返回值
  return uVar1;
}
```

```c
// FUN_9f0680b0  = AVB "is_device_unlocked" 回调
undefined8 FUN_9f0680b0(undefined8 ops, undefined1 *out)
{
  int iVar1 = _DAT_9f13d8c8;
  if (_DAT_9f13d8c8 == 1) {
      *out = 1;                          // 只读全局，从不调用 get_lock_status
      return 0;
  }
  *out = 0;
  return 0;
}
```

这两处都能在原始镜像上直接复核（`tools/xref_scan.py`，地址换算见 docs/01）：

```
file 0x064750   vaddr 0x9F064550   (str  w at +8)     get_lock_status 写全局
file 0x0682B0   vaddr 0x9F0680B0   (ldr  w at +16)    AVB 回调读全局
```

该回调在 `0x9F068AA8` / `0x9F068B40` 被注册到 AVB 接口的 **`+0x48`** 槽位。而写 `androidboot.vbmeta.device_state` 的函数 `FUN_9f069374` 正是这样调它的：

```c
iVar4 = (**(code **)(param_1 + 0x48))(param_1, local_88);
...
if (local_88[0] == '\0') pcVar7 = "locked"; else pcVar7 = "unlocked";
```

**所以：只改返回值 = 状态分裂。**

| 消费方 | 看到的状态 |
|---|---|
| 调用 `get_lock_status()` 的路径 | 已解锁 |
| **AVB 回调（读全局变量）** | **仍锁定** |

`verifiedbootstate` 由 AVB 路径决定，因此停在 `green`。

## 正确的补丁

同时写全局变量和返回值：

```asm
mov  w0, #1                  ; 52800020
adrp x9, 0x9F13D000          ; B00006C9
str  w0, [x9, #0x8C8]        ; B908C920   ← 写 _DAT_9f13d8c8
ret                          ; D65F03C0
```

应用于运行时地址 `0x9F064540`（文件偏移 `0x64740`）。

原厂前四条指令（`patches/get_lock_status.json` 会校验它们，不匹配就拒绝生成）：

```asm
stp x29, x30, [sp, #-0x30]!  ; A9BD7BFD
mov x0, #0x1dc               ; D2803B80
mov x29, sp                  ; 910003FD
stp x19, x20, [sp, #0x10]    ; A90153F3
```

### 编码校验

| 指令 | 校验过程 |
|---|---|
| `adrp x9, 0x9F13D000` | ADRP 位于 `0x9F064540`，所在页 `0x9F064000`；目标页 `0x9F13D000`；imm = (0x9F13D000 − 0x9F064000) >> 12 = `0xD9` = 217；immlo = 217 & 3 = 1，immhi = 217 >> 2 = 54 → `0xB00006C9` |
| `str w0, [x9, #0x8C8]` | 32-bit STR 无符号偏移：imm12 = 0x8C8/4 = `0x232`，Rn = 9，Rt = 0 → `0xB908C920` |

## magic64 的产物布局（逐字节核对）

需要区分：把 `mImgSize` 加倍、文件里存两份 payload（"改过的 payload + 原始 payload"）的是另一种包装方式 `bsp_sign_fxxker`，`magic64` 只存一份。

本机原厂 LK 与成功产物的对比：

| 项 | 原厂 | 产物 |
|---|---|---|
| 文件大小 | 1,262,792 B | 1,263,048 B（**+0x100**） |
| `mImgSize`（表头 0x30） | `0x133E10` | `0x133F10`（**+0x100**） |
| payload | — | **逐字节相同**，整体后移 0x10 |
| SHA-256 | `b286d8d3…` | `69145c92…` |

产物布局：

```text
0x000   DHTB header      只有 mImgSize 变了（0x133E10 → 0x133F10）
0x200   jump code        16 B：88CF0414 = b +0x133E20 → 跳到 0x134020
0x210   payload          原厂 payload，1,262,792 − 0x200 − 0x4B8 字节，memcmp 完全相同
0x134020 shellcode       0x70 B
0x134090 patch_data      0x80 B（本机用了 28 B）
0x134110 footer          0x60 B：payload_offset = 0x210，cert_offset += 0x100
0x134170 cert / priv     原样，整体后移 0x100
```

也就是说：**只有一份 payload，一个字节都没改**。改动全部落在它前面（跳转）和后面（框架 + 偏移修正）的附加区，总增长 0x100 字节。

上游 `magic64.cpp` 与之逐项对应：`img_h->mImgSize += add_lenth`、`add_lenth = (0x10 + 0x70 + 0xFF) & ~0xFF = 0x100`、`img_f->payload_offset = 0x210`、`img_f->cert_offset += add_lenth`。

### 运行时怎么走

1. 加载器把镜像放到运行基址，执行入口是 `+0x200` 的跳转码；
2. shellcode 先把 payload 从 `0x210` 搬回 `0x200`（恢复原布局）；
3. 按 `patch_data` 逐条 `str` 打补丁并 `IC IALLUIS`；
4. `B -(size+0x7C)` 跳回 **payload 入口 `0x9F000000`** —— 也就是原厂 LK 的第一条指令。

两个地址需要区分：`0x9F000000` 是 **payload 入口**（执行起点），`0x9F064540` 是**被补丁改写的 `get_lock_status()` 入口**（补丁目标）。跳转会落到前者，补丁写在后者上。

所以补丁地址必须写**运行时地址**（这里 `0x9F064540`），不是文件偏移。

### 为什么签名仍然通过

签名材料是"那段被指向的 payload 字节"，而它们与原件逐字节相同；magic64 只是把指向它们的偏移（footer 的 `payload_offset`/`cert_offset`）跟着挪到位。**没有重算任何哈希，也不需要私钥。**

本机佐证：把这样的 LK 写进 `uboot_a/b` 后设备正常启动（原厂 SPL 的校验通过）。

> 注意：这条路线**不是"刷入改签 SPL"**。SPL 保持原厂、不改不刷（见 docs/01 方案评估第 2 条）。

### 为什么 LK 可以、SPL 不行

magic64 的 shellcode 用 `ADRP` 做页对齐寻址，要求 **payload 运行基址页对齐**：

| 镜像 | payload 基址 | 页对齐 | 可用 |
|---|---|---|---|
| `uboot` (LK) | `0x9F000000` | ✅ | ✅ 本机实测可用 |
| `trustos` | `0xB0060000` | ✅ | 上游明确 `not trustos`，未采用 |
| `splloader` | `0x65000A00` | ❌ | ❌ |

## 首次启动的行为变化（本机观察）

刷入补丁版 LK 后第一次开机，**Android 会要求抹除用户数据**。

本机观察到的顺序是：开机 → 出现恢复出厂提示 → 抹除 → 重启 → `orange`。一个**可能的解释**是 FBE（文件级加密）密钥与 verified-boot 状态绑定，状态从 `green` 变 `orange` 后旧密钥解不开。但这条因果**尚未单独验证**，只能算合理解释。

同理：**"出现恢复出厂提示"本身不能当作补丁成功的证明** —— 启动失败、分区损坏、别的原因也可能触发类似提示。判据只认下面三个属性：

```
ro.boot.verifiedbootstate    = orange
ro.boot.vbmeta.device_state  = unlocked
sys.boot_completed           = 1
```

达到这三条，说明**LK 侧汇报的锁状态和 AVB 侧的判定确实翻过来了**。

但"AVB 不再强制校验 `init_boot`"这句话要分两层看证据：

- 这三个属性只证明**状态变了**；
- **真正证明它不再拦修改过的启动镜像**的，是后面那一步 —— 把 Magisk 修补过的 `init_boot` 写进去，设备**照常启动并拿到了 `su`**。本机做到了（见 docs/04 第 8 步）。

换句话说：只看到 `orange` 不等于可以随便改启动镜像，得看镜像是否真被执行。

## 权威来源对照

| 结论 | 出处 |
|---|---|
| magic64 适用于 `sml, uboot`，`not trustos`；`add_length` 与布局 | [unisoc_chipram_signcheck_exploit](https://github.com/TomKing062/unisoc_chipram_signcheck_exploit)（`magic64.cpp`） |
| UMS9230/QOGIRL6 上有 U-Boot + AVB 双层校验、内部错误路径可能早于外层判断 | [uis7861-uboot-avb-bypass](https://github.com/nec093/uis7861-uboot-avb-bypass) |
| LK 侧控制 AVB 结果并非无先例 | 同上 |

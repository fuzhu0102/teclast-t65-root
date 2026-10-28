# 03 — Root、恢复与工具链

> 完整顺序（从原厂到 Root）见 [`04-end-to-end-recipe.md`](04-end-to-end-recipe.md)。本文只讲各环节细节与坑。

## 一、Magisk 修补 init_boot

前提：LK 补丁已生效（`verifiedbootstate=orange`、`vbmeta.device_state=unlocked`），AVB 不再校验 `init_boot`。

### 关键技巧：文件名

Magisk v31 的 `boot_patch.sh` 内部会执行类似：

```sh
./magiskboot compress=xz magisk magisk.xz
```

它找的是名为 **`magisk`** 的文件。从 APK 里解出来的二进制叫 `libmagisk.so`，直接放进去会报：

```
./magisk: inaccessible or not found
lstat 'magisk.xz': No such file or directory
```

**必须复制（不是改名）一份并命名为 `magisk`。** 因为同一个二进制还要以 `magisk64` 的名字存在，所以两次都用 `cp`：

```sh
cp lib/arm64-v8a/libmagisk.so magisk      # ← boot_patch.sh 找的名字
cp lib/arm64-v8a/libmagisk.so magisk64
```

（本机实测：只有 `magisk` 是必需的；两个都留是为了兼容其它版本的脚本。）

### PC 端：准备文件

以下命令都在**仓库根目录**执行，路径都相对于它。先建好目录：

```powershell
New-Item -ItemType Directory -Force -Path downloads, work\magisk-apk | Out-Null
```

将官方 Magisk v31.1 APK 放到 `downloads\Magisk-v31.1.apk`（哈希见 04），然后解出需要的部分。**不要用 `Expand-Archive` 解 `.apk`** —— 本机实测它会在 `META-INF\CERT.RSA` 这类条目上失败（把它复制成 `.zip` 再解，报同样的错，所以不是扩展名问题）。用 Windows 自带的 `tar`（bsdtar）或 7-Zip：

```powershell
tar -xf downloads\Magisk-v31.1.apk -C work\magisk-apk          # 没有 tar 就用下一行
# 7z x downloads\Magisk-v31.1.apk -owork\magisk-apk

Get-ChildItem work\magisk-apk | Select-Object Name             # 期望看到 assets/ lib/ 等
```

再推到平板（`adb push` 不会自动建中间目录，而且"把目录推到目录"会在目标下再套一层，所以要推到 `/data/local/tmp/magisk/` 这一层）：

```powershell
adb shell "mkdir -p /data/local/tmp/magisk"
adb push work\magisk-apk\assets /data/local/tmp/magisk/
adb push work\magisk-apk\lib    /data/local/tmp/magisk/

# 原厂 init_boot（从自己的 PAC 里取，sha256 必须与 04 的清单一致）
adb push stock\init_boot.img /data/local/tmp/magisk/init_boot.img

# 确认落点正确：应看到 /data/local/tmp/magisk/{assets,lib}/... 与 init_boot.img
adb shell "ls -R /data/local/tmp/magisk | head -30"
```

### 平板端：跑官方脚本

`boot_patch.sh` 是为 Android 环境写的，在 PC 上跑（尤其 Windows）会遇到 `busybox`/`lz4` 等缺失。**直接在平板上执行最省事**。

先进入平板的 shell —— **下面的代码块是 Android 端命令，不要在 PowerShell 里跑**：

```powershell
adb shell
```

```bash
cd /data/local/tmp/magisk

# 1) 二进制改名（全部用 cp/cp 链，避免"源文件已移走"的失败）
cp lib/arm64-v8a/libmagiskboot.so   magiskboot
cp lib/arm64-v8a/libmagiskinit.so   magiskinit
cp lib/arm64-v8a/libmagisk.so       magisk      # ← 关键
cp lib/arm64-v8a/libmagisk.so       magisk64
cp lib/arm64-v8a/libmagiskpolicy.so magiskpolicy
cp lib/arm64-v8a/libbusybox.so      busybox
cp lib/arm64-v8a/libinit-ld.so      init-ld
cp assets/boot_patch.sh   .
cp assets/util_functions.sh .
chmod -R 755 .

# 2) 修补
export MAGISKBIN=/data/local/tmp/magisk TMPDIR=/data/local/tmp/magisk/tmp ASH_STANDALONE=1
mkdir -p tmp
sh boot_patch.sh init_boot.img

# 3) 退出平板 shell，回 PC 端取回产物
exit
```

成功输出（节选）：

```
- Stock boot image detected
- Patching ramdisk
- Repacking boot image
Repack to boot image: [new-boot.img]
RAMDISK_SZ  [2629681] → [2188462]
```

产物 `new-boot.img` = 8,388,608 B（正好一个分区）。

### PC 端：把产物取回来

```powershell
adb pull /data/local/tmp/magisk/new-boot.img work\magisk_patched_init_boot.img
Get-FileHash work\magisk_patched_init_boot.img -Algorithm SHA256
```

本机这次的产物：`167a4ccd67131c9bf4195e54acf76981463b72a9d543459f0f8f5a8510501651`（8,388,608 B）。
**注意**：Magisk 版本/参数不同，产物哈希就不同，不要拿它当"必须一致"的校验值。

### 写入哪个槽位

先看当前槽位，别照抄 `init_boot_a`：

```powershell
adb shell getprop ro.boot.slot_suffix    # 本机 = _a
```

本机是 `_a`，所以写 `init_boot_a`。如果输出是 `_b`，后面所有 `init_boot_a` 都要换成 `init_boot_b`
（`uboot_a/b`、`splloader` 的写法另见 04 —— 本机的 LK 补丁是**两个槽位都写**）。

## 二、写入分区

**用下载模式（spd_dump），不要用 fastboot。**

实测 fastboot 写入 8 MiB 镜像时会在写入阶段报 `usb_read failed` 并中断（`Sending OKAY` 之后失败），存在写坏分区的风险。下载模式全程稳定：

```powershell
# 在仓库根目录执行；产物已在上一节取回到 work\
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec w init_boot_a work\magisk_patched_init_boot.img
```

> ⚠️ **写分区必须用打过补丁的 FDL2**（`lk-fdl2-sign_patched.bin`）。原厂 FDL2 只能读，写会 `usb_send failed`。

### spd_dump 的坑

| 现象 | 解法 |
|---|---|
| `find port failed` / `Connection failed` / `usb_send failed (0/n)` | **加 `--reconnect`**，并配 8～10 轮重试 |
| 端口被占 | 执行前 `Stop-Process -Name spd_dump` |
| 设备变"半死 COM4" | 拔线 + **电源键长按 20 秒**，重新进下载模式 |
| `exec reset` 不生效 | 同样需要 `--reconnect`；成功时 COM4 会立刻消失 |
| 进了下载模式但工具跳过了装载 | `FDL2 ALREADY EXECUTED, SKIP` —— 说明设备里的 FDL2 还在，需先物理断电 |

## 三、激活 Root

1. 重启，安装 Magisk App
2. 打开 App → 若提示「需要额外安装」→ 点确定，它会自动完成并重启
3. 授予 shell 超级用户权限（见下）

### 关于 `adb shell su` 被拒

本机现象：Magisk 已装、`magiskd` 以 uid 0 运行、`magisk -v` 正常，但 `adb shell su` 返回 `Permission denied`，且 App 里没有弹授权窗。

**准确的说法**：Magisk 默认不自动把 root 授予 `adb shell`（uid 2000）；需要在 App 内授权。具体哪些情况下会自动授权、哪些要手动，取决于 Magisk 版本策略 —— 请对照 [Magisk v31.1 发布说明](https://github.com/topjohnwu/Magisk/releases/tag/v31.1)。

本机 `ro.debuggable=0`（user 版）是**一个可能的影响因素**，但不是唯一解释，不要把它当成通用结论。

授权成功后：

```
$ adb shell su -c id
uid=0(root) gid=0(root) groups=0(root) context=u:r:magisk:s0
```

## 四、恢复

> 以下命令都在**仓库根目录**执行；恢复素材统一放 `work\`。
> 说明：本机这些故障**都**是通过下载模式恢复回来的（5 次以上）。BootROM 位于掩膜 ROM，但此前成功恢复不保证所有故障都能恢复；下载通道能否进入、USB 通信、存储和硬件状态仍需满足恢复条件。

先准备恢复素材（一次拷好，两种情况的命令都用这几个名字）：

```powershell
Copy-Item stock\lk-sign.bin                  work\restore-lk.bin
Copy-Item stock\u-boot-spl-16k-emmc-sign.bin work\restore-spl.bin
Copy-Item stock\init_boot.img                work\restore-init_boot.img   # 情况 B 才需要
```

### 情况 A：LK 实验失败（还没刷过 Magisk）

现象：改了 `uboot` 后设备起不来或回落到下载模式。

恢复 `uboot_a/b`；如果实验中也改过 SPL，再一并恢复 `splloader` 和 `splloader_bak`。下面给出四项全部恢复的命令；SPL 未改过时省略对应两项写入。

| 分区 | 原厂镜像 | SHA-256 |
|---|---|---|
| `splloader` / `splloader_bak` | `u-boot-spl-16k-emmc-sign.bin` (66,552 B) | `f816ddee9ae751965a8d0a64f7e348340dbf7774e1a48f56e69e8c84d56b4ce2` |
| `uboot_a` / `uboot_b` | `lk-sign.bin` (1,262,792 B) | `b286d8d308f7f2c6cf6f6d997cefee182f32306994c8ad6e860cc53f307ed785` |

```powershell
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec w splloader work\restore-spl.bin w splloader_bak work\restore-spl.bin `
       w uboot_a work\restore-lk.bin   w uboot_b work\restore-lk.bin
```

### 情况 B：已经刷入 Magisk，要回到原厂

**顺序很重要**：原厂 LK 会重新校验 `init_boot`，所以必须先把 `init_boot` 恢复原厂，再恢复 LK；倒过来做会得到一个"LK 原厂 + 启动镜像被改"的组合，多半起不来。

| 分区 | 原厂镜像 | 说明 |
|---|---|---|
| `init_boot_a` / `init_boot_b` | `init_boot.img` (8 MiB) | 必须是**本机原厂**那份，sha256 `af497fdc8397fe8bdd92fd4d798e8f92e167207df128dec083e39c87adb7ac2f` |
| `uboot_a` / `uboot_b` | `lk-sign.bin` | 见情况 A 表 |
| `splloader` / `splloader_bak` | `u-boot-spl-16k-emmc-sign.bin` | 见情况 A 表 |

```powershell
# 1) 先恢复启动镜像。这里将两个槽位恢复到已核对的同版原厂镜像；
# 本次 Root 实际只修改了 init_boot_a，并非两个槽位都刷过 Magisk。
# 如果两槽位原本版本不同，应分别使用各自的原厂备份，不能共用同一镜像。
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec w init_boot_a work\restore-init_boot.img w init_boot_b work\restore-init_boot.img
```

```powershell
# 2) 会话断开后重新进下载模式，再恢复 LK / SPL（与情况 A 相同）
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec w splloader work\restore-spl.bin w splloader_bak work\restore-spl.bin `
       w uboot_a work\restore-lk.bin   w uboot_b work\restore-lk.bin
```

恢复后第一次开机可能仍提示需要恢复出厂（boot 状态从 orange 回到 green），按提示处理即可。

### 数据

- 初次刷入补丁 LK 时，**用户数据会被抹除**（见 docs/02）。
- 抹掉的数据**不能**靠刷回固件找回 —— 固件里没有你的数据。
- 需要保留数据时，先做备份再动手；本机是"数据无所谓"的前提。

### 恢复后必须验证

**「能开机」不能作为恢复成功的判据**，必须把**实际写过的每一个分区**都回读出来比对哈希（只恢复了一部分，就只读那几项）：

```powershell
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec read_part uboot_a 0 1262792 work\vf-uboot_a.bin `
       read_part uboot_b 0 1262792 work\vf-uboot_b.bin `
       read_part splloader 0 66552 work\vf-spl.bin `
       read_part splloader_bak 0 66552 work\vf-spl_bak.bin `
       read_part init_boot_a 0 8388608 work\vf-init_boot_a.bin `
       read_part init_boot_b 0 8388608 work\vf-init_boot_b.bin
```

```powershell
Get-FileHash work\vf-uboot_a.bin, work\vf-uboot_b.bin, work\vf-spl.bin, work\vf-spl_bak.bin, `
              work\vf-init_boot_a.bin, work\vf-init_boot_b.bin -Algorithm SHA256 |
  Select-Object @{n='file';e={Split-Path $_.Path -Leaf}}, Hash | Format-Table -AutoSize
```

对照期望值（都等于原厂镜像的哈希）：

| 回读文件 | 期望 SHA-256 | 对应原厂镜像 |
|---|---|---|
| `vf-uboot_a.bin` / `vf-uboot_b.bin` | `b286d8d308f7f2c6cf6f6d997cefee182f32306994c8ad6e860cc53f307ed785` | `stock\lk-sign.bin` |
| `vf-spl.bin` / `vf-spl_bak.bin` | `f816ddee9ae751965a8d0a64f7e348340dbf7774e1a48f56e69e8c84d56b4ce2` | `stock\u-boot-spl-16k-emmc-sign.bin` |
| `vf-init_boot_a.bin` / `vf-init_boot_b.bin` | `af497fdc8397fe8bdd92fd4d798e8f92e167207df128dec083e39c87adb7ac2f` | `stock\init_boot.img` |

> ⚠️ 读的字节数就是**写进去的字节数**（`uboot` 只读 1,262,792 B），所以这比对证明的是「**我们写过的这段有效范围**与原厂一致」，**不是**「整个 3 MiB 分区逐字节等于原厂」。分区尾部的剩余区域本次未判定。

这一步能发现「设备能开机，但 `uboot` 里仍是改过的 LK」的情况：功能上无害，但不是原厂字节。

## 五、工具链

### Ghidra（分析 LK / SPL）

必须 **JDK 25+**，且**路径不能含括号或空格**（先复制到无括号路径再导入）：

```text
analyzeHeadless <projdir> <name> -import <bin> -processor AARCH64:LE:64:v8A ^
  -loader BinaryLoader -loaderBaseAddr 0x9efffe00 ^
  -scriptPath <scripts> -postScript <Script>.java -deleteProject
```

基址速查：

| 镜像 | Ghidra 基址 | payload 基址 |
|---|---|---|
| LK (`lk-sign.bin`) | `0x9EFFFE00` | `0x9F000000` |
| SPL | `0x65000800` | — |
| TOS | `0xB005FE00` | `0xB0060000` |

### magic64（签名材料不变的运行时补丁）

来自 [unisoc_chipram_signcheck_exploit](https://github.com/TomKing062/unisoc_chipram_signcheck_exploit)。本仓库用 `tools/magic64_patch.py` 包了一层：

```text
python tools/magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json -o work\lk-global1.bin --magic64 <magic64目录>\magic64.exe
```

要点：

- 需要 `aarch64-linux-android-as/ld/objcopy.exe`（上游仓库 `bin/` 里或 NDK 里）
- **必须在无括号路径下运行**，否则内部调 `as`/`ld` 的命令行会被截断（脚本会自动复制到临时目录规避）
- `patch_data` 偏移 = `0x210 + mImgSize(原图) + 0x70`，可用 0x80 字节
- 补丁项格式：`[目标地址 4B LE][字数 4B LE][数据…]`，以 `0x00000000` 结尾
- 脚本会在以下情况**拒绝生成**：镜像 sha256 与 `patches/*.json` 不符、目标地址处的原始指令不符、payload 超出范围、产物大小/mImgSize 增长异常

### 寻找代码引用（ADRP + ADD/LDR 交叉引用）

不用 Ghidra 也能快速定位引用点：扫描 payload 区，匹配 `ADRP Xd, page` 且其后的 `ADD/LDR/STR` 满足 `page == (target & ~0xFFF)`、偏移 == `target & 0xFFF`。

例 1：字符串 `androidboot.vbmeta.device_state`（文件偏移 `0xDF5F8`，vaddr `0x9F0DF3F8`）→ 全镜像只有 **1 处**引用：`0x9F069488`。

例 2：全局变量 `0x9F13D8C8` → 9 处引用，其中 `0x9F064550`（`str w`，写）与 `0x9F0680B0`（`ldr w`，读）就是状态分裂的两端。

**边界**：这个扫描只认上述 ADRP 相对寻址形式，间接引用（指针表、寄存器间接）扫不到。**"未找到"不等于"没有引用"**，要下结论请用反汇编器复核。

## 六、附带的实际用途

Root 之后装了 [ACC](https://github.com/VR-25/acc) + [AccA](https://github.com/MatteCarra/AccA) 做充电上限控制（本项目 root 的初衷）。

安装：

```powershell
adb install AccA.apk
adb shell "su -c 'magisk --install-module /sdcard/acc_module.zip'"
```

重启后在 AccA 里设置上限百分比即可。

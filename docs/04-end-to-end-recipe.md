# 04 — 从原厂到 Root：完整操作顺序

> **所有命令都在仓库根目录执行**，路径一律相对于它：素材放 `stock\`，产物放 `work\`。
> 原理见 docs/02，细节与坑见 docs/03。**先读完全文再动手。**

## 0. 操作前须知

1. **数据会被抹掉。** 第一次刷入补丁 LK 后，boot 状态从 `green` 变 `orange`，Android 会要求恢复出厂（见 docs/02）。抹掉的数据**不能**靠刷固件找回。
2. **这是"改 LK 强制解锁"，不是厂商认可的永久解锁。** 原厂 LK、固件回刷、OTA 替换 `uboot`/`trustos`/`vbmeta` 都可能让方法失效。
3. **本机验证的是唯一一台设备。** 另一台 A7D5 若固件版本不同，`0x9F064540` 这类地址、甚至 `get_lock_status` 是否存在都要重新确认 —— `tools/magic64_patch.py` 会在镜像哈希或原始指令不一致时拒绝生成，别用 `--force` 绕过。

## 1. 准备素材

### 1.1 建目录

```powershell
# 当前目录应是仓库根目录
New-Item -ItemType Directory -Force -Path stock, work, downloads | Out-Null
```

### 1.2 分区镜像

两条路，任选：

**A. 从官方 PAC 解包**
台电 T65 的升级包是一个 PAC（本机：`Firmware.pac`，4,639,146,005 B，
sha256 `a11197ee2ac1c7b4780563668d43c03df48559ba13b3f7e8a1380878f56f25c0`）。
PAC 是"头部 + 多个分区段"的容器，用官方升级工具或任意 PAC 解包器都能取出分区镜像。

**B. 直接从设备回读（本机实际做法）**
进下载模式后用 `spd_dump ... exec read_part <分区> 0 <长度> <文件>` 读出来（完整命令见第 4 步）。
这条路不需要 PAC 解包器，但需要设备能进下载模式。

无论哪条路，放好之后**逐项核对**（长度是硬指标，哈希确认版本）：

```powershell
Get-ChildItem stock | Select-Object Name, Length | Format-Table -AutoSize
Get-FileHash stock\lk-sign.bin, stock\u-boot-spl-16k-emmc-sign.bin, stock\init_boot.img -Algorithm SHA256 |
  Select-Object @{n='file';e={Split-Path $_.Path -Leaf}}, Hash | Format-Table -AutoSize
```

| 文件（放 `stock\`） | 来自 | 长度 | SHA-256 |
|---|---|---|---|
| `lk-sign.bin` | `uboot_a`/`uboot_b` 的前 1,262,792 B | 1,262,792 | `b286d8d308f7f2c6cf6f6d997cefee182f32306994c8ad6e860cc53f307ed785` |
| `u-boot-spl-16k-emmc-sign.bin` | `splloader` / `splloader_bak` 的前 66,552 B | 66,552 | `f816ddee9ae751965a8d0a64f7e348340dbf7774e1a48f56e69e8c84d56b4ce2` |
| `init_boot.img` | `init_boot_a` | 8,388,608 | `af497fdc8397fe8bdd92fd4d798e8f92e167207df128dec083e39c87adb7ac2f` |

`trustos` / `sml` / `teecfg` 本路线**不需要**（见 docs/01 方案评估第 4、5 条）。
`uboot_a/b` 分区是 3 MiB，而 LK 镜像只有 1,262,792 B；下面凡是要"比对原厂"，比的都是**这 1,262,792 B 有效范围**，不是整个分区。

### 1.3 工具（版本固定）

| 工具 | 版本 / 来源 | 用途 |
|---|---|---|
| `magic64.exe` + `aarch64-linux-android-{as,ld,objcopy}.exe` | [unisoc_chipram_signcheck_exploit](https://github.com/TomKing062/unisoc_chipram_signcheck_exploit)（`add_length = 0x100` 的那版） | 生成签名材料不变的 LK |
| `spd_dump` | spd_dump `main` 分支、提交 `88f7ca6` 的 x64 Release（包名含 `spd_dump_main_250_88f7ca6_x64_Release`），**需要能加载 `Channel9.dll` 的串口版本** | 下载模式读写分区 |
| 补丁版 FDL2 | `gen_fdl2-dl.exe`（`fdl1_spl_pgpt_tools_win64only_260909.zip`）处理 `lk-fdl2-sign.bin` 得到，本机产物 sha256 `69cb0cdb0e8b159589d1d04c513eaae0b42a09d857958bc811371daaef2cd2d0` | 写分区必需（原厂 FDL2 只能读） |
| 补丁版 FDL1 | `gen_fdl1-dl.exe` 处理 `fdl1-sign.bin` 得到 | RAM exec 阶段需要 |
| ROP 载荷 `custom_exec_no_verify_65015f08.bin` | 通用包 `ums9230_universal_unlock_EMMC_1.72.zip` 内的载荷，**只用于 `exec_addr`，不写 flash** | 让 BootROM 执行我们送进去的 FDL1 |
| Magisk | v31.1（`Magisk-v31.1.apk`，10,679,283 B，sha256 `89cefac0a318afa39fb3880c45c87b036c05bc1ebf02a7a715b7f8d7c26ac6e7`） | 修补 `init_boot` |
| Python | 3.10+（本机 3.12） | 跑 `tools/*.py` |

把 `fdl1-dl.bin`、`lk-fdl2-sign_patched.bin` 放进 `work\`，后面命令直接用这两个名字。

> ⚠️ 通用包里的 SPL/LK **不要**混用：本机实测其 `fdl1-dl.bin` 与本机原厂 `fdl1-sign.bin` 只有 12.84% 字节相同。只用它里面的 ROP 载荷与工具，镜像一律用本机 PAC 的。

## 2. 确认设备状态与槽位

```powershell
adb devices
adb shell getprop ro.boot.slot_suffix        # 期望 _a
adb shell getprop ro.boot.verifiedbootstate  # 起点应为 green
adb shell getprop ro.product.board           # ums9230_6h10
adb shell getprop ro.build.version.incremental
```

槽位规则：

- 启动镜像写 `init_boot_<slot>`（本机 `init_boot_a`）。
- LK 补丁**两个槽位都写**（`uboot_a` 与 `uboot_b`）—— 下一次启动可能切槽，只写一个会表现为"改完有时生效有时不生效"。
- `splloader` 与 `splloader_bak` **全程保持原厂**，本方法不写它们（见 docs/01 方案评估第 2 条）。

## 3. 生成候选 LK 并核对

```powershell
python tools\magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json --check

# <magic64目录> 换成你解压 magic64 的那一层（同目录里要有 magic64.exe
# 和 aarch64-linux-android-as/ld/objcopy.exe）。
python tools\magic64_patch.py stock\lk-sign.bin 0x9F000000 patches\get_lock_status.json `
    -o work\lk-global1.bin --magic64 "<magic64目录>\magic64.exe"
```

第二条命令的期望输出（本机实测）：

```
payload base      : 0x9F000000
mImgSize          : 0x133E10 -> 0x133F10
patch_data offset : 0x134090  (28/128 bytes)
original payload intact (signature material unchanged): True
sha256            : 69145c92e72c9703b1fecce0631b9c4c43eeb9ca3f88f7b1bb169ce10b52ccdc
output            : work\lk-global1.bin  (1263048 bytes)
```

**通过判据**：退出码 0、`original payload intact ... True`、1,263,048 B、sha256 与 `patches\get_lock_status.json` 的 `expected_output.sha256` 相同。
脚本在写入输出之前校验 payload、预期大小和预期哈希。**不能只看文件是否存在**：之前运行留下的旧文件可能仍在，写盘错误也可能留下不完整文件。必须确认**本次命令退出码为 0**，并重新核对产物大小与 SHA-256：

```powershell
# 紧接生成命令执行；失败时停止，不继续刷写已有文件
if ($LASTEXITCODE -ne 0) { throw 'LK build failed; do not flash the output' }
$lkOutput = Get-Item -LiteralPath work\lk-global1.bin
$lkHash = (Get-FileHash -LiteralPath $lkOutput.FullName -Algorithm SHA256).Hash
if ($lkOutput.Length -ne 1263048 -or $lkHash -ne '69145c92e72c9703b1fecce0631b9c4c43eeb9ca3f88f7b1bb169ce10b52ccdc') {
    throw 'LK output size or SHA-256 does not match; stop here'
}
```

## 4. 进下载模式并备份

进下载模式：关机后**先按住音量键，再插线**，保持约 10 秒。

```powershell
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec read_part uboot_a 0 3145728 work\bak-uboot_a.bin `
       read_part uboot_b 0 3145728 work\bak-uboot_b.bin `
       read_part splloader 0 66552 work\bak-spl.bin `
       read_part splloader_bak 0 66552 work\bak-spl_bak.bin `
       read_part init_boot_a 0 8388608 work\bak-init_boot_a.bin `
       read_part init_boot_b 0 8388608 work\bak-init_boot_b.bin
```

校验：把备份的**前 1,262,792 B**（`uboot` 有效范围）与整份 `splloader` / `init_boot` 拿去比对原厂哈希：

```powershell
# uboot 只比前 1,262,792 B（分区是 3 MiB，我们只动这段有效范围）
python -c "import hashlib;print(hashlib.sha256(open(r'work\bak-uboot_a.bin','rb').read(1262792)).hexdigest())"
python -c "import hashlib;print(hashlib.sha256(open(r'work\bak-uboot_b.bin','rb').read(1262792)).hexdigest())"

# 这几项是整份读的，直接算整文件哈希
Get-FileHash work\bak-spl.bin, work\bak-spl_bak.bin, `
              work\bak-init_boot_a.bin, work\bak-init_boot_b.bin -Algorithm SHA256 |
  Select-Object @{n='file';e={Split-Path $_.Path -Leaf}}, Hash | Format-Table -AutoSize
```

> 别用 `[System.IO.File]::ReadAllBytes("work\bak-…")` 这种相对路径：.NET 按 PowerShell **进程目录**解析，会报 `Could not find a part of the path`（实测）。要用就先 `(Resolve-Path work\bak-uboot_a.bin).Path` 转成绝对路径。

六项必须分别等于 1.2 表里的 `b286d8d3…`（两个 uboot）/ `f816ddee…`（两个 spl）/ `af497fdc…`（两个 init_boot）；不等就**停下来**（说明设备不是原厂状态，先弄清楚再动）。

## 5. 写入补丁 LK（第一个不可逆动作）

> ⚠️ **从这一步起，下一次开机 Android 大概率要求恢复出厂 → 数据全丢。** 想留数据就先备份。

```powershell
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec w uboot_a work\lk-global1.bin w uboot_b work\lk-global1.bin
```

回读校验（两个槽位都要验）：

```powershell
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec read_part uboot_a 0 1263048 work\vf-uboot_a.bin `
       read_part uboot_b 0 1263048 work\vf-uboot_b.bin

Get-FileHash work\vf-uboot_a.bin, work\vf-uboot_b.bin -Algorithm SHA256 |
  Select-Object @{n='file';e={Split-Path $_.Path -Leaf}}, Hash | Format-Table -AutoSize
# 期望两行都是 69145c92e72c9703b1fecce0631b9c4c43eeb9ca3f88f7b1bb169ce10b52ccdc
```

退出下载模式：`exec reset`（需带 `--reconnect`）。若 `reset` 没生效、屏幕全黑且 COM4 一直在，拔线后**长按电源 20 秒**强制断电，再正常开机。

## 6. 验证解锁效果

```powershell
adb shell getprop ro.boot.verifiedbootstate    # 期望 orange
adb shell getprop ro.boot.vbmeta.device_state  # 期望 unlocked
adb shell getprop sys.boot_completed           # 期望 1
```

- 若第一次开机出现恢复出厂提示：抹除并按提示重启，然后再查这三项。
- 若仍 `green`/`locked`：别把"能开机"当成功，回第 3 步复核补丁地址与 `patch_data` 是否真的进去了（docs/02 的"状态分裂"就是这种情况）。
- 若设备起不来：走 docs/03 的**情况 A**。

> 这三项只证明**锁状态已翻转**。"AVB 不再拦修改过的启动镜像"要靠第 7、8 步（修补过的 `init_boot` 真启动并拿到 `su`）来证明。

## 7. 修补并刷入 init_boot

按 docs/03 第一节在**平板上**跑 `boot_patch.sh`（关键是 `cp libmagisk.so magisk`），再把产物取回 `work\`：

```powershell
adb pull /data/local/tmp/magisk/new-boot.img work\magisk_patched_init_boot.img
Get-FileHash work\magisk_patched_init_boot.img -Algorithm SHA256
# 记下这个哈希，下面写入后要拿它比对（本机是 167a4ccd…，你的版本可能不同，以本次为准）
```

再确认一次槽位，然后写入：

```powershell
adb shell getprop ro.boot.slot_suffix   # 本机是 _a
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec w init_boot_a work\magisk_patched_init_boot.img
```

回读校验（比对内容哈希，不是只看长度）：

```powershell
spd_dump --wait 60 --reconnect exec_addr 0x65015f08 `
  fdl work\fdl1-dl.bin 0x65000800 `
  fdl work\lk-fdl2-sign_patched.bin 0x9efffe00 `
  exec read_part init_boot_a 0 8388608 work\vf-init_boot_a.bin

Get-FileHash work\vf-init_boot_a.bin, work\magisk_patched_init_boot.img -Algorithm SHA256 |
  Select-Object @{n='file';e={Split-Path $_.Path -Leaf}}, Hash | Format-Table -AutoSize
# 两行哈希必须一致
```

## 8. 激活并验收 Root

1. 重启，装 Magisk App，按提示完成"额外安装"。
2. 在 App 里给 shell 授权（见 docs/03）。
3. 判据：

```powershell
adb shell magisk -v          # 31.1:MAGISK:R
adb shell su -c id           # uid=0(root) ... context=u:r:magisk:s0
```

## 9. 想回到原厂

按 docs/03 的**情况 B**：**先**恢复 `init_boot_a/b`，**再**恢复 `uboot_a/b`，最后按需恢复 `splloader`。
顺序反了会得到"原厂 LK + 改过的启动镜像"，多半起不来。恢复后按 docs/03 的"恢复后必须验证"逐分区回读比对。

## 一次性核对表

| # | 步骤 | 通过判据 |
|---|---|---|
| 1 | 素材 | 三个原厂镜像长度/哈希与 1.2 表一致 |
| 2 | 设备状态 | `green`、板级 `ums9230_6h10`，记下槽位 |
| 3 | 生成 LK | 退出码 0、payload intact = True、sha256 = `69145c92…` |
| 4 | 备份 | 回读哈希 = 原厂值（`uboot` 比前 1,262,792 B） |
| 5 | 写入 LK | 两个槽位回读 sha256 = `69145c92…` |
| 6 | 解锁 | `orange` / `unlocked` / `boot_completed=1` |
| 7 | 刷 init_boot | 回读 sha256 = 本次 Magisk 产物哈希 |
| 8 | Root | `su -c id` → uid=0 |

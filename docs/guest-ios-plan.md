# iOS 跑 iOS：把 Henge 的机器从 vmapple 换成 vphone（可行性 + 实施方案）

目标：让 Henge 在这台 iPhone 上把 **iOS 27** 当 guest 跑起来（宿主 iOS 15/16）。
参考实现：[Lakr233/vphone-cli](https://github.com/Lakr233/vphone-cli)（Mac 上用
Virtualization.framework 跑虚拟 iPhone）。

本文是**研究结论 + 分阶段方案**，不是已完成的功能。第 6 节是能马上做的第一批代码。

---

## 1. vphone-cli 到底怎么跑的（读它的 Research 笔记）

| 环节 | 做法 | 出处 |
| --- | --- | --- |
| 硬件身份 | DFU 时自报 **`vresearch101ap`**（BDID 0x90），运行时 DeviceTree 按 **`vphone600ap`**，`hardware target` 报 `vphone600ap` | `Research/Firmware/firmware_manifest_and_origins.md` |
| 固件来源 | **混合**：PCC `vresearch101ap` 提供引导链（LLB/iBSS/iBEC/iBoot）和 SPTM/TXM；PCC `vphone600ap` 提供 DeviceTree、SEP、KernelCache、RecoveryMode；**iPhone 17,3** 的 IPSW 提供用户态、trust cache、文件系统 | 同上 |
| 签名 | BuildManifest 用 `vresearch101ap` 字段做 **TSS/SHSH**（Apple 对 research 设备公开签） | 同上、`Research/Restore/native_restore_architecture.md` |
| 引导 ROM | 换成了开源的 **OpenAVB / OpenSEP**，不是 Apple 的 | README 致谢 |
| 运行 | Apple **Virtualization.framework**（宿主是 Mac） | README |
| 补丁 | iBoot / TXM / kernel 的 CFW 补丁（跑 JB 用户态、去 keybag 依赖、IOMobileFramebuffer ABI 等） | `Research/Patches/`、`Kernel/` |

一句话：**vphone 用的是一台"Apple 自己签得动"的虚拟 iPhone（PCC research 设备）**，
而不是真实 A 系列 SoC；它把 Mac 的 hypervisor 当 CPU，把 Apple 的 VZ 设备当外设。

---

## 2. 这件事在 iOS 上意味着什么

iOS 应用层**没有** Hypervisor.framework / Virtualization.framework，所以：

- 不能像 vphone 那样"用宿主 CPU 跑 guest"——只能用 HengeVM 这种**指令级 JIT 模拟**；
- 但 HengeVM 已经具备 vphone 依赖的那半条链路：**自报身份进 DFU → 走标准 restore → 得到磁盘**（vmapple 就是这么装的，`host/install.c` + `third_party/idevicerestore`）；
- 缺的是**另一半：设备模型**。vphone 的"设备"是 Apple VZ 里的 `vphone600ap`，没有公开寄存器手册；
  Henge 现有的 `devices/` 全是 vmapple 的（GICv3、PL011、PL031、PCIe+virtio、PVG GPU）。
  iOS guest 需要 `vphone600ap` 的 DeviceTree 约定、**IOMobileFramebuffer**（不是 PVG）、SEP（OpenSEP 那条路）等。

### 结论（先说不好听的）

1. **概念上可行**：身份 + TSS + restore 这套我们有；固件是公开可下载的 CloudOS/iPhone IPSW；
   指令集差异我们已经开始补（`core/simd.c`）。
2. **工作量是"再造一个机器模型"**，不是改几个常量：vphone 用 Apple 现成的设备实现，
   我们要自己写（或从 QEMU/其它开源项目借）。
3. **6s/2 GB 不适合当目标**：iOS 27 guest 至少 4 GB（vphone 默认 4–6 GB），
   A9 还要多模拟 LSE/FP16/DotProd 等。**现实目标：A14+ / 4 GB 以上的机器**（iPhone 12 起）。
   iOS 15 宿主可以，但 2 GB 的内存墙绕不过去——这点和 macOS guest 一样。

---

## 3. 与 Henge 现有结构的对照

| vphone 的东西 | Henge 里对应 | 状态 |
| --- | --- | --- |
| 自报 `vresearch101ap` 进 DFU | `devices/bdif.c` + `devices/virtio_usb.c`，`host/install.c` 驱动 idevicerestore | ✅ 有（身份是 vmapple） |
| 混合 BuildManifest + TSS | `third_party/libtatsu`、`idevicerestore` 原样 | ✅ 有（需要喂 vresearch 的字段） |
| 引导链 LLB/iBSS/iBoot | `ios/Henge/BootROM.swift` 取 AVPBooter | ⚠️ 需要换成 CloudOS 的 iBoot 链（OpenAVB） |
| 设备（CPU 外设） | `devices/*`（vmapple：GICv3/PL011/PCIe/virtio/PVG） | ❌ 需要 `vphone600ap` 一套 |
| GPU | `devices/pvg_lite.c` + `pvg_metal.m`（PVG 命令流 → Metal） | ❌ iOS guest 走 IOMobileFramebuffer，需要新的显示设备 |
| 存储 | `devices/virtio_blk.c` + `host/disk.c` | ⚠️ vphone 用 NVMe/ANS 风格，需要确认 |
| 输入 | `devices/virtio_input.c`、`ios/Henge/Input.swift` | ⚠️ 同上 |
| 网络 | `host/net_slirp.c`（本 fork 关掉了） | ⚠️ |
| 用户态补丁（CFW） | 无 | ❌ vphone 的 iBoot/TXM/kernel 补丁要移植或重做 |
| 指令集 | `core/simd.c` 已补 6 族，剩 FP16 算术/by-element | ⚠️ 见 `docs/ios15-a9-support.md` |

---

## 4. 分阶段方案（每阶段都有可验证产物）

### 阶段 0：把"镜像能不能装"变成可测的（1 天，不改引擎）

1. 从 CloudOS / iPhone IPSW 里读出 `BuildIdentities`（就像我们刚给 macOS 做的
   `hv_install_check_ipsw()`），打印 `DeviceClass`、`ApBoardID/ApChipID`、
   `BuildStyle`，确认 `vresearch101ap` 与 `vphone600ap` 的身份字段；
2. 用同一套 libzip/libplist 代码，做一个 `henge-fw probe` 小工具，在 Mac 上先跑通。

产物：一份 `vresearch101ap`/`vphone600ap` 身份清单（进仓库 `docs/`），不碰设备。

### 阶段 1：DFU 冒充 + restore 打通（工作量最大的一步里最小的一块）

1. `devices/` 新增 `vphone.c`：只做 **DFU 期**需要的寄存器（USB 复用现有 bdif/virtio_usb，
   `ApBoardID=0x90`、`ApChipID` 用 CloudOS 表里的值、ProductType `vresearch101ap`）；
2. `host/install.c` 增加 `HV_GUEST_IOS` 分支：喂给 idevicerestore 的是**混合 manifest**；
3. 验收标准：能拿到 Apple 的 SHSH/ApTicket，restore 跑到"写入文件系统"。

### 阶段 2：把 guest 引导起来（设备模型主体）

1. 引导链：OpenAVB（开源）→ iBSS/iBEC → iBoot，先把串口打出来（PL011 复用）；
2. DeviceTree：按 CloudOS 的 DT 结构喂给 guest（这是 vphone600 的"契约"）；
3. 存储：确认 guest 期望的块设备（NVMe vs virtio），先只读挂载；
4. 验收标准：iBoot 打印出 `vphone600ap` 的 boot args，然后 kernel 起来。

### 阶段 3：显示与输入（让"能用"）

1. `IOMobileFramebuffer` 设备：vphone 记录过 ABI（`SwapEnd` payload 0x548→0x560 这类），
   我们在 `devices/` 里实现一个最小 framebuffer，用现有 `pvg_metal.m` 的 Metal 呈现路径；
2. 触摸/键盘：复用 `virtio_input` 或新增 HID；
3. 验收标准：看到锁屏。

### 阶段 4：用户态与性能

1. CFW 补丁（vphone 的 iBoot/TXM/kernel 补丁要么移植，要么在模拟器里绕开）；
2. 指令集：补 FP16 算术/by-element/SHA512（差分测试已有框架，`tests/test_simd.c`）；
3. 内存：4 GB 起步，桌面机型（iPhone 12+）。

---

## 5. 明确的风险点

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| `vphone600ap` 设备模型无公开文档 | vphone 靠 Apple VZ，实现细节不在开源里 | 以 CloudOS **DeviceTree** 为唯一契约，逐节点实现；先用串口 + 只读存储把 kernel 跑起来 |
| TSS 是否给我们的 ECID 签 | vphone 证明 research 身份可签，但那是 Mac 上的 VZ 设备 | 阶段 1 先做"TSS 探测"，只验签不写盘 |
| TXM / PAC | iOS 26+ 有 TXM；Henge 只 strip PAC | 先按 vphone 的 TXM 补丁思路（禁 TXM 的路径） |
| 内存 | iOS 27 guest ≥4 GB | 目标机型 iPhone 12+/iPad；6s 明确不支持 |
| 时间 | 阶段 1–3 是数周级工程 | 每阶段都有可验收产物，不一次性押注 |

---

## 6. 已测到的真实数据（阶段 0 的一部分，已跑通）

用 `tools/cloudos_probe.py`（只发 HTTP Range 请求，不下整个文件）读了 vphone 文档里给的公开
cloudOS 镜像 `https://updates.cdn-apple.com/private-cloud-compute/c0ecdb4b310cf5239ab2b248dd3098eec297dc5aa3bbe6ada27273262b0b8b64`：

```
size: 1,199,454,323 bytes        (1.2 GB，对比 iPhone 17,3 的 IPSW 是 19.7 GB)
magic: PK zip, 97 个成员, 含 BuildManifest.plist
ProductVersion 26.4   ProductBuildVersion 23E5207q
SupportedProductTypes: ComputeModule14,1, ComputeModule14,2, ComputeModule17,2, Mac14,14, iPhone99,11
BuildIdentities: 7 —— j226cap, j236cap, j475dap, vphone600ap, vresearch101ap（各含 Variant 变体）
vresearch101ap: YES   vphone600ap: YES
```

这说明：

1. **恢复镜像很小**（1.2 GB），guest 的引导链/内核/SEP/DT 都在这一份里，iPhone 17,3 的 IPSW 只提供
   用户态；对手机侧存储友好（不用再搬 20 GB）。
2. `vphone600ap` 与 `vresearch101ap` 的身份确实在公开镜像里，说明"TSS 为 research 设备签"这条链路
   对被冒充的虚拟硬件成立。
3. `ApBoardID`/`ApChipID`/`BuildStyle` 不在我初版打印的键位下，要展开 `BuildIdentities[i].Info`
   的全部键——下一步几分钟的活。

## 7. 这个仓库现在能先做什么（第一批代码）

1. `core/guestimg.c`（新）：读 IPSW 的 `BuildManifest.plist`，输出
   `ProductVersion/ProductBuildVersion`、每个 `BuildIdentity` 的
   `DeviceClass/ApBoardID/ApChipID/BuildStyle/Variant`，并判断是否含
   `vresearch101ap` / `vphone600ap`。**纯读文件，不需要设备**，Mac 上就能验证。
2. `tools/guestprobe.c`：命令行包装，`make guestprobe` 后：
   `./build/guestprobe CloudOS.ipsw` → 打印身份表。
3. 复用 `host/install.c` 里刚给 macOS 写的 `hv_install_check_ipsw()` 的 zip/plist 读取代码。

先把这三件事做出来，我们就能用真实的 CloudOS IPSW 确认阶段 1 的字段，
而不是靠猜——和这次 macOS 27.2 的教训一样。

---

## 8. 参考

- vphone-cli：<https://github.com/Lakr233/vphone-cli>（MIT）
- 固件来源：`Research/Firmware/firmware_manifest_and_origins.md`
- 恢复：`Research/Restore/native_restore_architecture.md`
- 补丁：`Research/Patches/`、`Research/Kernel/`
- 开源引导 ROM：<https://github.com/owngoal-dev/OpenAVB>、<https://github.com/owngoal-dev/OpenSEP>

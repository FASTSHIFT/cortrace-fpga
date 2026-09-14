# Stage-3 · 上板 Bring-up 踩坑总结（A7-Lite 第一次点灯）

> 记录 MicroPhase A7-Lite (XC7A35T) 到货后第一次 JTAG 烧录点灯的全过程踩坑。
> 结论：链路打通，FPGA 成功配置（`End of startup status: HIGH`），两个 LED 按 blink bitstream 交替闪烁。
> 环境：Ubuntu（VMware 虚拟机）+ Vivado 2021.1 + 板载 FT232H JTAG（识别为 Digilent JTAG-HS2）。

---

## TL;DR

第一次上板烧录卡了很久，根因是**两个叠加的环境问题**，都和板子/比特流无关：

| # | 问题 | 症状 | 解法 |
|---|------|------|------|
| 1 | Linux `ftdi_sio` 内核驱动抢占 FT232H | cable 被当成 `/dev/ttyUSB0` 串口，Vivado 拿不到 | 装 Xilinx cable 驱动 + udev unbind 规则 |
| 2 | **VMware EHCI(USB 2.0)透传打不开 FTDI MPSSE 端点** | cable 一直 `(closed)`，`jtag frequency` 报 `port closed`，扫不到 FPGA | **VMware USB 控制器改 3.1（xHCI）** ← 决定性 |

改完后 `xsdb` 立刻扫到 `xc7a35t (idcode 0362d093)`，`vivado -mode batch` 烧录 `End of startup status: HIGH`，点灯成功。

---

## 现象时间线

1. 板子到货，左边 Type-C（JTAG 口）连进 VM，`lsusb` 看到 `0403:6014 FT232H`，电源灯亮、出厂 demo 在闪 —— **板子本身没问题**。
2. `vivado -mode batch` 烧录 → `ERROR: [Labtoolstcl 44-494] There is no active target ... may be locked by another hw_server`。
3. 反复 kill hw_server / 拔插 / 重写 tcl，错误不变。
4. `xsdb` 底层探测：能看到 cable `Digilent JTAG-HS2 210241106108 (closed)`，但 **JTAG 链上无器件**，`jtag frequency` 报 `port closed`。
5. 确认 `ftdi_sio` 抢占 → 装驱动 + udev 解绑（`/dev/ttyUSB*` 消失，问题缓解但 cable 仍 `(closed)`）。
6. `lsusb -t` 显示 cable 走 **ehci-pci（USB 2.0）**。怀疑 VMware EHCI 透传对 FTDI MPSSE 端点支持不稳。
7. **VMware Settings → USB Controller → USB compatibility 改 USB 3.1**，重启 VM。
8. cable 改走 **xhci_hcd**，`xsdb` 立刻扫到 `xc7a35t (idcode 0362d093 irlen 6 fpga)`。
9. 烧 `blink.bit` → `End of startup status: HIGH`，两个 LED 交替闪。✅

---

## 坑 1：`ftdi_sio` 抢占 FT232H

### 根因
A7-Lite 的 JTAG 用 FTDI **FT232H**（USB `0403:6014`）。Linux 的 `ftdi_sio` 内核串口驱动会在设备枚举时立刻把它 attach 成 `/dev/ttyUSB0`，**独占了 USB interface**，Vivado 的 libusb 拿不到 MPSSE（JTAG）访问 → 报 "locked"。

### 解法

**A. 装 Xilinx cable 驱动**（Vivado 手动解压安装时常被跳过）：
```bash
cd ~/workpath/tools/xilinx/Vivado/2021.1/data/xicom/cable_drivers/lin64/install_script/install_drivers
sudo ./install_drivers
```
装完设备会被识别成 `Digilent USB Device`（而非裸 FT232H），对应 `xsdb` 里的 `Digilent JTAG-HS2`。

**B. 加 udev 规则让 `ftdi_sio` 永久放手这颗设备**（驱动脚本的 udev 规则只改权限、不阻止 binding，需要额外 unbind 规则）：
```bash
sudo tee /etc/udev/rules.d/99-ftdi-unbind.rules <<'EOF'
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6014", RUN+="/bin/sh -c 'echo $kernel > /sys/bus/usb/drivers/ftdi_sio/unbind'"
EOF
sudo udevadm control --reload-rules
```
重新拔插后，`dmesg` 会看到 ftdi_sio attach 后立刻 `disconnected`，`/dev/ttyUSB*` 不再出现。验证：
```bash
ls /dev/ttyUSB*   # 应为空
# interface driver 应为 [none]，不是 ftdi_sio
```

---

## 坑 2（决定性）：VMware EHCI 透传打不开 FTDI MPSSE 端点

### 根因
即使 `ftdi_sio` 放手了、`lsusb` 正常（480M）、interface driver 为 `[none]`，Vivado/`xsdb` 仍然：
- cable 显示 `(closed)`，打不开数据端点
- `jtag frequency` 报 `port closed`
- JTAG 链扫描为空（扫不到 FPGA）

原因是 **VMware 默认的 USB 2.0（EHCI）控制器透传，对 FTDI 的 MPSSE bulk 端点支持不稳定**——USB 总线层枚举正常，但 JTAG 需要的 bulk in/out 端点打不开。这是 VMware + FTDI JTAG 的已知坑。

### 解法（关键）
1. **关闭 VM**（关机，不是挂起）
2. VMware **VM → Settings → USB Controller → USB compatibility** 改为 **USB 3.1**（xHCI）
3. 开机，让板子 USB 重新连进 VM

验证 cable 改走 xHCI：
```bash
lsusb -t   # FT232H 应挂在 xhci_hcd 下，而不是 ehci-pci
```

改完后 `xsdb` 一次就扫到：
```
1  Digilent JTAG-HS2 210241106108
   2  xc7a35t (idcode 0362d093 irlen 6 fpga)
```

---

## 验证链路（这一关打通了什么）

| 验证项 | 状态 |
|--------|------|
| 板子供电 / 电源灯 | ✅ |
| 50 MHz 晶振（J19） | ✅（blink 计数器靠它） |
| 复位按钮（L18） | ✅（约束生效） |
| 2 个 LED（M18/N18，低电平点亮） | ✅ 交替闪 |
| FT232H JTAG cable（xHCI 透传） | ✅ |
| Vivado bitstream 生成 + JTAG 烧录 | ✅ `End of startup status: HIGH` |
| 自写 RTL 上板运行 | ✅ |

这是 Stage-3 上板 PoC 的第一道门：**PC → JTAG → FPGA 配置链路完全打通**。后续 trace 数据走以太网（RGMII），不经过 JTAG，所以这两个坑只影响"烧 bitstream"这一步，不影响日常 trace 采集。

---

## 复现 / 后续使用

工程内的 bring-up 文件：
```
syn/artix7/bringup/
  blink.v              2-LED 交替闪 RTL（板子自检）
  blink.xdc            J19/L18/M18/N18 引脚 + SPIx4 flash 配置
  run_blink.tcl        综合→实现→生成 blink.bit/.mcs/.bin
  program_jtag.tcl     JTAG 烧录（容错 target 选择）
  build/               构建产物（git ignored）
```

构建 + 烧录（VM 已设 USB 3.1、驱动已装的前提下）：
```bash
source ~/workpath/tools/xilinx/Vivado/2021.1/settings64.sh
cd syn/artix7/bringup/build
vivado -mode batch -source ../run_blink.tcl       # 出 blink.bit
vivado -mode batch -source ../program_jtag.tcl    # JTAG 点灯（掉电丢失）
```

固化到 QSPI flash（IS25LP128F，128Mb，掉电不丢）：用 Vivado Hardware Manager → Add Configuration Memory Device → `is25lp128f-spi-x1_x2_x4` → 选 `blink.mcs`。

---

## 给后来者的一句话

板子、bitstream、Vivado 都没问题时，JTAG 还连不上，**Linux + VMware 下九成是这两个坑**：先 `ls /dev/ttyUSB*` 看 ftdi_sio 有没有抢（坑 1），再 `lsusb -t` 看 cable 在不在 xhci 上（坑 2）。两个都排掉就通了。

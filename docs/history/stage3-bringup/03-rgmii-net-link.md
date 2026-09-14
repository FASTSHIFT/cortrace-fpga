# Stage-3 · 千兆网口 RGMII 链路调试（A7-Lite + RTL8211E）

> 记录 A7-Lite (XC7A35T) 板载千兆以太网（RGMII + RTL8211E PHY）从「一个包都发不出」到「UDP 双向环回打通」的全过程。
> 结论：RX、TX 双向链路全通，**ARP 应答 + UDP 1234 端口环回实测通过**。
> 调试顶层用的是纯网络验证设计（`net_test_top`，不含 trace pipeline），基于 verilog-ethernet 的 NexysVideo example core（ARP + ICMP 占位 + UDP loopback）。

---

## TL;DR

千兆网口卡住的根因是**收发两侧各有一处「双重延迟」**，本质同一个道理：RTL8211E 这颗 PHY 的 strap **默认把 RGMII 的 RX/TX internal delay 都打开了**，FPGA 端如果再叠加自己的延迟，时钟和数据就错相，CRC 全错 / 发不出去。

| # | 方向 | 错误做法（双重延迟） | 正确做法 | 实测现象 |
|---|------|---------------------|----------|----------|
| 1 | RX | FPGA 端再加 IDELAY tap=16 | **旁路 IDELAY，phy_rxd 直连** | CRC 由全错→全对 |
| 2 | TX | MAC 用 90° 移相时钟驱动 TXC（`USE_CLK90="TRUE"`） | **`USE_CLK90="FALSE"`，TXC 与数据同相** | 由一个包都不发→ARP/UDP 正常 |

两处都「把 FPGA 端的额外延迟去掉，信任 PHY 自带的 delay」之后，链路打通。

---

## 网络拓扑

```
STM32F429-DISC ──(ETM 4-bit trace)──> [A7-Lite FPGA] ──RGMII──> RTL8211E ──RJ45──┐
                                                                                 │
PC (VMware, 桥接网卡, 192.168.10.245) ──WiFi/有线──> OpenWrt 路由器 192.168.10.1 ─┘
                                          FPGA IP = 192.168.10.42
```

- VMware 网卡设 **桥接模式**（不是 NAT），VM 直接拿到 `192.168.10.x`，和 FPGA 同网段。
- FPGA、PC 都接同一台 OpenWrt 路由器，省掉交叉网线 / 直连网卡的麻烦。
- FPGA IP/MAC 在 `fpga_core_net.v` 里硬编码：`local_ip = 192.168.10.42`、`local_mac = 02:CA:FE:A7:7E:5C`、`gateway_ip = 192.168.10.1`。MAC 第一字节 `0x02` 是 locally-administered 标志（自造、非厂商分配）。
  > 早期用过 `.200`，但那是局域网里一个旧下线设备复用过的 IP，换成 `.42`（“万物答案”）避免将来撞 IP。

---

## 现象时间线（含失败的尝试，如实记录）

1. **第一版 RX 加 IDELAY tap=16** → LED 编码显示「只收到 CRC-bad 帧」（快闪），RX 收到了电平但相位错。
2. 翻 verilog-ethernet 源码发现：`rgmii_phy_if` 的 RX 用 `ssio_ddr_in`，**根本不用 IDELAY**，它的设计假设是「PHY 端已经把 RX clock-to-data 居中了」。查 RTL8211E datasheet + A7-Lite strap：**RX delay 确实是 ON**。
3. **旁路 FPGA 端 IDELAY**（`rxd_dly = phy_rxd` 直连）→ LED0 慢闪 = **收到 CRC-good 帧 ✅ RX 通了**。
4. 但 ping 不通。`tcpdump` + OpenWrt ARP 表都确认：**FPGA 一个包都没发出来**（ARP 表里没有 FPGA 的 `02:00:00:00:00:00`）。
5. 把 LED1 接到 `tx_axis_tvalid`（协议栈想发包的信号）→ LED1 也慢闪，说明 **协议栈生成了 ARP/UDP 应答，但 RGMII TX 物理层没把它发出去**。问题锁定在 TX 的 RGMII 时序。
6. 判断 TX 也是「双重延迟」：MAC 默认 `USE_CLK90="TRUE"`，用 90° 移相时钟驱动 TXC，叠加 PHY 自己的 TX delay → 错相。**改 `USE_CLK90="FALSE"`**（TXC 和 TXD 同相输出，靠 PHY 的 TX delay 去居中采样）。
7. 重新综合烧录后：
   - `ip neigh` 删掉旧表项再 ping → ARP 解析出 `192.168.10.200 lladdr 02:00:00:00:00:00 REACHABLE`，**FPGA 回了 ARP，TX 通了 ✅**
   - `echo HELLO | nc -u 192.168.10.200 1234` → **原样回显，UDP 双向环回打通 ✅**

> 注：上面时间线里的 `192.168.10.200` / `02:00:00:00:00:00` 是当时调试用的旧地址。
> 后来发现 `.200` 是局域网里一个旧设备复用过的 IP，已改成 `.42` +
> MAC `02:CA:FE:A7:7E:5C`（见前文「网络拓扑」与文末「文件清单」）。

---

## 坑 1：RX 双重延迟（FPGA IDELAY × PHY RX delay）

### 根因
RGMII 的 RX 时钟和数据需要约 2ns 的相对延迟（让时钟沿落在数据眼图中心）。这个延迟**要么 PHY 出，要么 FPGA 出，只能有一个**。

- RTL8211E 的 strap 默认 **RX internal delay = ON**（PHY 已经把 RXC 延迟好了）。
- verilog-ethernet 的 `rgmii_phy_if` 用 `ssio_ddr_in`，**FPGA 端不加 IDELAY**，正是配合「PHY 出延迟」这种板子。
- 我们最初照搬别的工程加了 IDELAY tap=16 → 延迟叠加两次 → 相位转过头 → CRC 全错。

### 解法
`net_test_top.v` 里直接旁路 IDELAY：
```verilog
wire [3:0] rxd_dly   = phy_rxd;      // 不经 IDELAY，直连
wire       rxctl_dly = phy_rx_ctl;
```
LED0 立刻从「快闪（CRC bad）」变「慢闪（CRC good）」。

> 注：如果换一块 strap 把 RX delay 关掉的板子，就要反过来——FPGA 端加回标定好的 IDELAY。判断方法就是看 LED0 是 good 还是 bad 闪。

---

## 坑 2：TX 双重延迟（FPGA clk90 移相 × PHY TX delay）

### 根因
对称地，TX 方向 RGMII 也需要 TXC 相对 TXD 居中。

- verilog-ethernet 默认 `USE_CLK90="TRUE"`：用 MMCM 出的 **90° 移相时钟**驱动 TXC 的 ODDR，让 FPGA 自己把时钟移到数据中心。这是给「PHY TX delay = OFF」的板子用的。
- 但 RTL8211E strap **TX delay 也是 ON**：PHY 端会再把收到的 TXC 延迟一次 → 又是双重延迟 → PHY 采样错位 → 发出去的帧 PHY 端判废，物理层一个包都不上线。

### 解法
`fpga_core.v` 里 `eth_mac_1g_rgmii_fifo` 实例：
```verilog
.USE_CLK90("FALSE"),   // 原来是 "TRUE"
```
`USE_CLK90="FALSE"` 时，`rgmii_phy_if` 的 TXC ODDR 用**和数据同相的 clk**（见 `rgmii_phy_if.v` 里 `clk_oddr_inst .clk(USE_CLK90=="TRUE" ? clk90 : clk)`），把居中的活儿交给 PHY 的 TX delay。

改完 ARP/UDP 立刻正常。

---

## 验证链路（这一关打通了什么）

| 验证项 | 方法 | 状态 |
|--------|------|------|
| RGMII RX 物理层 | LED0 慢闪 = 收到 CRC-good 帧 | ✅ |
| RGMII TX 物理层 | `ip neigh` 看到 FPGA 回的 ARP（`02:CA:FE:A7:7E:5C REACHABLE`） | ✅ |
| 协议栈 RX→TX 全链路 | `nc -u 192.168.10.42 1234` 发 UDP，原样回显 | ✅ 5/5 |
| 时序收敛 | 综合 WNS=+1.385ns / WHS=+0.058ns，0 DRC error | ✅ |

> verilog-ethernet 的 NexysVideo core **不实现 ICMP echo**，所以 `ping` 不通是正常的（ARP 能解析就证明 TX 通）。真正的端到端验证用 **UDP 1234 端口环回**。

---

## 高效调试手段（板上只有 2 个可控 LED 时）

板上用户可控 LED 只有 2 个（M18 / N18，低电平点亮），所以用**闪烁频率编码多状态**，一个灯顶几个用：

```verilog
wire slow = cnt[24];   // ~1.9 Hz @125MHz
wire fast = cnt[21];   // ~15 Hz
// LED0: CRC-good 帧来过 -> 慢闪；只来过 CRC-bad -> 快闪；没收到 -> 灭
// LED1: MAC 发过帧（tx_axis_tvalid 拉过高）-> 慢闪；没发过 -> 灭
```
- **慢闪 = 好状态，快闪 = 坏状态，灭 = 没发生**，一眼能区分。
- 把协议栈内部信号（`rx_fifo_good_frame` / `rx_error_bad_fcs` / `tx_axis_tvalid`）从 `fpga_core` 引出来当 debug tap，比只看物理引脚强得多——能区分「物理层收到了但 CRC 错」和「协议栈根本没产生应答」。

> 下一步若要更细的运行时调试（不重新综合就调相位/tap），可上 **VIO + ILA**：VIO 在线改 IDELAY tap / 复位，ILA 抓 RGMII 波形和 AXIS 流。板上还有 3 个按键 + 1 路 CH340 串口（右边 Type-C）可做触发 / print 调试。

---

## 复现 / 文件清单

```
syn/artix7/bringup/
  fpga_core_net.v         verilog-ethernet NexysVideo example core 的本地 fork
                          （模块改名 fpga_core_net；submodule 保持 pristine）
    - local_ip = 192.168.10.42 / gateway = 192.168.10.1
    - local_mac = 02:CA:FE:A7:7E:5C（locally-administered）
    - USE_CLK90 = "FALSE"（TX 时序修复）
    - 新增 dbg_rx_good_frame / dbg_rx_bad_fcs / dbg_tx_axis_tvalid 三个 debug 输出
  net_test_top.v          纯网络验证顶层（RX 旁路 IDELAY，LED 频率编码）
  net_test.xdc            RGMII 引脚（全 BANK15）+ 时钟约束
  run_net_test.tcl        综合→实现→生成 net_test.bit/.mcs（读本地 fpga_core_net.v + submodule 库）
  program_net_test.tcl    JTAG 烧录 net_test.bit
  build/                  构建产物（git ignored）
```

构建 + 烧录：
```bash
source ~/workpath/tools/xilinx/Vivado/2021.1/settings64.sh
cd syn/artix7/bringup/build
vivado -mode batch -source ../run_net_test.tcl       # 出 net_test.bit
# 确保 hw_server 在跑、openocd 已 kill
vivado -mode batch -source ../program_net_test.tcl   # JTAG 烧录

# 验证（VM 桥接到 192.168.10.x 网段）
sudo ip neigh flush dev <iface>                      # 清本机 ARP 缓存，避免旧表项误导
ping -c1 192.168.10.42                               # 触发 ARP（ping 本身不通是正常的）
ip neigh show 192.168.10.42                          # 应为 ...02:ca:fe:a7:7e:5c REACHABLE
echo HELLO | nc -u -w2 192.168.10.42 1234            # 应原样回显
```

---

## 给后来者的一句话

RGMII 连 RTL8211E 这类「strap 默认开 RX/TX delay」的 PHY，FPGA 端的口诀是**两边都别再自己加延迟**：RX 旁路 IDELAY，TX 用 `USE_CLK90="FALSE"`。收发各有一处双重延迟，症状一个是 CRC 全错、一个是发不出包，但根因是同一个。判断延迟到底在哪一侧，就靠 LED 把协议栈内部的 good/bad/tx 状态用闪烁频率编码出来看。

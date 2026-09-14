# Stage-4 · V1：FPGA 自环 IDELAY 眼图扫描（不接 STM32）

> 第四阶段验证阶梯第二关（见 `../PLAN_STAGE4.md`）。
> 目的：在**完全不接 STM32** 的前提下，正面接触真命门——源同步 DDR 采样的相位。FPGA 自己发已知 DDR pattern、板上跳线绕回 trace 输入脚、用 Stage-2 的采样前端 `trace_capture_a7` 采样，自动扫 32 个 IDELAY tap，把每 tap/每 lane 的误码数做成「眼图」表，PC 用一个 UDP 包读走。
> 这样把「STM32 会不会发对」这个变量彻底摘掉——眼睛开不开，只取决于 FPGA 自己的采样链。

---

## 为什么先自环、不接 STM32

V1 要验的是 FPGA 采样链本身（IBUF→IDELAYE2→IDDR + IDELAYCTRL + 时钟 buffer）的相位能力。如果一上来就接 STM32，眼睛闭了你分不清是「STM32 发的有问题」还是「FPGA 采的有问题」。

自环把数据源换成 FPGA 自己——**已知、可控、确定性**。眼睛要是开了，说明 FPGA 采样链 + tap 扫描机制都对；之后接 STM32 眼睛闭了，就能锁定问题在 STM32 侧或线缆 SI，而不是 FPGA 逻辑。这就是「每关只引入一个未知」。

```mermaid
graph LR
    GEN[FPGA 内部<br/>计数器 DDR pattern] -->|ODDR| OUT[txclk_out + txd_out]
    OUT -->|板上跳线| IN[trace_clk_in + trace_data_in]
    IN --> CAP[trace_capture_a7<br/>IDELAY + IDDR]
    CAP --> CHK[trace_eyescan<br/>checker + tap 扫描]
    CHK --> TBL[眼图表 256B]
    TBL -->|UDP :5001| PC[PC 渲染眼图]
    style GEN fill:#ffe6cc
    style PC fill:#d6ffd6
```

## pattern：自对齐计数器，不需要帧同步

每个 `clk_tx`（100MHz）周期发一个自增字节 `cnt`：
- lane i 的上升沿位 = `cnt[i]`，下降沿位 = `cnt[4+i]`
- 所以接收端恢复出的字节 `{trace_b, trace_a}` 就是 `cnt`，相邻周期恒差 +1

checker 只校验「本字节 == 上一字节 + 1」——**自对齐**，不管环路延迟多少拍都能锁。误码按 lane 归属（lane i 拥有 bit i 和 bit 4+i），得到每 lane 的误码率。

> 用计数器而非 PRBS：计数器的 +1 关系让 checker 无需种子同步，最简单且对单 bit 错敏感。

## tap 扫描 FSM

跑在恢复出的 `trace_clk` 上（关键设计，见下）。对 tap 0..31：
1. `S_LOAD` 写 tap 到 IDELAYE2（VAR_LOAD + `tap_load` 脉冲）
2. `S_SETTLE` 等 512 周期让延迟线稳定
3. `S_CLR`→`S_COUNT` 在 2^20 个 `trace_clk` 周期窗口内累加每 lane 误码（饱和到 0xFFFF）
4. `S_STORE` 把 4 个 lane 的 16-bit 误码写进表，更新 best_tap
5. 全扫完 `S_DONE`，把 IDELAY 停在 best_tap，链路即可用

表结构（256 字节）：`addr = tap*8 + lane*2 + {hi,lo}`，大端 16-bit 误码。

## 踩坑：「没接跳线」绝不能读成「眼睛全开」

第一版有个隐蔽缺陷：扫描 FSM 由**恢复时钟 `trace_clk` 驱动**——没插跳线时 `trace_clk` 不翻转，FSM 根本不跑，误码表停在复位值（全 0），而「0 误码」会被渲染成「眼睛全开」。**第一次没插线读出来 32 个 tap 全 clean、eye centre=15 —— 这是假阳性。**

修复：读出口用 `scan_done` 把关——扫描未完成时返回 `0xFF`（→ 误码 0xFFFF → 渲染成闭眼），PC 端识别「整表全 0xFF」为「扫描没跑起来」并提示检查跳线。这样：
- 没接跳线 / `trace_clk` 死 → 全 0xFF → 明确报「scan never ran」
- 接了跳线、扫完 → 真实眼图

> 这是 V1 自己的「诚实性」修补：宁可报「没测到」，也不能把「没测」伪装成「完美」。

## 物理连线（板上跳线，GPIO1 / BANK 16）

5 根跳线把输出脚接回输入脚（都在 GPIO1，同 BANK16，3.3V）：

| 输出（FPGA 发） | package pin | → 跳线 → | 输入（FPGA 收） | package pin |
|----------------|-------------|----------|----------------|-------------|
| txclk_out | C13 | → | trace_clk_in | D17 |
| txd_out[0] | B13 | → | trace_data_in[0] | F13 |
| txd_out[1] | A13 | → | trace_data_in[1] | E14 |
| txd_out[2] | A14 | → | trace_data_in[2] | D14 |
| txd_out[3] | C14 | → | trace_data_in[3] | E16 |

> 仍以 package pin 为权威（GPIO1 排针 P/N 丝印有抄错历史，见 `02-trace-wiring.md`）。

## LED 指示

- **LED0** = IDELAYCTRL ready（稳定常亮 = 200MHz 参考钟 OK，采样链可用）
- **LED1** = 扫描状态：灭=扫描中/没跑、慢闪=扫完且找到干净 tap、快闪=扫完但**没有**任何全 lane 干净的 tap（SI 问题）

## 流程

```bash
source ~/workpath/tools/xilinx/Vivado/2021.1/settings64.sh
cd syn/artix7/bringup/build
vivado -mode batch -source ../run_eyescan.tcl        # 出 eyescan.bit
vivado -mode batch -source ../program_eyescan.tcl    # JTAG 烧录
# >>> 插好 5 根跳线 <<<，等 LED1 慢闪（扫完）
cd .. && python3 eyescan_read.py --ip 192.168.10.42   # 读眼图，给出 best tap
```

## 当前状态

- [x] RTL + 约束 + 综合：时序收敛，0 DRC error
- [x] UDP 读出路径实测可用
- [x] 「没插跳线 → 全 0xFF 报 scan-never-ran」防伪阳性已实测确认
- [x] 插好跳线（GPIO1 内部回环，JTAG 边界扫描确认 5 对连接正确）
- [x] **V1 完成：眼图扫出来了** —— tap 18~31 共 **14 个 tap 干净解码**，eye 中心 = tap 24，traceIF 解出帧 `123402030405060708090a0b0c0d0e0f` 逐字节正确。

## 走对的路：用 orbtrace 自己的 traceIF 做判据 + BUFR_IO 时钟

第一版我自己发明了「+1 ramp 逐 bit 误码计数」做判据，踩了 DDR 上升/下降相位的坑（raw 看着是干净 ramp，误码却乱报），反复几轮没收敛。**回头参考 orbtrace 原始方案才找对路：**

1. **判据换成 orbtrace 的 `traceIF.v`**：pattern 发生器发真正的 TPIU 帧（`FF FF FF 7F` 同步字 + 16 字节已知 payload），环回喂给上游 `traceIF.v`。traceIF 自己锁 `0x7FFF_FFFF` 同步字、用 `isREsync` 自动区分上升/下降沿对齐——**我之前手算 IDDR Q1/Q2 相位的坑，traceIF 内部本来就解决了**。tap 判据 = 「该 tap 下 traceIF 解出的帧 == golden 且无坏帧」。

2. **诊断纪律：先仿真定位，再上板**。反复上板试错几轮后，停下来写了纯逻辑 iverilog 仿真（`sim/eyescan_pattern_tb.v`）：pattern 发生器 → 理想 DDR 采样 → traceIF，**理想采样下完美解出 `1234…0f`**。这证明逻辑对、接法对，**硬件的 nibble 错位是物理采样相位问题，不是逻辑 bug**——把排查范围一下子缩小了。

3. **根因 = BUFG 时钟插入延迟**。硬件上（用 BUFG）traceIF 能锁 sync，但奇数字节高 nibble 错（falling 沿采在过渡区），32 tap 全扫不到干净点。换成 **`trace_capture_a7` 的 `BUFR_IO`** 时钟方案（区域局部时钟、skew 小，正是项目早期 r11 HG-2 敏感性研究预言的源同步正确选择）后：**tap 18-31 共 14 个 tap 干净开眼**。一个参数定乾坤。

### 完整诊断链（全实测，非猜测）
| 步骤 | 手段 | 结论 |
|------|------|------|
| 1 | JTAG 边界扫描 | 5 对连线正确 |
| 2 | iverilog 仿真 | 逻辑/接法正确（理想采样完美解码） |
| 3 | 硬件 BUFG | sync 锁定但 falling nibble 错 → 物理时钟相位问题 |
| 4 | 换 BUFR_IO | **眼睛开，14-tap 宽余量，best_tap=24** |

## 设计边界（诚实声明）
- 当前 pattern 速率 = 100MHz DDR（200Mbps/lane），保守低速首跑，证明采样链 + 判据 + tap 校准全链路正确。满速命门留 V4。
- 自环眼图（板内 GPIO1↔GPIO1 短路径）≠ 接 STM32 的真实链路眼图，但 14-tap 余量说明采样架构本身健康。
- **关键工程结论：源同步采样必须用 BUFR_IO 而非 BUFG**（已上板坐实，不再是 HG-2 的纸面推断）。

## 设计边界（诚实声明）

- 当前 pattern 速率 = **100MHz DDR（200Mbps/lane）**，是保守的低速首跑，先证明采样链 + tap 扫描机制正确。真实 trace 满速命门留 V4 升速。
- 自环眼图 ≠ 接 STM32 的真实眼图：自环走的是板内 GPIO1↔GPIO1 短路径，SI 比 STM32 杜邦线好。自环开眼只证明「FPGA 采样链 + tap 机制对」；真实链路眼图要 V2 接 STM32 后再测。
- 时钟 buffer 当前用 `BUFG`（保守）。`trace_capture_a7` 另有 `BUFR_IO` 选项（区域局部、skew 更小），是源同步更正的选择，可在 V4 对比。

## 下一步
- 插跳线采真实自环眼图，确认有干净 tap 窗口。
- 然后 **V2**：换 STM32 真实 ETM 低速输入（连线见 `02-trace-wiring.md`），用 V1 得到的 tap 起步。

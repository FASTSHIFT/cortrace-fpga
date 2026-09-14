# Proposal 40：一键诊断工具 `trace_doctor` 设计方案

> 日期：2026-07-25
> 状态：设计草案（未实现）
> 触发：过去两天在 2-bit 尝试过程中，反复卡在**分不清是硬件、网络、固件、FPGA bit、还是采集
>       前端相位问题**——每一层都要单独测一遍，很多次错误归因，反复 tap 扫描、断电重启。
>       需要一个自上而下的一键诊断，30 秒内定位到具体的失效层。
> 关联：AGENT.md 的坑点清单；`hw_selftest.py`（已有的物理通路 voltmeter）；
>       `fpga_health.py`（proposal 30 CSR 读取）

---

## 0. 一句话目标

**跑一次 `trace_doctor` → 输出"绿灯逐层通过"或"精确定位到哪一层坏了 + 该层的典型修复"**。
用来替代目前"分十几步手工 grep 各种寄存器/网络/串口"的调试流程。

---

## 1. 为什么现在需要它（踩过的坑穷举）

过去两天在同一硬件上反复卡壳的**每一个失效模式**：

### L0 — 主机侧环境
1. **openocd 残留进程占 SWD**：一次 openocd 未正常 shutdown 导致 DAP 端口占用；下一次
   openocd 报 `unable to open ftdi device` 或 `CMD_INFO failed`。
2. **openFPGALoader 干扰 DAPLink**：同 USB 总线烧 FPGA 时 DAPLink 出 I/O error，需拔插。
3. **USB 设备掉线**：FT232H 或 DAPLink 从 `lsusb` 消失。VMware 需重新连接可移动设备。
4. **hgfs 掉线**：`/mnt/hgfs/...` 变空，H743 工程读不到，需要重挂。
5. **VPN 路由抢占**：`utun` 抢了大量路由，虽然 FPGA 192.168.10.42 本地没抢，但要确认。
6. **网络路由错**：曾用 USB 千兆网卡加 /32 host 路由，某次残留路由或未清理导致混淆。

### L1 — 物理硬件
7. **TRACECK/TRACED 焊线松**：47R 串阻焊接 + GND 双绞飞线，任一 lane 断开会出错误率飙升。
8. **FT232H 未上电/被 VMware 断开**：`lsusb` 缺，无法烧 FPGA。
9. **STM32 板未上电**（DAPLink 可能仍能被识别但 SWD 不响应）。

### L2 — FPGA bit 加载
10. **openFPGALoader 报 Done 但 FPGA 没换 bit**：多次遇到，UDP 响应格式对不上新 bit。
    需要 `md5sum` 对比 + 读一个已知 CSR 值区分不同 bit。
11. **烧了错误的 bit**：clktap vs pin_la vs 2bit_traceif 三个 bit 长得像，容易混。
12. **FPGA build 目录留了 2-bit 实验的 bit**（`trace_iddr_clktap_2bit*.bit`），后续 agent 可能烧错。

### L3 — FPGA 网络 / UDP
13. **ARP 未建立**：FPGA 只回 ARP 不回 ICMP。刚烧完 bit 需要几秒。
14. **UDP status 通但 rearm 后 dump 挂**：rearm 让 FPGA 进入特定采样态，某些情况下后续 UDP
    会短暂无响应（今天卡壳的直接原因，未查清）。
15. **DEPTH 读回 0xFFFF (65535)**：CSR 未初始化的表现，意味着当前 bit 不是 clktap 或 FPGA
    加载失败。

### L4 — 采集前端相位
16. **IDDR 半 nibble 相位偏移**：字节流 `f7ff f7ff` 而非 `7fff 7fff`，跨会话/上电漂移。
17. **clock IDELAY tap 需重新扫**：每次 FPGA 重烧后需扫 0-31 找 fsync 最多的 tap。
18. **tap CSR 是运行时状态**：FPGA reset 后回默认值 2；跨会话不保留。

### L5 — STM32 侧 debug 授权 / CoreSight
19. **DBGMCU_CR 的 TRACECLKEN / D1/D3 dbg clk 位没设**：ETM 无 trace 时钟。
20. **DEMCR.TRCENA (bit24) 未开**：CoreSight 无供电，ETM 寄存器读回 0。
21. **CoreSight 软件锁**：LAR 需先写 0xC5ACCE55 解锁，否则 CURPSIZE/FFCR 写入被静默丢弃。
22. **TRCPDCR.PU 未置**：ETM 电源门被关，TRCSTATR 卡在不稳定态。
23. **CSTF ENS0 未置**：ETM ATB 未通过 funnel，ETF 收不到数据（ETF_STS.Empty=1）。
24. **ETF_MODE 未 =0x2 (HW FIFO)**：ETF 卡在 disabled 或 SW FIFO 模式。
25. **CURTPM 非 0**：TPIU 在跑内置测试模式，覆盖了真 ETM 数据（AA/55 pattern）。
26. **TRCAUTHSTATUS non-invasive debug 未开**：ETM 输出被 gate 关（今天遇到 0xC0 状态）。

### L6 — STM32 侧时钟 / 固件
27. **PLL 配错**：曾用 8MHz HSE 基准超频崩溃（实际 25MHz）。
28. **VOS0 + overdrive 未设**：≥300M 会启动失败或运行不稳。
29. **flash latency 不够**：高频跑 flash 时数据错乱。
30. **CoreMark cache 运行时 flag 未生效**：RAM_D1 0x24000000 读回不是 0xCACE。
31. **固件 make 判"无需重编"**：改宏后 build/*.hex 未更新，串口 banner 的 FLAGS_STR 是旧的。
32. **ELF ≠ flash**：解码用的 ELF 与实际烧的 hex 不对应（proposal 38 mem 基址 bug 已定位）。

### L7 — 解码器
33. **mem.bin 基址 skew 0x2a0**（proposal 38 已修）。
34. **`trc_pkt_lister` 等 stdin 卡死**：必须 `< /dev/null`。
35. **`opencsd_etm4_run` 自动检测走错分支**：CAP_RAW=1 数据被误认作已 deframe（tap 未对齐时）。

---

## 2. 工具设计：分层探测 + 早停

**核心原则**：**从最外层到最内层逐层测**（主机→物理→FPGA bit→FPGA UDP→采集相位→STM32
debug→ETM 数据流→解码），**每一层是下一层的前提**，任一层 FAIL 立即停止并给修复建议
（不再往下测徒劳）。每一步都要有**明确的判据**（数值范围+期望位模式），不用"感觉像/大概"。

### 2.1 输出格式（示意）
```
$ python3 trace_doctor.py

[L0 host]      openocd/openFPGALoader 无残留               [PASS]
[L0 host]      DAPLink (0d28:0204) + FT232H (0403:6014)   [PASS]
[L0 host]      /dev/ttyACM0 存在, hgfs 可读                [PASS]
[L0 host]      FPGA 192.168.10.42 路由 = ens33            [PASS]
[L1 phys]      DAPLink SWD DPIDR = 0x6ba02477             [PASS]
[L1 phys]      STM32 IDCODE = 0x20036450 (H743)           [PASS]
[L2 fpga_bit]  DEPTH readout = 63472 (clktap 特征)         [PASS]
[L2 fpga_bit]  bit md5 = 9831ac82... 匹配 clktap 4bit    [PASS]
[L3 net]       ARP 192.168.10.42 = REACHABLE              [PASS]
[L3 net]       UDP status 3次连续成功                       [PASS]
[L4 phase]     TPIU test-pattern (AA/55) err = 0.09%      [PASS]
[L4 phase]     TRACECLK 边沿计数 = 100.0 MHz              [PASS]
[L5 debug]     DEMCR.TRCENA=1, DBGMCU_CR=0x0070003f       [PASS]
[L5 debug]     TRCAUTHSTATUS non-invasive=11             [PASS]
[L6 clk]       PLL1DIVR 解 sysclk=300M TRACECLK=100M      [PASS]
[L6 fw]        RAM_D1 cache flag = 0xCACE                 [PASS]
[L6 fw]        UART banner FLAGS_STR = "rt-cache300M"     [PASS]
[L7 etm]       TRCPRGCTLR=1, TRCSTATR=0, TRCCONFIGR=1     [PASS]
[L7 etm]       CSTF ENS0=1, ETF Empty=0 (数据在流)         [PASS]
[L7 etm]       抓 8KB 样本 fsync>10, deframed>1KB         [PASS]
[L8 decode]    opencsd 解出 A-sync>10, INSTR_RANGE>100    [PASS]
[L8 decode]    verify_calls 调用边全对 ELF                  [PASS]

==== ALL GREEN — ready for 抓样/冲频/perf 导出 ====
```

**FAIL 时**：
```
[L4 phase] TPIU test-pattern err = 6.7%                   [FAIL]
  Diagnosis: clock IDELAY tap 相位漂移（AGENT.md 坑 #4）
  Details:   D0 err=6.7 D1=6.7 D2=6.7 D3=6.7 (齐同 → 时钟问题非某lane)
  Fix:       ./trace_doctor.py --fix tap_scan
             或手动: trace_ctrl.py set-tap-clk 20..28 各试一个
  Stop.  下游 L5-L8 未测。
```

### 2.2 分层探测清单

**每一层的具体探测和判据**：

#### L0 — 主机环境（不动硬件即可测）
- `pgrep openocd/openFPGALoader/hw_server` = 0，否则 `pkill` 建议
- `lsusb`: DAPLink `0d28:0204` + FT232H `0403:6014` 都在
- `/dev/ttyACM0` 存在且可读
- `test -r /mnt/hgfs/DESIGN/STM32_Project/H743_Blink/Makefile`
- `ip route get 192.168.10.42`：接口 dev 明确（ens33 或 USB 网卡）
- `ping 192.168.10.1`（本地网关）通，证明主机网络健康

#### L1 — 物理连接（openocd 探测）
- `openocd -c init -c "dap info"`：DPIDR = 0x6ba02477
- STM32 IDCODE = 0x20036450（H743/H750 = 0x450 family）
- **不进入 halt**（避免打断 STM32），只读 IDCODE

#### L2 — FPGA bit 加载
- UDP status read → DEPTH：
  - 63472 → clktap 4bit
  - 65535/0xFFFF → **未加载或 stale**（FAIL）
  - 32639 → pin_la
  - 0 → 刚烧 fresh 状态（下一步 rearm 后应变正常值）
- 计算当前烧的 bit 文件 md5，对照白名单表（`.trace_doctor.bit_db.json`）
- 建议：`.bit_db.json` 记录每个已知 bit 的 md5、顶层、TRACE_WIDTH、DEPTH 特征

#### L3 — 网络
- `ip neigh show`: FPGA MAC = `02:ca:fe:a7:7e:5c REACHABLE`
- 3 次连续 status UDP 全部 <100ms 返回（无 timeout）
- rearm → sleep 1s → status 再 3 次连续成功（rearm 不 break UDP）

#### L4 — 采集相位（voltmeter，绕开 STM32 侧数据流）
- 烧 STM32 侧 **TPIU test pattern (CURTPM=0x00020004)**（AA/55 强制模式）：
  ```
  openocd -c init -c halt -c "mww 0x5C015204 0x00020004" -c resume -c shutdown
  ```
- FPGA 抓 4096 字节：
  - 期望：所有 4 lane err < 1%, divergence < 2%（`hw_selftest.py --quick` 的判据）
  - err = 与理想 AA/55 pattern 的位翻转差异
- **同时读 FPGA TRACECLK 边沿计数器**（如有）→ 期望 == 预期 TRACECLK 频率（100MHz±5%）
- 结束后清 CURTPM=0
- **可选自动 tap 扫描修复**（--fix tap_scan）：扫 clk tap 0-31 找 err 最小值

#### L5 — STM32 debug 授权
- DEMCR (0xE000EDFC) bit24 TRCENA = 1
- DBGMCU_CR (0x5C001004): bits 20/21/22 = 111 (0x00700000 mask)
- TRCPDSR (0xE0041314) = 0x01（PowerOn + StickyPD clear）
- TRCAUTHSTATUS (0xE0041FB8) 期望 non-invasive debug = 11（bits[3:2]）
  - 若为 00，DebugMonitor 或 Secure 授权异常，需要检查 stlink/DAPLink 是否正确附加

#### L6 — 时钟 + 固件版本
- PLL1DIVR + PLLCKSELR：解码 sysclk / TRACECLK，报告数值
- RAM_D1 (0x24000000)：读 cache flag，报告是 `0x0000CACE`（enable）/`0`（disable）
- UART banner：读串口 5 秒，正则匹配 `Compiler flags\s*:\s*(\S+)`，报告 FLAGS_STR
- **固件 SHA**：ELF 的 md5 vs flash 读回镜像的 md5（proposal 38 教训）
- **CoreMark 分数抽样**：每档预期分数表（150M→611, 200M→815, 300M→1223, 400M→1631），
  实测分数在 ±5% 内则频率正确
- **DWT cycle count 校验**：跑固定 N 指令测实际频率（防 PLL 配错）

#### L7 — ETM 数据流
- TRCPRGCTLR = 1 (ETM enable)
- TRCSTATR = 0（trace 中，非 IDLE）
- TRCCONFIGR: 检查 BB/TS/CCI 位
- CSTF_CTRL bit0 ENS0 = 1
- ETF_MODE = 2 (HW FIFO), ETF_CTL = 1 (TraceCaptEn)
- **ETF_STS**：bit4 Empty = 0（有数据在流）；bit0 Full 若=1 报 overflow 警告
- TPIU_CURPSIZE：4bit=0x08 / 2bit=0x02 / 1bit=0x01

#### L8 — 端到端解码
- 抓 8KB 样本 → deframe → 期望：
  - fsync ≥ 10（TPIU 帧对齐 OK）
  - deframed ETM ≥ 1KB（非全填充）
  - opencsd 输出 A-syncs ≥ 5, INSTR_RANGE ≥ 100
- 可选：跑 `verify_calls.py`，报告 "call edges: N, mismatch: 0/N"

---

## 3. 工具实现建议

### 3.1 CLI 接口
```bash
trace_doctor.py                    # 逐层跑，遇 FAIL 停
trace_doctor.py --continue-on-fail # 全部跑完，输出总表
trace_doctor.py --layer L4         # 只跑指定层
trace_doctor.py --fix tap_scan     # 遇 L4 相位 FAIL 时自动扫 tap
trace_doctor.py --fix kill_procs   # 遇 L0 残留时自动清
trace_doctor.py --json out.json    # 机器可读输出，供 CI/AGENT 消费
trace_doctor.py --ci               # 无颜色/无 interactive，退出码非0即失败
```

### 3.2 关键组件
- **`.trace_doctor.bit_db.json`**：已知 bit 白名单
  ```json
  {"trace_iddr_clktap.bit": {"md5":"9831...", "depth":63472, "top":"trace_stream_top",
                             "cap_raw":1, "trace_width":4}, ...}
  ```
- **`.trace_doctor.fw_expect.json`**：固件档预期
  ```json
  {"rt-150M":  {"iter_min":580, "iter_max":640, "pll_sysclk_mhz":150},
   "rt-300M":  {"iter_min":1150, "iter_max":1300, "pll_sysclk_mhz":300}, ...}
  ```
- **复用现有工具**：
  - `hw_selftest.py quick` → L4 test pattern voltmeter
  - `trace_dump.py --status-only` → L2/L3 探测
  - `trace_ctrl.py set-tap-clk / rearm` → L4 修复
  - `opencsd_etm4_run.py` → L8
  - `verify_calls.py` → L8 深度验证

### 3.3 openocd 会话复用
**关键设计决策**：诊断中的 openocd 命令用 **一次 openocd + 多命令**（`-c ... -c ...
-c shutdown`）而不是每次新起。避免 SWD 反复 reconnect 开销和抢占。**绝不用 `reset halt` 除非
必要**（每次 reset 打断 STM32），改用 `halt` 保存 PC 后立即 resume 只读寄存器。

### 3.4 状态保存
- 每次跑输出到 `~/workpath/orbcode/.trace_doctor.log/<timestamp>.json`
- 支持 `--diff` 对比上次 vs 这次差异（"上次这一层是 PASS，本次 FAIL"）

---

## 4. 你可能漏掉的诊断维度（我加进来）

红方式补漏：

### 4.1 时序 / 环境
- **温度**：FPGA 长时间跑发热，采样眼可能收窄。加读 XADC 温度（Xilinx 7 系 System Monitor）。
- **SWCLK 频率**：openocd DAPLink 默认 SWCLK 可能过高/过低影响命令可靠性。报告当前值。
- **UART 波特率漂移**：读串口时若字节乱码，可能是 STM32 时钟漂移或波特率计算错。
  尝试其他波特率反馈。

### 4.2 版本 / 一致性
- **HAL 库版本 vs .ioc CubeMX 版本**：CubeMX regen 可能引入 HAL 差异。加校验。
- **arm-none-eabi-gcc 版本**：编译器换版本会改代码布局。ELF 里能读 producer 字段。
- **openocd 版本**：不同版本对 H7 CoreSight 支持不同。
- **opencsd/trc_pkt_lister 版本**：解码器版本影响 ETMv4 支持。

### 4.3 权限 / 隔离
- **`sudo` 无密码可用性**：某些操作（USB 网卡 IP 配置）需要 sudo。测一次 sudo -n 判断。
- **`/dev/ttyACM0` 群组**：非 plugdev 用户读串口会 EACCES。
- **FPGA JTAG 是否被 hw_server 占用**（Vivado xvcserver）。

### 4.4 干扰 / 竞争
- **其他 openocd 会话**：网络（gdb :3333）+ 本地（USB）双通道。
- **其他 UDP 5001 客户端**：多个 trace_dump 并发会互相打断。
- **VMware 独占 USB 设备**：设备在 host 侧被占用，VM 里看不到。

### 4.5 数据完整性交叉验证
- **同一 workload 重复 3 次 CoreMark 分数方差 < 0.5%**（防间歇性时钟毛刺）
- **同一 tap 3 次抓样 fsync 数方差 < 20%**（防间歇性相位漂移）
- **ETF Empty=0 时序**：resume 后 100ms/500ms/1s 三次采样，若一直 Empty 说明 ETM/CSTF 有断

### 4.6 兜底诊断
- **`--dump-all`**：无论 PASS/FAIL 都把所有关键寄存器 + 8KB 采样 + UART banner + `dmesg | tail`
  打包成 `trace_doctor_bundle_<ts>.tar.gz`，方便远程 debug 或红方复盘。

---

## 5. 分阶段落地

**阶段 1（P0，一天）**：L0-L4 五层，纯 shell + Python，复用 hw_selftest；只输出 PASS/FAIL
不自动修复。**目标：能快速定位过去两天的失效模式（相位漂移、bit 未加载、openocd 残留）**。

**阶段 2（P1，半天）**：L5-L8，读所有 CoreSight 寄存器 + 端到端 opencsd 解码 + verify_calls。
加 `.bit_db.json` 和 `.fw_expect.json` 白名单。

**阶段 3（P2，一天）**：`--fix` 自动修复选项（清进程、扫 tap、烧默认 bit）+ `--json` +
`--dump-all` bundle。

**阶段 4（可选）**：加**红方模式**：诊断走完给出的"绿"结论，让另一份脚本用**独立方法**
（比如 opencsd 走一遍 + orbetto 走一遍）验证一致性，防止诊断本身的盲区。

---

## 6. 反例（工具**不该**做的事）

- **不要自动 reset halt STM32**：打断真运行的 workload，改变 trace 内容。所有诊断优先用
  `halt→读→resume` 而非 `reset halt`。
- **不要重烧固件/bit** 除非明确 `--fix reflash`。诊断以只读为主。
- **不要长期占用 openocd 后台**：诊断跑完立即 shutdown，把 SWD 让出来给用户后续工具。
- **不要过度信任 grep 通过**：某个寄存器读回=0 时要区分"真的 0" vs "读失败"（openocd
  `mrw` 失败也返回 0）。判据用位模式而非"非零"。
- **不要把 STM32 的 CoreSight 错读**：读 `0xE00Fxxxx` 地址在 H7 上从 MEM-AP 返回 0，必须
  用 `0x5C0xxxxx` 系统总线别名（AGENT.md 已有此坑）。

---

## 7. 判据表汇总（工具查表用）

| 层 | 名字 | 期望 | 失败含义 |
|:---:|------|------|------|
| L0 | pgrep openocd | =0 | 有残留，SWD 占用 |
| L0 | lsusb 0d28:0204 | 存在 | DAPLink 掉线 |
| L0 | lsusb 0403:6014 | 存在 | FT232H 掉线 |
| L0 | /dev/ttyACM0 | 可读 | 串口权限或掉线 |
| L0 | ip route 192.168.10.42 | 有 route | 网络配置丢 |
| L1 | DPIDR | 0x6ba02477 | DAPLink→STM32 SWD 断 |
| L1 | STM32 IDCODE | 0x20036450 | STM32 未上电 |
| L2 | FPGA DEPTH | 63472 (clktap) | 烧了错 bit 或未加载 |
| L2 | bit md5 | 匹配白名单 | 未知 bit |
| L3 | ARP | REACHABLE | FPGA 网络断 |
| L3 | UDP status ×3 | 全成功 | UDP 不稳 |
| L4 | test pattern err | <1% | 物理通路故障 |
| L4 | TRACECLK 边沿 | 目标±5% | 时钟未输出或频率错 |
| L5 | DEMCR bit24 | 1 | CoreSight 未使能 |
| L5 | DBGMCU_CR bits20-22 | 111 | trace clock 未开 |
| L5 | TRCPDSR | 0x01 | ETM 未上电 |
| L5 | TRCAUTHSTATUS | non-inv=11 | debug 授权问题 |
| L6 | CoreMark 分数 | 表内±5% | PLL 配错或未生效 |
| L6 | RAM_D1 flag | 0xCACE / 0 | cache 状态与预期不符 |
| L6 | ELF vs flash md5 | 相同 | 解码用错 ELF |
| L7 | TRCPRGCTLR | 1 | ETM 未 enable |
| L7 | TRCSTATR | 0 | ETM IDLE (不 trace) |
| L7 | CSTF ENS0 | 1 | funnel 未打开 |
| L7 | ETF Empty | 0 | 无数据流入 ETF |
| L7 | ETF Full | 0 | overflow 警告 |
| L7 | CURTPM | 0 | TPIU test pattern 未清 |
| L8 | 抓样 fsync | ≥10/8KB | TPIU 未对齐 |
| L8 | deframed 字节 | ≥1KB/8KB | 全填充/无数据 |
| L8 | INSTR_RANGE | ≥100 | 解码失败 |
| L8 | verify_calls mismatch | 0 | 调用边错（解码 bug 或 ELF 不符）|

---

## 8. 一句话给未来的自己（和 agent）

**跑之前先跑 `trace_doctor`**。今天卡壳的每一分钟，都可以用它省下来。

---

## 补充（2026-07-25）：已存在的 FPGA 内置错误码机制（proposal 30 P1）

**关键发现**：proposal 30 已经把 FPGA 内置错误码/诊断寄存器**完整实现**了，
`rtl/dbg_regfile.v` + `scripts/fpga_health.py` 已在项目里可用。trace_doctor 应
**直接复用**，不重造。

### 9.1 已实现的错误码（`rtl/dbg_regfile.v` + `led_status.v` 定义，`fpga_health.py` 消费）

| 错误码 | 含义 | 触发源 | 定责 |
|:---:|------|------|------|
| `0x0000` | 无错误 | — | — |
| **`0x0101`** | **no TRACECLK edges** (GPIO 上无边沿) | 引脚级探针 | STM32 ETM 未启 / DBGMCU_CR TRACECLKEN 未开 / TRACE 引脚未 AF0 / 接线断 |
| **`0x0102`** | **trace MMCM lost lock**（有 TRACECLK 但采样 MMCM 失锁） | MMCM 时钟监视 | TRACECLK 频率与 bitstream 期望不符 / 频率抖动 / clktap 相位问题 |
| **`0x0301`** | **capture FIFO overflow** (ETM 生成率 > drain) | FIFO 溢出 | STM32 ETM 生成率过高（BB=1 高频） |
| **`0x0401`** | **self-TX HDR stuck (ARP deadlock)** | self-TX FSM 超时 | 主机未回 ARP，网络自发 TX 死锁（HANDOFF §7.1） |
| **`0x0501`** | RX bad frame（超阈） | MAC 侧 | 主机侧发的包 CRC 错 / PHY 信号完整性 |
| **`0x0502`** | TX FIFO overflow | MAC 侧 | 出方向反压异常 |
| **`0x0503`** | RX FIFO overflow | MAC 侧 | 主机灌包过快 |

**Sticky 语义**：只锁**第一次**发生的错误（`FIRST_ERR_CODE` 0xFF16），并记时间戳 `FIRST_ERR_TIME`（0xFF18）+ 现场 `FIRST_ERR_CTX`（0xFF1C）。清除方式=软复位（写 REG_SOFTRST 0x10）或断电重启。这一条特别关键——**如果只有零星错误，sticky 会把最早那次抓住，避免"跑了一晚上不知道哪一时刻坏的"**。

### 9.2 已实现的可观测寄存器（`fpga_health.py` 消费）

| 地址 | 名称 | 内容 | 用于 |
|:---:|------|------|------|
| `0xFF10` | `DBG_MAGIC` | 固定 `0xDB` | trace_doctor L2 认证：读回 0xDB 证明 dbg_regfile 在线（当前 bit 支持诊断） |
| `0xFF11` | `LIVE_STATUS` | `{mmcm_lock, tck_active, fsm bits, ...}` | live 状态 |
| `0xFF12-15` | `CYCLE` | free-run cycle counter (LE, 4B) | 时间戳基准 |
| `0xFF16-17` | `FIRST_ERR_CODE` | 首错 sticky 码 | **核心：一句话定责** |
| `0xFF18-1B` | `FIRST_ERR_TIME` | 首错发生的 cycle | 时序取证 |
| `0xFF1C` | `FIRST_ERR_CTX` | 首错现场快照 | 深度取证 |
| `0xFF20-26` | `ERR_COUNT[7]` | 各错误饱和计数 (8b each) | 频率分析：0x0301 累计 vs 一次 |
| `0xFF30` | `GPIO_LEVEL` | `{0,0,0,clk,d3,d2,d1,d0}` 原始电平快照 | **绕开 MMCM 直读引脚** |
| `0xFF31-3A` | `GPIO_EDGES` | TRACECK+TRACED0-3 各 16b 边沿计数 | **引脚翻转证明**（区分"引脚死"vs"MMCM 没锁"） |
| `0xFF3B-3D` | `TRACECLK_FREQ` | 16.777ms 窗口内 TRACECK 边沿数 | 实测 TRACECLK 频率 |
| `0xFF3E-41` | `GAP_COUNT / GAP_MAX` | TRACECLK 停顿 >8clk 的次数 + 最长 gap | 区分"频率错" vs "断续导致 MMCM flap" |
| `0xFF70-73` | `BUILD_ID` | 综合时打入的 Unix 时间戳 | **bit 身份识别**（AGENT.md 坑点：md5 白名单的强化） |

### 9.3 已实现的自愈机制

- **软复位**（写 `REG_SOFTRST` 0x10 到 :5002 CSR）：拉伸 256 clk125 复位采集前端 + 清 sticky 计数。`fpga_health.py <ip> reset` 一键。
- **看门狗**：TRACECLK 有活动但 MMCM 持续 ~134ms 未锁 → 自动脉冲软复位重锁。

### 9.4 关键 GAP：主力 clktap bit **没有** dbg_regfile

**扫遍 rtl/*.v 例化 dbg_regfile 的顶层**：
- ✅ `trace_mmcm_stream_top.v`（流式，proposal 25/26）
- ✅ `trace_ddr_blackbox_top.v`（DDR3 黑匣子，proposal 32）
- ✅ `trace_ddr_selftest_top.v`（DDR3 自检）
- ❌ **`trace_stream_top.v`（对应主力 `trace_iddr_clktap.bit`）——未例化 dbg_regfile**

这解释了今天下午跑 `fpga_health.py` 时看到：
```
[WARN] debug regfile magic = 0x00 (expected 0xDB). Old bitstream without proposal-30 observability? Falling back.
```

**这是 trace_doctor 设计的第一个必须补的 gap**。两条路：

**方案 A（推荐）**：把 `dbg_regfile` 接进 `trace_stream_top.v`。工作量小（几百 LUT+两条 wire），
主力 clktap bit 从此支持 sticky 首错 + 引脚级 GPIO 探针，这是**最有价值的一次投资**——
今天下午卡的每一个失效点，如果有 dbg_regfile 都能秒级定位：
- 相位漂移（`f7ff` 模式）→ `TRACECLK_FREQ` 显示频率对 + `GAP_COUNT>0` 直接给结论
- FPGA bit 加载失败 → `BUILD_ID` 一读就知
- 没 fsync（"trace 太稀"）→ `GPIO_EDGES` 显示引脚在翻转但计数不涨 → MMCM 侧问题

**方案 B（兜底）**：trace_doctor 检测 `DBG_MAGIC != 0xDB` → 走 fallback 逻辑（只依赖
`trace_dump --status-only` 的 DEPTH 特征 + `hw_selftest quick` 的 TPIU voltmeter）。
诊断力比 A 弱，但**当前主力 bit 立即可用**。

**建议实施顺序**：先做 P0（方案 B 兜底），能立刻用；同时并行做 A（把 dbg_regfile 加进 clktap 顶层重综合一个 clktap+dbg bit），一旦有新 bit 就升级到 A 的强诊断。

### 9.5 更新 §2.2 分层探测清单

在 **L2/L3** 层加：
- **L2.a**：读 `0xFF10 DBG_MAGIC`。若 = 0xDB → 后续走 dbg_regfile 强诊断路径；若 = 0x00 → fallback（现主力 bit）+ 报"当前 bit 未含 dbg_regfile，建议烧含诊断的顶层"。
- **L2.b**：读 `0xFF70 BUILD_ID`（若 magic 有），vs 白名单里最新期望值，报 build 是否为最新。

在 **L4** 层用 dbg_regfile 强化：
- **L4.a**：读 `0xFF31-3A GPIO_EDGES` 三次采样，判**引脚是否翻转**（区分接线断 vs 只是 MMCM 没锁）。
- **L4.b**：读 `0xFF3B-3D TRACECLK_FREQ`，与固件预期 TRACECLK（PLL 计算得到）对比 ±5%。
- **L4.c**：读 `0xFF3E-41 GAP_COUNT/MAX`，若 GAP_COUNT>0 且 MAX >>1 → **区分频率错 vs 断续**。
- **L4.d**：读 `0xFF16 FIRST_ERR_CODE`，若非 0 → 直接查错误码表报根因。

在 **L7** 层：
- **L7.a**：读 `0xFF22 ERR_COUNT[cap_overflow]` 累计溢出计数，作为 STM32 生成率超 drain 的**片上证据**（比 opencsd 的 I_OVERFLOW 更准，因为后者依赖流未破坏）。

### 9.6 判据表补丁（追加到 §7）

| 层 | 名字 | 期望 | 失败含义 |
|:---:|------|------|------|
| L2.a | DBG_MAGIC | 0xDB | 当前 bit 无诊断（走 fallback） |
| L2.b | BUILD_ID | ≥ 白名单最新 | bit 过期或未知 |
| L4.a | GPIO_EDGES 增长 | clk+d0-3 全部 >0 且随时间递增 | 某 lane 静止→接线断 / STM32 ETM 未启 |
| L4.b | TRACECLK_FREQ | 固件 PLL 计算值 ±5% | TRACECLK 频率异常 |
| L4.c | GAP_MAX | <8 clk 或 gap_count=0 | 断续 (STM32 TPIU 停发) |
| L4.d | FIRST_ERR_CODE | 0x0000 | 首错锁存，查表 |
| L7.a | ERR_COUNT[cap_overflow] | 0 或稳态 | 溢出频发→需 BB-OFF 削峰 |

---

## 修订总结

- **原设计（§1-8）** 是"外部探测"路线：主机 grep / openocd 读 CoreSight / opencsd 解码。
- **补充 §9** 引入"**片上诊断**"：dbg_regfile 已实现的 sticky 首错 + 引脚级 GPIO 探针 + 频率计 + gap 检测。
- **两者互补**：外部探测独立于 bit，任何 bit 都能测（L0/L1/L4-TPIU/L5-L8）；片上诊断**主力 bit 加了 dbg_regfile 之后**能秒级给出根因码，避免几小时的 tap 扫描/断电重启。
- **trace_doctor 实现优先级**：先跑外部探测（P0，工作量小），检测到 dbg_regfile 在线时自动切到片上诊断（强化 L2-L7）。
- **主力 clktap bit 的诊断能力升级**：作为独立 P0.5 任务——把 `dbg_regfile` 例化进 `trace_stream_top.v`，重综合一份 `trace_iddr_clktap_dbg.bit`，与现有 `trace_iddr_clktap.bit` 并存供选。

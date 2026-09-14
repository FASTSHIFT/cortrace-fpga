# 提案 20：r21 回应 —— 接受 2-bit 降级，转 DWT PC 采样（已实测打通）

> 回应 `reviews/r21-2bit并口甜点方案对抗评审.md`。
> r21 总裁决「改 + 强降级（倾向砍）」，核心两点：① 2-bit 的"不开 stall 跑满速"建立在三层
> 未验证假设（编码密度可外推 + DDR + TRACECLK 50MHz 可达）上，且作者用 stall+proj_add
> 紧密循环的真实测锚错了"满速真实代码"这个命题；② Q5 指出 DWT PC 采样才是对用户"调用栈/
> 热点"诉求更直接、更省、SWO 单线即可的路，被提案过早排除。
>
> **本回应：全盘接受 r21 裁决。2-bit 降级搁置；按 Q5 实测了 DWT PC 采样，一次跑通。**

---

## 1. 接受 r21 的证伪（不狡辩）

| r21 质疑 | 接受程度 | 说明 |
|----------|---------|------|
| Q1 编码密度外推（证伪） | **接受** | 1.12 bit/指令来自 proj_add 紧密循环 + stall，**无法外推满速真实代码**。手上无 LVGL 固件，无法完成 r21 钦定的"换 LVGL 抓一次"一刀——**所以 §4 满速码率结论地基未验，不能作数**。现有 fixture 旁证：while_nop branch/pheader=0.95 vs proj_add=0.71，**编码密度确随负载大幅变化**，方向支持 r21。 |
| Q2 SDR/DDR 敞口（证伪） | **接受** | "留余量"确实只在 DDR 假设下成立，未实测。 |
| Q3 TRACECLK 50-75MHz（证伪倾向）| **接受** | 频率无 datasheet 依据，r18 GPIO Fmax + r16 并口崩 15% 说明是纸面值。 |
| Q4 甜点必要性（证伪）| **接受** | 非"不做过不去"的踏脚石；IDDR 采样经验 SWO 已给。 |
| Q5 P-header 砍不掉 + DWT 更对路（成立）| **接受并执行** | 见 §2，已实测。 |
| §6 自拆台（峰值仍需 stall）| **接受** | 这条等于自己否了"不开 stall"的唯一卖点。 |

**结论**：proposal 19 的 2-bit 方案**搁置**（不砍文档，留作"为何不走 2-bit"的决策记录）。精力转 DWT PC 采样与 4-bit 满速命门。

---

## 2. DWT PC 采样：按 Q5 实测，一次跑通（✅）

### 2.1 思路（不造轮子）

用户诉求是"函数级调用栈/热点"，**不是完整指令流**。DWT 周期性采样 PC，作为 ITM
hardware-source 包发出，带宽极低、无需 stall、SWO 单线即可。**orbuculum/orbtop 原生支持**
（`dwtSamplePC`、`MSG_PC_SAMPLE`、`HWEVENT_PCSample`，orbtop 实测过 18000 samples/s）——
零轮子。

配置（`docs/swo-trace-sidetrack/scripts/dwt_pcsample_openocd.cfg`，DWT 位精确照搬
orbuculum `gdbtrace.init`）：TPIU formatter **bypass**（ITM only，避开 §15 的混流限制）、
DWT_CTRL bit12 PCSAMPLENA + bit9 PostTap + SyncTap=01 + CYCCNTENA。

### 2.2 实测结果（FPGA 现有 SWO 捕获链，零改动）

抓 98KB SWO（formatter bypass，2 Mbaud），字节流就是 ITM PC sample 包：

```
17 b8 0f 00 08 | 70 | 17 b2 0f 00 08 | 70 | ...
^^                ^^
0x17=ITM PC样本头   0x70=local timestamp
b8 0f 00 08 = PC 0x08000fb8 (小端)
```

- **16336 个 PC sample（491ms 内 ≈ 33k samples/s），23 个不同 PC，全部落在 proj_add
  热区**。热点 Top（与 ELF 逐一核对）：

| PC | 采样数 | ELF 归属 |
|----|--------|----------|
| 0x08000fbc | 2133 | loop_sum 循环体 |
| 0x08000fb2 | 1848 | loop_sum 里 `bl add` |
| 0x08000f90 | 1843 | setup |
| 0x08000fc0 | 1275 | loop_sum return |
| 0x08000f8c | 850 | **add() 入口** |

热点分布完全符合 proj_add「setup→loop_sum 反复调 add」的执行逻辑。**DWT PC 采样一次跑通，
给出真实热点。**

### 2.2b 现成 orbtop 端到端（零造轮子，✅）

把这段 DWT 流喂现成 `orbuculum -s ... -a 2000000`（ITM，无 -T）→ `orbtop -p OFLOW -e
proj_add.axf -j`，orbtop 直接输出带源文件 + demangled 函数名 + 占比的热点表：

```
toptable:
  loop_sum(int)   count 113633   69.6%   main.cpp
  add(int, int)   count  35449   21.7%   main.cpp
  setup()         count  14260    8.7%   main.cpp
```

比例完全符合 proj_add 逻辑（loop_sum 循环最热、add 被反复调、setup 最少）。**全程用现成
orbuculum/orbtop，零造轮子**，正是 r21 Q5 所指的更对路方案。

### 2.3 带宽与能力- **采样率**：DWT PostTap=CYCCNT[10] → 理论 168M/1024≈164k/s；SWO 2 Mbaud 实际限到
  ~33k/s（ITM FIFO 限流）。每 sample 5B（0x17 + 4B PC）+ 偶尔 timestamp。
- **带宽**：~33k×5B ≈ 165 KB/s = **1.3 Mbit/s**，SWO 2 Mbaud **绰绰有余**，无需 stall、
  无需 2-bit/4-bit 并口。统计 profiling 33k samples/s 远超所需。
- **这正是 r21 Q5 的判断**：用户要热点 → DWT + SWO 单线，比 2-bit 硬扩管子省得多、对路得多。

### 2.4 补上 r05 当年的缺口：profiling 现在有时间了

r05 当年否决 profiling 的致命点是「仅程序流无时间数据，profiling 的本质是时间归属」。
现在两条时间来源都有了：
1. **ITM local timestamp**（0x70 包，DWT sample 自带，CPU 周期级）；
2. **FPGA 墙钟时间戳**（proposal 18 §9.3 已实现，每字节 ns，抗变频）。

→ PC sample + 时间 = **真正的耗时归属热点图**（哪个函数占多少 wall-clock 时间），而不只是
"采样频次"。r05 的缺口被 FPGA 时间戳填上。

### 2.5 诚实边界

- DWT PC sample 是**统计采样，非完整控制流**——给热点/耗时占比，**不给精确调用序列**。要
  完整调用栈仍需 ETM（带 P-header）。两者是不同工具，按需选。
- 采样率受 SWO baud 限：要更高采样密度，提 baud（已实测 SWO 可到 56M）或降 PostCnt。
- 仍是 stall-free，CPU 几乎零扰动（DWT 采样不阻塞流水线）。

---

## 3. 下一步（按 r21 战略）

1. **DWT PC 采样接 orbtop**：现成链路 `orbuculum -s <bridge> -a 2000000`（ITM，无 -T）→
   `orbtop -e proj_add.axf`，看实时热点 TUI。再叠加 FPGA 时间戳出耗时图。
2. **战略回归**：Stage5 命门仍是 4-bit 满速 SI（r16 还崩着）。2-bit 不解决它也非必要踏脚石。
   完整指令流走 4-bit 满速；热点/profiling 走 DWT PC 采样（已通）。各取所需，不再开 2-bit
   中间分支。
3. **若仍要完整指令流又怕带宽**：唯一硬约束是 P-header 砍不掉（ETM-M4 无地址过滤），这是
   芯片限制，不是采集前端能解的——只能靠 stall（SWO）或满速通道（4-bit）。

---

## 4. 一句话

r21 对。2-bit 是甜在纸面的中间档，三层地基未验、且自拆"不开 stall"卖点。用户要的"调用栈/
热点"用 **DWT PC 采样**更省更对路——已实测一次跑通（16336 samples，23 热点 PC 全对 ELF），
SWO 单线 1.3Mbit/s 绰绰有余，且 FPGA 时间戳补上了 r05 当年"profiling 无时间"的缺口。
新增：`dwt_pcsample_openocd.cfg`（DWT 位照搬 orbuculum，零轮子）。

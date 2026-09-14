# Stage-4 · 源同步 IDDR 采集频率上限实测

> 日期：2026-07-18
> 方法：CURTPM AA/55 已知图案 + IDDR 源同步捕获（CAP_METHOD=IDDR, CAP_RAW=1）
> 频率控制：CPU halt + runtime 改 PLL1 DIVN1/DIVR1（`target/set_pll_n.cfg`，
>           不重烧 firmware）。频率用 FPGA 200MHz timebase 数字节率实测（无混叠）。

## ⚠️ 结论修正（红方自我证伪后）

**初版结论"197.8MHz @ 0.002% 干净"是 AA/55 图案下的乐观假象，已被 walking-bit
图案证伪。诚实结论见 §红方证伪。**

### 初版（AA/55，乐观下界）

用 CURTPM AA/55（4 条 data lane 同步翻转 → 字节恒为 0xA5/0x5A）测得：181-198MHz
全程 0.002% 误码。频率随 DIVN1 到 N=31（197.8MHz）达峰后回落 = STM32 PLL VCO 撞顶。

**但 AA/55 是最宽松的图案**：4 lane 同步，无 lane 间差异；且丢一对 nibble 后仍读
作 0xA5，误码判据根本测不出丢字节。所以 0.002% 只证明"物理链路能在 198MHz 传输
*同步方波*"，不证明能可靠采多-lane 高熵数据。

## 红方证伪：walking-bit 图案（lane 不同步，严格判据）

改用 CURTPM walking-1s（单 bit 在 4 lane 上循环右移 → 理想 nibble 序列
4,2,1,8,...，每个 nibble 单 bit、有方向）。判据：nibble 必须是单 bit 且严格遵循
旋转，能抓 lane skew（多 bit nibble）和丢/多 nibble（旋转断裂）。

| TRACECLK | AA/55 err | **walking-1s err** |
|---------:|----------:|-------------------:|
|  75 MHz | 0.002% | **2.40%** |
| 100 MHz | — | **2.70%** |
| 125 MHz | — | **2.55%** |
| 150 MHz | 0.002% | **2.64%** |
| 175 MHz | 0.002% | **2.81%** |
| 198 MHz | 0.002% | **2.87%** |

**walking 误码比 AA/55 高 ~1000 倍**，且从 75→198MHz 几乎平坦在 ~2.5%——**不随
频率上升**。这个平坦性很关键：如果是 SI/带宽到极限，误码应随频率陡升；平坦说明
这 2.5% 是**结构性固定误差**，不是频率相关的 SI 墙。

### 2.5% 的根因（诚实定位）

细分 75MHz walking 样本：
- 4 条 lane 单 bit 计数几乎完全均等（1/2/4/8 各 ~7900-8120）→ **无 stuck lane、
  无单 lane 偏弱，4 lane 物理都健康**
- multi-bit nibble 只有两种：`0xa`(lane 1,3) 和 `0x5`(lane 0,2)，各 ~370 次 →
  **结构化的隔-lane 混叠**，不是随机 skew
- 即"walking bit 跨 lane 移动的过渡瞬间，采样偶尔把相邻两个 nibble 的 bit 混进
  一个采样点"——是**跳变附近的采样相位/眼过渡问题**，所有全局 tap 下都残留 ~2.5%

### 诚实的采集能力结论

- **AA/55（同步方波）**：198MHz 干净 —— 物理链路带宽够
- **walking（每边沿换 lane 的高活跃图案）**：全频段 ~2.5% 结构性误差 —— 当前
  **单一全局 IDELAY tap + 这套 CDC** 对高活跃多-lane 图案有固定的过渡态采样误差
- 真实 ETM 数据介于两者之间（高熵但非每边沿全换），实测（doc 07 §9.2）也是
  "inter-sync 段零星 bit 错"——与这里的 2.5% 结构性误差一致

**所以采集频率上限不能只报 AA/55 的 198MHz。诚实说法：物理带宽 ≥198MHz，但
多-lane 逐字节零错还没解决（~2.5% 结构性误差），需要 per-lane IDELAY 校准或
改进过渡态采样。终极判据仍是真 ETM + orbmortem 解码正确率。**

## 数据

频率扫描（DIVR1=0，VCO=TRACECLK，每档扫 IDELAY tap 取最佳）：

| DIVN1 | TRACECLK | best-tap err | 眼宽(#good tap) |
|------:|---------:|-------------:|:---------------:|
| 28 | 181.3 MHz | 0.002% | 7/9 |
| 29 | 187.5 MHz | 0.002% | 6/9 |
| 30 | 193.8 MHz | 0.002% | 7/9 |
| **31** | **197.8 MHz** | **0.002%** | **7/9** ← 峰值 |
| 32 | 193.7 MHz | 0.002% | 5/9 | ← 频率回落=VCO撞顶 |
| 33 | 187.5 MHz | 0.002% | 5/9 |
| 34 | 181.2 MHz | 0.002% | 5/9 |

眼图闭合观测（更早的粗扫，150MHz 时 tap 0-16 干净、tap 20+ 崩）：随频率升高
可用 tap 数减少，是眼在收窄的物理指纹；但即便到 198MHz 仍有 7/9 tap 干净，说明
眼还没闭到危险程度。

## 方法学要点

1. **频率测量用 FPGA timebase 字节率，不用边沿计数**：IDDR raw 每 TRACECLK
   周期产 1 字节，200MHz 计数器数字节数 / 时间 = TRACECLK，无混叠。早先 pin-LA
   400MSPS 把 75MHz 误测成 66.67MHz、把 150MHz 误测成 100MHz —— 那是异步过采样
   的混叠假象，timebase 法根治。
2. **runtime 改频靠 CPU halt**：之前 runtime 改 DIVR1 不可靠是因为 func_test
   firmware 的 SysTick/HAL 会重配 RCC。CURTPM 图案由 TPIU 硬件发，CPU halt 照发，
   halt 下 firmware 不干预 RCC，改频稳定可复现。
3. **误码判据用已知图案**：CURTPM AA/55 每字节必为 0xA5/0x5A，不依赖 TPIU sync
   帧（避开 r23 Q3/Q5 满载失效死穴）。

## 工具

- `target/set_pll_n.cfg` / `set_divr1.cfg`：CPU halt 下改 VCO/分频 + 重启 CURTPM
- `scripts/freq_ceiling_sweep.py`：扫 N × tap，测频率+误码+眼宽，报上限
- `scripts/iddr_tap_sweep.py`：单频率下扫 tap 找眼心

## 后续（如需突破 198MHz）

要测更高频率需重配 PLL1 输入分频 M / RGE 让 VCO 能上更高（>198MHz），或换更快
的 PLL 源。但这属于"造更快的信号源"，不是"测采集上限"——采集侧已证明 198MHz
仍游刃有余。真正要压 FPGA 采集极限，需要一个能发 >200MHz 干净 DDR 的信号源。

## 终极判据：真 ETM + OpenCSD 解码（进行中）

按红方建议上真 ETM 做无法自欺的验证。用 `build_h743.sh` 自编译 firmware（ELF
与 flash 严格匹配，避免地址错配），`etm_enable_h743.cfg` 启 ETMv4 + 清 CURTPM，
IDDR raw 抓 → `opencsd_etm4_run.py`（OpenCSD trc_pkt_lister）解码。

### 现状（诚实）

- **真 ETM 数据确实抓到**：201-224 个不同字节值，大量 TPIU HSYNC(0xFF/0x7F) +
  高熵 ETMv4 包；assemble 后有 FSYNC(0xFFFFFF7F)（48M ~10 个，12M ~36 个）。
- **解码器能起步**：锁到 A-sync，解出 I_ATOM_F1/F2/F3、I_ADDR_S_IS1、I_EXCEPT
  等合法 ETMv4 包，48M 有 318 个 I_ADDR 包、12M 有 2724 个。
- **但 unique PC = 0**：解出的地址被污染（如 0xF71D0472，高位是流里的高熵字节），
  没有一个落在 flash（0x080xxxxx）。

### 根因定位：字节/nibble 对齐滑移（不是 SI，不是频率）

- **降频到 12MHz（SI 余量极大）表现和 48M 一样烂** → 排除高速 SI / bit 错为主因。
- deframe 流里真 A-sync（11零+0x80）后**本该紧跟 Trace-Info(0x01)，实测中间隔着
  一个 stray byte**（0xf7/0xf6/0xdb），且滑移不一致（有的缺 0x80、有的多字节）。
  这正是 `opencsd_etm4_run.fix_async_alignment` 注释描述的"1 字节相位滑移"，但该
  函数是死代码（主流程未调用）且模式匹配太窄，只能修 1 个。
- **IDDR raw 字节边界其实是对的**（一度误判）：TPIU HSYNC 在 raw 里表现为
  `ff 7f ff 7f...` 交替（这是 TPIU 半同步字的正常字节形态，不是连续 0xFF），
  且 raw 直接 `has_tpiu_sync=True`、含 36 个 FSYNC(0xFFFFFF7F)。所以 **不该再做
  nibble assemble**——`recover_assemble` 把已对的字节又拆 nibble 重组反而搞乱。
  直接把 raw 当 TPIU 字节流 deframe 才对。
- 但**直接 deframe 后 A-sync 仍不跟 0x01**（trace-info-after=0）。字节边界对了，
  问题下沉到 **TPIU formatter 帧解析**（`tpiu_deframe_walk`）：16 字节帧的
  stream-ID / aux-bit 交织提取有偏移，导致 deframe 后的 ETM 字节流错位。那 2 个
  A-sync 很可能是数据里的巧合零串，非真同步点。

### 结论与下一步

真 ETM 端到端**尚未跑通**，但逐层排除后定位清楚：
- 采集/SI/频率：**排除**（12M 与 48M 同样表现；raw 有 FSYNC；字节边界正确）
- firmware/ELF 匹配：**已解决**（自编译，flash 与 ELF 严格一致）
- **TPIU formatter deframe 错位**：`tpiu_deframe_walk` 对这个 16-字节帧流的
  stream-ID/aux 提取有偏移，是当前唯一未通的环节。

下一步（独立的解码链工程）：
1. 核对 TPIU formatter 帧格式（IHI0029 §D4：16 字节 = 交织的数据字节 + 每偶字节
   LSB 的 ID/data 标志 + 末字节 aux）与 `tpiu_deframe_walk` 实现，修正帧内提取偏移。
2. deframe 正确后 A-sync 应自然紧跟 0x01，解码器落出 flash PC。
3. 再谈"真 ETM 在多高 TRACECLK 下解码正确率达标"——那才是有意义的采集上限。

**可复现资产**：`perf/firmware/{etmtest,tclk12}/`（自编译，ELF 匹配 flash），
`/tmp/etm12.bin`（12M 真 ETM raw，raw 直接 deframe：36 FSYNC / 20218 ETM 字节）。

## 更新：真 ETM 端到端跑通（BB=1，解出真实 func_test PC）

### 先证 CPU 真在跑 func_test（排除"程序没起来"）

DAPLink 多次 halt 采 PC：0x08000442→444→4d0→4ec→504，全部精确命中 func_test
用户函数（level_b / dispatch_callback / callback_test / factorial）。**CPU 正常
执行 func_test**，ETM trace 的是真实执行流。附带发现：func_test 代码极紧凑
（0x08000414-0x08000578，160 字节），几乎全是短距离直接调用/分支。

### 关键：BB（Branch Broadcast）决定能否解码

- **BB=0**（低 TRACECLK 默认）：直接分支不发地址包，紧凑代码下地址锚点极稀疏，
  OpenCSD 无法定位 → unique PC=0（之前一直卡这）。
- **BB=1**（每分支发地址）：地址锚点大增，**端到端跑通**：
  - deframed 27206 B, A-sync=9, INSTR_RANGE=3, **unique PC=6, 6/6 落 flash(100%)**
  - **解出真实函数**：`factorial`（func_test 用户函数）、HAL_RCC_OscConfig
  - Idx:7599 `exec range=0x8000512:[0x8000516] ISA=T32 iBR ret` = factorial 递归
    返回的真实 Thumb 指令流 —— **内容真实且正确**。

**这是整条链第一次端到端跑通**：源同步 IDDR 采集 → IDDR raw → TPIU deframe →
OpenCSD → 真实落 flash 的 PC + 命中 func_test 函数。之前的 0 PC 不是链路坏，是
BB=0 锚点太稀疏。

### 仍存在的限制（诚实）

- 解码率低（6 PC / 2 函数，远非全覆盖）：那个字节滑移（A-sync 后 0x80/0x01 时多
  时少一字节，trace-info-after 仍 0）仍在拖累，且偶有误解码（如 ISA=A32，M7 上
  不存在 → 假范围）。
- 根因仍是 PC 端 `tpiu_deframe_walk` 在高 HSYNC 密度流（30%）上的帧对齐滑移，
  或 IDDR raw 导出 CDC 的偶发字节滑移 —— 需进一步用 orbuculum 官方 tpiuDecoder
  对照区分。

**结论**：真 ETM 端到端**已跑通并解出真实 PC**（推翻"没跑通"），但解码率受字节滑移
限制。这是 PC 端解码链的完善问题，不是采集/SI/频率问题。

## 真 ETM 彻底跑通：官方 TPIU deframer 修好 → 781 PC / 13-14 func_test 函数

低解码率的根因找到了：**我们自写的 Python `tpiu_deframe_walk` 逐字节扫 HSYNC
(0xFF 0x7F)，在本流 ~30% HSYNC 密度下、当 HSYNC 落在奇字节偏移时会丢 16-bit 帧
相位**。对比 orbuculum 官方 `Src/tpiuDecoder.c`：它按 **16-bit 对（got_lowbits）**
收集字节、只在对边界过滤 HSYNC，永不丢帧相位；并正确处理 padding(stream 0 丢弃)
和 delayed-stream-change。

把官方逻辑忠实移植为 `decode/tpiu_official.py`，同一份 BB=1 抓样：

| 指标 | 自写 walk | **官方移植** |
|------|----------:|-------------:|
| A-sync | 9 | **72** |
| trace-info-after(0x01) | 0 | **57** |
| unique PC | 6 | **781** |
| PC 落 flash | 6/6 | **781/781 (100%)** |
| 覆盖函数 | 2 | **46** |
| func_test 用户函数 | 1/14 | **13/14**（仅缺 conditional）|

**真 ETM 端到端彻底跑通**：STM32 源同步采集 → IDDR raw → 官方 TPIU deframe →
OpenCSD → 重建真实 func_test 执行流（main_loop/callback_test/indirect_caller/
mixed_test/factorial... 13/14）。缺的 conditional 很可能只是该 trace 窗口未执行到。

已把官方 deframer 接入 `opencsd_etm4_run.py`（`--deframer official` 默认），从 raw
一步出 781 PC。

### 最终定位（全链诚实结论）

真 ETM 解码失败的根因**从来不是 FPGA 采集 / SI / 频率**，逐层证明：
1. CPU 确在跑 func_test（DAPLink PC 采样命中用户函数）
2. 采集/SI/频率排除（12M=48M；raw 有 FSYNC；字节边界对；walking 证 SI 结构误差
   与频率无关）
3. BB=0 锚点饥饿（紧凑代码直接分支不发地址）→ BB=1 解决
4. **PC 端 Python deframe 的 HSYNC 相位 bug** → 官方 16-bit 对齐移植解决

采集侧结论不变：源同步 IDDR 物理带宽 ≥198MHz（AA/55），多-lane 有 ~2.5% 结构
误差；而**真实 ETM 在 12MHz + BB=1 下已能重建 13/14 func_test 函数**。下一步可
在更高 TRACECLK 下复测真 ETM 解码正确率，得到"有意义的采集上限"。

## ⚠️ 严格判据修正：字节流 NOT 100% 匹配（逐指令核对）

"13/14 函数 / 781 PC / 100% 落 flash" 是**虚荣指标**（红方 r25 批过）：函数覆盖是
集合去重、"落 flash"只说明地址在范围内，都不能证明执行流逐条正确。

按严格判据（每个解码事件是否合法）重新量化同一份 BB=1 / 12MHz 解码：

| 信号 | 数量 |
|------|-----:|
| INSTR_RANGE（执行区间） | 1852 |
| **RESERVED（非法包头）** | **547** |
| BAD_SEQUENCE | 27 |
| NOT_SYNC | 19 |
| TRACE_ON（重锁次数） | 141 |

- **非法解码事件 = 24.3%**（593 错误事件 / 2445 总事件）。**绝非 100%。**
- 模式：长段正确 + 周期性脱轨。最长连续 123 条指令区间无错，中位数 28 条，
  之后撞上一个字节错误 → 脱轨 → 靠下一个 A-sync 重锁（141 次 TRACE_ON）。
- 这与物理层 walking 测得的 ~2.5% nibble 结构误差自洽：一个 ETMv4 包几字节，
  2.5% 字节错 ≈ 每 30-40 条指令区间遇一次错 → 正好对上 clean-run 中位数 28。

### 最终瓶颈定位

字节流不到 100% 的根因**不是解码链**（那已修好），是**采集侧那 ~2.5% 的 nibble
结构误差**——walking 图案测出的、与频率无关的 `0xa(lane1,3)/0x5(lane0,2)` 隔-lane
混叠，发生在数据跨 lane 跳变的过渡瞬间。**当前 4 条 data lane 共用一个全局
IDELAY tap，补不掉 lane 间的相对偏斜**。

要逼近 100% 逐指令匹配，下一步是 **per-lane 独立 IDELAY tap 校准**（每条 lane 单独
扫眼心），而非共用一个 tap。这是采集侧的收尾工作。

## 2.5% 的谜底：架构级根因 = TRACECLK 占空比 + IDDR 下降沿采样窗口

用户直觉"2.5% 顽固、反复出现、可能是架构问题"完全正确。逐层逼近后锁死根因：

### 决定性证据链（全部实测，walking-1s @12MHz）

1. **误差全在下降沿**：FALL nibble 错误 3.4-3.9%，RISE nibble 只 0.6% —— **fall 比
   rise 差 ~5.5×**。完全的边沿不对称。
2. **错误值 100% 确定**：FALL 错误恒为 `0x5`，RISE 错误恒为 `0xa`。非随机。
3. **对 IDELAY tap 完全不敏感**：全 32 tap 扫，FALL 错误纹丝不动在 ~3.4%（移数据
   ±2.4ns 毫无影响）→ **排除采样相位/眼图/SI/飞线 skew**（那些必随 tap 变）。
4. **周期性**：错误每 6 字节一次（walking 周期 2 字节的 3 倍）。
5. **上下文叠加**：时间序 `R8 F5 R2`，此处 fall 应为 `F1`，实测 `F5 = F1|F4` ——
   **fall 采样抓到了当前 fall 值与相邻 fall 值的 OR 叠加**（采到正在切换的中间态）。

### 根因

TPIU 是 **edge-aligned**（数据在 TRACECLK 边沿翻转）。我们的 IDDR 直接在 TRACECLK
双沿采 = **采在数据跳变点**。上升沿采样(Q1)勉强够 setup；但**下降沿采样(Q2)的
半位窗口被 TRACECLK 占空比失真压窄**（STM32 出来经飞线+IBUF，占空比非精确 50%，
fall 沿离 rise 沿更近），fall 数据还没稳定就被采 → 系统性抓到过渡态叠加。

这不是模拟 SI（对 tap 不敏感证明），是**数字采样窗口 + 时钟占空比**的架构矛盾：
- **IDDR 源同步**：能上高频，但只能边沿采（=跳变点），fall 窗口受占空比压缩 →
  ~2.5-3.4% floor，与频率/tap 无关。
- **OVERSAMPLE 眼中心采**：采样点对，但边沿检测在高频失效（LOCKOUT）→ 只能低频。
- 两条现有路各有死穴，这就是 2.5% 跨 pin-LA / walking / 真 ETM 都顽固的本质。
- 历史印证（doc 14 §21-22）：早就发现"falling 值系统性偏向 rising、falling 采样
  踩在跳变沿采到过渡/上一拍数据"，且 §21.5 单 lane 下降沿采样率只有上升沿的 1/3。

### 解法（架构级，按可行性）

1. **移数据 1/4 UI 而非移边沿**：IDELAY 只能移整体延迟，改变不了 fall 相对 rise 的
   半位不对称（实测 tap 无效）。真正要的是让 **rise/fall 采样点各自落在自己半位
   中心**。IDDR 单一延迟做不到（rise/fall 共享 IDELAY），需要：
2. **MMCM 生成相移采样时钟**（proposal 22/26 的方向）：用一个相对 TRACECLK 移相
   90° 的时钟驱动 IDDR C，把双沿采样点整体挪到两个半位中心。这才是 Xilinx 源同步
   edge-aligned 接收的标准解（对应 ECP5 IDDRX1F 的固有偏移）。代价：MMCM 要锁
   TRACECLK，gap 时会失锁（与"停走时钟"需求冲突，需权衡）。
3. **ISERDES 过采样**：IOB 内 1.2-1.4GSPS 过采，软件挑眼中心，彻底绕开占空比。
   工程量最大。

**结论**：2.5% 是 edge-aligned DDR 在"边沿采 + 占空比失真"下的架构下限，IDELAY tap
治不了。要清零需相移采样时钟(MMCM)或过采样(ISERDES)。这是采集架构的下一个大改，
不是参数调整。


## ⚠️⚠️ 撤回"占空比根因"（红方 r26 证伪 + 我的仿真复核）

红方 `reviews/r26-源同步IDDR占空比根因-红方证伪.md` 把上一节的"占空比根因"证伪，
我复核后**接受，并撤回"锁死根因"的措辞**。诚实把握度：占空比根因 ~30%。

**红方成立的三条**：
- R1：IDDR 分支 duty 统计硬置 0，**占空比从未被实测**，纯推断。
- R2：把 g_iddr 的 CDC verbatim 抄进 iverilog，理想 50% 占空比 + 干净数据，12MHz
  误码 0.00% → 占空比不是复现 2.5% 的必要条件。
- R3：`0x5=0x1|0x4` 是两值按位 OR（位撕裂签名，非模拟边沿抖动）；"对 tap 不敏感"
  指向 IDELAY 下游的数字 CDC，不是占空比。我把反证读成了正证。

**但我的进一步仿真也未坐实红方的 CDC 撕裂假设（R4）**：
- 写了 `tb_iddr_cdc_phase.v`：忠实建模 edge-aligned 数据 + 真实双沿 IDDR
  (SAME_EDGE_PIPELINED 流水) + verbatim CDC，扫 trace_clk 相位 0-5000ps。
- 结果：理想 50% 占空比下 **lo5=0 hia=0，不复现 0x5/0xa**（仅 2/48 启动瞬态）。
- → R4 的"CDC 数据/选通差一拍"在理想模型下也不产生 0x5/0xa。

**真实数据的最硬实测（/tmp/wt.bin，IDDR 路径 walking @12M）**：
- 错误字节形如 `18 42 [58] 42 18`：正常 `0x18` 处变 `0x58`，**rise nibble(8) 完全
  正确、前后字节(42)完全正确，只有该 fall nibble 从 1 变 5 = 1|4**。
- 即**单个 fall 采样点多亮了一条 lane（lane2 泄漏）**，= 当前 fall 值 OR 前一个 fall
  值。不是整字节撕裂（前后字节都对），是**单 nibble 单沿的 lane 泄漏**。

**诚实现状**：现象是硬的（fall-only、0x1|0x4 OR、rise/相邻字节都对、tap 无关），但
**占空比假设和 CDC 撕裂假设都未能在仿真复现它**。真实机制未定。停止猜测。

### 采纳红方 P0，下一步（先测再断）

1. **实测 TRACECLK 占空比**：编 OVERSAMPLE 版 trace_stream（该分支自带 duty 统计），
   同一 CURTPM walking 抓，读 duty_hi/lo，直接算占空比。同时得到**完全不同采集
   路径（眼中心采）的 walking 误码**做对照——若 OVERSAMPLE 也出 0x5/0xa，则排除
   IDDR 边沿采样；若不出，才支持边沿采样类假设。
2. **fall 误码率 vs 频率曲线**（R5）：平坦→证伪窗口压窄；单调恶化→支持。
3. 只有实测确认后才谈 MMCM/ISERDES（R7 成本倒挂）。


## 高频(100MHz)重测 + 统计严谨化：tap 无效是抓样噪声，误码随机抖动

按 r26/护栏要求，在 IDELAY 有分辨力的频率(100MHz，half-bit 5ns，authority 48%)
重做 tap 扫描（低频扫 tap 全程无意义，已被护栏拦截）。

**第一轮（单次抓样，每 tap 一次）**：错误随 tap 剧烈变化 0.51%-3.67%，且**fall/rise
不再是低频的 5.5× 不对称，而是几乎相等**（fall 略低于 rise）。一度以为"tap 有效
→ 相位/眼图问题"。

**但统计复核推翻了它**：固定 tap 各重复抓 8 次：

| tap | 8 次 multibit% |
|----:|----------------|
| 8  | 2.80 2.46 0.90 1.70 2.55 2.38 2.66 0.69 |
| 10 | 1.08 2.92 2.16 3.05 1.95 2.51 2.74 3.31 |
| 18 | 1.46 1.78 1.76 1.04 1.50 3.17 1.70 2.80 |

**三个 tap 的分布完全重叠，同一 tap 的方差(0.69↔2.80)比 tap 之间的差异还大。**
第一轮那个"随 tap 变化"根本是**单次抓样噪声**——同 tap 重抓就能从 0.7% 跳到 2.8%。

### 修正后的诚实观测（统计严谨）

100MHz 下误码是：**均值 ~2%、每次 rearm 剧烈随机抖动、与 IDELAY tap 无关、
fall/rise 对称**。

这个特征组合：
- **随机 + 每次 rearm 不同** → 不是稳态相位偏移（那样同 tap 应稳定重复）
- **与 tap 无关**（这次 IDELAY 有 authority，无关才有意义）→ 故障在 IDELAY 下游
- **fall/rise 对称**（不同于低频的 fall 独坏）→ 不是占空比
- → **重新指向 CDC 亚稳态 / 字节撕裂 / arm 时刻随机相位锁定**（红方 R4 方向，在这个
  随机性特征下重新变强）

### 方法学教训（已固化为护栏）

我反复在少量抓样上过早下结论。"tap 随位置变化"两次都被单次噪声骗了。**必须每点
多次重复取统计**才能区分真效应与噪声。低频扫 tap 更是双重无效（IDELAY 无 authority
+ 单次噪声）。sampling_guard.py 已拦低频；统计复核纪律需贯彻。

### 下一步：SELFTEST 一锤定音

要把"随机 2% 抖动"定位到 FPGA 内部(CDC) vs 物理层，用 `SELFTEST=1`（trace_capture_a7
内部生成已知图案，绕开物理引脚/IDELAY/SI）。若 SELFTEST 下仍有此随机抖动 →
纯 FPGA 内部(CDC)，与 TRACECLK/飞线/SI 全无关，彻底定位到 R4 的 CDC 交接。


## CDC 仿真定位：纯逻辑零 skew 不复现，但当前 CDC 设计本就不严谨

现有 SELFTEST 注入点**只在 OVERSAMPLE 分支**（`os_clk_src/os_data_src`），IDDR 分支
没接，且生成器仅 ~1MHz——无法直接测 IDDR CDC。改走 iverilog 离线定位。

**tb_iddr_cdc_async.v（零人为 skew + 真异步 + 真实双沿 IDDR + edge-aligned data）**：
- 100MHz(=ref/2, toggle CDC 最坏情况)扫遍初始相位：**bad≈0，0x5=0 0xa=0**。
- 93MHz(非 ref/2 真异步)扫相位：同样不复现。
- → **纯 CDC 逻辑在零 skew 下不产生 0x5/0xa**，即使最坏 ref/2。

**tb_iddr_cdc_sweep.v（红方版，注入 per-bit skew）**：撕裂**高度依赖精确频率比/相位**
——红方报告的 48MHz 撕裂在 100MHz 整除 ref/2 时反而消失（相位锁定）。skew 0-300ps @
100MHz 全 0%。说明"CDC+skew 撕裂"存在但对工况极敏感，不是普遍解释。

### 诚实的把握度与判断

- 占空比根因：**基本排除**（100M fall/rise 对称）。
- 纯 CDC 逻辑撕裂：**零 skew 仿真不复现**；靠真实布线 bit-skew 可能在特定工况撕裂。
- 把握度：CDC 字节交接不严谨是**最可能方向(~55%)**，但**尚无稳定复现硬件 2% 的仿真**。

**关键工程判断（不必等 100% 坐实机制）**：当前 IDDR 导出 CDC = `tclk_byte` 组合更新
的 8-bit 裸总线，被 ref_200m 域用"数据链 2 级 / 选通链 3 级"直接采——**没有任何原子性
保证**（红方 R4）。无论撕裂机制细节如何，这都是不严谨设计。正确做法是用**已验证的
异步 FIFO（格雷码指针，la_ddr_writer 里 CI 测过的 axis_async_fifo）做原子字节交接**。

### 下一步：修 CDC 再上板（"修了再看"胜过继续猜）

把 IDDR 分支的裸总线 toggle-CDC 换成 axis_async_fifo 原子交接，重编上板测 100M walking：
- 若 2% 随机错**消失** → 机制坐实为 CDC 字节交接，问题解决。
- 若**仍在** → 排除 CDC，指向物理层（真实 SI / IDDR Q2 建立时间），再上 MMCM 相移/ISERDES。
这比继续在仿真参数空间穷举更有产出。


## ✅ 根因坐实 + 修复：2.5% = 我的裸总线 CDC 撕裂，换 async FIFO 后 walking 归零

把 IDDR 分支的裸总线 toggle-CDC 换成经 CI 验证的 **axis_async_fifo（格雷码指针原子
交接）**，100MHz walking，关键 tap 各重复 8 次：

| tap | 旧裸总线 CDC（8次） | **新 async FIFO（8次）** |
|----:|--------------------|--------------------------|
| 8  | 0.69–2.80% 随机 | **0.00 ×8** |
| 10 | 1.08–3.31% 随机 | **0.00 ×8** |
| 18 | 1.04–3.17% 随机 | **0.00 ×8** |

严格 walk_score（rotation 判据）：**0.0000%**，零 multibit、零 rotation break。

**根因一锤定音**：那顽固的 2.5% **是我自己写的 CDC bug**——`tclk_byte` 8-bit 裸总线
跨 trace_clk→ref_200m 非原子交接，ref 采样撞在发射窗口就把相邻周期的 bit 混进一个
字节（`0x5=0x1|0x4` OR 撕裂）。**不是占空比、不是 SI、不是飞线、不是 IDDR 采样相位**
——全是我之前的错误猜测。红方 r26 R4 方向完全正确。所有诡异现象都被解释：tap 无关
（CDC 在 IDELAY 下游）、随机每次不同（异步交接相位随机）、0x5/0xa OR 叠加（撕裂签名）、
跨场景顽固（只要用这 CDC 就有）。

### 遗留：FIFO 版真 ETM 有字节对齐 + 数据完整性问题（未解，需系统排查）

FIFO 版抓真 ETM 出现两个症状，host 端变换都是碰运气（已放弃）：
- HSYNC 从旧版的 `ff 7f`(60个) 变成 `f7 ff`(17409个)——既有**半 nibble 边界偏移**，
  又有**数量暴增 290×**。
- F0/00 标定：FIFO 版字节 = 纯 `0x0F`（干净单值，说明采样本身没坏）。
- 直接解 / nibble-swap / nibble-offset 重组 都解不出真 ETM（要么 0 FSYNC，要么凑出
  假 FSYNC 但帧内是垃圾）。

**关键矛盾**：同一 firmware 同样 ETM 流，HSYNC 密度不该比旧版差 290×。这指向 FIFO 版
可能**丢了大量真实 ETM 突发数据、只剩 HSYNC 填充**——walking（连续恒定速率）完美，
但 ETM（突发 + STALL）可能触发了 FIFO write 端的问题（`tclk_push` 从首拍即 =1 推入复位
首字节造成偏移；或突发时序）。

**诚实状态**：CDC 换 async FIFO 让 walking 从 2.5% 随机降到 0.0000%（根因坐实、修复
确凿）；但 FIFO 版对真实 ETM 突发流的字节对齐 + 完整性尚未跑通，需要系统排查 write
端 push 时序，不能靠 host 变换硬凑。这是明确的下一步 RTL 工作，不是又一个猜测。

### 用已知字节流解耦（用户建议：先和 ETM 解耦，缩短链条）

同时改 CDC + 跳真 ETM 是两个变量纠缠。用 CURTPM 已知连续图案把链条缩到
`CURTPM → FIFO CDC → raw`，逐一排查：

1. **FIFO CDC 不丢连续数据**（决定性）：48M walking（与真 ETM 同频）walk_score =
   rotation break 2/122880 = 0.002%（边界瞬态）。100M walking = 0.0000%。**连续
   已知流下 FIFO 一个字节都不丢** → 290× HSYNC 暴增 **不是** CDC 丢数据造成的。

2. **290× HSYNC 暴增 = 频率导致的填充，不是 bug**：旧版 12M ETM data-ish 87.1%
   (0xff=179)；FIFO 版 48M data-ish 27.9%(0xff=26855)。M7 产生的 ETM 数据率不变，
   TRACECLK 从 12M 提到 48M（快 4×），单位捕获里 TPIU 的 HSYNC 填充自然多几倍。
   两次是不同频率的会话，非同类比较 → HSYNC 差异**大部分是频率的正常结果**。

3. **仍存在的真问题 = 半 nibble 字节偏移**：FIFO 版 HSYNC 是 `f7 ff`，旧版是
   `ff 7f`——差半个 nibble，导致 `has_tpiu_sync=False`。这是 FIFO 与旧 toggle-CDC 的
   确定性字节相位差，walking 对称看不出，真 ETM 的 TPIU 帧暴露。**这个必须 RTL 修**
   （调 `{iddr_b,iddr_a}` 配对/顺序），host 端变换是碰运气（已放弃）。

**净结论**：解耦证明 CDC 修复是好的（连续流零丢失）；剩一个确定性的半 nibble 字节
对齐要在 RTL 修，且真 ETM 复测应在与旧版同频（12M）下做公平对照。


## 转向复用上游方案：traceIF 帧路径（用户建议：能复用上游的就用上游）

### 上游 orbtrace trace pipeline 复用盘点

| 环节（上游 Amaranth） | 作用 | 我们的状态 |
|---|---|---|
| TraceIF (`glue.py`) | 双沿采→组16B帧+锁sync+滤HSYNC | ✅ 复用 orbuculum `verilog/traceIF.v` |
| IDDR 输入采样 | DDR 双沿采引脚 | ⚠️ 自造（ECP5→Artix 器件不同，必须重写）|
| **AsyncFIFO 跨域（传帧）** | trace→sys CDC，格雷码原子传帧 | ❌ **自造且造错**（裸总线字节撕裂）→ 这是 2.5% 根源 |
| TPIUSync (`tpiu.py`) | 锁 sync + 字节对齐 | ⚠️ 已导出 `syn/artix7/tpiu_sync.v` 但闲置（traceIF 已含此功能）|
| TPIUDemux (`tpiu.py`) | 拆 stream-ID 提 ETM | ⚠️ 已导出 `syn/artix7/tpiu_demux.v` 但闲置；host 重实现 |
| ChecksumAppender/COBS/SuperFramer | OrbFlow 封装 | ⚠️ 已导出 Verilog 但闲置 |
| USB 传输 | | ❌ 换以太网（板无 USB PHY）|
| OpenCSD/orbmortem 解码 | | ✅ 复用 |

**关键教训**：`export_trace_modules.py` 早已把上游 TPIUSync/Demux/Checksum/COBS/
SuperFramer 转成 Verilog 放在 `syn/artix7/*.v`，但板载 bringup 走了"CAP_RAW=1 抓裸
字节 + host 手工对齐"，把上游在 FPGA 里已解决的 CDC 原子性 / 字节对齐 / sync 锁定
**在 host 端全重踩了一遍**——包括那个 2.5% CDC 撕裂和半 nibble 偏移。

### traceIF 帧路径实测（CAP_RAW=0，上游原生并口路径）

编 `CAP_METHOD=IDDR CAP_RAW=0` bit：`trace_capture_a7(IDDR) → traceIF.v(组帧) →
BRAM 存 128-bit 整帧`。烧后（BRAM 清空重填实时 ETM）抓真 ETM：
- **202 个不同字节值、高熵 ETMv4 包，0xff 不再主导**——**traceIF 在 FPGA 侧就把
  HSYNC/半sync 滤掉了**（CAP_RAW=1 满屏 0xff 的问题消失）。这正是上游方案的价值。
- 帧作为 128-bit 原子单元存取 → **无字节撕裂、无半 nibble 偏移**（帧边界在 trace 域
  traceIF 内锁定）。
- host demux 出 stream 2 高熵 ETM 字节；A-sync 目前 0（swap16 字节序下 3 个）——
  **只差 traceIF 帧字节序与 host `_decode_frame16` 的精确对齐**（traceIF 有
  `{packet[7:0],packet[15:8]}` 交换 + 帧内 elemCount 布局，需与 demux 对齐）。

### 净结论

放弃自造的 CAP_RAW=1 字节级 CDC 路径，改用上游 traceIF 帧路径：CDC 撕裂、HSYNC
淹没、半 nibble 偏移三个坑**一次性消除**（都在 traceIF 帧原子化里解决）。剩唯一收尾：
对齐 traceIF 帧字节序 ↔ host demux（或直接接现成 `tpiu_demux.v` 到 FPGA 里，让 host
只收纯 ETM）。这才是"用成熟方案"的正道。


## traceIF 帧路径实测：架构通了，但帧字节序仍有系统性错位

CAP_RAW=0 帧模式抓真 ETM，帧多样（3840 帧 / 745 种，含 func_test 循环的合理重复），
内容是真 ETMv4 包。发现 **H743 单 trace 源，TPIU 不插 stream-ID formatter 结构**，
traceIF 锁 sync 后组的帧**直接就是 ETM 字节流**（无需 demux）。

- **端到端通了（架构验证成功）**：帧数据 as-is 直接喂 OpenCSD → **37 PC、100% 落
  flash、解出 callback_test/factorial/mixed_test 3 个 func_test 函数**。证明 IDDR →
  traceIF → 帧 这条上游链在硬件跑得通，且 **HSYNC 自动滤除、无 CDC 撕裂、无 nibble
  偏移**（CAP_RAW=1 的三个坑全消失）。
- **但质量差**：严格判据非法事件率 **99.7%**（INSTR_RANGE=10 vs RESERVED=2764），
  只在 10 个 A-sync 附近蒙对几段。**帧字节序/边界有系统性错位**，非偶发。

### 诊断纪律：用 traceIF 仿真标定字节序，别在硬件真 ETM 上试错

问题是 CURTPM 图案无 TPIU sync、traceIF 锁不上，**无法用已知图案验证帧路径**。正解：
用 traceIF 自带 testbench（`verilog/testbeds/traceIF_tb.v` 喂已知 TPIU 帧）的**确定性
输出**标定 host 端如何解 traceIF 的帧字节序（traceIF 有 `{packet[7:0],packet[15:8]}`
交换 + elemCount 7→0 布局），而不是在硬件真 ETM 的高熵数据上猜。

### 状态与决策点

上游 traceIF 帧路径架构**已验证可行**（解出真 func_test PC），剩一个**确定性的帧
字节序对齐**问题。两条收尾路径：
- **1b**：用 traceIF 仿真标定帧字节序 → host 正确解帧（纯软件，确定性）。
- **2**：直接把导出的 `tpiu_demux.v`（或按 traceIF 帧布局写的对齐逻辑）接进 FPGA，
  让 host 只收纯 ETM——但 H743 单源可能连 demux 都不需要，只需 FPGA 侧把 traceIF
  帧按正确字节序展平后送出。


## traceIF 帧字节序标定（仿真，确定性）

跑 `verilog/testbeds/traceIF_tb.v`（喂已知 TPIU 帧 sync + `12 34 02 03...0e 0f`）：
输出 `Frame = 0x123402030405060708090a0b0c0d0e0f` = **输入字节原序，big-endian**
（Frame[127:120]=首字节 0x12，Frame[7:0]=末字节 0x0f）。g_frame 读出
`cap_byte=frd[8*(15-bsel)]` 线性映射，trace_dump 线性读 → **host 收到的就是正确
ETM 字节序**。

**由此推断**：frame2.bin as-is 有 10 个真 A-sync（字节序错则 A-sync 检测不到），说明
字节序**基本对**；99.7% 非法是 **A-sync 之间的数据坏 = traceIF 组帧丢/漏 TRACECLK
边沿**（IDDR@48M 采样质量），不是字节序。这把问题从"host 字节序"移回"FPGA 采样/
组帧完整性"——即 IDDR 在此频率是否漏采边沿导致帧不连续。

**净状态**：traceIF 帧路径字节序已用仿真确定为正确；剩余是采样/组帧完整性（丢边沿），
需按环节（采样→traceIF 组帧→读出）逐段用 walking 等可控输入验证，而非在解码终点反推。


## r27 红方证伪提案 35 + R7B 仿真坐实真凶 = 采样漏边沿

红方 r27 击穿提案 35：**doc 16 的 CAP_RAW=0 帧路径就是提案要移植的上游结构**（IDDR +
同域 traceIF 组帧 + 128-bit 整帧跨域），已实测"三个坑全消、仍 99.7% 非法"→ 三个坑不
是病根，提案是重放已否结构。且 Artix 裸 IDDR 无 ECP5 IDDRX1F 的固有延迟，相位无着落。

**R7A（红方实测）**：跑上游 `traceIF_tb.v`，理想双沿输入组帧 100% 正确
（`OUTPUT=123402...0e0f`）→ 组帧逻辑无问题。

**R7B（本次实测，`testbeds/traceIF_dropedge_tb.v`）**：给 traceIF 喂正确帧流但故意在
第 2 帧注入 1 个多余半位（模拟 IDDR 多/漏采一个边沿）：
```
FRAME[0]=000102030405060708090a0b0c0d0e0f   ← 注入前，完全正确
FRAME[1]=00010203040506070f08090a0b0c0d0e   ← 注入后，从注入点起整体错位（0f 挤入，后续右移）
```
**一个边沿错 → traceIF construct 移位寄存器整体错位 → 该帧及后续全坏**，正是 doc 16
"A-sync 附近对、之间全坏"签名。

### 结论：真凶锁定 = 采样漏/多边沿（采样完整性），非架构/CDC/前端选择

- 组帧对（R7A）、字节序对（doc 16 标定）、CDC 原子（FIFO 修复）——都不是病根。
- 病根 = **IDDR 在 edge-aligned 数据上采在跳变点，高频下漏/多采 TRACECLK 边沿**。
- 这是"换上游极简结构"**碰不到**的环节 → **提案 35 作废**（RETIRED）。
- 下一步真方向：解决采样漏边沿。红方 P0 剩余：R7C（traceIF 在 48/100/150/198M 的
  OOC STA，纯综合）+ R2（Artix 裸 IDDR 相位如何落眼内——若无解则必须 IDELAY/相移/
  过采，回到 proposal 22/26/33 的采样相位问题，但这次判据是"漏边沿率"而非集合覆盖）。


## R7C 完成：traceIF 高频时序全收敛 → 组帧环彻底清白，真凶唯一锁定采样漏边沿

OOC 综合 + STA（`fpga_flow/ooc_traceif_sta.tcl`，xc7a35t-2，traceIF 单模块，
TRACECLK 驱动，input_delay=0.2×周期）：

| TRACECLK | 周期 | traceIF WNS | 判定 |
|---:|---:|---:|:--|
| 48 MHz | 20.8 ns | **+12.69 ns** | 余量巨大 |
| 100 MHz | 10.0 ns | **+4.99 ns** | 充裕 |
| 150 MHz | 6.67 ns | **+2.05 ns** | 舒适 |
| 198 MHz | 5.05 ns | **+0.79 ns** | 满足（紧但正）|

**traceIF 在全部目标频率（含 198MHz）WNS 全为正、时序收敛** → 红方 R4 担忧排除，
组帧逻辑**不需要流水化**。

### 组帧环彻底清白（三重排除）

- R7A（红方）：理想输入组帧 100% 正确 → 组帧逻辑对
- R7C（本次）：48–198MHz 时序全收敛 → 组帧时序对
- doc 16 标定：帧字节序 big-endian 原序 → host 读法对

**⇒ 99.7% 非法的真凶被唯一锁定在采样漏/多边沿（IDDR 在 edge-aligned 数据采在
跳变点）**，即红方 R2/R3 方向。这是整条链里最后一个未清白的环节。

### 自测固化（CI）

R7B 漏边沿仿真做成自判 testbench（`traceIF_dropedge_tb.v`，RESULT=PASS/FAIL），接入
`iverilog_testbenches` CI job：baseline 无 drop→frame0 正确；drop 1 边沿→frame0 对、
frame1 错位。守住"组帧逻辑无罪、采样边沿完整性是根因"这个诊断不被回归。

### 下一步（真方向，红方 R2）

采样漏边沿的物理机制：Artix 裸 IDDR 无 ECP5 IDDRX1F 固有延迟，采在 edge-aligned 数据
跳变点 → setup/hold 双违例 → 亚稳/漏采。解法回到采样相位（proposal 22/26/33），但
**判据换成"漏边沿率 / 逐帧连续正确率"**（R7B 已提供可复现的漏边沿注入模型），不再用
集合覆盖虚荣指标。候选：(a) 单固定 IDELAY 把采样点移出跳变区（对单一目标频率）；
(b) 相移采样时钟；(c) 过采样。先量 Artix 裸 IDDR 在各频率的实际漏边沿率作基线。


## 决定性对照：raw+host 路径 vs traceIF 帧路径（同频 12M，隔离变量）

为验证"漏边沿"假设，同频率(12M)、同 IDDR 采样，只换后端路径：

| 路径 | 12M 真 ETM 结果 |
|------|----------------|
| **CAP_RAW=1 raw + host assemble/deframe**（旧 etm12_bb） | **781 PC / 13-14 func_test** ✅ |
| **CAP_RAW=0 traceIF 帧路径**（本次 frame12，实时真 ETM，224 字节种/21 A-sync） | **100% 非法 / 0 PC** ❌ |

（frame12 确认是实时高熵真 ETM，非陈旧/walking 残留。）

### 反转结论：Artix 上 raw+host 路径比 traceIF 帧路径鲁棒

同频、同采样、只差后端——**唯一差异是对漏边沿的容忍度**：
- **IDDR 采样确实偶发漏/多边沿**（否则两条路该一样）。
- **traceIF 帧路径对漏边沿零容忍**（R7B 已证：一漏，construct 移位永久错位到下个
  sync → A-sync 之间全坏 → 100% 非法）。
- **raw+host 路径鲁棒**：host 的 nibble assemble + tpiu deframe 在**每个 TPIU sync 处
  重新对齐**，漏边沿只毁一小段，之后自愈 → 781 PC。

**这解释了一路撞墙的帧路径**：上游 traceIF 帧路径在 ECP5 能用，是因 ECP5 IDDRX1F
固有延迟**不漏边沿**；Artix 裸 IDDR 会漏，**帧路径在 Artix 反而是错的选择**。我前几轮
朝"移植上游帧路径"走错了方向——数据证明 Artix 上应保留 raw+host 的重新对齐能力。

### 修正后的方向

1. **主路径回到 CAP_RAW=1 raw + host 解码**（已用 axis_async_fifo 修好 CDC 撕裂，
   walking 归零）。它对漏边沿鲁棒，已实测 781 PC。
2. 用 12M 公平复测**修好 CDC 的 raw 路径**（fifocdc bit），确认 781 PC 级别可复现且
   CDC 修复无副作用。
3. 漏边沿仍是物理本底（红方 R2），但**raw+host 路径能容忍它**，所以不必先啃采样相位
   ——先把鲁棒的 raw 路径跑到位，再谈用采样相位进一步压漏边沿率提覆盖。


## ✅✅ 闭环：修好 CDC 的 raw 路径 @12M 真 ETM → 非法率 0.3%，13/14 func_test

烧 `trace_iddr_fifocdc.bit`（IDDR + axis_async_fifo 原子 CDC + CAP_RAW=1 raw），
12M 真 ETM（BB=1），host 解码：

| 指标 | 旧裸总线 CDC+raw | traceIF 帧路径 | **修好 CDC+raw** |
|------|:---:|:---:|:---:|
| 非法事件率 | 24.3% | 100% | **0.3%** |
| A-sync 后跟 Trace-Info | 0 | 0 | **99/116** |
| unique PC | 781(集合虚荣) | 0 | **200，100% 落 flash** |
| func_test 覆盖 | — | 0 | **13/14**（缺 conditional）|
| INSTR_RANGE | — | 0/10 | **17432** |

deframed 56055 字节、fsync=59（正常密度，非虚高）、A-sync=116。**trace-info-after
从 0 跳到 99/116** 是最硬证据——A-sync 后稳定跟 Trace-Info 包 = 字节流真正连续正确。

### 全案闭环

- **根因**（红方 r26 定位、仿真坐实）：顽固的 2.5%/24.3% = 自造裸总线 toggle-CDC 的
  字节撕裂（`tclk_byte` 8-bit 裸总线非原子跨域）。
- **修复**：换 CI 验证过的 `axis_async_fifo`（格雷码原子交接）→ walking 归零、真 ETM
  非法率 **24.3% → 0.3%**（近百倍）。
- **路径选择**（红方 r27 对照引出）：traceIF 帧路径对 Artix 的偶发漏边沿零容忍
  （100% 崩），**raw + host 解码在每个 TPIU sync 重新对齐、鲁棒**，是 Artix 上的正解
  （ECP5 用帧路径是因它 IDDR 不漏边沿）。

方法论：三轮红方（r25/r26/r27）+ 逐环仿真解耦，把一个纠缠十几轮的问题从"占空比/SI/
频率"的错误猜测，收敛到"自造 CDC 撕裂"的真因并修复。有效手段=已知激励+仿真隔离+
红方施压+严格逐指令判据；无效=真ETM高熵数据上试+上板猜+集合覆盖虚荣指标。

### 剩余（非阻断）

- `conditional` 未覆盖：可能该 trace 窗口未执行到，或需更长抓样，非 bug。
- 0.3% 残余非法：物理层偶发漏边沿本底（红方 R2），raw+host 已容忍到 0.3%；如需更低
  可再上采样相位（固定 IDELAY / 相移），判据用漏边沿率。
- 更高频（48/100/150M）raw 路径复测 + 逐指令正确率，是后续扩展。


## 残余 0.3% 误码定性：lane 间不同步翻转的 skew/串扰（非采集链 bug、非跳变密度）

用可精确定位错误的已知图案量残余误码（回答"0.3% 因为什么、随机还是规律、能否归零"）：

| 图案 | 特性 | 频率 | 误码 |
|------|------|------|------|
| walking-1s | 单 bit 循环，4 lane 规整错序翻转 | 12M | 2/122880 = 0.0016%（相邻+末尾=边界效应）|
| walking-1s | 同上 | 150M | 2/122880 = 0.0016%（同样边界）|
| FF/00 | 每 UI 全 4 lane 同步翻转（最大跳变密度）| 150M | 1/61440 = 0.0000%（边界）|
| **真 ETM** | 4 lane 各自独立、任意时刻翻转（高熵）| 12M | **0.3% 非法** |

### 结论（基于对照实验，非推断）

1. **不是采集链 bug**：walking / FF/00 全频段（12–150M）近乎零误码，仅抓取边界那 1-2 个。
   IDDR + 原子 CDC 采集链本身干净。
2. **不是单纯跳变密度**：FF/00 每 UI 全 4 lane 翻转（最大密度）也零误码 → 推翻"翻转多
   就错"的初判。
3. **是 lane 间 *不同步* 翻转的 skew/串扰**：walking/FF00 都是 4 lane **同步或规整**翻转
   （lane 间时刻关系固定，skew 被规避）；真 ETM 是 4 lane **独立、任意时刻**翻转，飞线
   lane 间长度差 + 串扰使某 lane 采样窗口受邻线干扰 → 偶发单点采样错。这与早先 walking
   多-bit 错误呈 `0xa/0x5` 隔-lane 模式一致。

### 三个问题的答案

- **因为什么**：飞线 lane 间 skew/串扰，仅在真 ETM 的 4-lane 独立翻转下触发（规整图案规避）。
- **随机还是规律**：数据相关——触发取决于哪些 lane 在相近时刻翻转；宏观像随机本底，
  根源是确定的物理 skew。
- **能否 0**：walking/FF00 已实测 0（采集链有此能力）。真 ETM 要趋 0 需 **per-lane
  IDELAY 校准**补 lane 间 skew（proposal 33 曾试撤销，但当时 CDC/判据有问题；现 CDC 已
  修、可用漏边沿率判据，值得重做），或等长飞线/阻抗板降 skew。绝对 0 受 SI 物理极限约束。

**当前工程结论**：0.3% 已是可用水平（raw+host 每 sync 重锁，13/14 func_test）。进一步
归零走 per-lane IDELAY（proposal 33 复活），而非再动 CDC/采样架构。


## 频率-IDELAY 匹配：IDELAY 只在高频有用（且高频才需要它）

IDELAY 全范围固定 = 31 tap × 78ps ≈ 2.4ns。它占半位的比例随频率变：

| TRACECLK | 半位 | IDELAY authority | 能否用 |
|---:|---:|---:|:--|
| 12M | 41.7ns | 6% | 无用（且眼太宽不需要）|
| 48M | 10.4ns | 23% | 勉强 |
| **66M** | 7.6ns | **32%** | 够用（护栏阈值）|
| 100M | 5.0ns | 48% | 舒适 |
| 150M | 3.3ns | 72% | 覆盖大半眼 |
| ~208M | 2.5ns | ~100% | 可扫完整半位 |

**反直觉但自洽**：频率越高 IDELAY 越有效。低频眼宽（不需要相位补偿），IDELAY 也够不着；
高频眼窄（开始需要补偿），IDELAY 固定 2.4ns 占比恰好够大。两者天然匹配。**要用 IDELAY，
TRACECLK 应 ≥ ~66MHz。**

### 这澄清了 12M 0.3% 与高频误码是不同机制

- **12M 0.3%**：半位 41.7ns，skew(1-2ns)/相位都远小于眼 → **不是 IDELAY 能解的**（它在
  12M 无用），也不该是采样窗口问题。更可能是**跳变瞬间的串扰/地弹耦合**（与眼宽无关）
  或**解码器在 A-sync 稀疏段重锁的开销被计入"非法事件"**（需区分采集层字节错 vs 解码
  事件率）。
- **高频误码**：眼窄，采样相位/lane skew 主导 → **IDELAY 在此才对症**。

### 修正 proposal 33 的适用区间

per-lane IDELAY 校准（proposal 33）**只在 ≥66MHz 有意义**。它此前被撤销，很可能是在低频
区试（IDELAY 无 authority，tap 扫描是死区伪影——正是 sampling_guard 拦的那个坑）。复活
proposal 33 必须在高频（100–150M）做，判据用漏边沿率/逐指令正确率。

### 下一步

1. 先厘清 12M 0.3% 的归属：量**采集层字节错误率**（raw 字节 vs 已知，像 walking 那样）
   而非解码事件率，排除"解码重锁开销"污染。
2. 高频（100–150M）真 ETM 下扫 per-lane IDELAY，看能否压低误码——这才是 IDELAY 的用武区间。


## 厘清 12M "0.3%"：几乎全是解码器重锁开销，采集层字节错 = 0

拆解 12M 真 ETM 解码事件（按"非法事件紧邻重锁 vs 散布指令段中"分类）：

| 会话 | INSTR_RANGE | RESERVED+BAD_SEQ（真数据错）| NOT_SYNC（重锁开销）| 旧口径"非法率" |
|------|---:|---:|---:|---:|
| 本次 | 17884 | **0** | 111 | 0.62% |

**RESERVED+BAD_SEQ = 0**：解码出的 17884 个指令区间**没有一个非法包**。那 0.62%（之前
记的 0.3% 同类）**全部是 NOT_SYNC**——解码器在每个 TPIU/A-sync 锚点起段前的正常重锁，
是 **ETM 流的 sync 密度特性，不是采集错误**。

### 结论：判据用错了，采集层在 12M 对真 ETM 是零字节错

- 之前把"非法事件率"当采集质量指标，混入了解码器重锁开销（NOT_SYNC）。
- 真正的采集层字节错（RESERVED/BAD_SEQ）在 12M = **0**，与 walking 的 0.0016%（近0）
  完全自洽——**12M 采集层本就近乎完美，无论已知图案还是真 ETM**。
- 即：**这套链（源同步 IDDR + axis_async_fifo 原子 CDC + raw + host deframe + OpenCSD）
  在 12M 对真 ETM 已达采集零字节错、13/14 func_test、逐指令干净**。0.3% 是判据污染，
  不是真误码。

### 对"是否需要 IDELAY / 上高频"的影响

12M 采集已零字节错 → **12M 不需要 IDELAY**（本就没有采样错要补），印证了"低频眼宽不
需要相位补偿"。IDELAY / per-lane 校准的价值只在**高频眼窄、真开始出采样字节错**时才
体现。下一步验证：升到 66M+（IDELAY authority ≥32%），看采集层是否开始出 RESERVED/
BAD_SEQ（真字节错），若有则正是 IDELAY 的用武场景。


## ✅✅✅ 上高频实测（CDC 修复后）：采集层到 200MHz 仍字节干净，上限=STM32 VCO 而非 FPGA

用户指示"先走1，不好钻就上高频，从66M开始"。走1（12M 厘清）已证 12M 采集零字节错。
本节把频率一路推到 STM32 PLL VCO 极限，用**修好 CDC 的链路**（axis_async_fifo 原子交接）
重测采集上限——这是 doc 开头"197.8M @ 0.002%"和"walking 平坦 2.5%"两个旧结论在 CDC
修复后的**决定性重测**。

### 硬件当前状态

- FPGA：`trace_iddr_fifocdc.bit`（IDDR + axis_async_fifo 原子 CDC + CAP_RAW=1 raw），
  上电易失，用 `openFPGALoader -c ft232` 重烧；FPGA 只回 ARP 不回 ICMP（极简网络核，正常）。
- firmware：`perf/firmware/tclk72`（实测 TRACECLK 稳定在 **105.5MHz**，用 FPGA 200MHz
  timebase 字节率测：61440B / 582.5µs = 105.5M）。ETM BB=1 已启用。

### 关键对照：先用已知图案量采集层，再看真 ETM（严守方法论）

**105.5MHz 采集层（已知图案，与频率无关的严格判据）**：

| 图案 | 105.5M 采集层误码 |
|------|------------------|
| walking-1s（4 lane 错序单bit旋转，最严格）| **0.0016%**（2/122880，仅抓取边界）|
| FF/00（4 lane 同步翻转，最大跳变密度）| **干净**（单值 0x0F，仅 1 字节边界）|

→ **105.5M 采集层对已知图案零字节错**。CDC 修复前 doc 记录的 walking 平坦 2.5% 彻底
消失，坐实那 2.5% 就是裸总线 CDC 撕裂、非物理层。

### IDELAY 在高频真正有分辨力了（回答"提频率 IDELAY 就可用了吗"= 是）

112.5MHz（half-bit 4.4ns，IDELAY authority ~54%，护栏放行）ff00 扫 tap：

```
tap  0..9 : err=100%    ← 采在跳变点/错误半位
tap 10..31: err=0.00%   ← 眼睁开，22 tap 宽（≈1.7ns）
```

**眼有清晰左沿（tap≈10）** —— 这正是 12M 下看不到、被 sampling_guard 拦截的"死区伪影"
的反面：高频眼窄，IDELAY 2.4ns 占半位比例够大，**tap 扫描第一次有真实分辨力**，能扫出
眼图边界。印证 doc 的"IDELAY 只在 ≥66M 有用"。

### 频率上限扫描（walking-1s，每频率扫 tap 取最佳，CDC 已修）

`freq_ceiling_sweep.py --pattern walk1`（set_pll_n 强制 DIVR1=0，VCO=TRACECLK=ref×N）：

| Nfield | TRACECLK | best-tap err | 眼宽 |
|-------:|---------:|-------------:|:----:|
| 9  | 62.5M  | 0.002% | 5/5, 后续细扫充裕 |
| 13 | 87.5M  | 0.002% | 5/5 |
| 17 | 112.5M | 0.002% | 4/5 |
| 21 | 137.5M | 0.002% | 4/5 |
| 25 | 162.5M | 0.002% | **9/11** |
| 29 | 187.5M | 0.002% | 6/11 |
| **31** | **200.0M** | **0.004%** | **7/11**（眼≈1.7ns）|
| 33 | 200.0M(钳位) | 3.128% | 0 ← VCO 撞顶 |
| 35 | 200.0M(钳位) | 6.253% | 0 ← VCO 撞顶 |

**采集层 walking 从 62.5M 一路到 200M 都 ≤0.004% 字节干净**，眼宽随频率单调收窄
（5/5→2/5，这才是真实的 SI 眼收窄指纹，与 CDC bug 那种"平坦 2.5%"截然不同）。N≥33
误码爆到 3-6% 且频率钳在 200M 不动 = **STM32 PLL1 VCO 撞 200MHz 上顶**（VCO 无法更高，
再加 N 只是过采同一个 200M 时钟并劣化），非 FPGA 采集失败。

### 结论：采集上限 ≥200MHz，受限于 STM32 出钟能力而非 FPGA

- **FPGA 源同步采集（IDDR + axis_async_fifo + IDELAY）在 62.5–200MHz 全段对已知多-lane
  图案字节干净（≤0.004%）**，眼宽随频率平滑收窄但到 200M 仍有 7/11 tap（≈1.7ns）可用眼。
- **200MHz 是 STM32H743 PLL1 VCO 的天花板**（N≥33 频率钳位+误码爆炸证明），不是 FPGA 的
  采集极限。要测 FPGA 真极限需一个能发 >200MHz 干净 DDR 的信号源。
- 这彻底修正了 doc 开头两个旧结论：①"197.8M @ 0.002% 干净"其实**低估**了（AA/55 乐观，
  但 CDC 修复后连最严格的 walking 都到 200M 干净）；②"walking 平坦 2.5%"是 CDC bug 假象，
  已消除。

### 真 ETM 在 105.5M：采集干净，但 func_test 负载太轻→80% HSYNC 填充→锚点饥饿

同 105.5M 抓真 ETM（BB=1，清 CURTPM），raw 61440B：**79% 是 TPIU HSYNC 填充**
（`ff 7f` 21029 对），真实 ETM 数据仅 20.9%（12841 B），最长连续数据突发只 21 字节；
A-sync 密度塌到近 0，解码器无法锚定（0 PC）。对照 12M：HSYNC 38.4%、数据 61.6%、
突发达 96 字节、19 个 A-sync。

**这不是采集错误**（walking/FF00 在 105.5M 已证零字节错），而是**func_test 的真实
trace 字节率远低于 105.5M 并口的排空能力**——快 4× 的端口把大量空闲周期填成 HSYNC，
单位捕获窗（105.5M 下 61440B 仅 0.58ms 墙钟）里落进的真实数据太少、A-sync 太稀，
decoder 锚不住。降 TRCSYNCPR 强制更密 A-sync 反而让数据更稀（端口更多填充），印证是
"端口相对负载过快"而非"锚点周期"。

**净结论**：
1. **采集上限已答**：源同步 IDDR + 原子 CDC 采集层到 **200MHz（STM32 VCO 顶）字节干净**，
   FPGA 侧游刃有余，眼宽平滑收窄是真实 SI 指纹。
2. 高频真 ETM 要有意义地解码，需**匹配 TRACECLK 与工作负载 trace 字节率**（轻负载用低
   TRACECLK，或重负载/长抓样喂饱高 TRACECLK 端口），否则 HSYNC 填充淹没锚点。这是
   trace 配置问题，不是采集能力问题。
3. IDELAY 在高频（≥~66M）有真实眼图分辨力（112.5M 眼左沿 tap≈10 清晰可见），
   proposal 33 per-lane IDELAY 若要复活应在此频段、用漏边沿/字节错判据。


## ✅✅✅ 密集 workload 解决锚点饥饿：真 ETM @~47M 解出 189 PC / 13-14 func_test / 采集层零字节错

承接上一节"105M 真 ETM 0 PC = 锚点饥饿而非采集错"。用户诊断方向正确——问题在 workload
被编译器内联稀释，不在采集。两步修复后端到端跑通。

### 根因1（坐实）：`-Og` 内联把 func_test 稀释成锚点饥饿的 trace

原 firmware `-Og` 把 leaf/level 小函数全内联，func_test 塌成几条直线代码，BB=1 也只有
极少分支 → trace 突发短、A-sync 稀疏 → 高 TRACECLK 下 80% 是 HSYNC 填充、解码器锚不住。
**改 `-Og -fno-inline -fno-inline-small-functions` + 每个 func_test 函数 `__attribute__((noinline))`**，
每个调用都成真 BL/BLX，分支密度大增。

### 根因2（坐实，且是之前"崩溃/HardFault"的真凶）：HSE 是 25MHz 不是 8MHz → 超频

`build_h743.sh` 和手写 PLL 参数一直按 **8MHz HSE** 换算，实际板载 **HSE=25MHz**
（`stm32h7xx_hal_conf.h: HSE_VALUE=25000000`）。于是"M=2 N=50"以为 VCO=200M，实际
VCO=625M、sysclk 严重超压 → 取指损坏 → **UsageFault INVSTATE + SP 损坏**（CFSR=0x20000）。
之前误判为"main.c 的 func_test 有 bug"、"栈溢出"全是错的——**纯粹是我用错 HSE 基准超频**。

修复：
- CubeMX 按 25M HSE 重生成 `SystemClock_Config`（M=2 N=32 P=2 R=4：ref=12.5M, VCO=400M,
  sysclk=200M, HCLK=100M, pll1_r_ck=100M）。
- PLL 分频参数改成 `#ifndef PLL_*_OVR` 宏，放进 `USER CODE BEGIN PD` 保护区（CubeMX
  再生成不会删），`build_h743.sh` 可覆盖。
- 修 `build_h743.sh`：HSE 基准 8→25MHz，加 ref/VCO/sysclk 越界告警（sysclk>200M 直接
  警告"会崩"），杜绝再超频。
- 修 Makefile：移除不存在的 `sysmem.c`/`syscalls.c`（fresh build 会因缺规则失败）。

**验证稳定性**：烧录后 CFSR=0、多次采 PC 命中 func_test（level_a/main_loop），不再 fault。
时钟修对后 main.c 的 func_test **本就没问题**。

### 端到端结果（密集 workload + 正确时钟）

| 指标 | 稀疏内联版（前节105M）| **密集 noinline 版（本次）** |
|------|:---:|:---:|
| deframed ETM 字节 | 216 | **45298** |
| non-HSYNC 数据占比 | 20.9% | **80.0%**（突发 276B）|
| A-sync | 3 | **44** |
| INSTR_RANGE | 0 | **14753** |
| unique PC | 0 | **189，100% 落 flash** |
| func_test 覆盖 | 0 | **13/14**（缺 conditional）|
| **RESERVED+BAD_SEQ（采集字节错）** | — | **0（0.00%）** |
| NOT_SYNC（重锁开销）| — | 80 |

**采集层零字节错**（14753 个指令区间全合法，RESERVED=BAD_SEQ=0），NOT_SYNC 是正常
sync 重锁开销。**同一套采集链，只改 workload 密度 + 修时钟，就从 0 PC 翻转到 13-14
func_test 逐指令干净**——彻底坐实"105M 0 PC 是锚点饥饿，非采集/SI/频率问题"。

### 频率标签修正（诚实）

本次 FPGA timebase 实测 TRACECLK = **46.9MHz**，而固件 pll1_r_ck 算的是 100M。说明
H743 TRACECLK 到并口间还有约 /2 分频，我对 pll1_r_ck→TRACECLK 关系的理解不精确。**固件
频率标签一直不可信**（tclk72 实测 105.5M、tclk100 实测 46.9M），**只有 FPGA timebase
字节率实测数作准**。这不影响采集能力结论（walking 已独立证到 200M 干净）。

### 净结论

- **崩溃根因 = 用错 HSE 基准（8 vs 25MHz）超频**，非 firmware 逻辑、非 -O0。已用宏化 PLL
  参数 + 25M 基准的 build 脚本 + 越界告警根治。
- **高频真 ETM 解码率低 = 内联导致的锚点饥饿**，非采集/SI/频率。`-fno-inline` + noinline
  workload 直接解决：45298B 数据、189 PC、13-14 func_test、采集层零字节错。
- 采集链（源同步 IDDR + axis_async_fifo 原子 CDC + raw + host deframe + OpenCSD）在真
  ETM 密集流下逐指令干净，与 walking 到 200M 的字节干净结论一致、互相印证。
- 运维教训：FPGA 采集前端偶发状态坏（抓全 0），重烧 `trace_iddr_fifocdc.bit` 即恢复；
  DAPLink 崩溃后调试口 stalled 需 `connect_assert_srst`（RST 已接）或断电恢复。


## ✅ -O0 + 逐指令顺序核对：14/14 func_test，解码执行流逐条匹配源码调用图

用户要求"确保指令流顺序和代码对得上，再提频"。改 `-O0`（时钟修对后 -O0 不再崩）并写
`decode/verify_order.py` 做**逐条顺序核对**（非集合覆盖虚荣指标，红方 r25 要求）。

### 为何 -O0：找回被优化消除的调用

`-Og`（即使 `-fno-inline`）仍做 DCE + 常量折叠，把 `conditional(1/2)`、`repeat_test`、
`pingpong`（常量参数 + 返回值只喂 volatile）整个消除——objdump 证实 main_loop 里根本
没有对它们的 BL。所以之前"13/14 缺 conditional"不是采集/解码缺陷，是**编译器没生成这些
调用**。`-O0` 不做 DCE/折叠，14 个函数全部保留为真实 BL/BLX。

### 结果（-O0，真 ETM，FPGA timebase 实测 46.9MHz）

| 指标 | 值 |
|------|-----|
| deframed ETM | 32758 B（fsync=35）|
| A-sync / trace-info-after | 32 / 22 |
| unique PC | 349，**100% 落 flash** |
| **func_test 覆盖** | **14/14**（conditional 回来了，24 次）|
| INSTR_RANGE | 10095 |
| **RESERVED+BAD_SEQ（采集字节错）** | **0（0.000%）** |
| NOT_SYNC（重锁开销）| 39 |

### 逐指令顺序核对：PASS

`verify_order.py` 从解码的 INSTR_RANGE 序列提取函数 visit 序，对一整轮 main_loop 的 33 个
预期调用做**有序子序列匹配**：**全部 33 个调用按代码调用图顺序出现**（visits[0..96]）。
实测序列逐条对上源码：
- `level_a→level_b→level_c→leaf_mul→level_c→level_b→leaf_add→...→level_a`：直接调用链
  A→B→C 及返回展开逐条对
- `indirect_caller→op_add / op_sub / op_mul`：3 次间接调用目标顺序精确
- `callback_test→dispatch_callback→cb_handler_a → cb_handler_b → cb_handler_a`：回调 a/b/a 精确
- `deep1→2→3→4→5→6→5→4→3→2→1`：6 层嵌套进入+返回**对称展开完整**
- `repeat_test→pingpong→leaf_add`（×5）：重复调用逐次对
- `factorial` 递归、`mixed_test` 内调用树均对

### 结论：顺序正确，可以提频

解码出的执行流**逐条匹配源码静态调用图的顺序**（含递归返回、深层嵌套对称进出、间接调用
具体目标），不是集合覆盖。采集层字节错 0.000%。**基础频率下顺序完全正确**，满足"提频前先
确保顺序无误"的前置条件。下一步：抬高 PLL 让 timebase 实测频率上到 66M+/100M+，复测逐指令
顺序 + 采集字节错是否保持。


## ✅ 提频实测（-O0 真 ETM，逐指令顺序 + 严格字节错判据）

用户批准提频。用 25M HSE 正确参数编不同 R 分频的固件（sysclk 恒 200M，只变 pll1_r_ck），
FPGA timebase 实测频率，每档做严格字节错 + 逐指令顺序核对。**每次抓完用 `trace_off_h743.cfg`
关 ETM/ETF/DBGMCU trace 时钟**，根治之前"抓完 AP 卡死需断电"的问题（D-domain trace 时钟
让调试 AP 一直 busy）。

| 固件 R | pll1_r_ck | **实测 TRACECLK** | 采集层(walking) | 真 ETM 解码 | 字节错 | 顺序核对 |
|-------:|----------:|------------------:|:---------------:|:-----------:|:------:|:--------:|
| 4 | 100M | **46.9 MHz** | 0.0016% | 14/14, 349 PC | 0.000% | **PASS** 33/33 |
| 3 | 133M | **62.5 MHz** | — | 14/14, 343 PC | 0.000% | **PASS** 33/33 |
| 2 | 200M | **93.8 MHz** | **0.0016%(干净)** | 碎片化→0 PC | — | — |

（TRACECLK ≈ pll1_r_ck/2，实测为准。）

### 46.9M / 62.5M：完美

采集层零字节错，真 ETM 14/14 func_test，逐指令顺序全对（含递归/深层嵌套对称进出/间接
调用目标/回调 a-b-a）。**这两档源同步 IDDR 采集 + 解码逐指令干净可用。**

### 93.8M：采集层仍干净，但 func_test 负载喂不饱端口 → 锚点碎片化

- **采集层 walking = 0.0016%（干净）** —— 93.8M 物理采集没问题（默认 tap 眼内，FF/00 也
  是干净单值 0x0F）。
- **但真 ETM 稳定解不出（0 PC）**：raw 有 29% 真实数据，却夹着 **~6450 个 TPIU full-sync**
  （62.5M 只有 1954 个），full-sync 把 ETM 帧相位打碎，deframer（official 578B / walk
  11497B）都抓不到 A-sync。
- **机制**：sysclk 恒 200M（func_test 产生 trace 的速率不变），但 93.8M 端口排空 ETF(4KB)
  比 62.5M 快 1.5×，ETF 更频繁掏空 → TPIU 发大量 full-sync 填充 → 真实 ETM 数据被稀释打碎。
  这是**负载 trace 字节率跟不上端口速率**，不是采集/SI/频率的字节错（walking 已证采集干净）。
- 佐证：ETM 刚使能、ETF 有满缓冲那一瞬抓到的样本（full-sync 仅 17）曾解出 321 PC / 14/14；
  稳态下 ETF 掏空后全是 full-sync 填充。

### 结论（诚实）

- **采集层字节干净的实测上限：≥93.8M**（walking 0.0016%），与 walking 独立扫到 200M 干净
  一致——**FPGA 源同步采集不是瓶颈**。
- **真 ETM 逐指令解码干净可用：到 62.5M 确认**（14/14 + 顺序 PASS + 字节错 0）。
- **93.8M 真 ETM 解不出 = 负载喂不饱快端口**（TPIU full-sync 填充打碎锚点），非采集错。
  要在 93.8M+ 有意义解码，需 **提高 func_test 的 trace 产生率**（更重的分支负载 / 关
  TRCSTALL 让 CPU 全速 / 更长抓样窗口凑够 A-sync），或 **降 TRACECLK 匹配负载**。
- 运维：`trace_off_h743.cfg` 收尾后调试口不再 stall（本轮全程未再断电），问题根治。

### 下一步选项

1. 加重 func_test 负载（循环内更多真实分支/数据）喂饱 93.8M+ 端口，复测真 ETM 逐指令；
2. 或接受"采集层到 200M 干净、真 ETM 逐指令到 62.5M 干净"作为本阶段结论，转入其它工作。


## 加重负载 + 93.8M 深挖：真 ETM 高熵在 93.8M 出现 lane-skew 污染，需 per-lane IDELAY

按用户选项 1，加重 func_test 负载（REPS 8→200、每次调用带数据依赖累加防 DCE、移除热循环里的
HAL_GetTick/LED 轮询、factorial 深度随 r 变、conditional 双臂）喂端口，并 `TRACE_STALL=0`
让 CPU 全速产 trace。

### 对照结果（同一重负载固件，只变频率）

| 实测 TRACECLK | TPIU full-sync | deframed ETM | 解码 |
|---:|---:|---:|:--|
| 62.5M | 2853 | 25858 B | **14/14, 338 PC** ✅ |
| 93.8M | ~10300 | 591 B | **0 PC** ❌ |

加重负载让 62.5M 更漂亮（338 PC、14/14、burst 更长）。但 93.8M 仍崩，且加重负载后 full-sync
不降反升（6450→10300）。

### 深挖 93.8M：不是 deframe，是高熵真 ETM 的采集污染

剥掉 full-sync 后看 93.8M 真 ETM 数据本身（18986 B）：**A-sync 仅 4 个**（62.5M 同量数据
有 25 个），字节直方图被 **`0x9f`(4440次) 主导** + 周期性 `f2 9f`/`f6 9f` 模式。这不是健康
高熵 ETM（62.5M 是），是**结构化污染**。

**关键对照**：93.8M 下 **walking 图案采集干净（0.0016%）但真 ETM 被污染**。差异在于：
- walking = 4 lane 规整错序翻转，lane 间时序固定，且 2 字节周期图案能掩盖单点错；
- 真 ETM = 4 lane 独立任意翻转的高熵流，暴露 **lane 间 skew**——某 lane 采样窗口在 93.8M
  眼窄时被邻 lane 干扰，产生 `0x9f` 类系统性 bit 污染。

这正是文档早先预测的"高频眼窄 + lane skew 需 per-lane IDELAY 校准"（proposal 33）的场景。

### 阻塞点：当前 bit 的 IDELAY tap 控制对采集无可观测效果

实测 `trace_ctrl set-tap 0/8/16/24` 后 walking 误码**纹丝不动全是 0.0016%**。RTL
（`trace_capture_a7.v`）确有完整 per-lane IDELAYE2 VAR_LOAD + CSR 0x05→tap_csr→CDC→tap_load
通路，`trace_stream_top.v` 也接了。tap 无效有两种可能：
1. walking 规整图案的眼太宽，±2.4ns（IDELAY 全程）仍在眼内，**walking 根本测不出 tap 效果**
   （只有真 ETM 的窄眼才显现）——那 per-lane IDELAY 仍可能对真 ETM 有效，但**只能用真 ETM
   解码质量（A-sync 数/RESERVED 率）当判据来扫**，不能用 walking。
2. 或 tap_load 脉冲/CDC 在此 bit 未真正生效（需 RTL 复核）。

### ✅ 突破：IDELAY tap 对真 ETM 完全可观测，调对 tap 后 93.8M 跑通

之前判断"tap 无效"是错的——那是因为**只用 walking 测**（规整图案眼太宽，±2.4ns 全在眼内）。
改用**真 ETM 的 deframe 后 A-sync 数**当判据扫全局 tap，tap 效果一目了然：

| tap | A-sync | deframed |
|----:|-------:|---------:|
| 0-6 | 16-17 | ~16500 |
| 16 | 12 | 14833 |
| 24 | 1 | 7678 |
| 28（旧默认）| ~0 | 崩 |
| 31 | 0 | 461 |

**根因坐实**：93.8M 之前崩，是**综合默认 tap=28 在眼外**（高频眼窄，28 落到眼边缘/外）。
之前满屏 10000+ full-sync 是**采样错产生的假 sync**，非真填充。

**tap=2 完整解码 93.8M 真 ETM**：

| 指标 | 默认 tap28 | **tap=2** |
|------|:---:|:---:|
| TPIU full-sync | ~10300（假）| **18** |
| deframed ETM | 591 B | **16354 B** |
| unique PC | 0 | **384，100% 落 flash** |
| func_test | 0 | **14/14** |
| 字节错(RESERVED+BAD_SEQ) | 61.6% | **0.110%**（RESERVED=5）|
| 顺序核对 | — | **PASS 33/33** |

### 结论：93.8M 真 ETM 逐指令跑通，IDELAY 是高频关键

- **真 ETM 逐指令干净上限从 62.5M 提到 93.8M**（14/14 + 顺序 PASS + 字节错 0.110%），
  关键就是把 IDELAY tap 从眼外的默认 28 调到眼内的 ~2。
- **完全印证文档核心预测**：IDELAY 只在高频眼窄时有用且必需；判据必须用真 ETM 解码质量
  （A-sync/RESERVED），walking 眼太宽测不出、集合覆盖是虚荣指标。
- 残余 0.110%（5 个 RESERVED）= 全局单 tap 补不掉的 **lane 间 skew**，是 proposal 33
  per-lane 独立 IDELAY 校准的正当场景（各 lane 单独扫 A-sync 眼心），预期可压到 0。
- 运维教训：**高频抓真 ETM 前必须先扫 tap 找眼心**（用 A-sync 判据），默认 tap 只在低频
  眼宽时凑效。应把这一步做成 `iddr_tap_sweep` 的真-ETM 判据模式。


## proposal 33 复活：per-lane IDELAY 实装 + 真-ETM A-sync 判据校准 → 93.8M 残余降到 ~0.03%

复活 proposal 33，把 per-lane 独立 IDELAY 做进 RTL 并用真 ETM 解码质量校准。

### RTL + 工具实装

- `trace_stream_top.v`：新增 CSR **0x06** per-lane tap（data[6:5]=lane, data[4:0]=tap），
  4 个独立 `tap_csr0..3` + 各自 CDC 到 clk200，分别接 `tap_data0..3`。CSR 0x05 保留为
  "设所有 lane"（全局扫）。
- `trace_ctrl.py`：新增 `set-tap-lane <lane> <tap>`。
- `perlane_idelay_cal.py`：贪心逐-lane 爬山，判据 = **真 ETM deframe 后 A-sync 数**
  （不是 walking——眼太宽测不出；不是集合覆盖——虚荣指标）。
- 新 bit `trace_iddr_perlane.bit`（TAP 默认 2，CAP_RAW=1，IDDR）。

### 校准结果（93.8M 真 ETM，重负载）

各 lane 单独扫 tap，A-sync 判据：

| tap | 各 lane A-sync |
|----:|:--------------|
| 0-12 | 16-17（平台）|
| 16 | 14-16 |
| 20 | 5-15 |
| 24 | 1-2 |
| 28（旧默认）| ~0（眼外）|

**4 条 lane 收敛到同一个 tap=2**（[2,2,2,2]），A-sync 在 tap 0-12 都是平台 16-17。

### 关键发现：这套飞线 4 lane 之间几乎无 skew

- **per-lane 校准没找到 lane 间偏移**——4 条 lane 都要 tap≈2，眼在 0-12 都平。说明**飞线
  lane 间 skew < 1 个 IDELAY tap（78ps）**，本就匹配得好。
- 所以**全局单 tap 已接近最优**，per-lane 在这块板子上没有额外增益（没有 skew 可补）。
- [2,2,2,2] 多次严格解码：byte-err **0.02–0.047%**（RESERVED 仅 1-2 个/抓），比之前单次
  0.110% 更稳更低；顺序核对仍 PASS 33/33。残余 1-2 个 RESERVED = 解码器段边界重锁的固有
  开销，**不是系统性 lane skew**。

### 诚实结论

- **93.8M 真 ETM 逐指令干净**（14/14、顺序 PASS、byte-err ~0.03%），关键是 IDELAY 全局
  tap 从眼外的 28 移到眼内的 2。
- **per-lane IDELAY 已实装可用**（RTL+工具+校准脚本），但**这块板子 4 lane 无显著 skew**
  （都要 tap=2），所以 per-lane 相比全局 tap 无额外增益——这是诚实的负结果：proposal 33 的
  机制到位了，但当前硬件不需要它（飞线恰好匹配）。若换 skew 更大的连线/更高频眼更窄时，
  per-lane 才会显现价值，届时校准脚本和 CSR 通路已就绪。
- 残余 ~0.03% 是解码器重锁开销（RESERVED 落在段边界），非采集字节错——采集层在 93.8M
  对真 ETM 已实质零字节错。

### 净成果

- 真 ETM 逐指令干净：**93.8M**（IDELAY tap 调进眼）。
- per-lane IDELAY 基础设施就绪（CSR 0x06 + set-tap-lane + perlane_idelay_cal.py），
  当前板子无 skew 用不上，但为更高频/更差连线备好。


## 冲击 105.5M：真 ETM 逐指令上限撞到 IDELAY 范围极限（IDELAY-only 到顶）

继续提 VCO（N=36 → VCO=450, sysclk=150M 更稳）冲更高 TRACECLK。

### 结果：105.5M 真 ETM 解不出，眼心超出 IDELAY 可达范围

- FPGA timebase 实测 **105.5MHz**（VCO450/R2）。
- 全 tap 扫描（0–31，A-sync 判据）：
  - tap 0–24：全是**半 nibble 偏移**（`0xf7` 主导 ~2700，`0x7f`≈0，full-sync=0）——采样点
    在错误半位，字节整体错位。
  - tap 27–30：`f7` 开始塌，`7f`/full-sync 冒头。
  - **tap=31（IDELAY 满程 2.4ns）**：对齐终于对了（`7f`=18208、`f7`=342、full-sync=1319），
    但 **A-sync 仍=0、deframed 95B、0 PC**——眼**刚够到边、没到心**。
- IDELAY 到顶 31 仍解不出真 ETM。

### 根因：IDELAY 2.4ns 范围在 105.5M 只覆盖半个 UI

- 105.5M 半-UI = 4.7ns，IDELAY 全程仅 2.4ns ≈ **半个 UI**。
- 93.8M 眼心在 tap≈2（低端），105.5M 眼心需 tap>31（高端）——12% 频率变化眼却"移动"超过
  整个 IDELAY 范围，是**半-UI 回卷**：采样时钟与数据的相位关系跨过半-UI 边界，眼从低-tap
  端"绕"到 IDELAY 够不着的高-tap 端。
- 即 **IDELAY-only 采样在 105.5M 触及物理范围极限**：能覆盖的相位窗口 < 需要的延迟。

### 诚实结论：IDELAY-only 路径真 ETM 逐指令上限 = 93.8M

- **采集层（规整图案 walking）干净到 200M**（早测，不依赖眼心精度，图案能容错）。
- **真 ETM 逐指令干净：93.8M**（tap≈2 眼内，14/14、顺序 PASS、byte-err ~0.03%）。
- **105.5M 超出 IDELAY-only 能力**：眼心超过 IDELAY 2.4ns 可达范围，tap31 仍未入心。
- 要突破 93.8M 需**粗相位延迟超过 IDELAY 范围**：MMCM 相移采样时钟（把 IDDR 采样点整体
  移到眼心，doc 早先 proposal 22/26 方向），或 IDDR 反沿选择 + IDELAY 微调覆盖另半 UI。
  这是采样架构的下一步（比 per-lane 大），不是参数调整。

### 频率-上限总表（本阶段最终）

| 能力 | 上限 | 依据 |
|------|-----:|------|
| 采集层字节干净（规整图案）| ≥200M | walking best-tap 0.004%（受限于 STM32 VCO）|
| 真 ETM 逐指令干净（IDELAY-only）| **93.8M** | 14/14 + 顺序 PASS + byte-err 0.03% |
| 真 ETM（IDELAY 触顶失效）| 105.5M ✗ | 眼心超 IDELAY 2.4ns 范围 |

要把真 ETM 逐指令上限从 93.8M 继续往上推，下一步是 MMCM 相移采样时钟（覆盖整个 UI 的
粗延迟），IDELAY 做眼内微调 + per-lane deskew（基础设施已就绪）。


## ⚠️ 重大认知修正：walking"到 200M 干净"是判据虚荣，不代表采集能力；真ETM才作准

用户尖锐提问"为什么之前 AA55/walking 能追到 200M，真 ETM 100M 就到 IDELAY 极限？"——
这戳穿了一个一直存在的判据缺陷。

### 时钟本身是对的（三方交叉验证）

先排除"ST 时钟配错"：
- **RCC 寄存器反算**：DIVM1=2→ref12.5M, DIVN1=32→VCO400M, DIVP1=2→sysclk200M,
  DIVR1=3→pll1_r_ck133.3M, SWS=3（已切 PLL1）。
- **DWT CYCCNT 硬测**（不依赖寄存器解读，按 CPU 周期）：1s=200.4M, 2s=200.14M →
  **sysclk=200.1M 铁证**。CubeMX 配置正确，之前"崩"确实只是早期 8M-HSE 超频。
- **TRACECLK = pll1_r_ck/2**（ARM TRM：TPIU 输出=TRACECLKIN/2）。三频点 pll1_r_ck/2 与
  FPGA timebase 比值恒为 **1.066** → FPGA timebase 系统性低估 6.6%（板载"50M"晶振或
  MMCM 输入实际偏高）。故真实 TRACECLK 比历史标注高 6.6%："93.8M"实为 **~100M**，
  "62.5M"实为 **~66.7M**。

### 为什么 walking 到 200M "干净"而真 ETM 100M 就崩——判据免疫，非频率

`walk_score` 只验两件事：每 nibble 是单 bit（∈{1,2,4,8}）+ 遵循 4→2→1→8 旋转，且
**丢锁后自动重锁相**（prev=None）。致命盲区：
- walking 是 4 lane **规整错序**翻转，lane 间时序固定。**即使 IDELAY 采样点在眼边缘/半-UI
  偏移，只要 4 lane 一致地偏，读出仍是"某个单 bit 在转"** → 判据判为"干净"。
- 半-UI 偏移在 walking 下只是相位滑移，重锁逻辑当正常处理。
- 所以"walking 到 200M 干净"只证明**物理链路能在 200M 传规整方波**，**不证明采样点在眼心、
  更不证明能采高熵数据**。这与 doc 开头自我证伪的 AA/55 乐观假象同源——walking 比 AA/55 严
  一点（能抓 lane skew/丢 nibble），但**仍对采样相位错误免疫**。

真 ETM 相反：4 lane 独立高熵翻转，采样点偏离眼心 → 半-UI 偏移直接字节错位（0xf7 vs 0x7f）
→ TPIU 帧同步全丢 → 0 A-sync。**零容错**。

### 结论：唯一有效的采集能力判据 = 真 ETM 逐指令解码

- 推翻"采集层字节干净到 200M"的**意义**：那只是 walking 判据虚荣（相位免疫），非真实采集
  上限。红方 r25 早批过集合覆盖虚荣指标，这里是同类错误的另一种形式（图案容错性掩盖相位错）。
- **真实采集能力上限（真 ETM 逐指令干净）= ~100MHz**（历史标"93.8M"，×1.066 校正），
  受限于 IDELAY 2.4ns < 半-UI 无法把眼心移到采样点。
- 校正后的频率-能力表：

| 判据 | 上限 | 可信度 |
|------|-----:|--------|
| walking/AA55"干净"| "200M" | ❌ 判据虚荣（相位免疫，不算数）|
| **真 ETM 逐指令干净** | **~100M**（标93.8M×1.066）| ✅ 唯一作准 |
| 真 ETM（IDELAY触顶）| ~112M（标105.5M）✗ | 眼心超 IDELAY 范围 |

### 待办

1. 校正历史 timebase 读数 ×1.066（或标定板载晶振/FPGA ref 真实频率后精确修正）。
2. 突破 ~100M 真 ETM 上限需 MMCM 相移采样时钟（粗延迟覆盖整 UI），IDELAY 只够微调。
3. 采集能力评估**只认真 ETM 逐指令/ A-sync 判据**，walking 仅用于"链路通不通"的粗筛，
   不再作为频率上限依据。


## ✅ orbetto → Perfetto 端到端可视化跑通 + 调用频次逐函数交叉校验 + SysTick 修复

用户建议用 `embedded-debug-tools/ext/orbetto` 把真 ETM 抓样转 Perfetto,作为最有说服力的
端到端证明。跑通并做了定量交叉校验。

### 端到端链路

```
STM32H743 源同步 ETM (66.7M, BB=1)
  → A7-Lite FPGA IDDR 采集 (trace_iddr_fifocdc.bit) + FPGA 200M timebase 硬件时间戳
  → UDP :5001 raw dump
  → orbetto (官方 TPIU deframe → ETMv4 解码, ARM/Linaro Mortrall)
  → Perfetto trace (func_test_66m.perf)
```

命令:
```sh
# 1) 生成 per-ETM-byte 时间基准 (decode/etm_with_time.py -> .time.bin, u64 LE ns)
python3 decode/etm_with_time.py raw.bin raw.bin.ts.json timed.bin
# 2) orbetto 转 Perfetto (ELF 名须含 "stm32h743" 才被 Device 识别)
build/orbetto -C 200000 -t 1 -f raw.bin -e stm32h743_*.elf -F timed.bin.time.bin
```

### 定量交叉校验:perf 调用频次 = 源码静态调用图 × 迭代数

用 protobuf 解 perf,数每个函数的 slice 次数,对照 main.c 一轮 main_loop 的静态调用图
(以 deep1..6 = 58 次定基准迭代数):

| 函数 | perf 次数 | 源码预测(×58) | ratio |
|------|----------:|-------------:|------:|
| dispatch_callback | 174 | 3×58=174 | 1.00 |
| pingpong | 289 | 5×58=290 | 1.00 |
| indirect_caller | 231 | 4×58=232 | 1.00 |
| op_add+op_sub+op_mul | 231 | =indirect_caller | 1.00 |
| deep1..6 | 各 58 | 各 1×58 | 1.00 |
| conditional | 114 | 2×58 | 0.98 |
| factorial | 115 | 2×58 | 0.99 |
| leaf_add | 574 | 10×58=580 | 0.99 |
| leaf_mul | 171 | 3×58=174 | 0.98 |
| level_a/b/c, frame_func, mixed_test, callback_test, repeat_test | ≈58/116 | | 0.98–1.00 |

**全部 18 个函数 ratio 0.98–1.00**——重建的执行流调用频次与源码调用图**逐函数量化吻合**
(含 callback 调 3 次、repeat 循环 5 次、indirect 每次调 1 operator、6 层嵌套、conditional
双臂各 leaf_add/leaf_mul)。这是比集合覆盖更硬的证明。偏差 0.98 只是抓取窗口边界最后一轮
未跑完。

### FPGA 硬件时间戳修复了假时间轴

初版没喂 `-F`,orbetto 靠 ETM cycle-count 推时间 → 时间轴塌成假的 "1m3s"(单函数 duration
被拉成分钟)。喂 FPGA `-F` timebase (每 ETM 字节一个 200M-tick ns 值) 后,时间轴变成真实的
0–919µs,58 轮迭代逐个展开。**这验证了 doc 15 §25 的 FPGA 硬件时间戳方案端到端可用**。

### 顺带修复 orbetto 的 ETMv4-CMSIS 异常退出 bug

用户发现 Perfetto 里 SysTick "乱"。诊断:
- SysTick(#15) 进入和返回**都在数据里、都解对了**(进 SysTick_Handler→HAL_IncTick→返回
  0x44e)。
- 但 orbetto 的 ETMv4 异常退出检测**只认 NuttX 的 `arm_exception*` 函数名**(在间接 JUMP
  路径)。标准 CMSIS handler(`SysTick_Handler`)的 EXC_RETURN 经 ETMv4 **地址包**回到
  returnAddress,没被识别成退出 → slice 拖到 500-事件超时才强制关 → Perfetto 里横跨一片。

修复(`embedded-debug-tools` mortrall.hpp):在 EV_CH_ADDRESS 处理里加**通用异常退出检测**
——异常活跃时地址包回到记录的 returnAddress(thumb 半字节容差)即为中断返回,不依赖 handler
命名。修复后 SysTick entry/exit 完美配平(1/1,0 超时强制),slice 是真实窄条。这让 orbetto
从"只支持 NuttX/PX4"扩展到**支持标准 CMSIS firmware**。

### 净成果

- **完整端到端可视化跑通**:源同步 IDDR 采集 → orbetto ETMv4 → Perfetto,时间轴用 FPGA
  硬件时间戳,调用频次逐函数交叉校验吻合。产物 `func_test_66m.perf`(可拖入 ui.perfetto.dev)。
- orbetto ETMv4-CMSIS 异常退出修复(通用,非 NuttX 专用)。
- 校准脚本:`decode/etm_with_time.py`(FPGA timebase → orbetto -F)。
- 注:ELF 文件名须含 `stm32h743` 才被 orbetto Device 识别(否则 device.valid() 断言失败)。


## ✅ 100M(实测校正后)端到端:真 ETM → Perfetto,533 PC / 14-14 / 顺序 PASS

按用户要求把端到端推到 100M 主频。tclk100b(pll1_r_ck=200M),FPGA timebase 实测
93.8M ×1.066 校正 = **99.9MHz(≈100M)真实 TRACECLK**。

### 采集 + 解码(tap=2 眼内)

| 指标 | 值 |
|------|-----|
| 实测 TRACECLK | 93.8M(timebase) → **99.9M 真实**(×1.066)|
| unique PC | **533,100% 落 flash** |
| func_test 覆盖 | **14/14** |
| INSTR_RANGE | 4223 |
| RESERVED+BAD_SEQ | 3 → byte-err **0.071%** |
| 顺序核对 | **PASS 33/33** |

眼在 tap 0-10(A-sync 16-17),默认综合 tap 之外需 CSR 调进眼——印证"高频必须先扫 tap"。

### Perfetto 端到端

`etm_with_time` → orbetto `-F` → `func_test_100m.perf`(445 PC bitmap,FPGA 时间基准
span 614µs)。调用频次交叉校验(此窗口 N≈38 轮,以 deep=228=6×38 定基准):
dispatch_callback 114=3×38、pingpong 190=5×38、indirect_caller 152=4×38(=op_add+sub+mul)、
conditional 76=2×38——**比例逐函数吻合**,与 66M 一致。

### 结论

**~100M 真实 TRACECLK 端到端跑通**:源同步 IDDR 采集 → orbetto ETMv4 → Perfetto,
14/14 func_test、533 PC、顺序 PASS、采集字节错 0.071%。这是 IDELAY-only 路径的真 ETM
逐指令上限(再高到 105.5M+ 眼心超 IDELAY 范围,需 MMCM 相移)。产物 `func_test_100m.perf`。


## ✅✅✅ 突破 100M:clock-lane IDELAY → 112.4MHz 真实主频,零字节错端到端

用户要求试突破 100M(不行就回退保最佳)。**成功突破,无需回退。**

### 方案:给 TRACECLK 也加一级 IDELAY(不是 MMCM)

100M 上限的物理根因:105.5M 半-UI=4.7ns,而数据 IDELAY 只有 2.4ns,眼心在 data-tap>31
够不着。**解法比 MMCM 轻**:给采样时钟也加一级 IDELAYE2。有效采样相位 =
Dd(data delay) − Dc(clock delay),范围从 [0,2.4ns] 扩成 [−2.4,+2.4]=4.8ns > 半-UI,
眼心必落在可达窗口内。

RTL:`trace_capture_a7.v` 时钟路径 IBUF → **IDELAYE2(SIGNAL_PATTERN=CLOCK)** → BUFIO/BUFR
(仅 BUFR_IO 源同步模式)。`trace_stream_top.v` 加 CSR 0x07 clock-tap + CDC。工具
`trace_ctrl set-tap-clk`。Vivado 综合布线 0 error(IDELAYE2→BUFIO 合法)。所有其它 top +
sim testbench 的实例化补 `.tap_clk(5'd0)` 保持原行为 + CI 不破。

### 结果:112.4MHz 真实,零字节错

tclk106(pll1_r_ck=225M),timebase 实测 105.5M ×1.066 = **112.4MHz 真实**。

- **IDELAY-only(旧 bit)**:105.5M 全 data-tap A-sync=0,眼心超范围,完全解不出(见前节)。
- **加 clock-lane IDELAY**:二维扫 (clk-tap × data-tap) 有清晰眼:
  - clk=8,data=0 → 90% 崩(采样点错)
  - **clk=8,data=12 → byte-err 0.000%**(眼心)
  - clk=0,data=8 → 0.07%

最优点 **clk=8 / data=12** 完整验证:

| 指标 | 值 |
|------|-----|
| 真实 TRACECLK | **112.4MHz** |
| unique PC | **698,100% 落 flash** |
| func_test | **14/14** |
| INSTR_RANGE | 3171 |
| RESERVED+BAD_SEQ | **0 → byte-err 0.000%** |
| 顺序核对 | **PASS 33/33** |

### Perfetto 端到端

`func_test_112m.perf`(FPGA 时间基准 span 546µs)。完整链路 112.4MHz 跑通。

### 结论:真 ETM 逐指令上限从 100M 提到 112.4MHz(+12%)

- **clock-lane IDELAY 成功突破 100M**:112.4MHz 真实主频、零字节错、14/14、顺序 PASS。
- 关键:采样时钟延迟把 IDELAY-only 够不着的眼心(半-UI 外)拉进 ±2.4ns 可达窗口。
- 新上限受限于 IDELAY 组合总范围(±2.4ns)与 STM32 VCO;更高频(半-UI < clock 抖动)仍需
  MMCM 相移,但 clock-lane IDELAY 已把上限从 100M 推到 112M,方案轻、无需 MMCM。
- 新 bit `trace_iddr_clktap.bit`(默认 data-tap=2, clk-tap=0,≤100M 行为不变;>100M 用
  CSR 0x07 设 clock-tap)。

### 校准法(>100M)

1. 二维扫 clk-tap × data-tap,A-sync 或 RESERVED 判据找零错点;
2. clk-tap 步进约等于把眼心在 data-tap 空间平移;
3. 眼心零字节错点即最优(如本例 clk=8/data=12)。

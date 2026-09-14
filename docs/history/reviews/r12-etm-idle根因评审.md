# r12 · STM32F429 ETM "配置全对却只发 idle" 根因评审

> 评审对象：`stage4-datapath/06-v3-orbuculum-decode.md` 的结论"卡在寄存器层之下、转 ITM+DWT"
> 立场：红方（CoreSight 调试架构）。**不接受"每个位都对却放弃"——这种局面的根因必然是某个被双方默认成立、从没独立验证的前提。**
> 相关提交：`d0fa807`（ETM config 全对仍 idle）、`42e872d`（判定 OpenOCD native etm 在 HLA 下不可用、建议转 ITM+DWT）、`161e868`（声称示波器确认 4-bit trace enabled）

---

## 0. 先把逻辑收敛到唯一问题（这一步蓝方没做，做了就不会乱猜"时钟/电源域"）

蓝方在结论里把根因摊成"疑似 trace 时钟 / 电源域门控 / ATB 连接 / HLA 限制"一大片——**这是没有用现有证据做排除**。现有证据足以把嫌疑面砍到只剩一个点：

**TPIU 在连续发 idle（`0x7fff` + 周期 sync `0xffffff7f`）这一个事实，单独就证明了以下全部成立：**

| 被 TPIU idle 证明为「健康」的环节 | 为什么 |
|---|---|
| TRACECLKIN（trace 时钟）在跑 | 没有 trace 时钟，TPIU formatter 一个 bit 都发不出来，更不会有 sync |
| TPIU formatter 工作正常 | idle/sync 帧就是 formatter 在 ATB 空闲时的标准输出（FFCR=0x102，EnFCont=1）|
| PE2-6 引脚 / 杜邦线 / FPGA 采样链 | 你能抓到 sync，说明物理层和采样相位对（V1 14-tap 眼也独立证了）|
| 4-bit 端口模式 | sync `0xffffff7f` 的字节铺开方式就是 4-bit formatter 的 |

**所以"trace 时钟门控""电源域""RCC 漏开某位"这些猜测可以直接划掉**——它们若成立，TPIU 连 idle 都发不出来。STM32F4 的 trace cell 时钟来自 HCLK 域、随核心运行常在，且 TPIU 正在发 idle 已反证它在跑。**不要再查 RCC trace 时钟。**

剩下唯一没被证明的环节是：**ETM 没有把任何 trace 数据放上 ATB。** 而 `ETMSR bit2 = 0` 正是 ETM 自己承认这一点。

**于是整个问题塌缩成一句话：为什么 TEEVR=always、区域=全部、powerdown 已清、prog 已清，ETMSR bit2（TraceEnable status）却始终 = 0？**

下面按"最可能→最不可能"逐条给可证伪的检查。每条都给：动作 / 期望值 / **如果是这个原因应看到 X**。

---

## 1. ETMSR bit2 的权威定义（先把蓝方注释纠正）

蓝方把 ETMSR bit2 注释成"trace start/stop status"——**错位了**。按 **ARM ETMv3 Architecture Spec（IHI0014Q）§3.3.4 "Status Register, ETMSR"**（ETM-M4 TRM DDI0440 引用同一寄存器）：

| bit | 名称 | 含义 |
|---|---|---|
| 0 | Untraced overflow | 溢出 sticky |
| 1 | Programming bit | ETM 当前是否在 programming 模式（=effective prog 位）|
| **2** | **TraceEnable status** | **当前 TraceEnable 信号的电平** |
| 3 | TraceStartStop status | start/stop 块当前状态 |

**bit2 = TraceEnable 信号的实时电平**，不是 start/stop。TraceEnable 信号 = `TEEVR 事件` 经区域包含/排除逻辑、并被 powerdown / prog / **非侵入式调试授权(NIDEN)** 等全局门控之后的结果。

蓝方已经把 TEEVR（always）、区域（ETMTECR1=0 → 全部）、powerdown（bit0=0）、prog（ETMSR bit1=0）全排除了。**那么 bit2=0 只能来自这三类未验证前提之一：①配置在运行态没真正生效（读回是 halted 假象）②授权(NIDEN)门控 ③软件锁/写入根本没落到运行态的 ETM。** 这正是下面 1~4 的排序。

> ⚠️ 一个测量陷阱先排掉：**ETMSR bit2 在核心 halt 时本来就会是 0**（halt 时没有指令在执行，TraceEnable 自然不 assert）。如果蓝方是 halt 状态下 `mdw` 读 ETMSR，bit2=0 可能只是测量假象。但"抓 16KB 全 idle"是在**运行态**取的，它独立地证明了运行态确实没 trace——所以问题真实存在，bit2 读法只是别误判。

---

## 2. 嫌疑排序（可证伪检查）

### 🥇 嫌疑 1（最可能）：配置在「运行态」根本没生效——读回 0x980 是 halted 假象 / HLA resume 把它擦了

**为什么排第一**：蓝方所有"已确认正确"的回读，极可能都是在**核心 halt**时做的。HLA(ST-Link) 下 OpenOCD 几乎所有寄存器访问都强制 halt。读回 0x980 只证明"halt 那一刻寄存器是 0x980"，**不证明 resume 之后运行态的 ETM 还是 0x980**。OpenOCD/ST-Link 在 `reset`/`resume`/重连时可能重跑 init、或 ETM 在某些 resume 路径下 powerdown 位被重置回 0x411。

**检查动作**（OpenOCD，关键是"不重写、只观察运行态/二次 halt"）：
```
# 1) 配好 ETM、resume，让程序跑几秒
resume
sleep 2000
# 2) 二次 halt（注意：不要重新跑 etm_enable.cfg）
halt
mdw 0xE0041000     ;# ETMCR  期望仍 0x980
mdw 0xE0041010     ;# ETMSR
mdw 0xE0041000
```
**如果是这个原因，应看到**：二次 halt 后 ETMCR **不再是 0x980**（回到 0x411 或 powerdown bit 复位），或 ETMSR bit1 重新置 1（又进了 prog 模式）。→ 证明配置没活到运行态。

**进一步证伪**：把 ETM 配置写完后**先 resume 再 halt 立即回读**，对比"写完立即回读（未 resume）"。两者不一致 = resume 擦写。

**修法**：在**最后一次 resume 之前**完成 ETM 配置，且确认 OpenOCD 的 reset/init 脚本里没有在 resume 时复位 DBGMCU/ETM；或用 `monitor` 钩子在 resume 后再写一次并验证。

---

### 🥈 嫌疑 2（很可能，且被你点名）：prog 位"清除即生效"这个前提没被 ETMSR bit1 轮询确认

**ETMv3 强制时序**（IHI0014Q §3.5.1 "Programming bit and associated state"）：清除 ETMCR bit10(prog) 后，**必须轮询 ETMSR bit1 直到读到 0**，才代表配置已 latch、ETM 退出 programming。中间不允许其它 ETM 访问。蓝方序列 `0x400→配置→0x980` **没有证据做了这个轮询**，也没证据"清 prog"与"resume"之间的顺序受控。

**检查动作**：
```
mmw 0xE0041000 0x00000400 0   ;# set prog
# ... 写 TEEVR/TECR1 等
mmw 0xE0041000 0 0x00000400   ;# clear prog
# 轮询：
mdw 0xE0041010                 ;# 重复读 ETMSR，直到 bit1=0
```
**如果是这个原因，应看到**：清 prog 后 ETMSR bit1 **在一段时间内仍是 1**（没轮询就 resume → 配置未 latch）。轮询到 bit1=0 再 resume 后，重抓不再全 idle。

> 注：蓝方现在读到 ETMSR=0（bit1=0）说明**某个时刻**确实退出了 prog。但若退出发生在 resume 之后、或与写 TEEVR 的相对顺序错了，配置仍可能没 latch 进运行态。与嫌疑 1 联动验证。

---

### 🥉 嫌疑 3（必查，单条命令）：非侵入式调试授权 NIDEN 未授予

TraceEnable 被**授权信号 NIDEN（Non-Invasive Debug Enable）**全局门控。NIDEN 没 assert，ETM 即使配置完美也不产 trace，且 ETMSR bit2 恒 0。STM32F429 通常在调试连接时授予，但这是**从没被独立验证的前提**，且只需一条命令。

**检查动作**：
```
mdw 0xE0041FB8     ;# ETMAUTHSTATUS（ETM Authentication Status, offset 0xFB8）
```
**期望值**：NID(非侵入)相关位为"granted"——典型 `0x...A` 模式（bits[1:0]=10 表示 NSNID implemented+granted，bits[3:2] 同理）。
**如果是这个原因，应看到**：ETMAUTHSTATUS 显示 NID **not granted**（对应位 = `01` implemented-but-not-enabled）。→ 授权被门控，查 DBGMCU / 调试授权链。

---

### 4（HLA 强相关，你点名的方向）：ETM 软件锁未解 / 写入被 HLA 静默丢

ETM 有 **Lock Access Register（ETMLAR, 0xE0041FB0）**。许多 HLA 流程不解锁就写，**写被静默忽略、回读可能走 debug 旁路看到旧值或部分值**。你已经发现 ETMTECR1 写不进（蓝方归因"0 比较器 RAZ/WI"——**这点蓝方是对的**：ETMCCR[3:0]=0 → 无比较器 → ETMTECR1 的区域/start-stop 位包括 bit24 全是 RAZ/WI，所以 ETMTECR1=0 不是 bug，别再纠结它）。但**锁状态要独立确认**。

**检查动作**：
```
mww 0xE0041FB0 0xC5ACCE55   ;# 写 ETMLAR 解锁
mdw 0xE0041FB4               ;# ETMLSR：bit1(Locked)应=0
# 然后逐寄存器 write-then-readback 全量 diff：
mww 0xE0041000 0x00000d80 ; mdw 0xE0041000
mww 0xE0041020 0x0000006f ; mdw 0xE0041020
mww 0xE0041200 0x00000002 ; mdw 0xE0041200   ;# ETMTRACEIDR 见嫌疑5
```
**如果是这个原因，应看到**：解锁前 ETMLSR bit1=1（锁着）；解锁后某些之前"写不进"的寄存器开始能写进。

**更狠的证伪（绕开 HLA）**：换传输——新版 OpenOCD 用 ST-Link 的 **dapdirect** 模式拿到真 AP 访问：
```
adapter driver st-link
transport select dapdirect_swd
```
或直接换 **CMSIS-DAP** 固件的探针。**如果 dapdirect/CMSIS-DAP 下同样的序列就出 trace**，坐实是 HLA 静默丢写。

---

### 5（便宜必做）：ETM Trace ID = 0 导致 formatter 丢弃

**ETMTRACEIDR（0xE0041200）** 是 ETM 在 ATB/TPIU formatter 里的流 ID。若为 0，部分 formatter 实现把它当 null 流丢弃 → TPIU 只剩 idle。

**检查动作**：`mww 0xE0041200 0x2`（设非零，如 2），重抓。
**如果是这个原因，应看到**：重抓后出现带 ID=2 的 formatter 帧（FPGA 抓到的不再是纯 `0x7fff`）。

---

### 6（按 M4 TRM 逐位核，别用通用 ETMv3.5 理解）：ETMCR 使能值

你点名的对——**用 ETM-M4 TRM（DDI0440C）逐位核 ETMCR，不要用通用 ETMv3.5**。两个具体点：

- **0x980 vs mcuoneclipse 给 F407 的 0xd90/运行态 0x990**：差的是 **bit4**。在 ETM-M4 上 bit4 属端口/模式相关域。Cortex-M ETM 的 trace 端口宽度其实由 **TPIU CSPSR** 定（你已设 4-bit），ETM 端口位多为 RAZ/WI——所以 bit4 多半不影响。**但这是"按通用理解设了 0x980、没按 DDI0440 核 bit4"的典型盲点，花 2 分钟比对一次**：
```
# 试运行态值 0x990（带 bit4），按嫌疑2的 prog 时序写
```
**如果是这个原因**：0x990 出 trace、0x980 不出。

- **ETMCCR=0x8c842000 vs TRM 复位 0x8c802000，差 bit18**：ETMCCR 是**只读配置码**，bit18 只是描述"这颗 ETM 比通用复位值多一个 external input 资源"，**不需要也不能配置**。蓝方不必纠结它——它只告诉你这是带额外外部输入的 ETM 变体，与 idle 无关。**划掉这条担心。**

---

### 7（重新审视那个"已验证"）：示波器"确认 TRACED 有信号"是假阳性

`161e868` 声称"ETM 4-bit trace enabled + verified on scope"。**结合现在的事实——抓到的 100% 是 idle——那次示波器看到的"有信号"，看到的就是 idle/sync 的方波，不是真 trace。** 这是"从一开始就成立的假验证"：它证明了 TRACECLK+TRACED 在翻转，没证明里面是指令 trace。**把这条"已验证"从绿色降级**，它误导了后续所有判断（让大家以为"信号都出来了，只差解码"）。

---

## 3. DBGMCU_CR = 0xe7 逐位复核（你要求的）

`0xe7 = 0b1110_0111`：

| bit | 字段 | 值 | 判定 |
|---|---|---|---|
| 0 | DBG_SLEEP | 1 | 调试时 sleep 保持时钟 ✓ |
| 1 | DBG_STOP | 1 | ✓ |
| 2 | DBG_STANDBY | 1 | ✓ |
| 4:3 | 保留 | 0 | ✓ |
| 5 | TRACE_IOEN | 1 | trace IO 开 ✓ |
| 7:6 | TRACE_MODE | 11 | 4-bit sync ✓ |

`0xe7 & 0xc0 = 0xc0`（TRACE_MODE=11 ✓），`0xe7 & 0x20 = 0x20`（TRACE_IOEN ✓）。**DBGMCU_CR 确实全对，这个字节不是问题**——蓝方这条结论成立。低 3 位的 sleep/stop/standby 即使不置位也只影响低功耗态，与"运行态全 idle"无关。**别在 DBGMCU 上继续花时间。**

---

## 4. 判断："转 ITM+DWT" 是合理止损还是逃避？

**两者都是——但蓝方用错了它的定位。** ITM+DWT 不该是"放弃 ETM 的退路"，它应该是**当前最该立刻做的那一刀诊断**（bisection），因为它能一次性定位根因在哪一侧：

> **关键实验**：让 **ITM 软件打点 / DWT 周期采 PC**，走**同一条 TPIU 4-bit 并行口**出来，FPGA 采样/抓取/Orbuculum 全不动。

- **若 ITM/DWT 数据真的出来了**（FPGA 抓到非 idle、`orbtop` 出函数热度）→ **TPIU、ATB 时钟、formatter、引脚、采样链 100% 健康，问题被钉死在 ETM 专属环节**（= 嫌疑 1/2/3/4 之一：运行态配置/prog 时序/NIDEN/锁）。**这时绝不该放弃 ETM——你已经把战场缩到 4 个单命令检查。**
- **若 ITM/DWT 也只出 idle** → 问题是**共享层**（TPIU formatter 输入 / ATB / 这条路的根本接法），那么前面死磕 ETM 寄存器从一开始就是错的方向，但也说明 ETM 不是元凶。

**所以**：转 ITM+DWT 去**验证 PC 侧符号还原链路 + 定位故障侧**，是 100% 正确且高价值的下一步。但把它写成"ETM 卡在寄存器层之下、放弃 ETM"是**逃避**——因为：

1. **J-Trace 支持列表里就有 F429**（蓝方自己查到了），这颗芯片的 ETM 指令 trace 确定能 work，是使能细节问题，不是硬件能力缺失；
2. **根因已被逻辑收敛到"ETMSR bit2 为何=0"这一个点**，且只剩 4 个**每个一条 OpenOCD 命令**的未验证前提（运行态回读 / prog 轮询 / ETMAUTHSTATUS / ETMLAR+dapdirect）；
3. 这些检查的成本是分钟级，远低于"换整条 ITM 路线 + 接受只能拿 PC 采样而非完整指令流"的能力损失。

**结论**：ITM+DWT 立刻做——但作为**诊断 bisection** 和 **PC 链路验证**，不是作为对 ETM 的判决。**在跑完第 2 节嫌疑 1/2/3/4 这四条单命令检查之前，不允许宣布"ETM 不可行"。**

---

## 5. 给蓝方的执行清单（按顺序，全部可证伪）

| # | 动作 | 期望（正常） | 若异常说明根因 |
|---|---|---|---|
| 1 | ITM+DWT 走同 TPIU 口 bisection | ITM 出数据 → 问题在 ETM 侧 | 都 idle → 共享层/ATB |
| 2 | resume 跑 2s 后二次 halt 读 ETMCR/ETMSR | 仍 0x980 / bit1=0 | 变回 0x411 → 配置没活到运行态（嫌疑1）|
| 3 | 清 prog 后轮询 ETMSR bit1→0 再 resume | 重抓非 idle | 没轮询就 resume → 未 latch（嫌疑2）|
| 4 | `mdw 0xE0041FB8` ETMAUTHSTATUS | NID granted | not granted → NIDEN 门控（嫌疑3）|
| 5 | `mww 0xE0041FB0 0xC5ACCE55` + 读 ETMLSR + 全量 write/readback diff | 锁解开、写都进 | 锁着/写丢 → 软件锁/HLA（嫌疑4）|
| 6 | 换 `dapdirect_swd` 或 CMSIS-DAP 重跑序列 | 出 trace | 出 trace → 坐实 HLA 静默丢写 |
| 7 | `mww 0xE0041200 0x2` 设 TraceID 重抓 | 出带 ID 帧 | 之前 ID=0 被丢（嫌疑5）|
| 8 | 试运行态 ETMCR=0x990（按 DDI0440 核 bit4）| 出 trace | bit4 是 M4 必需位（嫌疑6）|

**最高优先级：#1（bisection 定位侧）+ #2（运行态回读，戳穿"读回 0x980"假象）。** 这两条做完，根因侧基本就定了。

---

## 6. 一句话结论

**蓝方把"配置全对却 idle"摊成一片模糊嫌疑（时钟/电源/ATB/HLA）是没做排除——而 TPIU 正在发 idle 这一个事实就已证明 trace 时钟、formatter、引脚、采样链全部健康，问题唯一地塌缩为"ETM 没往 ATB 放数据 / ETMSR bit2 为何=0"。这个 bit2 的门控只剩四个从没被独立验证的前提：①配置没活到运行态（读回 0x980 是 halted 假象，最可能）②prog 位清除没轮询 ETMSR bit1 ③NIDEN 授权（一条 `mdw 0xE0041FB8`）④ETM 软件锁/HLA 静默丢写（解锁 + 换 dapdirect）。ETMTECR1=0 和 ETMCCR bit18 是红鲱鱼（蓝方对，别再查）；DBGMCU=0xe7 确实全对。转 ITM+DWT 是必须立刻做的诊断 bisection（同口验证 TPIU 链路 + 定位故障侧），但不是放弃 ETM 的理由——在跑完那四条单命令检查前，ETM 不可判死刑。**

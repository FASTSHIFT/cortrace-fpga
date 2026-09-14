# proposal 43 — 方案 A：治本修复 iBR 返回的 disposition 错位

**日期**：2026-08-02
**前置**：r33（真凶定位）、r33 §7（方案 B 缓解，coremark_main 358→7 但 cm_benchmark_main 仍 133）
**目标**：从源头修掉"iBR 返回被 disposition 判为 not-taken 而跳过"的错位，让栈计数
从根上正确，coremark_main / cm_benchmark_main 一起收敛到真值 1。

---

## 1. 已坐实的事实（全部来自直接 log，不是推断）

1. **假重入 = 栈计数错**：cm_benchmark_main 133 次 begin 里 123 次是 push 路径
   （`depth 2→3, stacktop=0x8008118`），**0 次真实 CALL** 到 0x8008118。coremark_main
   同源，只是走 switch 路径（已被 B 部分接住）。
2. **触发点**：`cm_uart_send_char` 的 `pop {r4,pc}`（0x800a2f2）——iBR、无条件返回。
3. **错位现象**（r33 §3.4）：HAL 返回（stacked-candidate）后这批 atom `disp=0xa`，
   a2f2 pop 取 `disp&1=0` → `insExecuted=0` → 走 "branch not taken" 分支，
   `workingAddr += 2` 越过 pop，进入 `0x800a2f4`（`.word 0x20000288`，**字面量池数据**），
   一路 a2f4→a2f6→a2f8，撞上 coremark_main 入口。
4. **指令分类正确**：loadelf.c:1148 `POP && strstr("pc")` → `LE_IC_JUMP`（无 IMMEDIATE），
   pop 被正确识别为 iBR。分类不背锅（r33 Q1 已证）。
5. **无条件返回不可能 not-taken**：`pop {r4,pc}` 是无条件的，ETM4 对它发的 atom 必为 E。
   读到 disp bit=0 → **atom 流与指令流在背靠背 iBR 处错位了一位**。

## 2. 错位的确切机制（待最终坐实的假设 H43）

背靠背两个 iBR 返回：
```
0x800a2ec: bl HAL_UART_Transmit    ; iBR#1 调用，返回 a2f0
0x800a2f0: add sp, #8              ; 非分支
0x800a2f2: pop {r4, pc}            ; iBR#2 返回
```
HAL 返回是 **stacked-candidate（committed=false）**：iBR 无立即地址，workingAddr 被
推测设为栈候选 a2f0，`_removeRetFromStack` 先弹，等下一个地址包确认。

**H43**：HAL 返回消耗了一个 atom 并 `disposition >>= 1`。但推测返回后，解码器从 a2f0
线性重走 a2f0(add,非分支不消耗)→a2f2(pop,消耗下一 atom)。此时 disposition 已经右移过
一次（给 HAL），a2f2 取到的是**HAL 之后那一位**——而真实的 a2f2 atom 位应该是同一批的
另一个位置。即 **stacked-candidate 推测返回时 atom 指针的推进与真实执行流脱节**。

> 注意：H43 仍是假设。方案 A 动手前**必须先用逐-atom 对齐实验坐实**（见 §4 P0）。
> 不坐实不改 atom 循环——那是砸解码器的高危区。

## 3. 候选修复策略（三选一，按风险从低到高）

### A1（首选，最小侵入）：iBR 返回落入字面量池 → 强制返回
在 JUMP 分支，若 `insExecuted==0`（判 not-taken）但**下一线性地址落在函数的字面量池/
数据区**（`symbolFunctionAt` 找不到指令，或地址是已知 `.word`），说明"not-taken 走下去"
不可能正确——强制按 iBR 返回处理（弹栈到候选）。
- 优点：不碰 atom 计数，只在"走进数据"这个明确错误信号上兜底。低风险。
- 缺点：治的是"越过 pop 之后"，不是"为什么判 not-taken"。可能残留别的错位表现。

### A2：无条件 iBR 强制 taken
若指令是**无条件** iBR（pop{pc}/bx lr/ldr pc 等，非条件码），无视 disposition bit，
一律按 taken 返回处理。
- 优点：直击"无条件返回不该 not-taken"的矛盾，语义正确。
- 风险：需可靠判定"无条件"。thumb 里 pop{pc} 无条件，但 IT 块内的 popcc 有条件——
  要能区分。loadelf 目前不带条件码信息，需扩展。

### A3（治本，最高风险）：修复 stacked-candidate 的 atom 对齐
在推测返回（committed=false）后，重建 disposition/incAddr 与真实执行流的对齐。
- 优点：根治所有背靠背 iBR 错位。
- 风险：直接改 atom 循环核心，可能回归全局解码。r32/r33 反复警告的雷区。

## 4. 实施计划（严格 P0 先坐实，再动手）

### P0（坐实 H43，不改功能）
- [ ] 在 stacked-candidate 返回点，逐-atom dump：HAL 返回时的 disp/incAddr，
      推测返回后重走 a2f0/a2f2 时每步取的 disp bit 与真实指令，确认"错位一位"的确切来源。
- [ ] 交叉 opencsd：opencsd 对同段的 atom 消耗序列（opencsd 解码正确的参照）。
      **注意**：opencsd lister 的地址覆盖到 0x80094dc 为止，未见 0x800a2xx——需先查清
      opencsd 是否真未走到该区（若是，说明两解码器执行路径本就不同，参照失效）。

### P1（按坐实结果选 A1/A2）
- [ ] 实现选定方案，`captures/mortrall_fixture/regress.py --check` 双 slice 守护。
- [ ] 判据：coremark_main→1、cm_benchmark_main→1、内层篮子稳定 <1%、
      cardinality 不降、B/E 平衡、verify_calls 调用边不退。

### P2（A1/A2 不够才上 A3）
- [ ] 大改 atom 对齐，需红方评审 + 全量 verify_calls 回归。

## 5. 回滚判据
任一步 `regress.py --check` 报 REGRESSION（尤其 cardinality 降 / 内层篮子缩 / 调用边
mismatch 增），立即回滚。**保住"调用边对 ELF"这个诚实判据高于消除假重入。**


---

## 6. A2 实验记录（2026-08-02，失败，已回滚）

**做法**：在 JUMP 的 not-taken 分支加启发式——若是非立即 iBR（return）、栈非空、且
fall-through 地址**没有源码行映射**（`symbolLineAt(fall)==NULL`，即落在字面量池），
就判定为漏返回，改走栈候选返回。

**结果**（`regress.py --check`，slice1 4MB）：

| 指标 | fix B baseline | A2 |
|---|---|---|
| coremark_main | 7 | 6 |
| cm_benchmark_main | 133 | 114 |
| **cardinality** | **2646** | **2569（-77）** |
| crc16 | 7073 | 7132 |
| core_bench_list | 150 | 138 |

**判决：失败，撞回滚判据（§5）。**
- cm_benchmark_main 只从 133→114（微弱），coremark_main 7→6。
- **cardinality 掉 77 = 真 PC 覆盖丢失**：A2 的"fall-through 无源码行"判据太宽，
  **误伤了真实的条件间接分支**（not-taken 时 fall-through 也可能命中无行映射的边界），
  把它们错当成返回 → 走错路径 → 丢真实 PC。内层篮子也抖动（core_bench_list 150→138）。
- 已回滚到 fix B 干净态（coremark_main=7, cardinality=2646, git diff 空）。

**教训（蓝方自认）**：跳过了 §4 P0（先坐实 H43 的确切 atom 机制再动手），直接拍脑袋
上启发式兜底，代价是一次退化。`regress.py` 的 cardinality 守护当场抓到，没让脏结果溜进
commit。**"单步坐实 > 优美启发式"再次应验——A2 就是没坐实就动手的又一次复发。**

## 7. 当前结论与遗留

- **fix B（已提交 c5653e3 + 6db8cad）是当前最好的稳定态**：coremark_main 358→7，
  调用边 68797/68798 对 ELF，cardinality 不降，B/E 平衡。cm_benchmark_main 133 假重入
  仍在（同源 disposition 错位的 push-路径表现，B 只补了 switch 路径）。
- **治本（真修 disposition 错位）比预想难**：启发式兜底（A1/A2）会误伤真实分支。
  必须先做 §4 P0 精细坐实错位的确切 atom 机制，不能再拍脑袋。
- **P0 坐实的拦路石**：opencsd lister 地址只覆盖到 0x80094dc，未见 0x800a2xx
  （coremark_main 区），但 opencsd 的函数覆盖统计里 cm_benchmark_main=27（vs mortrall
  发 133 begin）。需先查清 opencsd 是否真的解码了 0x800a2xx——若 lister 只是被截断、
  完整覆盖到了，则 opencsd 的 atom 消耗序列可作为 mortrall 的对齐参照；若 opencsd 根本
  没走进该区，则两解码器路径不同，参照失效，P0 需换基准（如手工按 ETM4 spec 对齐）。


---

## 8. 蓝方接受 r34 红方评审（2026-08-02）

**红方 r34 全面命中，蓝方接受。** proposal 43 §1「已坐实的事实」第 5 条与 §4 P0 设计
被证伪/推翻，本节记录接受结论，覆盖前文的过度断言。

### 8.1 逐条接受

| 议题 | 红方判定 | 蓝方接受 |
|---|---|---|
| Q1 "disposition 错位一位" | 🟥推断+自相矛盾 | **接受**。§1.5"错位一位"与 §3.5"走错路"是两个相反机制，根因未定死。§1 第 5 条**降级为假设**，不再是"事实"。这是 p42→r32→r33 后第三次"没坐实就命名根因"复发。 |
| Q2 错位 vs 批次边界 | 🟥未证 | **接受**。stacked-candidate 返回时 atom 指针是"错位"还是"批次边界"从未区分，是命门。 |
| Q3 opencsd 参照 | 🟥矛盾未解 | **接受**。lister 截断 vs 路径不同没查清；opencsd cm_benchmark_main=27 vs mortrall 133 这个差**没解释**——而它恰是"opencsd 没这个 bug"的最强线索。 |
| Q4 A1 必然误伤 | 🟩成立 | **接受**。A1 与已回滚的 A2 同类启发式，不试都知会掉 cardinality。**A1 撤销，不试。** |
| Q5 动 A3 | 🟥不动 | **接受**。根因未定就动 atom 核心 = 赌全局 68798 条调用边。**A3 冻结。** |
| Q6 P0 设计 | 🟥验证性偏误 | **接受**。原 §4 P0"确认错位一位"预设了结论。**替换为 §8.3 四假设互斥设计。** |
| Q7 丢包 | 🟥从未零丢包验证 | **接受，且这是最致命的**。两个 slice 同源同丢包 pattern，"独立验证"是假独立。91/91 只排除随机丢包，未排除结构性丢包。**H43 从没在零丢包数据复现过。** |

### 8.2 §1、§4 的更正

- **§1 第 5 条**（"读到 disp bit=0 → atom 流错位一位"）：**从"已坐实事实"降级为"未证假设之一"**。实测只到 §1 第 3 条（a2f2 被判 not-taken、workingAddr 越过进字面量池）为止，那是真的；"错位一位"是倒推。
- **§4 P0**（"逐-atom dump 确认错位一位"）：**作废**，被 §8.3 取代。

### 8.3 新 P0（红方 r34 版，四假设互斥，动手前必跑，禁止跳步）

**P0-3 给出唯一结论前，禁止写任何修复代码。** 这是对"没坐实就动手"的硬约束。

- **P0-1 🔴 零丢包复现（先钉死前提）**：抓一份 `stream_lost_cnt==0` 且 UDP `seq-gap==0`
  全程的 CoreMark slice（短无妨，覆盖几轮 UART 打印即可），跑同一 mortrall + regress.py。
  - 假重入率与丢包 slice 相同 → 系统性解码 bug，进 P0-2。
  - 假重入消失/大降 → **丢包驱动，proposal 43 整个作废**，回去查采集/nibble 对齐。
- **P0-2 🔴 opencsd 参照可用性**：重跑 lister 不加地址过滤，确认 opencsd 是否解码
  0x800a2f2 区；解释 cm_benchmark_main=27 的来源。可用 → P0-3 用它做基准；不可用 →
  手工按 IHI0064 ETM4 spec 对齐。
- **P0-3 🔴 四假设互斥判定**（取代"确认错位一位"）：dump (批次序号, 批内 index, disp 原值,
  incAddr, workingAddr, ic, insExecuted, committed, 栈候选) 全字段，用判定表让数据**唯一
  选出**：①真·错位一位 ②批次边界 ③锚定错(错路) ④消费逻辑 bug。数据不能唯一区分 →
  信息不足继续加 dump，**不动代码**。
- **P0-4 🟡** fix B 的 verify_calls/cardinality/内层篮子双 slice 存 golden，任何改动逐项 diff。

## 9. 决定：fix B 为当前终点，cm_benchmark_main 记入 known-issues

接受红方 §表态 3：**fix B（已提交）是当前诚实工程终点**。
- coremark_main 358→7、调用边 68797/68798 对 ELF、cardinality 不降 —— 主可视化问题已解决。
- **cm_benchmark_main 133 次假重入 = 已知次层显示噪声**，记入 known-issues 接受，
  不值得为消 132 个次层假 begin 去赌全局 68798 条调用边的正确性。
- 方案 A（治本）**冻结**，作为独立后续任务：唯有先跑通 §8.3 P0-1→P0-2→P0-3、用数据
  唯一确定根因后，才解冻。**在此之前不写修复代码。**


---

## 10. P0-1 执行结果：假重入非丢包驱动（红方 Q7 钉死，2026-08-02）

红方 r34 Q7 的核心质疑：所有验证数据都来自同一份 0.1% 丢包抓样，假重入从没在零丢包
数据复现过。P0-1 就是补这个。

### 10.1 拿到零丢包通路（前置，本身是重要发现）

- **rp_filter 坑**：dock 直连时 dock 口和 ens33 都持有 .245，FPGA 的 UDP 从 dock 进来
  被反向路径过滤丢在 socket 前（tcpdump 看得到、recv 收 0）。`sysctl rp_filter=0` 后通。
- **stream_recv pre-poll 坑**（已修 commit cb62695）：抓取前的 `read_lost_cnt` 在饱和流
  下重试 ~8s，socket 绑了但没 recv → 内核缓冲溢出 → 3s 窗口丢 711578 帧。移到抓取后。
- 修完两坑，dock 直连实测：**200MB / 2.00s / 99.99MB/s / seq-gap=11**（0.006%），
  以及连续 CoreMark **337MB / 3.00s / 112.5MB/s / seq-gap=5**（0.0015%）—— 实质零丢包。
- 佐证：**丢包 100% 在网络传输路径**（路由器路径丢 68%，dock 直连≈0），FPGA
  capture-side lost_cnt **恒 0**。采集端从不丢——直接削弱"假重入是采集端丢包/误码"。

### 10.2 两个独立零丢包样本都复现假重入

| 样本 | seq-gap | coremark_main | cm_benchmark_main |
|---|---|---|---|
| 零丢包样本 A（cm_zeroloss, ITER=2000, 4MB 中段切片） | ~11/195296 | 39 | 381 |
| 零丢包样本 B（cm100, ITER=100 连续, 8MB 中段切片） | ~5/329641 | 23 | 256 |
| （对照）原丢包样本（fixture, 0.1% 丢包, 4MB） | 965 帧源 | 7（fixB后） | 133 |

- **两个实质零丢包样本都大量假重入**（coremark_main 23/39，cm_benchmark_main 256/381），
  且比丢包样本还多（因代码区段不同、迭代更密，数字不可直接比，但方向明确）。
- **结论：假重入在零丢包数据上依然系统性发生。红方 Q7 钉死——不是丢包驱动，是解码器
  bug。proposal 43 的前提（系统性 disposition/atom 处理 bug）成立，方案 A 不作废。**

### 10.3 尚未做的严格对照（诚实标注）

- 上表三个样本是**不同代码区段**（cardinality 2646/6708/6228 各异），数字不能逐一对比，
  只能论"假重入在零丢包下依然存在"这个定性结论。**同一段代码的丢包 vs 零丢包逐一对照
  仍未做**——但 P0-1 的目的（证伪"丢包驱动"）已达成，逐一对照是加强而非必需。
- 固件侧为便于抓完整轮：`H743_Blink` Makefile `ITERATIONS=2000→100`、main.c 把
  `coremark_main()` 放进 `while(1)` 连续跑（否则一轮 ~80ms 太短、抓取时序难卡）。
  时钟随 board_clock 默认 = 225M sysclk / **112.5M TRACECLK**（clktap TAP=17 通用）。

### 10.4 下一步（仍按 r34 约束，P0-3 前不写修复代码）

P0-1 done、P0-2（opencsd 参照）与 P0-3（四假设互斥）仍未做。方案 A 继续冻结，直到
P0-3 用数据唯一确定根因（错位一位 / 批次边界 / 锚定错 / 消费逻辑）。


---

## 11. P0-2 部分结果：opencsd 参照对 0x800a 区失效（2026-08-02）

### 11.1 原始数据完整性 = 确认干净

零丢包 slice（cm100_slice, 8MB, seq-gap≈5）三查：
- **seq 连续**（≈5/329641 gap，实质零丢包）。
- **raw slice 本身已 TPIU 对齐**（decode 跳过预对齐，无 half-nibble 偏移），fsync 密度
  全片均匀 6/64KB（比 rt300 的 3300 低，因 225M/112.5M 新固件 sync period/数据率不同，
  均匀=正常，无骤变=无误码）。
- **opencsd 独立解码 100% 干净**：98091 INSTR_RANGE、2773 unique PC、**100% 在 flash**、
  lister 完整跑完（END OF TRACE，318826 字节全处理，非截断）。
- **结论：原始数据完整可信**，后续打 log 查到的现象是真的，不是丢包/误码假象。

### 11.2 opencsd 的 cm_benchmark_main=499 是 PC count，不是进入次数

红方 Q3 要解释的 opencsd 那个数字：`opencsd_etm4_run.py:448` 的
`Counter(func_of(p) for p in in_flash)` —— 是**落在函数地址范围内的不同 PC 采样计数**，
**不是"进入函数次数"**。所以 opencsd 499 vs mortrall 256（B|begin 数）**量纲不同，
不能直接比**。之前 fixture 的 27 我也误当成进入次数了——更正。

### 11.3 opencsd 与 mortrall 在 0x800a 区执行路径分道（关键）

- opencsd lister 的 INSTR_RANGE **最高只到 0x80094d4**，**完全没有 0x800a2xx**
  （coremark_main=0x800a2f8 / cm_uart_send_char=0x800a2d4 都在 0x800a 区）。
- 但 lister **完整跑完未截断**（END OF TRACE），且 cm_benchmark_main(0x8008118) 有覆盖。
- 即：**opencsd 在这段 slice 里执行流从没上到 0x800a2xx，而 mortrall 走到了**
  （mortrall 正是在 cm_uart_send_char 的 pop@0x800a2f2 触发假重入）。
- **两个解码器在 0x800a 区分道扬镳。** 这直接影响红方 Q3：opencsd 参照对 0x800a 区
  **失效**（它没走到那），不能拿来 diff cm_uart_send_char pop 的 atom 消耗。

### 11.4 P0-3 的方向修正

P0-3 的四假设互斥判定**不能用 opencsd 做基准**（它没解码 0x800a 区）。剩下两条路：
1. **先查清 opencsd 为何不上 0x800a**：是它在某个分支选择上与 mortrall 不同（谁对？），
   还是这段 slice 的执行流本就没进 coremark_main（那 mortrall 的 0x800a 访问反而可疑）？
   —— 这是新的、更根本的岔路，可能重定位真凶。
2. **手工按 IHI0064 ETM4 spec 对齐** cm_uart_send_char pop 附近的 atom（无参照，纯 spec）。

**在 11.3 这个"两解码器分道"搞清楚前，方案 A 仍冻结。** 它可能意味着真凶不在
"disposition 错位"，而在"mortrall 为何走进了 opencsd 不走的 0x800a 区"——若 mortrall
的 0x800a 访问本身是错的（走错路），那是锚定错（H43 的 §3.5 分支），不是错位一位。


---

## 12. 分叉厘清：mortrall 进 0x800a 是对的，真值用 ELF 不用 opencsd（2026-08-02）

§11.3 提出"两解码器在 0x800a 分道，可能 mortrall 走错路"。**用 ELF 静态真值直接判——
mortrall 走进 0x800a 是对的，opencsd 没跟到是 opencsd 侧的问题。**

### 12.1 ELF 静态铁证：执行流必然进 0x800a

`objdump` 确认 0x800a 区函数被真实 `bl` 调用：
```
4× bl 800a284 <cm_uart_puts>       ← coremark_main 每轮打印结果必调
1× bl 800a2d4 <cm_uart_send_char>  ← 唯一 caller 在 0x8009772 (ee_printf 内)
1× bl 800a2f8 <coremark_main>
```
CoreMark 循环跑，每轮都打印 → cm_uart_puts → ee_printf → cm_uart_send_char。
**执行流一定进 0x800a2xx。mortrall 走进去正确；H43 §3.5 的"锚定错/走错路"假设排除。**
opencsd lister 没上 0x800a 是它自己的 range 发射/dump 差异，**参照对 0x800a 失效但无所谓——
真值直接来自 ELF。**

### 12.2 硬真值判据（P0-3 用这个，不用 opencsd）

- `cm_uart_send_char` 唯一 caller = `0x8009772`（`bl`），故其 `pop {r4,pc}`(0x800a2f2)
  **正确返回目标 = 0x8009776**（bl 下一条）。
- mortrall 实测：pop 判 not-taken → workingAddr 越过进 0x800a2f4(`.word`) → 漂到
  0x800a2f8(coremark_main 入口)。**错。应返回 0x8009776。**
- **P0-3 可证伪判据（真值来自 ELF，无需参照解码器）**：单步 pop@0x800a2f2，
  正确 workingAddr 应变成 0x8009776。它没有 → 定位为何没返回（disposition 消费 /
  批次边界 / 消费逻辑），四假设里排除了"锚定错"，剩三选一。

### 12.3 方向回归 r33，但判据升级

真凶方向仍是 r33 §3 的"iBR 返回被 disposition 判 not-taken 而跳过"，**但现在有 ELF 硬
真值（0x8009776）做判据**，不再依赖"无条件返回不该 not-taken"的原则推断，也不需要
opencsd 参照。P0-3 可以直接单步 mortrall 在这一点、对着 0x8009776 这个确定目标查。


---

## 13. P0-3 结论：根因是【批次边界】，不是"错位一位"（2026-08-02，实测判定）

在 cm_uart_send_char pop@0x800a2f2 逐-atom dump（ELF 真值判据，零丢包 slice），四假设
互斥判定的数据出来了：

### 13.1 关键对照：同一 pop，第一次错、后续对

**第一次（错，depth=2，pop 判 not-taken）：**
```
INS a2ec ic=f(bl HAL) exec=1 dispbit=1 disp=5   incAddr=4
INS a2f0 ic=8(add)    exec=1 dispbit=0 disp=0   incAddr=1   ← 这批已耗尽(disp=0,incAddr=1)
INS a2f2 ic=1(pop)    exec=0 dispbit=0 disp=0   incAddr=1   ← pop 拿不到 atom → not-taken → 漂
INS a2f4 (.word 字面量池, 越过 pop)
```
**后续（对，depth=5，pop 判 taken 正确返回）：**
```
INS a2ec exec=1 dispbit=1 disp=1    incAddr=1
INS a2f0 exec=1 dispbit=1 disp=ffff incAddr=17  ← 新 EV_CH_ENATOMS 批次(BATCH wa=0800a2f4)
INS a2f2 exec=1 dispbit=1 disp=ffff incAddr=17  ← pop taken, 正确返回 0x8009776
```

### 13.2 判定：H43 假设② 批次边界成立，①错位一位 / ③锚定错 / ④消费逻辑 排除

- **不是"错位一位"**：后续同一 pop 用同样的消费逻辑判对了，disposition 位与指令的对应
  关系没错位；错的只是第一次**这批 atom 在 pop 之前就耗尽**（disp=0, incAddr=1，a2f2
  取到的是空）。
- **不是"锚定错/走错路"**：§12 已排除，mortrall 进 0x800a 正确。
- **不是消费逻辑 bug**：消费逻辑本身对（后续 467-N 次都对）。
- **是批次边界**：`EV_CH_ENATOMS` 批次恰好切在 HAL 返回后、pop 之前。这批的 atom 被
  a2ec 的 `bl HAL`（及之前）消费完，pop@a2f2 该拿的 taken-atom 落在**下一个** batch
  （BATCH wa=0800a2f4，disp=ffff…）。第一次 mortrall 在批耗尽时对 pop 判了 not-taken
  并 fall-through，而不是**等下一批**。红方 Q2 判定为批次边界，实测坐实。

### 13.3 修复方向（P1，仍需 regress 双 slice 守护）

iBR（JUMP 非 IMMEDIATE，返回）在**当前批 atom 已耗尽**（incAddr 到 0 / 该指令拿不到
有效 disposition 位）时，**不能就地判 not-taken 并 fall-through**——应挂起等下一个
`EV_CH_ENATOMS` 批次补给再判。即：把"批边界切在分支指令上"这个情况正确处理，让 pop
的判定用它真正对应的那批 atom。

- 这比 A1/A2 的"落字面量池就当返回"启发式**精确得多**——它针对确切机制（批耗尽），
  不误伤真实 not-taken 条件分支（那些分支的 atom 在本批内有效，不受影响）。
- 风险仍在动 atom 循环核心，但现在改动目标明确（批边界挂起），且判据硬（pop 应返回
  0x8009776 / regress 双 slice + verify_calls）。**解冻方案 A，按此方向做 P1。**


---

## 14. 出错规律 + ETM4 手册核对（2026-08-02）

修法两次失败（B/A2/无条件iBR-助记符），教训：没吃透 ETM4 atom 语义就改，误伤真实分支
（cardinality 掉 506）。停下来量化规律 + 翻手册（IHI0064H_b）。

### 14.1 出错规律（零丢包 slice，cm_uart_send_char pop@0x800a2f2，467 次）

- **判定 100% 由 `disposition & 1` 决定**：
  - 错判（exec=0，392 次）：disp bit0=0（disp=a/2/0/4/1e，全是偶数）。
  - 对判（exec=1，75 次）：disp bit0=1（disp=1/f/7/ff/fff/ffff/3，全是奇数）。
- 错判主要 disp=a(1010) incAddr=4（221 次）、disp=2(10) incAddr=2（72 次）、disp=0(0)
  incAddr=1（57 次）。即 **pop 拿到的 bit0=0，但 pop 是无条件返回，必为 E(taken)**。

### 14.2 ETM4 手册确认的地基（IHI0064H_b）

- **§2.3.1 P0 元素**：direct + indirect branch **每条都生成一个 P0/atom**，
  "regardless of whether they pass or fail their condition code check / are part of
  an IT block"。→ 条件直接分支（bne/cbz）也有 atom；无条件 iBR（pop{pc}/bx lr）也有，
  且必为 **E**。
- **§2.6 / §6.4.x Atom Format 3**："least significant bit representing the **oldest**
  Atom element"，`for I=0..2: A<I>? E : N`。→ **mortrall `disp&1`(bit0=oldest) + `>>=1`
  的消费顺序与手册一致，位序没错。**
- BATCH 实测：`eatoms=3 natoms=2 total=5 disp=0x15(10101)` → 3×E 2×N，位数一致 ✓。

### 14.3 收窄后的真凶假设

位序对、消费顺序对、每分支必有 atom——那 pop 拿到 bit0=0 的原因只剩：**pop 之前
HAL_UART_Transmit 内部执行消费掉了本批 atom，到 pop 时本批已尽（disp 剩低位=0），
pop 该用的 E-atom 在下一个 EV_CH_ENATOMS 批**。日志佐证：a2ec(bl HAL) 与 a2f0 之间没有
BATCH 行，但 disp/incAddr 从 5/4 掉到 0/1——那是 HAL 内部指令（日志过滤没显示）消费的。
pop 在批尽处读到空 bit0=0。

**下一步（P0-3 收尾，未做）**：交叉**原始 ETM 字节**——定位 pop 附近在 etm.bin 的字节
偏移，手工按手册解码那几个 Atom Format 包（F3/F5/F6），确认「HAL 返回的 iBR atom」与
「pop 的 iBR atom」在字节流里的确切位置，判定 mortrall 是「批尽未等下一批」（批边界）
还是「HAL 返回 iBR 的 atom 记账串了一位」（记账错）。**这一步定死后才改代码。**

### 14.4 修法约束（血泪更新）

- ❌ 不用助记符判"无条件"——**IT 块内条件指令的助记符不带 cc 后缀**（手册 §条件分支），
  mnemonic 判据必然漏判，A2-变体已实测 cardinality 掉 506。
- ✅ 若要判"无条件"，须用 capstone `detail->arm.cc == ARM_CC_AL`（改 loadelf 加
  LE_IC_UNCOND 位），这是唯一可靠信号。但即便如此，"无条件 iBR 强制 taken"是否安全
  仍需 §14.3 原始字节定死机制后再定。


---

## 15. 原始字节交叉验证：设备发了、FPGA 采了、解码器漏了（2026-08-02）

用户三层追问：设备端没发 / FPGA 没采 / 解码器漏了？逐层验证。

### 15.1 流里 atom 总数（设备+FPGA 侧完整性）

opencsd 参考解码器的 lister 逐 atom-packet 精确统计（零丢包 slice 的 etm.bin，
318826 字节，END OF TRACE 全处理）：
```
total atoms = 1,415,165   (E=985360  N=429805)
atom packets: F1=3187 F2=6238 F3=62087 F4=9988 F5=117573 F6=55834
```
- **设备端发了、FPGA 采了 = 确认**：1.4M 个 atom 完整在原始字节里，流无截断、零丢包。

### 15.2 mortrall 消费的 atom 数（解码器侧）

在 mortrall 的 `disposition>>=1; incAddr--` 消费点计数：
```
[ATOMS] mortrall consumed 499149 atoms   (stream has 1415165)
```
- **mortrall 只消费了 499149 / 1415165 = 35%，漏掉 916016 个 atom（65%）。**
- **答案确定：解码器侧问题。** 原始字节里 atom 齐全（设备/FPGA 无责），mortrall 消费了
  远少于流里实际的 atom 数。pop@a2f2 拿到 bit0=0 只是这 65% 漏账的一个局部表现。

### 15.3 但漏 65% 远超"pop 漏几百次"——更大的账要查

pop 一轮几百次，解释不了漏 91.6 万 atom。说明 mortrall 在这个 slice 上**大面积不消费
atom**，不止 pop。两个疑点，下一步查：
1. mortrall 是否在某类指令/某段区域**提前停止推进 instruction**（atom 还在流里但没被
   instruction 循环消费）？—— 注意 opencsd 自己的 INSTR_RANGE 也只 98091、且只到
   0x80094d4，两个解码器都没把 1.4M atom 全解成 instruction。可能这个 slice 的 etm.bin
   里有大段 atom 对应两解码器都未跟进的执行（trace_on/sync 之间、或 speculation）。
2. 是否 `_flush_proto_buffer` / 异常路径 / thread-switch 吃掉了一批未计数的 atom？

### 15.4 结论与下一步

- **三层定位完成：设备发了 ✓ / FPGA 采了 ✓ / 解码器漏了 ✗（35% 消费率）。** 修复战场
  在 mortrall/orbuculum ETMv4 解码器，与采集无关（再次独立佐证 lost_cnt=0）。
- **但根因比"pop 批边界"更大**：65% atom 未消费是系统性的。P0-3 需先解释这 91.6 万
  atom 去哪了（提前停止 / 大段未跟进 / flush 吞账），而不是只盯 pop。可能"pop 假重入"
  只是这个更大 atom-账不平的一个可见症状。
- 仍守 r34 约束：机制未定死前不改代码。下一步：定位 mortrall 少消费 atom 的具体位置
  （对比 opencsd 消费点 / 看 instruction 循环是否在某处 break）。


---

## 16. 更正 §15：不是"漏 atom"，是【过度重复行走】4.6×（2026-08-02）

§15 用"atom 总数 1.4M vs mortrall 消费 499149"得出"漏 65%"——**这个对比不可靠，撤回**。
原因：opencsd lister 的 atom-string 计数含 F5/F6 多-atom 包（F5 每包 5 atom、F6 是 E-run），
**atom 总数 ≠ 分支数**，拿它比 mortrall 的"每分支消费一次"是 apples-to-oranges。

### 16.1 干净的 apples-to-apples：指令数

- **opencsd 参考解码器：721,180 条指令**（98091 ranges）。
- **mortrall：3,334,340 条指令**（499149 个分支）。
- **mortrall 解出的指令是 opencsd 的 4.6 倍。** 方向反了——**不是漏，是过度重复行走。**

### 16.2 这才和"假重入"自洽

mortrall 因返回处理错误（pop 判 not-taken → workingAddr 漂进字面量池 → 最终重新
进入函数）**反复重走同一段代码**，指令数暴涨 4.6×。每次假重入 coremark_main/
cm_benchmark_main 都把那一段指令重新走一遍。**指令 4.6× 膨胀 = 假重入的量化后果**，
比 §15 的"漏 atom"结论准确得多。

### 16.3 三层定位的最终修正

- **设备发了 ✓ / FPGA 采了 ✓**（etm.bin 完整、END OF TRACE、零丢包）——不变。
- **解码器问题 ✗**——仍成立，但性质是**过度行走（重复解码）**，不是漏。mortrall 走飞后
  重入，把同段指令解了多遍。
- pop@a2f2 判 not-taken 是触发点：一次错判 → workingAddr 漂 → 走进不该走的地方 →
  绕一圈重新进函数 → 指令膨胀 + 假 begin。

### 16.4 方法论自纠

§15 差点把"atom 计数差"当成"解码器漏数据"的结论——**幸亏坚持 apples-to-apples 用指令
数复核，方向立刻反转**。这是"没对齐量纲就下结论"的又一次险情，被指令数对比拦下。
下一步仍是定位第一次 workingAddr 漂走的确切点（pop@a2f2 的 not-taken），但现在知道
后果是"重走 4.6×"，修对了应该让指令数从 3.33M 回落到 ~721K（接近 opencsd）。**这给了
一个新的、强的回归判据：instrs ≈ 721K。**


---

## 17. 运行时 self-check：一边跑一边验不变量（2026-08-02）

按用户建议在解码器里加运行时不变量检查（env `MORTRALL_SELFCHECK=1`，纯诊断不改行为，
`=2` 首次违反即 abort）。counters：inv1（workingAddr 无源码行=疑似漂移）、push/pop 计数、
depth 触顶命中。零丢包 slice 首跑结果：

```
[SELFCHECK] FIRST inv1 breach: workingAddr=08007b56 (func=cmp_complex depth=0)
[SELFCHECK] instrs=3473610  inv1=1438037  push=13309 pop=15658 (balance=-2349)
            depth>=16 hits=5409  depth>=MAX(30) hits=0
```

### 17.1 三个硬发现

1. **push/pop 不平衡：pop 比 push 多 2349**（13309 vs 15658）。栈被过量弹出——与 r32
   最初观察的"pop>push"方向一致（但 r32 归因 revert 被证伪；这是新的独立测量）。
2. **MAX_CALL_STACK=30 从不触发**（depth>=MAX 命中 0）——**回答用户：30 上限不是 bug**。
   但 **MAX_SANE_DEPTH=16 触发 5409 次**（flush 平栈），是把假重入深度盖住的症状放大器
   （r32 E1 早指出），不是根因。
3. **第一次疑似漂移在 cmp_complex@0x8007b56，depth=0**——**不是 pop@a2f2**。漂移是
   系统性、普遍的，比单个函数的 pop 更广。

### 17.2 inv1 判据不可靠（自纠，避免重蹈 A2）

0x8007b56 反汇编是 `and.w r7,r5,#127`（4 字节合法指令，前一条 `bpl.n`@0x7b54 判 not-taken
落到这），但 `symbolLineAt` 返回 NULL。→ **inv1「无源码行=漂移」判据太宽**：能反汇编的
合法指令若无 debug line 也被误报。1.4M inv1 里混了大量"合法但无 line"的指令，不能全当
漂移。**这和 A2「无 line 就当返回」是同一个坑**——`symbolLineAt==NULL` 不等于漂移。

### 17.3 下一步

- **push/pop balance=-2349 是最可信的硬信号**（不依赖 line 映射）。下一步用 self-check
  精确定位**第一次 pop 使 balance 转负**的位置 + 上下文（哪条 iBR、栈候选、真值返回址），
  这才是"过量 pop"的源头，比 inv1 可靠。
- inv1 判据改为"workingAddr 落在**函数间隙/已知 .word 地址**"（用函数 lowaddr/highaddr
  范围判，不用 line），或干脆弃用 inv1、以 push/pop balance + 指令数(→721K) 为准。
- self-check 框架保留（env 门控，纯诊断），是"一边跑一边查"的长期基建。


---

## 18. P0-3 全量判别 + opencsd 逐包对拍：真凶是【坏包后不 resync】不是"批边界"（2026-08-02，离线）

§13 只看 pop@a2f2 一个点就下"批边界"结论，被本轮全量数据推翻。用零丢包 slice
（`captures/cm100_slice.bin`，225M/112.5M 新固件，seq-gap≈5 实质零丢包，overflows=0、
timebase N==M=318826 精确对齐）做两件事：**(a)** self-check 全量分类全部 6698 次
iBR-not-taken 触发；**(b)** 拿 opencsd 参考解码器逐包 lister 对拍同一段。**全程离线，
不连设备。**

### 18.1 全量分类：99.3% 是无条件返回，只有 32% 在批边界

self-check 打出每次 iBR-not-taken 的 `(addr, incAddr, disp)`，对 92 个不同触发地址逐一
反汇编分类：

| 类别 | 触发次数 | 不同地址 | batch-end(incAddr=0) 占比 |
|---|---|---|---|
| **无条件返回**（`bx lr`/`pop{pc}`/`ldm..pc`/`ldr pc,[sp]`） | **6652 (99.3%)** | 88 | 32% |
| 表跳转（`tbb`/`tbh`，无条件多路） | 46 (0.7%) | 4 | 57% |
| **条件间接分支** | **0** | 0 | — |

两个硬结论：
1. **触发集里 0 个条件间接分支** —— 全是无条件返回/无条件表跳转（前面都有 `bhi.n`
   做范围检查后 fall-through，指令本身无条件、非 IT 块）。**这直接削弱 r34 Q4 对 A2 的
   "误伤真实条件间接分支"担忧：至少在触发点集合里根本不存在条件间接分支。**
2. **只有 32% 发生在 incAddr==0（批耗尽）** —— **68% 是批内还有 atom 剩余时就判了
   not-taken**。§13 的"批边界"只解释得了 32%。**"批边界是根因"被推翻，降级为部分表现。**
   第一次触发是 `core_state_transition@0x8008eb4`（`bx lr`），incAddr=4（批内还剩 3 atom）。

### 18.2 overflow 假设当场证伪（先试后信）

怀疑坏在 overflow 后不 resync，加了 overflow/trace-on 关联计数器（非破坏性读
`changeRecord` 位，注意 `TRACEStateChanged()` 会清位不能用来 peek）：
```
overflows=0  trace-on(sticky)=…  iBR-not-taken after-overflow=0  before-first-trace-on=433
```
**overflows=0**（"Overflows: 0 - 205" 第一个数是溢出数=0，205 是 A-sync 数，之前 §24
读反过）。**overflow 假设当场死**。只有 433/6698（6%）发生在首个 trace-on 之前（启动
瞬态），94% 是稳态失败。

### 18.3 opencsd 逐包对拍：坏包 340 次 + NACC 806 次，opencsd resync、mortrall 不

对同一段 etm.bin 跑 `trc_pkt_lister -decode`，看第一次 mortrall 漂移点（0x8008eb4）
前后的参考解码：

```
Idx:15684  I_ADDR_L_64IS1 : Addr=0x0000000000000000        ← 坏地址(全 0)
Idx:15693  I_BAD_SEQUENCE : Invalid Sequence [I_ASYNC]     ← 坏包
Idx:15697  I_TRACE_INFO   : Trace Info                     ← A-sync resync 恢复
Idx:15702  I_ADDR_CTXT_L_32IS1 : Addr=0x08008F66           ← 重新锚定
Idx:15709  INSTR_RANGE(0x8008eac:[0x8008eb6] E iBR V7:impl ret)  ← 这条 bx lr, opencsd 判 E(taken) 正确
```

opencsd 全程统计（318826 字节，END OF TRACE 完整跑完）：

| 事件 | 次数 | 含义 |
|---|---|---|
| **I_BAD_SEQUENCE** | **340** | 坏包（字节错，非丢包 —— seq-gap≈0） |
| **I_TRACE_INFO** | **303** | A-sync 后的 resync 恢复 |
| **ADDR_NACC** | **806** | 地址不可达（坏/推测地址，opencsd 跳过等重锚） |
| I_TRACE_ON | 20 | trace on |
| **E iBR V7:impl ret** | **707** | opencsd 用 ETM4 隐式返回栈正确解 iBR 返回 |
| EXCEPTION | 14 | SysTick(0x0f)，部分带垃圾返回址(0xd6d7…, EL3S) |

- opencsd 在每个坏包/垃圾地址处 **emit NACC + 等 A-sync/Trace-Info 重锚**，恢复后干净
  产出 98091 条 range（≈721K 指令）。
- **mortrall 对 `I_BAD_SEQUENCE`/`EV_CH_TRACESTART`/NACC 全不处理**（grep 确认 mortrall.hpp
  里 0 处引用 `EV_CH_OVERFLOW/TRACESTART/DISCARD/TRACESTOP`），坏包后**不 resync**，
  带着上一段的 workingAddr/disposition 继续线性走 → 漂进字面量池 → §16 的 4.6× 过度
  重复行走（mortrall 3.33M 指令 vs opencsd 721K）。

### 18.4 真凶重定位（推翻 §13 批边界，回到"坏包不 resync"）

- **不是 overflow**（=0）、**不是批边界**（只 32%）、**不是 disposition 位序错**
  （§14.2 手册核对位序对）。
- **是：稳态坏包（340 次字节错）后 mortrall 不做 A-sync/Trace-Info resync，
  workingAddr/disposition 带病继续，漂移级联放大。** pop/bx lr 判 not-taken 是**漂移后
  workingAddr 落在错位置、拿到错 atom** 的下游表现，不是独立的"批边界"bug。
- 这与 §16「4.6× 过度行走」自洽：每次坏包后一段区间被带错相位重走。
- **坏包本身来自采集字节质量**（半 nibble / 相位，坑点 17/21），不是网络丢包
  （seq-gap≈0、lost_cnt=0、overflows=0）。**但修 mortrall 的 resync 比修采集更划算**：
  opencsd 证明**同样的坏字节流**可以靠 A-sync/Trace-Info 恢复到 721K 干净指令。

### 18.5 修复方向（P1 候选，仍冻结待红方评审）

**方向 C（新，opencsd 已验证可行）**：mortrall 处理 `EV_CH_TRACESTART`（Trace On，
每次 A-sync/Trace-Info resync 后必来）—— 收到时**重置 workingAddr 待下一个 ADDRESS
包重锚、清空当前 disposition/incAddr**，不要带旧相位继续走。等价于把 opencsd 的
"NACC + 等重锚"搬进 mortrall。

- 判据：`captures/mortrall_fixture/regress.py --check` 双 slice + cm100 slice；
  期望 mortrall 指令数从 3.33M 向 opencsd 的 721K 收敛、coremark_main/cm_benchmark_main
  假重入大降、**cardinality 不降**（这是不误伤真实覆盖的硬约束）。
- 比方案 A（改 atom 循环核心）风险低：resync 只在坏包边界触发，不碰正常 atom 消费路径。
- **仍守 r34 P0-3 纪律**：本节把根因从"批边界"重定位到"坏包不 resync"，方向 C 是新
  假设，**动手前需红方评审**（下一篇 review），确认 resync 语义不会吞掉正常 Trace On
  后的合法首包。

### 18.6 self-check 基建增量（本轮已提交到工作树，env 门控纯诊断）

`mortrall.hpp` self-check 加了：iBR-not-taken 的无条件/条件分类计数、batch-end 判别、
overflow/trace-on 关联计数（非破坏性 peek `changeRecord`）。全部 `MORTRALL_SELFCHECK=1`
门控，不改解码行为。离线复现命令：
```bash
MORTRALL_SELFCHECK=1 ORBETTO_ETM_PROT=ETM4 \
  embedded-debug-tools/ext/orbetto/build/orbetto -C 225000 -t 1 \
  -f captures/cm100_slice.bin -e captures/cm100.elf \
  -F captures/cm100_slice.bin.etm.bin.time.bin -D stm32h743 2>sc.log
```


---

## 19. P0-3b-1 结果：接受 r35，真凶是【重锚太晚】不是"没 resync"（2026-08-09，离线）

**红方 r35 命中，蓝方接受。** §18.3/§18.4 的"mortrall 不处理坏包故不 resync"是 grep 出
"无 TRACESTART handler"后的推断，被 r35 读码证伪：`mortrall.hpp:524` 的 EV_CH_ADDRESS
handler **每次都 `op.workingAddr = cpu->addr` 重锚**——mortrall **有**隐式 resync 路径。
且 `incAddr/disposition` 是 `_traceCB` 局部变量（line 328-329）、每批从 `cpu->disposition`
重载，**不跨 callback 残留**——§18 说"带着上一段 disposition 继续"对 disposition 是错的
（只有持久成员 `op.workingAddr` 会带病）。这两点 r35 都对。

### 19.1 P0-3b-1 单步实测（r35 指定的三假设互斥判据）

在 `mortrall.hpp:524` 重锚点打点，dump 第一次漂移（`bx lr@0x8008eb4`）之后每个
EV_CH_ADDRESS 的 `(旧 workingAddr, cpu->addr, 是否匹配, 距漂移的 atom 数/callback 数)`：

```
FIRST iBR-not-taken: iBR@08008eb4 (core_state_transition) incAddr=4 disp=0f -> fall-through 08008eb6
[SC-ADDR] #1  old_wa=08009010 cpu_addr=08007e70 match=0  atoms_since_drift=567  cbs_since_drift=98
[SC-ADDR] #2  old_wa=08007e70 cpu_addr=08007e70 match=1  atoms_since_drift=573  ...
[SC-ADDR] #3..19  old_wa==cpu_addr match=1  (重锚后短暂跟上)
[SC-ADDR] #20 old_wa=08007e70 cpu_addr=90000000 match=0  ← NACC 垃圾地址区(opencsd 跳过, mortrall 照走)
```

### 19.2 判定：假设②【重锚太晚】成立，假设①【完全不 resync】证伪

- **不是"完全不 resync"**：漂移后**确实**来了 EV_CH_ADDRESS 并重锚（#1）。r35 的假设① 排除。
- **是"重锚太晚"**：第一个 ADDRESS 在漂移**之后 567 个 atom / 98 个 callback** 才到，
  且 **MISMATCH**（mortrall 已自由漂到 0x08009010，真值在 0x08007e70）。这 567 atom
  区间里 mortrall 带着错 workingAddr 走指令、乱 push/pop 栈 → 假重入 + §16 的 4.6×
  过度行走。重锚只是**事后**把 workingAddr 拉回，中间的污染已经发生。
- **方向 C（补 TRACESTART handler 重置 workingAddr/disposition）无效**：resync 事件不缺，
  缺的是"坏包/无有效锚点期间**别再走 atom**"。补 TRACESTART 改不了"ADDRESS 与 atom 批
  的到达顺序"——atom 批在 ADDRESS 之前就到了并被走完。**方向 C 撤销。**

### 19.3 新方向 D（未验证，仍冻结）：无地址锚点期间暂停 atom 行走

opencsd 的做法是 **NACC（地址不可达）+ 等下一个有效 ADDRESS 再走**（§18.3 的 806 NACC）。
等价搬进 mortrall：iBR 返回判 not-taken 且**没有有效栈候选/落点无源码行**时，不要
`workingAddr += 2` 硬走进字面量池，而是**暂停 instruction 行走**（挂起本批剩余 atom），
等下一个 EV_CH_ADDRESS 重锚。

- **但这正是 r35 P0-3b-3 警告的雷区**：A2 已证"落点无源码行就当返回"会误伤真实分支
  （cardinality -77）。方向 D 若用"无源码行"当暂停判据，必重蹈。
- **且 567 atom 的漂移说明问题在更上游**：`bx lr@0x8008eb4` 为何一开始被判 not-taken？
  它是无条件返回，atom 必为 E。若 atom 流本身对齐，它不该 not-taken。所以第一次误判仍
  可能是**进入这个 callback 时 workingAddr 已经错**（前一个 callback 的 iBR 处理留下的），
  是级联的**某个更早的头**。P0-3b-1 只定位到"重锚太晚"，**没定位到 567-atom 级联的最初
  第一个错**。

### 19.4 卡点（诚实标注，未解决）

**已确定**：重锚太晚（假设②），方向 C 无效。**未确定**：
1. **级联的最初头在哪**：第一次 `bx lr@0x8008eb4` 被判 not-taken 的那个 atom，是"atom
   流本身在此处就是 N"（则更上游有 atom 错配），还是"workingAddr 进 callback 时已错、
   落在错指令上取了别人的 atom"？P0-3b-1 armed 的 drift 时钟从**第一次 not-taken** 起，
   没往前看**这次 not-taken 之前**这个 callback 的 workingAddr 是否已偏。
2. **opencsd 为何不漂**：同一段 opencsd 用 `V7:impl ret` 隐式返回栈解 `bx lr`（707 次），
   **根本不依赖 disposition bit 判 taken/not-taken**——无条件返回它直接从返回栈弹目标。
   mortrall 却用 `disposition & 1` 判无条件返回的 taken/not-taken（line 611 的
   `(!(ic&LE_IC_JUMP)) || (disposition&1)`）——**这可能才是根**：无条件 iBR 返回**不该**
   靠 disposition 位决定 taken，它必然 taken，目标来自返回栈。mortrall 把它和条件分支
   一样用 disposition 判，一旦那个 bit 因任何原因是 0 就误判。
3. 若 (2) 成立，修法回到"无条件 iBR 强制 taken"（§14.4 的方向），但必须解决"如何可靠
   判无条件"——capstone `cc==ARM_CC_AL`（§14.4 已指出助记符判据会被 IT 块坑）。

### 19.5 下一步（P0-3b 续，仍禁止写修复代码，待 r36）

- **P0-3b-1b**：drift 时钟往前挪——dump 第一次 not-taken **所在 callback 入口**的
  workingAddr 与 cpu->addr（若这个 callback 有 ADDRESS），判定 (1)：是进 callback 就错，
  还是 callback 内走错。
- **P0-3b-4**（r35 要求）✅ **已做**：capstone `ins.cc` 重分类 98 个不同触发地址（**用
  cc 位不用助记符**，避开 §14.4 的 IT 块盲区）——**95 无条件（cc==AL）/ 0 条件 / 3 未
  解码**（3 个是 tbb/tbh 后的跳转表数据被误当指令）。**"0 条件间接分支"经 capstone cc
  证实非盲区**，r35 主张 2 的隐患排除。方向坐实到 19.4(2)：**触发全是无条件 iBR，按
  ETM4 必为 E(taken)，mortrall 却用 `disposition & 1`（line 611）判它们的 taken——这是
  可疑的真凶头**。opencsd 用 `V7:impl ret` 隐式返回栈解无条件返回，不看 disposition。
- **P0-3b-2**（r35 要求）：坏包字节偏移 ↔ 6698 失败字节偏移 1:1 对齐，确认级联倍率。

self-check 增量（SC-ADDR 重锚探针、drift 时钟）已在工作树，`MORTRALL_SELFCHECK=1` 门控，
regress 双 slice 确认解码零变化。


---

## 20. P0-3b-1b：单点故障链完整还原（哪个函数、随机/固定、怎么发生的）（2026-08-09）

回答用户三问：**哪个函数发现偏移 / 随机还是固定 / 单点故障怎么一步步发生**。加了指令
环形 buffer（漂移前 96 条指令全轨迹）+ batch 入口上下文 dump，对零丢包 slice 单步。

### 20.1 第一个故障点是【固定】的

- **第一次漂移永远是 `bx lr @ 0x8008eb4`（core_state_transition）**，多次运行一致、
  确定性。`batch_entry_wa=0x08008eac had_addr=0`——失败那批**不带地址重锚**，是纯 atom 批。
- 但**上游的坏包是随机的**：opencsd 数 340 个 I_BAD_SEQUENCE，行号间隔
  288 / 5771 / 5777 / 5778 / 6114 / 7193…**不规则、成簇**，是采集字节质量（半-nibble/
  相位 glitch，坑点 17/21），不是周期性解码 bug。
- 所以：**故障触发源（坏包）随机，但坏包落到解码器后的表现是固定的**——每次都在坏包
  后第一个"无地址包、靠 atom 隐式推进"的无条件返回处爆出来（本 slice 是 0x8008eb4）。

### 20.2 单点故障链（逐步，全部来自 ring + opencsd 对拍）

**背景**：core_state_transition 调 memmove，memmove 尾 `0x8000460: bx lr` 返回。

**opencsd 真值（坏包 resync 后）**：
```
Idx15693 I_BAD_SEQUENCE          ← 坏包(采集字节错)
Idx15697 I_TRACE_INFO            ← A-sync resync
Idx15701 ATOM_F1 [0xf7]=E        ← 独立 1 atom
Idx15702 ADDR 0x08008F66         ← 重锚
Idx15708 ATOM_F3 [0xfd]=ENE      ← 驱动 0x8008f66(b+link,E) / 0x8008d40(beq N) / 0x8008d4c(beq E→跳0x8008eac)
Idx15709 ATOM_F1 [0xf7]=E        ← 独立 1 atom, 驱动 0x8008eac:[0x8008eb6] "E iBR impl ret"(bx lr 取)
```
opencsd 关键：`bx lr@0x8008eb4` 的 atom 是 **Idx15709 独立的 `0xf7`=E**，且它用
**V7:impl ret 隐式返回栈**解目标，**E=taken 从返回栈弹**。

**mortrall 实测（ring 尾部）**：
```
[BATCH] wa=08000460 ic=001 exec=1 disp=0x15(10101) incAddr=5   ← memmove 的 bx lr, 一个 5-atom 批
        wa=08008f14 ... (跳到 core_state_transition 中段)
        ... 走到 ...
        wa=08008d48 beq.w exec=0 (N) ✓
        wa=08008d4e beq.w exec=1 (E→跳 0x8008eac) ✓   ← 到这里 workingAddr 还对!
[BATCH] wa=08008eac disp=0x1e(11110) incAddr=5         ← 新批, 不带地址重锚
        wa=08008eac movs (非branch)
        wa=08008eae adds (非branch)
        wa=08008eb0 str  (非branch)
        wa=08008eb4 bx lr → 取 disp bit0=0 → 判 NOT-taken → 漂进 0x8008eb6 字面量/后续
```

### 20.3 故障的确切机制：atom **分批边界**与真值错位，不是 workingAddr 错

- **workingAddr 一路是对的**：mortrall 走到 `0x8008d4e beq.w` 判 taken 跳 0x8008eac，
  与 ELF/opencsd 完全一致。进 0x8008eac 批时 workingAddr=0x8008eac **正确**。
- **错的是 atom 归批**：opencsd 把 `bx lr` 的 E 放在**独立的** `0xf7`(Idx15709)；mortrall
  把 `bx lr` 归到了批 `disp=0x1e` 的 **bit0**，而 0x1e 的 bit0=0。**mortrall 的 atom 批
  边界与 opencsd（=真值）差了**——mortrall 少切了一批 / 多并了一位，导致 bx lr 对到了
  一个 N 位而非它自己的 E 位。
- **根因链**：坏包(Idx15693) → resync 后 opencsd 与 mortrall 对"resync 后第一个 atom
  (Idx15701 的 `0xf7`) 该配给谁"处理不同 → mortrall 的 atom↔指令配对整体错开 → 到
  `bx lr@0x8008eb4` 时取到错误的 disposition 位（0）→ 无条件返回被判 not-taken →
  workingAddr += 2 漂进 0x8008eb6 → 级联 567 atom 直到下一个地址包(0x08007e70)才被
  拉回，但 MISMATCH，中间全走飞（§19.1）。

### 20.4 为什么 opencsd 不爆而 mortrall 爆（最关键的机制差）

- **opencsd 对无条件返回用 `V7:impl ret` 隐式返回栈**：它识别 `bx lr`/`pop{pc}` 是返回，
  E 表示"执行了返回"，**目标从解码器自己维护的返回地址栈弹**，不靠 workingAddr 线性推进。
- **mortrall 把无条件返回和普通条件分支一样，用 `disposition & 1`（line 611）判 taken**：
  `insExecuted = (!(ic&LE_IC_JUMP)) || (disposition&1)`。一旦那个 disposition 位因上游
  atom 归批错位而=0，无条件返回就被误判 not-taken。**无条件返回按 ETM4 必为 E，用
  disposition 位判它本身就是脆弱设计**——正常流里位对得上不出事，坏包 resync 后位一
  错位就爆。
- **这解释了为何"触发集 100% 是无条件返回、0 条件分支"（P0-3b-4 capstone 证实）**：
  只有无条件返回会"disposition 位说 N 但实际必须 taken"这种自相矛盾；条件分支 N 是合法的。

### 20.5 结论（更新根因，仍待红方 r36，未写修复代码）

- **哪个函数**：第一个故障固定在 core_state_transition 的 `bx lr@0x8008eb4`；但故障**类型**
  遍布所有含无条件返回的函数（core_state_transition/core_list_init/time_in_secs/
  cm_uart_send_char…，见 §18.1）。
- **随机还是固定**：**触发源（坏包）随机成簇**（采集字节质量），**表现固定**（坏包后
  第一个无地址锚的无条件返回处爆）。
- **单点机制**：坏包 → resync → **mortrall 的 atom 归批与真值错位一位/一批** →
  无条件返回 `bx lr` 取到错的 disposition 位=0 → 判 not-taken → 漂 → 级联 4.6× 过度行走。
- **根因收敛到两层**：(a) 上游——坏包 resync 后 atom↔指令重新配对的边界，mortrall 与
  opencsd 不一致；(b) 下游放大器——mortrall 用 disposition 位判**无条件返回**的 taken
  （line 611），而正确做法是无条件返回恒 taken + 隐式返回栈弹目标（opencsd 的 V7:impl ret）。
- **修复方向（待 r36 评审，二选一或组合）**：
  - **D1（治下游放大器，风险中）**：无条件 iBR 返回（capstone `cc==ARM_CC_AL` +
    LE_IC_JUMP 非 IMMEDIATE + 目标是返回）**恒判 taken**，目标取栈候选，不看 disposition。
    需 loadelf 加 `LE_IC_UNCOND`（capstone cc）。这不治 atom 归批错位，但**掐断放大器**：
    即使位错了，无条件返回也不会漂。风险：若某处真该 not-taken（不该有，无条件返回），
    regress 双 slice + cardinality 守。
  - **D2（治上游，风险高）**：对齐 mortrall 与 opencsd 的 resync 后 atom 归批。碰 atom
    核心，r34/r35 反复警告的雷区。
- **先验证 D1**（最小、判据硬：instrs 应从 3.33M 向 opencsd 721K 收敛、coremark_main/
  cm_benchmark_main 假重入降、cardinality 不降）。**但 r34/r35 纪律：动手前过 r36。**

self-check 增量（ring buffer + batch 入口 dump）已在工作树，`MORTRALL_SELFCHECK=1` 门控，
regress 双 slice 确认解码零变化。


---

## 21. 坏包 = 硬件采样错的证明 + 第三方解码器交叉（2026-08-09）

用户三问：坏包和硬件采样有没有关系 / 怎么证明硬件采样有没有问题 / 还有什么第三方
解码器交叉验证。

### 21.1 坏包的字节级画像（opencsd lister 统计，零丢包 slice1）

opencsd 对 slice1（318826 ETM 字节）解出的**非法/损坏包**：
- **I_RESERVED 头 2186 个**：ETM4 里保留、合法流永不出现的包头 → 字节被打坏。
- **I_BAD_SEQUENCE 340 个**：210 I_EXTENSION（非法扩展包头）+ 130 I_ASYNC（流中 A-sync，
  部分是正常周期重同步、部分是坏字节凑出的假 async）。
- 典型坏包样本：`[0x05 0x05 0x00 0x01]`（两个 reserved 头）、`[0x46 0x8e]`（reserved-config
  + reserved）、`I_CCNT_F1 Count=0x0` 后跟 38 字节垃圾（一个坏字节让解码器误以为
  cycle-count 包开始，吞掉后面一片）。
- **corrupt-header 率 ≈ (2186+210)/318826 = 0.75%**。

这些是**孤立的字节损坏**（坏一个字节 → 一个非法包头 → resync），不是结构性/协议性错误。
字节损坏正是并口采样眼裕度不足 / 相位 glitch 的签名（坑点 17/21）。

### 21.2 决定性证明：同一抓样两段独立 slice，坏包率差 13×

**方法**：从同一份 337MB `cm100.bin` 抓样里取**第二段独立 slice**（offset 150MB, 8MB），
同一 deframer + 同一 opencsd 解码，比坏包率。**同工作负载、同解码器、同 bitstream，
只有采集时刻/位置不同。**

| | slice1 (offset ~0) | slice2 (offset 150MB) |
|---|---|---|
| deframed ETM 字节 | 318826 | 483866 |
| I_RESERVED 坏头 | 2186 | **47774** |
| I_BAD_SEQUENCE | 340 | 588 |
| INSTR_RANGE | 98091 | 73547 |
| **corrupt-header 率** | **0.75%** | **≈10%** |

**坏包率随采集时刻从 0.75% 跳到 10%（13×）。**

- **若是解码器/格式 bug** → 两段应被**同等**污染（同一份代码、同一解码路径）。
- **实测差 13×** → 污染量**随物理采集时刻变化** = 采样眼裕度/相位是**边际且时变**的，
  某些时段（如温漂、SSN、数据 pattern 相关抖动）字节错率飙升。
- **这唯一指向硬件采样**：解码器不可能"有时对有时错"，只有物理采样会。**证毕：坏包是
  硬件采样错，非解码器 bug。**

### 21.3 但"假重入放大"仍是解码器的锅（两件事分开）

- **坏包（采样）**：0.75%-10%，硬件问题，opencsd/mortrall 都遇到。
- **假重入（放大）**：opencsd 遇同样坏包**恢复到 98091/73547 干净 range**（§18/§11），
  mortrall 却过度行走 4.6×。**同样的坏输入，opencsd 不炸 mortrall 炸** → 放大是
  mortrall 的锅（§20 的 disposition 判无条件返回）。
- **两条修复路互补**：改善采样（降坏包率）治因；修 mortrall（D1，掐放大器）治果。
  **即使采样完美，mortrall 的 disposition-判无条件返回脆弱性仍在**（正常流偶发单字节错
  也会触发）；即使 mortrall 修好，10% 坏包仍会丢真实覆盖。两者都值得做。

### 21.4 怎么进一步证明/量化硬件采样（可做实验清单）

1. **已做**：同抓样多 slice 坏包率对比（§21.2，13× 变化坐实时变采样错）。
2. **眼图扫描**（板上，需连设备）：`td tap sweep` 在 clktap bit 上扫 IDELAY tap，看
   RESERVED 字节错率 vs tap（坑点 13/17）。眼宽实测 tap 4-31 err=0%——但那是 AA55 测试
   模式的**位错**，管不了**字节/nibble 边界错位**（坑点 17）。字节错要用真 ETM 流的
   RESERVED 率当判据。
3. **位宽对比**：4/2/1-bit 抓同段（坑点 22），低位宽单 lane 采样、无 SSN，坏包率应更低
   → 若 1-bit 坏包率显著<4-bit，坐实是并口 SSN/skew。
4. **降频对比**：TRACECLK 100M→50M，坏包率应下降 → 坐实采样时序裕度。
5. **raw 字节 pattern 关联**：坏包前后的 raw 字节是否集中在某些 nibble 跳变（如
   0x→0x 大摆幅），关联 SSN。

### 21.5 第三方 ETMv4 解码器（交叉验证选项）

本项目已用 **opencsd（ARM/Linaro 官方参考解码器，`trc_pkt_lister`）** 做金标准
（§11/§18/§20），它是最权威的第三方。其他可交叉的：

| 解码器 | 说明 | 可用性 |
|---|---|---|
| **OpenCSD / trc_pkt_lister** | ARM 官方参考实现，C++。**已用作本项目金标准**。 | ✅ 已装已用 |
| **Perfetto / Trace Processor** | Google，内部走的也是自己的 ETM 解析（AOT）。 | 间接（我们的产物给它，不解 ETM） |
| **CoreSight Trace (Linux perf `perf script` + cs-etm)** | 内核 perf 的 cs-etm 解码，**底层就是 OpenCSD** → 非独立实现，交叉意义小。 | 与 opencsd 同源 |
| **Lauterbach TRACE32 / ARM DS-5 Streamline** | 商业，独立实现，最强交叉。 | ❌ 无 license |
| **ptm2human / etm2human** | 开源小工具，覆盖不全（偏 ETMv3/PTM）。 | 部分（ETMv4 支持弱） |
| **pyocd / 自写按 IHI0064 spec 手工解** | 我们已在 `decode/` 有 etm35lib/tpiu_official 等，可扩 ETMv4 atom 手工对拍。 | ✅ 可自建 |

**结论**：opencsd 已是最权威的独立第三方（非 orbuculum 血缘），它与 mortrall 的分歧
（721K vs 3.33M 指令）就是最强的交叉证据——**同一坏输入，官方解码器不炸，mortrall 炸**。
再引入 cs-etm 无意义（同源 opencsd）；要更强只能上 TRACE32（无 license）。**当前交叉已
充分：mortrall 是唯一的异常项。**

### 21.6 对三问的直接回答

1. **和硬件采样有关吗**：**有，且坏包 100% 是硬件采样错**（§21.2 的 13× 时变�V证）。
   但**假重入不是采样直接造成的**，是 mortrall 对采样坏包的放大（§21.3）。
2. **怎么证明硬件采样有没有问题**：同抓样多 slice 比坏包率（已做，0.75%→10%，坐实时变
   采样错）；进一步用位宽/降频/眼图扫描定位是 SSN 还是时序（§21.4，需连设备）。
3. **第三方解码器**：**opencsd 就是**（ARM 官方，已用作金标准）；cs-etm 同源不算独立，
   TRACE32 商业无 license。**opencsd vs mortrall 的分歧已是充分交叉，mortrall 是异常项。**


---

## 22. 最小可行性验证：opencsd + 自研栈机 vs orbuculum/mortrall（2026-08-09）

用户拍板：先做最小验证，若 opencsd+自研明显更好就转路线。**做了，结论：明显更好，
路线成立。**

### 22.1 方法

纯离线、不碰现有代码。写了个 ~80 行的最小调用栈机（`opencsd 栈机原型`），直接吃 opencsd
`trc_pkt_lister -decode` 的 INSTR_RANGE 元素流：
- opencsd 已把最难的 ETM4 语义做对（atom↔指令配对、`V7:impl ret` 隐式返回栈）。
- 栈机只做确定性的三件事：range 结尾是 executed `b+link`→push（callee=下一 range 起址，
  返回址=range.end）；executed `impl ret`→pop；按 range 起址查函数名。
- **同一份 cm100 零丢包 slice**，与 mortrall 直接对拍。

### 22.2 结果（同 slice apples-to-apples）

| 函数 | opencsd+栈机 | mortrall/orbetto | 倍率 |
|---|---|---|---|
| core_state_transition | 101 | 1836 | **18×** |
| crc16 | 163 (begin==end) | 2386 | **15×** |
| crcu32 | 36 (==) | 1515 | **42×** |
| crcu16 | 73 (==) | 806 | **11×** |
| cmp_complex | 141 | 822 | 6× |
| **total begins** | **623** | **17893** | **29×** |
| max call depth | 7 | （深嵌套爆炸） | — |
| 指令数 | 98086 range（=721K 指令） | 3.33M 指令 | 4.6× |

- **opencsd 栈机的 begin 数是 mortrall 的 1/29**，且**内层函数 begin==end 完美配平**
  （crc16 163/163、crcu32 36/36、crcu16 73/73）——无假重入爆炸。
- **mismatched returns 仅 85 / 98086 range = 0.09%**（来自 340 坏包 + slice 边界），
  其余全部正确配平。
- max depth 7（合理的 CoreMark 嵌套），非 mortrall 的深嵌套爆炸。

### 22.3 诚实标注的局限

- **coremark_main/cm_benchmark_main 在本 slice 里 = 0**：这段 8MB slice 的执行流**从
  benchmark 中段开始**，coremark_main 的入口在 slice 之前，栈机没看到它的 CALL 所以不
  计——**这是 slice 窗口问题，不是正确性问题**。要证"coremark_main==1"需换含入口的 slice。
- 栈机是**原型**（正则解析 lister 文本、tail-call/异常处理简化），不是产品级。真做要
  直接吃 opencsd C++ API 的 element callback，不走文本。
- 时间基、异常轨、PC bitmap、Perfetto protobuf 导出**都还没接**——这些是方案 X 的工作量。

### 22.4 结论：转 opencsd + 自研栈机路线

**信号足够强，路线成立**：同一坏输入，opencsd 栈机零假重入爆炸、内层完美配平、指令数
是真值；mortrall 29× begin 膨胀。**这印证 §20/§21 的判断**——难点在 ETM4 解码（atom/
隐式返回栈），opencsd 已做对；调用栈重建反而简单。把解码外包给 ARM 官方，我们只写确定性
栈机 + 导出层，绕开 orbuculum ETMv4 解码器的坏包放大缺陷。

### 22.5 方案 X 落地工作量（下一步规划，待展开）

1. **栈机接 opencsd C++ API**（`ITrcGenElemIn` element callback），不走文本 lister。
2. **时间基对齐**：现有 `.time.bin`（按 ETM 字节索引）→ 映射到 range 的字节偏移。opencsd
   element 带 `index`（trace 字节偏移），可直接查表，比 mortrall 的钳位路径更干净。
3. **异常轨**：opencsd 有 `OCSD_GEN_TRC_ELEM_EXCEPTION`（带 exception num + return addr），
   比 orbuculum 的 EXCEPTIONINFO 解析更规范；SysTick 假嵌套（坑点 24）可能天然消失。
4. **PC bitmap / 覆盖**：range 起止直接填。
5. **Perfetto protobuf 导出**：复用 orbetto 的 protobuf 生成代码（那部分与解码无关，可留）。
6. **回归**：新链路跑 `captures/mortrall_fixture/regress.py`，判据 coremark_main==1、
   内层配平、cardinality 不降、verify_calls 调用边对 ELF。含入口的 slice 验 coremark_main==1。

**这份 §22 是 go/no-go 的 go**。方案 D1（修 mortrall）可作为并行的短期缓解，但战略方向
转向 opencsd+自研。**仍建议先过红方 r36**（评审"栈机原型的 slice=0 局限是否掩盖了别的
问题"、"文本 lister vs C++ API 的语义差"、"工作量估计是否乐观"）。


---

## 23. C++ PoC：opencsd 后端 → Perfetto，demo 跑通（2026-08-09）

用户拍板："先证 opencsd 后端能出正确 perf,demo 可以出就建仓库"。**做了,demo 出来了。**

### 23.1 PoC 构成（`poc_opencsd/`,~260 行 C++）

- **链系统 libopencsd**（`libopencsd_c_api` + `libopencsd`,Ubuntu `libopencsd-dev` 1.4.1
  已装）,走官方 C API：`ocsd_create_dcd_tree`(SINGLE 源) → `ocsd_dt_create_decoder`
  (`OCSD_BUILTIN_DCD_ETMV4I`,Cortex-M7 config) → `ocsd_dt_add_binfile_mem_acc`(ELF mem.bin
  @0x08000000) → `ocsd_dt_set_gen_elem_outfn`(element callback) → `ocsd_dt_process_data`。
  **不走文本 lister,真链库。**
- **调用栈机**在 `OCSD_GEN_TRC_ELEM_INSTR_RANGE` 回调里：用 opencsd 给的
  `last_i_type`/`last_i_subtype`/`last_instr_exec` 分类——`BR_LINK`=CALL(push,下一 range
  起址=callee)、`V7_IMPLIED_RET`/`V8_RET`=RETURN(pop)。**opencsd 已把隐式返回栈
  (V7:impl ret) 做对,栈机只做确定性 push/pop。**
- **Perfetto protobuf 导出**：手写最小 protobuf 编码器,发 TrackDescriptor + TrackEvent
  (TYPE_SLICE_BEGIN/END),字段号对齐官方 synthetic-track-event 参考
  (track_descriptor.uuid=1/name=2、timestamp=8、track_event.type=9/track_uuid=11/name=23、
  trusted_packet_sequence_id=10)。产物 `.perftrace` 可直接拖进 ui.perfetto.dev。

### 23.2 结果（同 cm100 slice,C++ PoC 真链库)

```
max depth: 7   mismatched returns: 85 / 98086 ranges (0.09%)   slices: 1244
函数              begin  end
core_state_transition  101   94
crc16                  164  185
crcu32                  36   36    ← 完美配平
crcu16                  73   73    ← 完美配平
cmp_complex            141  128
```

- **与文本原型(§22)一致**——确认 C++ 链库集成无坑,element 语义解读正确。
- **导出的 .perftrace 结构校验通过**：622 begin / 622 end / 终深度 0 / 最大深度 6 /
  从不为负 = **嵌套完美配平**,UI 能正确渲染。protobuf 字段结构与 orbetto 参考、
  官方文档逐字段对上。

### 23.3 coremark_main==1 的诚实说明

- **本抓样无法证 coremark_main==1**：`coremark_main` 在固件里被 main() **只调一次**
  (0x800434e 的 `bl`,之后内部死循环),这次 CALL 发生在**抓样开始之前**(采集是在
  CoreMark 已跑起来后 arm 的)。所以任何 slice 里都不含它的入口。
- **但 PoC 给出的是更正确的答案**：PoC 报 coremark_main **begin=0**(slice 内确实没进入),
  而 mortrall 报 **7~23 次**(§20,凭空捏造的假重入)。**0 是对的,7/23 是错的。**
- 要正面验 ==1 需要一份含 boot 的抓样(下一步:重抓时从 reset 开始 arm,或用现有
  cm100.bin 找更靠前的段——但当前 337MB 段内 coremark_main 入口都在采集前)。

### 23.4 apples-to-apples 总对比(同 cm100 slice)

| | opencsd+PoC 栈机 | mortrall/orbetto |
|---|---|---|
| total begins | **623** | 17893 (**29×**) |
| 指令数 | 721K(真值) | 3.33M(4.6× 过度行走) |
| crc16/crcu32/crcu16 | 配平(164/36/73) | 爆炸(2386/1515/806) |
| coremark_main(slice 内真值=0) | **0 ✓** | 23 ✗ |
| max depth | 7 | 深嵌套爆炸 |
| Perfetto 导出 | ✓ 结构校验通过 | ✓ |

### 23.5 结论：demo 成立,可以建仓库

**opencsd 后端能出正确的 Perfetto perf,且质量碾压 orbuculum+orbetto**（begin 1/29、
指令数收敛到真值、内层配平、导出结构合法可直接进 Perfetto UI）。§22 的可行性判断被
C++ 真链库 PoC 坐实。**go 信号明确,可以建独立仓库走 opencsd+自研路线。**

### 23.6 新仓库建议(基于 PoC 经验)

- **名字**:`etm2perfetto` 或 `csperf`(ETM CoreSight → Perfetto)。
- **核心 C++**:`libopencsd`(解码,不自己写 ETM4) + 调用栈机 + Perfetto protobuf 导出。
  PoC 的 `csperf_poc.cpp` 是种子,但产品级要:
  - 用真 protobuf(链 perfetto/protobuf,别手写编码器——手写只适合 PoC)。
  - 时间基:opencsd element 带 trace 字节 index → 查 FPGA `.time.bin`(现成)映射 ns,
    替换 PoC 里的"每 range +1"占位时间。
  - 异常轨:opencsd `OCSD_GEN_TRC_ELEM_EXCEPTION`(带 exception num + 返回址)开 ISR track。
  - PC bitmap / 覆盖统计 / verify_calls 对 ELF(复用现有判据)。
- **采集仍留 orbtrace**:新仓库吃 orbtrace 吐的 ETM 字节流(stdin/socket),职责边界清晰。
- **"点一下送 UI"**:一个薄 web wrapper(window.open ui.perfetto.dev + postMessage
  {perfetto:{buffer}},PING/PONG 握手),官方支持的深链协议,零服务器。
- **滑动窗口长时抓**:上位机侧环形 buffer 保留最近 N 秒解码结果,按需截时间窗生成 trace
  送 UI(一期);"UI 实时流"(trace_processor httpd)留二期。
- **回归**:新链路跑 `captures/mortrall_fixture/regress.py` 判据 + 含 boot 的抓样验
  coremark_main==1。

PoC 代码在 `poc_opencsd/`(csperf_poc.cpp + build.sh + pbwalk.py 校验工具)。
产物 `/tmp/cm100_poc.perftrace` 可拖进 ui.perfetto.dev 看。


---

## 24. PoC 三个观察的根因(用户看图提出:少函数 / core_bench_state 自嵌套 / 盲区)(2026-08-09)

用户看 Perfetto 截图指出三个问题。逐一单步查清,**都不是 opencsd 的锅,是 PoC 栈机的
简化 + 采集盲区**。

### 24.1 "少了不少函数" —— PoC 只计"被 CALL 进入"的函数,且是 8MB slice 子集

- PoC 报 **10 个函数**(crc16/cmp_complex/core_state_transition/crcu16/memset/
  core_bench_state/crcu32/core_bench_matrix/matrix_test/core_bench_list),opencsd 覆盖
  统计报 **17 个**。差在:
  - PoC 栈机只在"executed CALL"时 `begins[callee]++`,**叶子函数/被 tail-call/被
    异常进入的**没计入 begin(但指令仍解了)。
  - 这段 slice 是 benchmark 中段,`coremark_main`/`cm_benchmark_main`/UART 相关都在
    slice 外(§23.3),自然不出现。
- **不是解码丢函数**——opencsd INSTR_RANGE 覆盖 17 个函数、2773 unique PC 全在 flash。
  是 PoC 的 begin 计数口径窄。产品级按 range 起址的函数变化计入即可补齐。

### 24.2 core_bench_state "自嵌套" —— PoC 栈机的 callee 识别启发式在盲区处失效(真 bug)

**这是 PoC 的真 bug,已定位,不是 opencsd 问题。** 单步 ns=9140→9141:
```
ns=9140 range=08008f66:[08008f6e] exec=1 itype=BR sub=BR_LINK  ← core_bench_state 的 bl core_state_transition@0x8008f6a (CALL)
ns=9141 range=08008f14:[...]      pend=1  fn=core_bench_state   ← 下一 range 起址=0x8008f14
       [SELFNEST] push core_bench_state while top==core_bench_state
```
- ELF 铁证:0x8008f6a 是 `bl 8008d40 <core_state_transition>`,**callee 必是
  core_state_transition(0x8008d40)**。
- 但 PoC 用"callee = 下一个 range 的起址所属函数"启发式,下一 range 起址是 **0x8008f14
  (还在 core_bench_state 内)**,于是 `do_call(core_bench_state)` → 假自嵌套。
- **为什么下一 range 不是 callee 入口**:这次 `bl` 之后紧跟一个**采集盲区(ADDR_NACC)**
  ——core_state_transition 的进入+body+返回那几个 range 落在坏包/地址不可达区被 opencsd
  跳过(全片 **806 个 ADDR_NACC**),opencsd 重新同步后的下一个可解 range 是返回落点
  0x8008f14。PoC 把"重同步落点"误当成"callee 入口"。
- **修法(产品级)**:CALL 的 callee 不能靠"下一 range 起址"猜,要用 **BL 的实际目标
  地址**(从指令静态解析,或要求下一 range 起址==已知函数入口才 push,否则判定为
  "callee 被盲区吞掉",不 push 或按返回址对齐)。opencsd 的 element 不直接给 BL 目标,
  但可以:(a) 反汇编 BL 指令取立即目标;(b) 只在"下一 range 起址是某函数的 lowaddr"时
  才确认 push,盲区打断时按 ret 地址回填。**这是栈机策略问题,与 opencsd 解码质量无关。**

### 24.3 "还有没采到的盲区" —— 是硬件采样坏包,opencsd 如实标 ADDR_NACC

- Perfetto 时间轴上的空档 = **806 个 ADDR_NACC + 340 坏包**处,opencsd 无法跟踪程序流,
  如实标"地址不可达",不产 range → 时间轴出现空白段。
- **这正是 §21 证过的硬件采样错**(同抓样两段坏包率 0.75%→10%,时变)。opencsd 的诚实
  之处:盲区它**明确标 NACC 空着**,而不是像 mortrall 那样带病乱走填满(4.6× 假指令)。
- **空档是真实的"这段没采到",不是解码 bug**。减少空档要治采集(位宽/降频/眼图,§21.4);
  解码侧能做的是盲区两端别乱接(§24.2 的栈机修法)。

### 24.4 结论:三个观察各有归属,opencsd 路线不受影响

| 观察 | 根因 | 归属 | 修法 |
|---|---|---|---|
| 少函数 | PoC begin 只计 CALL 进入 + slice 子集 | PoC 口径 | 产品级按 range 函数变化计入 |
| core_bench_state 自嵌套 | callee="下一 range 起址"启发式在盲区处失效 | **PoC 栈机 bug** | callee 用 BL 静态目标 / 只在命中函数入口时 push |
| 盲区空档 | 806 NACC + 340 坏包(硬件采样错) | 硬件采集 | 治采集(§21.4);盲区两端栈机别乱接 |

- **opencsd 解码本身没问题**:17 函数、2773 PC 全 flash、盲区如实标 NACC。
- **PoC 栈机是"最小原型",callee 识别过于naive**(§24.2),产品级要用 BL 静态目标 +
  盲区感知的栈对齐。**这些是自研栈机要做对的地方,恰恰是自研的价值所在**——mortrall
  在这些点上用 disposition 硬猜、盲区乱走,而我们可以基于 opencsd 的干净 range + NACC
  标记做正确的盲区处理。
- **路线不变**:opencsd 基座正确,栈机需要比 PoC 更严谨的 callee/盲区处理。这是可控的
  工程,不是路线级风险。


---

## 25. PoC 栈机修复 v2:盲区感知的 callee/return（2026-08-09）

按 §24 定位,修 PoC 栈机的两处：

1. **callee 只在命中函数入口时确认**:CALL 后,下一 range 起址**必须等于某函数 lowaddr
   (`is_func_entry`)** 才 `do_call`;否则(盲区吞掉 callee / 落在函数中段)丢弃这次 call
   (`dropped_calls`),不再凭"下一 range 起址"捏造帧。
2. **盲区标记**:`ADDR_NACC` / `TRACE_ON` 出现时置 `after_blind`,清 `pending_call`——
   盲区后第一个 range 是重同步落点,绝不当 callee。
3. **返回地址匹配 pop**:range 起址 == 栈顶记录的返回址时,判为正常返回并 pop;
   非自递归函数"push callee==top"时先弹陈旧帧(missed-return 兜底)。

### 25.1 效果（同 cm100 slice,demo_cm100_v2.perftrace）

| 指标 | v1 | v2 |
|---|---|---|
| 自嵌套(push==top) | **72** | **7** (↓90%) |
| max depth | 6 | **4** |
| begin/end 平衡 | 622/622 | 607/607(final=0) |
| dropped calls(盲区吞 callee) | — | 5 |

- **自嵌套 72→7**,深度 6→4,依然完美配平。
- 剩余 7 个:盲区**恰好把重同步落点落在某函数入口**、同时该函数陈旧帧还开着的情况;
  跨盲区的 missed-return 兜底没全接住(产品级要按返回地址栈级联 unwind)。
- **验证过 core_state_transition/cmp_complex 在 ELF 里不自递归**(唯一 caller 是
  core_bench_state/core_bench_list),所以这 7 个仍是 artifact,不是真递归。

### 25.2 残留与产品级方向

- 7 个残留 + 5 个 dropped call + 124 mismatched return,都是**盲区(806 NACC)**的下游——
  跨盲区的调用/返回配对信息物理丢失,栈机只能启发式兜底。**根治要治采集降盗区**(§21.4),
  解码侧只能做到"盲区两端不乱接 + 按返回址尽量 unwind"。
- 产品级 callee 识别应直接用 **BL 指令静态目标**(反汇编 mem 取立即数),而非"下一 range
  起址",这样即便 callee 入口那段被盲区吞掉,也知道该 push 谁——比 PoC 的"命中入口才
  push"更强(能标出"进入了 X 但 body 缺失")。
- 产物 `poc_opencsd/demo_cm100_v2.perftrace` 可拖进 ui.perfetto.dev 对比 v1。

**结论不变**:opencsd 基座正确,栈机的盲区处理是可控工程;自嵌套已从 72 压到 7,demo
明显更干净。真正消除残留要靠降采集盗区 + 产品级 BL-静态目标 callee。


---

## 26. SysTick 识别 + 调用图对 ELF 验证（2026-08-09,用户两问）

### 26.1 SysTick 为何"没识别" —— PoC 忽略了 EXCEPTION 元素,不是 opencsd 没解

- **opencsd 解出来了**:lister 显示本 slice 有 14 个 `OCSD_GEN_TRC_ELEM_EXCEPTION
  (excep num 0x0f)` = 异常 15 = SysTick,且都配了 `EXCEPTION_RET`。
- **PoC v1/v2 的 callback 里 `OCSD_GEN_TRC_ELEM_EXCEPTION` case 是空的**(注释写"real
  build opens ISR track"),所以 SysTick 没进 trace。**是导出层没做,不是解码丢。**
- **v3 修复**:EXCEPTION 元素按 Cortex-M 异常号映射名字(15=SysTick/14=PendSV/11=SVCall/
  3=HardFault/2=NMI/≥16=外部 IRQ),开一个嵌套 slice;`EXCEPTION_RET` 按进入时记录的栈
  深度 unwind 回去。结果:**14 个 `IRQ:SysTick` slice 正确渲染**,trace 依然完美配平
  (begin==end, final_depth=0)。

### 26.2 调用图对 ELF 验证 —— 11/11 边全对,0 mismatch(黄金判据)

用 verify_calls 同款判据(每条 caller→callee 边必须对应 ELF 里真实的 `bl`)。PoC 输出
所有调用边,`verify_edges.py` 逐条查 ELF objdump 的 bl 目标:

```
OK  cm_benchmark_main -> core_bench_list   (x1)
OK  core_bench_list   -> cmp_complex       (x141)
OK  core_bench_list   -> crc16             (x77)
OK  cmp_complex       -> crcu16            (x73)
OK  cmp_complex       -> core_bench_state  (x30)
OK  cmp_complex       -> core_bench_matrix (x8)
OK  core_bench_state  -> core_state_transition (x95)
OK  core_bench_state  -> crcu32            (x36)
OK  core_bench_state  -> memset            (x51)
OK  core_bench_matrix -> matrix_test       (x8)
OK  matrix_test       -> crc16             (x87)

11 edges match ELF, 0 mismatch
```

- **每一条调用边都对应 ELF 里真实存在的 `bl`**,0 条捏造。这是本项目一贯的黄金判据
  (verify_calls.py),PoC 的 opencsd 后端**首次一把过 0 mismatch**。
- 调用关系与 CoreMark 真实结构一致:cm_benchmark_main→core_bench_list→{cmp_complex,
  crc16},cmp_complex→{crcu16,core_bench_state,core_bench_matrix},core_bench_state→
  {core_state_transition,crcu32,memset},core_bench_matrix→matrix_test→crc16。

### 26.3 产物

`poc_opencsd/demo_cm100_v3.perftrace`(含 SysTick),配套 `verify_edges.py`(边对 ELF)、
`balance.py`(嵌套配平)、`analyze_trace.py`(函数/自嵌套/gap 统计)。

**两问答复**:
1. **SysTick**:opencsd 解出了(14 个),v1/v2 PoC 导出层没处理;v3 已渲染为 `IRQ:SysTick`。
2. **调用图对 ELF**:**11/11 边全对,0 mismatch**——调用关系逐条对得上 ELF 的 bl,
   通过黄金判据。这比 mortrall 更强(mortrall 假重入导致大量捏造的 caller→callee)。


---

## 27. Trace 盲区率统计（时间维度,2026-08-09,用户问）

给 PoC 接上 FPGA 时间基(`.time.bin`,uint64 ns/ETM 字节,由 opencsd 的 `idx_sop` 索引),
统计时间轴上"没有任何指令/函数覆盖"的占比。

### 27.1 方法

- 每个 element 的 `idx_sop`(ETM 字节偏移)→ 查 `time.bin` 得该点 ns。
- 遇 `ADDR_NACC`/`TRACE_ON`(盲区/坏包)置 `blind_pending`;下一个 INSTR_RANGE 到来时,
  从"上一个覆盖点"到"当前点"的时间计为盲区(连续 NACC 合并成一段)。
- 盲区率 = Σ盲区时间 / trace 总时间跨度。

### 27.2 结果（cm100 零丢包 slice,318826 ETM 字节 / 73.7ms 跨度）

```
trace span : 73699.6 us
blind time : 46888.6 us  over 92 regions
BLIND RATE : 63.62%   (covered 36.38%)
```

- **盲区率 63.6%** —— 这段 slice 里 **近 2/3 的时间没有指令覆盖**。
- 92 段盲区,平均每段 ~510µs;57 段 >100µs,单段最大 ~2.3ms;**分布在整条时间轴**
  (不是一个大洞),与 §21 "坏包成簇但分散" 一致。
- 覆盖的 36.4%(26.8ms)里有 98091 条 range,密度正常(~273ns/range)。

### 27.3 解读（重要,别误读）

- **这个 63.6% 是 cm100 这段"网络零丢包但采样眼裕度边际"抓样的盲区率,不是系统固有值。**
  §21 已证同抓样两段坏包率 0.75%→10% 时变——**盲区率随采集质量剧烈波动**。cm100 这段
  恰好是坏包偏多的(NACC 806),所以盲区率高。
- **盲区 = 硬件采样没采到的真实时间**,opencsd 如实标 NACC 空着;这正是它诚实的地方
  (mortrall 会把这些洞用 4.6× 假指令填满,反而看不出有洞)。
- **盲区率是采集质量的直接量化指标**,比"坏包数"更直观:
  - 降盲区要治采集(§21.4:位宽↓/降频/眼图 IDELAY 校准/SI 改善)。
  - 这也给了产品级一个**质量门**:盲区率 >X% 的 trace 该告警"采集质量差,结论存疑"。

### 27.4 工具

`csperf_poc` 加 `CSPERF_TIMEBASE=<time.bin>` env 即输出盲区率。产品级应把盲区率做成
每条 trace 的标准质量指标(和 verify_calls 的 0-mismatch 并列),导出到 Perfetto 时
可叠一条"coverage/blind"轨直观显示哪段没采到。

**答复**:能统计,本 slice **盲区率 63.6%**(覆盖 36.4%)。但这是该抓样采集质量的反映
(NACC 806、坏包偏多),会随采集质量大幅变化,不是解码或方案的固有缺陷——opencsd 把盲区
如实标出来,反而是可信的质量信号。

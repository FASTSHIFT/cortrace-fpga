# r32 — 审核 `proposals/42-Mortrall栈模型重构-BB0调用图深嵌套根因与修复.md`（红方）

**日期**：2026-07-25
**对象**：proposal 42（Mortrall 栈拆分重构方案）
**复核**：直接读 `embedded-debug-tools/ext/orbetto/src/mortrall.hpp` 相关行、`subprojects/orbuculum/Src/loadelf.c` 分类逻辑
**立场**：严格证伪；只接受能被源码/离线数据/原文引用支持的判断。

---

## 一句话裁决

**这个方案在假设未证的病灶上重构 150 行最复杂的模块。** §2 一路推理走到"iBR/return 简单路径其实是对的（`stack[0]=retA, stack[1]=B_pc` 不冲突）"，然后临门一脚在 §2.5 把根因赖给"pop 分类失败"——但这是一个纯猜测，蓝方自己在 §2.5 结尾用"必须验证 —— 见 §5"承认了。§5.1 是**一行 fprintf** 就能立刻做的实验，成本几分钟；**这条实验做完之前，proposal 42 的重构不能落地**。除此之外我在源码里挖出两个 proposal 42 一字未提的关键设施——`MAX_SANE_DEPTH=16` 安全网强制平栈（mortrall.hpp:1384）、以及 `resentStackDel` **推测性弹栈 + 撤销** 机制（1281-1298）——它们**本身就足以解释"深度 16.5 + coremark_main 反复出现"这个症状**，且是根本不同的失效通路。方案没触及这两处，重构完后症状很可能只是转移形态而非消失。备选方案（tag 位、shadow 栈）proposal 42 完全没对比就跳到重构，违反项目"小步快跑 + 每步验证"纪律。

---

## 逐条打击

### Q1 §2.5 "pop 分类失败" 猜测　🟥 **未证的核心假设，且大概率证伪**

**打击 1（源码核验）**：直接读 `loadelf.c:1148-1152`：
```c
*ic |= (
    ((insn->id == ARM_INS_SUB) || (insn->id == ARM_INS_MOV) ||
     (insn->id == ARM_INS_LDM) || (insn->id == ARM_INS_POP) ||
     (insn->id == ARM_INS_ISB) || (insn->id == ARM_INS_ORR))
    && strstr(insn->op_str, "pc")
) ? LE_IC_JUMP : 0;
```
判据是 `insn->id == ARM_INS_POP` **且** `strstr(op_str, "pc")`。Capstone 对 `pop {r4, pc}` 的 op_str 是 `{r4, pc}`——`strstr(..., "pc")` **必然命中**。**§2.5 的"pop 分类可能失败"在源码层面就是假的**：只要 Capstone 把它识别为 `ARM_INS_POP`（这是 Thumb-2 pop 的标准解码），LE_IC_JUMP 必设。而 CoreMark 全 Thumb-2 且蓝方自己 §1.2 承认"opencsd 侧数据正确 verify_calls 100%"，说明 Capstone 分类正确。**这个"pop 分类失败"的假设 90% 概率证伪**。

**打击 2（如果 §5.1 证伪，proposal 塌一半）**：蓝方自己在 §2.5 结尾写"这个猜测必须验证——见 §5 实验清单"。§5.1 的实验（临时 fprintf 看 0x800a2f2 处的 ic 值）成本是**改一行代码重跑一次抓样**，不到 30 分钟。**在 §5.1 结果出来之前，proposal 42 不应作为落地方案存在。** 现在写 150 行重构的时序假设都建立在这个未证猜测上——这**是本项目 r25/r28/r30 一路批的"未验证假设当结论"的典型复发**，且是最严重的一次（对象是最复杂的模块）。

**证明/证伪实验设计（比 §5.1 更严格）**：
1. 在 `_pumpAction` 处理 ETM4 分支时打印 `(pc, ic值, ARM_INS_POP判定, workingAddr_before, workingAddr_after)` 到独立日志。
2. 抓一次 CoreMark @300M BB=0，跑到 UART 打印期间，从日志中 grep 所有 `pc = 0x800a2f2` 的事件。
3. 判据：
   - 若 100% ic & LE_IC_JUMP 且都走了 iBR/return 分支 → **§2.5 假设证伪**，根因不在 pop 分类，proposal 42 需要重新推导。
   - 若发现某些 pc 的 ic 缺 JUMP → §2.5 成立，但仍需解释"为什么同一条 pop 有时分类对有时错"（这更可能是 opcode 表加载/多个静态 elf 加载覆盖问题，不是栈模型问题，仍不构成"重构栈"的理由）。
4. 平行判据（覆盖蓝方 §5.2 iBR hit 数不足假说）：iBR 触发计数 vs opencsd 输出的 `iBR ret` 元素数。差 <5% → iBR 路径工作正常，根因在别处。**这条更能定位真凶**。

### Q2 §3.1 拆栈的时序 / 单调性　🟥 **蓝方没算过账**

**打击**：源码 `mortrall.hpp:539-545`：
```cpp
_addTopToStack(Mortrall::r, Mortrall::r->op.workingAddr);
_generate_protobuf_entries_single(Mortrall::r->op.workingAddr);
```
每条指令走完都调 `_generate_protobuf_entries_single`。看它内部 (`865-880`)：
```cpp
csb.fpga_ns_buffer[csb.proto_buffer_index] = Mortrall::fpga_ns;
if (stackDepth < perfettoStackDepth) perfettoStackDepth--;
else if (stackDepth > perfettoStackDepth) perfettoStackDepth++;
```
`perfettoStackDepth` **每次最多 ±1**——它是**逐步靠拢**当前 `stackDepth`，靠 FPGA 时间戳序列化输出。

**proposal 42 的 §3.4 计划改成 `while (perfettoDepth < retDepth) emit_B(...)`——一次性发射多层 B|**。这与现有的"每次 ±1、每步喂 FPGA 时间戳"完全冲突：
- 现有代码为什么每步 ±1？因为**每层 B|/E| 都需要一个 FPGA 时间戳**（`fpga_ns_buffer`），且必须**单调递增**（Perfetto 要求）。同一 Atom batch 内一次弹 3 层，怎么给 3 个时间戳？插值？插值算法在哪？
- 现在的 `_generate_protobuf_entries_single` 在 Atom batch 里每指令步调一次，其实是**用循环外的时间戳递增**（`fpga_ns` 每次 EV_CH_ATOMS 分配一份）来保证 B|/E| 时间戳单调。改成 while 循环一次 emit 多层就打破这个约束。

**结论**：§3.1/§3.4 提供的 while 循环重构**没有说清楚 FPGA 时间戳如何分配给同一 batch 内的多层事件**。proposal 41 §21 的"10ns 步进不变式"（蓝方在 Q7 提到）确实会被这个改动威胁——**蓝方在 §3.4 用 `...` 省略了 emit_B/emit_E 的时间戳来源**，这不是设计文档，是伪代码。**Q2 成立**。

### Q3 top-match 只匹配地址、不匹配 IS bit　🟨 **成立但影响面窄**

**核验**：IHI0064H §5.3.1 line 19472-19483：
> "the trace unit compares both the target address of the branch, and **the IS indicator for the instruction at that address**, with the address and instruction set contained on the top entry of the return stack."

原文明确要求 address **and** IS 两者。§3.3 proposal 只写：
```cpp
if (retDepth >= 0 && cpu->addr == retStack[retDepth]) retDepth--;
```
只匹配地址。

**影响**：CoreMark 全 Thumb，IS bit 恒定，短期无害。但：
- 项目未来若跑 Mixed ARM/Thumb（比如 legacy blob 或引导代码），会**误弹**——考虑 Cortex-M 只有 Thumb，误弹只在真跑到 CoreSight-M-Plus-A profile 时才出现。
- **打击**：既然要"重构"、要"1:1 对齐 ETMv4 规范"，那就应该按规范做完。当前方案是"照抄一半规范然后加中文注释说这才是正确实现"。**Q3 成立，但优先级低**。

**修复动作**：`retStack` 元素类型改为 `{addr, isThumb}` 对，match 时两者都比。存储成本可忽略。

### Q4 §2.6 "其他函数只 1-2 层深" 循环论证　🟥 **成立且暴露论证结构问题**

Q4 命中要害。蓝方逻辑是：
- 观察 A：UART 4 层深路径**每次** iBR 都失效 → 症状路径依赖
- 观察 B：`core_state_transition` 内层 iBR 一次都不失效
- 蓝方解释：因为内层深度 1-2 层，"任何一次错误 pop 都能被下一个 BL 覆盖修复"

**打击**：如果 bug 是**通用**的（如 §2.5 所述 pop 分类失败），**路径无关**。不管 UART 还是 CoreMark hot function，只要遇到 pop `{r4, pc}` 就分类失败——**同一条 pop 指令在两条路径下不可能分类不一致**。所以观察 A/B 的路径选择性**证伪了 §2.5 的"pop 分类是普遍失败机理"** —— 病灶必然是**路径条件**触发的（比如深度阈值、异常插入、某个特定 fpga_ns 采样点）。

蓝方"1-2 层深能被覆盖修复"这个说法**没解释为什么覆盖能修复**——如果栈项被真实污染，浅栈也应该看到污染，只是**观察者从 Perfetto 看到**"每次内层调用重置了栈"而已，这不是 bug 消失。**Q4 成立**。

**替代解释（自然涌现）**：`MAX_SANE_DEPTH=16` 强制平栈（mortrall.hpp:1384-1394）—— 深路径积累到 16 触发 flush → coremark_main 反复出现深度 16.5 正是这个 flush 后新的 push；浅路径永远碰不到 16，flush 从不触发，看着"干净"。**这条替代路径 proposal 42 一字未提，且更符合观察**（"深度 16.5" 正好是 MAX_SANE_DEPTH 附近）。

### Q5 opencsd 漏发 iBR 事件的可能性　🟨 **成立但可低成本排除**

Q5 命中：`verify_calls` 只验 BL/BLX 目标对 ELF，不验 iBR 事件数**是否匹配** BL 事件数。**Mortrall 少 pop 一次 = 深度永久 +1**——正是观察到的现象。

**低成本排除实验**：
1. `opencsd_etm4_run --dump-lister` 全流跑一次；
2. 统计 `INSTR_RANGE(... E iBR V7:impl ret)` 计数 = X，以及 `b+link` 目标计数 = Y。
3. 期望 X ≈ Y（不必严格相等：异常入口/退出、TRACE_ON 边界、超出流末尾未返回等会产生 X < Y 的小差）。
4. 若 |X - Y| > 10（在 500KB 段），说明 opencsd 层就在系统性漏发某类 iBR 事件——**根因在 orbuculum/opencsd，不在 Mortrall**，proposal 42 全部作废。
5. 若 X ≈ Y，Mortrall 侧才是嫌疑。**Q5 有独立价值，proposal 42 §6 那句"opencsd 大 range 归约到 NACC 已排除"论据不充分**，应把这条实验列进 §5 优先级 1。

### Q6 §5.4 replay 单测可行性　🟨 **成立且蓝方低估工作量**

Q6 命中：Mortrall 消费的是 `TRACEDecoderCB` 回调（`traceDecoder_etm4.c` 层输出），不是 opencsd `trc_pkt_lister` 输出。这两个是**独立的 ETMv4 解码实现**（orbuculum 有自己的 ETMv4 decoder，`Src/traceDecoder_etm4.c`）。

**抽取难度**：
- 要 replay Mortrall，需要序列化 `TRACEDecoderPumped` 每次调用时的 `(state_changes bitmap, cpu 快照)`。
- 现有 orbetto 没有这个 dump 能力。需要在 `mortrall.hpp` 加 `pumpAction` 入口的 fprintf，把每次调用的 `(EV_bits, cpu->addr, cpu->disposition, cpu->eatoms, cpu->natoms, ic, ...)` 序列化到文件。
- 然后写一个独立 replay 驱动，重放序列到 `_pumpAction`。**这至少半天工作**，蓝方在 §8 把"回归测试"估半天里，没显式给 §5.4 单独工时。
- **如果做不到 §5.4** →§5 实验清单退化成"部分验证"，proposal 42 缺最后一步的确定性证据，正如 Q6 所说会"看起来对就发布"。

**建议路径**：不用 replay 单测这条重路。改成**双抓样对照**——修复前后跑同一 2MB 抓样，检查 `verify_calls 100%` + `coremark_main 出现次数=1` + `perfettoDepth ≤ 3` 三个不变式；再补一个 BB=1 抓样跑对照。**这个方案零基础设施，绕开 replay。**

### Q7 §8 时序估算 1 天　🟥 **严重低估**

**打击**：`_generate_protobuf_entries_single` 涉及：
- `_appendTOProtoBuffer` 的 cycle-count buffer 平滑逻辑（`mortrall.hpp:860-900`）
- FPGA 时间戳插值（`fpga_ns_buffer` + `global_interpolations` array）
- 单调性约束（Perfetto B|/E| 要求）
- 与 `_inconsistentFunctionSwitch` / `_revertStackDel` / `_catchInconsistencies` 的耦合

**且**：`_revertStackDel`（1281-1298）实现**推测性弹栈 + 撤销**——iBR 分支先弹栈（`_removeRetFromStack`）+ 设 `resentStackDel=true`+ `committed=false`，之后若下一步 EV_CH_ADDRESS 与推测一致就 confirm、不一致就 revert（`stackDepth++`）。**这是一个跨调用的状态机**。proposal 42 §3.2 把 iBR 分支重写成简单 pop（`pc = retStack[retDepth--]`），**完全忽略了 revert 语义**——如果没有 revert，遇到 exception-after-jump 场景会失败（正是 `_revertStackDel` 注释描述的场景 2）。

**这一处漏改足以让重构版本在异常/中断场景下比现有版本更差**。

**修正工时估算**：
- §3 重构实施 1 天 → **2-3 天**（含 revert 机制迁移、Perfetto 时间戳与 depth 的耦合）
- 回归测试半天 → **1-1.5 天**（含 proposal 41 §21 10ns 步进不变式验证）
- **Q7 成立，蓝方低估约 2× 工时**。

### Q8 备选方案对比缺失　🟥 **成立且是核心方法论问题**

**打击**：本项目 AGENT.md §6 明文"单变量递增、小步快跑、每步验证"。proposal 42 直接跳到"重写 150 行最复杂模块"，**违反自家方法论**。

红方给的两个备选方案至少值得对比：

**备选 A（tag 位）**：`stack[]` 每项加 1 bit 标签（LSB 因为地址至少 2-byte 对齐，可复用）区分 `RET_ADDR` / `CURSOR`：
```cpp
#define TAG_RET 1
#define TAG_CUR 0
// _addRetToStack: stack[d] = p | TAG_RET; d++
// _addTopToStack: stack[d] = (p & ~1) | TAG_CUR;  // 不改 d
// iBR: while (d>=0 && !(stack[d]&1)) d--;   // 跳过 cursor
//      pc = stack[d] & ~1; d--;
```
**改动 <30 行**，完全兼容现有 Perfetto 生成逻辑。若病灶真是"iBR 读栈时读到 cursor"，这个改动直接修复。**若 §5.1/§5.2 证伪"pop 分类"、根因确定是栈项污染，tag 位方案能立刻验证**。

**备选 B（shadow 栈）**：`_addTopToStack` 前把 `stack[depth]` 备份到 `shadow[depth]`，iBR 从 shadow[depth-1] 弹目标：
```cpp
_addTopToStack: shadow[d] = stack[d]; stack[d] = p;
iBR:            pc = shadow[d-1]; d--;
```
**改动 <20 行**。缺点：`shadow` 与 `stack` 双维护，但语义等价于 retStack/pc 拆分，工程量小 5-10 倍。

**Q8 成立**：proposal 42 应先对比 A/B 两个小 fix，先落地一个再看效果，不达标再考虑重构。**"两周内能落地的 patch 比彻底重构更符合项目'小步快跑 + 每步验证'的方法论"**这句话是硬约束不是建议。

---

## 我额外挖到的两个 proposal 42 未提及的失效通路

这两条是我读源码时挖出来的，proposal 42 §2 一字未提，且**任一条足以独立解释"深度 16.5 + coremark_main 反复出现"**——如果这两条是真凶之一，proposal 42 的栈拆分重构**无法修复**。

### E1 🟥 `MAX_SANE_DEPTH=16` 强制平栈（mortrall.hpp:1384-1394）
```cpp
constexpr int MAX_SANE_DEPTH = 16;
if (stackDepth >= MAX_SANE_DEPTH) {
    while (stackDepth > 0) _removeRetFromStack(...);
    _generate_protobuf_entries_single(workingAddr);
}
```
**行为**：一旦栈深≥16，无条件平到 0。**观察到的 "Perfetto 深度 0.5..16.5"** 恰好是这个安全网触发的签名——16 就 flush、下一次 push 又从 0 涨起来。

**这条与 UART 深路径 4 层被观察成"深度 16"矛盾**——UART 只有 4 层真调用，怎么积累到 16？只可能是**多轮 UART 迭代**中每轮少 pop 1 次，累积到 16 后 flush，然后新一轮从 0 又开始。**但 proposal 42 §1.2 说"64 次 coremark_main 假嵌套"——若每轮少 pop 1 次、每 16 轮 flush 一次，则 64 次 = 4 次 flush 事件，恰好对应"平均每 flush 出现 16 次假 coremark_main"**。数字对得上。

**若 E1 是真凶**：proposal 42 拆栈后，若 pop 语义仍有微小误差，安全网继续触发、症状继续存在。**必须先禁用 MAX_SANE_DEPTH 安全网重跑一次**，看深度会涨到多少、假嵌套次数如何变化——这条实验 1 分钟改代码 + 30 分钟抓样，蓝方漏做。

### E2 🟨 `resentStackDel` 推测性弹栈 + 撤销（mortrall.hpp:1281-1298 + iBR 分支 line 667-679）
iBR 分支现在的写法（`_pumpAction`）：
```cpp
Mortrall::r->op.workingAddr = Mortrall::r->callStack->stack[stackDepth - 1];
Mortrall::r->committed = false;      // 标记未 commit
Mortrall::r->resentStackDel = true;   // 允许下一步 revert
_removeRetFromStack(Mortrall::r);     // 弹
```
然后下一步 EV_CH_ADDRESS 到达时 `_revertStackDel` 检查一致性：
- 若地址一致 → confirm 弹
- 若地址不一致（`inconsistent && !exceptionEntry`）→ `stackDepth++` **撤销弹栈**

**症状路径**：若 iBR 触发时预测的返回地址（`stack[depth-1]`）与实际下一个 EV_CH_ADDRESS 不符（可能因 §Q1 的 iBR 目标其实是被采样窗口 aliased 的另一个函数），则 revert，**栈越来越深**——正是深度 16 触发 E1 的机制。

**若 E2 是真凶**：proposal 42 §3.2 iBR 分支写死 `pc = retStack[retDepth--]`，**没有 revert 机制**，遇到同样场景**直接读错 pc**——比现在更糟（现在至少 revert 能撤销）。**必须迁移 revert 语义到新方案，proposal 42 §3 完全没提**。

**判定**：E1 和 E2 都是 proposal 42 §2 分析里**结构性漏掉**的失效通路。**这是本次评审最重要的红方发现——蓝方在推理"root cause"时只跟着自己的假设走，没做 diff 式地把 `mortrall.hpp` 里所有影响 stackDepth 的代码路径列一遍**。

---

## 结论表

| Q | 议题 | 判定 | 关键理由 |
|---|------|------|---------|
| 1 | pop 分类失败假设 | 🟥未证且大概率证伪 | loadelf.c:1148 命中 ARM_INS_POP && strstr("pc")；§5.1 实验必做 |
| 2 | 拆栈的时序单调性 | 🟥未算过账 | while emit 多层没解释 fpga_ns 分配；proposal 41 §21 不变式威胁 |
| 3 | top-match 缺 IS bit | 🟨窄影响 | IHI0064 §5.3.1 要求 addr+IS 两者；CoreMark 无害但方案要向后兼容 |
| 4 | 路径选择性 | 🟥循环论证 | "1-2 层能覆盖修复"没机制解释；反而支持 E1 安全网假说 |
| 5 | opencsd 漏发 iBR | 🟨可低成本排除 | verify_calls 不覆盖 iBR 数一致性；X vs Y 计数比较即可 |
| 6 | replay 单测可行性 | 🟨蓝方低估 | 需要在 mortrall 加 pump-action dump；建议改双抓样对照绕开 |
| 7 | 时序估算 1 天 | 🟥低估 2× | revert 机制+cycle-count buffer+FPGA 时间戳耦合 |
| 8 | 备选方案对比缺失 | 🟥违反方法论 | tag 位/shadow 栈都是 <30 行改动，未对比就跳重构 |
| E1 | MAX_SANE_DEPTH=16 安全网 | 🟥未提及且是首要嫌疑 | 症状"深度 16.5"恰好指纹 |
| E2 | resentStackDel revert 机制 | 🟨proposal 完全没提 | 新方案没迁移 revert，异常/中断场景更差 |

---

## 整篇裁决

**不能落地。要求做 5 件事，缺一不可**：

1. **P0 立即跑 §5.1 pop 分类实验**（30 分钟）。若 pop 分类正确 → §2.5 假设证伪 → **本 proposal 必须重新推导根因**。
2. **P0 关掉 MAX_SANE_DEPTH 安全网重跑一次**（1 小时）。看深度自由增长后能涨多少、假嵌套模式怎么变。**若关掉后症状消失或大变** → E1 是真凶，proposal 42 修错了对象。
3. **P0 iBR 事件数 vs BL 事件数计数比较**（Q5，1 小时）。排除 opencsd 层漏发 iBR。
4. **P1 对比备选方案 A（tag 位）和 B（shadow 栈）**，各写 30 行原型跑对照。**任一备选达标就落地，不做重构**。
5. **P2 仅当 1-4 全部证伪现有小 fix 后**，才走 §3 重构，且工时按 2-3 天算，`_revertStackDel` 语义必须迁移到新方案。

**拒绝性质**：这是本项目 r25/r28/r30 一路批的"未验证假设当结论 + 违反小步纪律"老毛病的又一次复发，且这次改动对象是最复杂的模块——如果照原方案上，回归成本远高于 §8 估的 1 天。

---

## 一句话给用户

**别急着重构。你在 §2 推理走到"简单 3 层路径其实是对的"就临门一脚，然后 §2.5 说"根因是 pop 分类失败"——这是纯猜测，且 loadelf.c:1148 明明写着 pop && strstr("pc") 命中 LE_IC_JUMP，Capstone 对 `pop {r4, pc}` op_str 就是 "{r4, pc}"，strstr 一定命中，猜测大概率错。你自己在 §5.1 也说必须验证，那就先验证再谈重构——30 分钟的实验凭什么放到后面。而且我在 mortrall.hpp 里挖出两个你 §2 一字未提的东西：line 1384 那个 `MAX_SANE_DEPTH=16` 安全网强制平栈——你观察到的"Perfetto 深度 0.5..16.5"就是它触发的签名，coremark_main 出现 64 次恰好可以解释成 4 次 flush × 每次 16；还有 line 1281 那个 `resentStackDel` 推测性弹栈 + 撤销机制，你 §3.2 iBR 分支重写成 `pc = retStack[retDepth--]` 一行完全没搬 revert 语义，异常/中断场景会比现在更差。备选方案 tag 位（<30 行）和 shadow 栈（<20 行）你完全没对比就跳到 150 行重构，违反项目自家方法论。落地前必须先做 3 件事：跑 §5.1 pop 分类实验、关 MAX_SANE_DEPTH 重跑一次、iBR vs BL 事件数比对；然后再对比 tag 位/shadow 栈；重构只在小 fix 都不够时才做。**

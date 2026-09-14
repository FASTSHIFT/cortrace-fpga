# Proposal 42：Mortrall 栈模型重构 —— BB=0 场景假深嵌套根因与修复

> 状态：分析完成 + 方案设计，等红方评审后实施
> 前置：proposal 39（BB=0 冲频成果、调用边逐条对 ELF 全对）+ AGENT.md 坑点 25
> 关联：orbuculum `Src/traceDecoder_etm4.c`、`ext/orbetto/src/mortrall.hpp`
> 目的：把 CoreMark 流式 perf 里"coremark_main 嵌套到深度 16"这类**只是显示脏但不影响
>       verify_calls 的**假嵌套彻底修掉，让 Perfetto UI 直接可用

## 0. 一句话

Perfetto 里 `coremark_main` 反复深嵌套是 **Mortrall 把"当前工作地址游标"和"返回地址栈"
混在同一个 `stack[]` 数组**，遇到 iBR (indirect branch return) 时 `_removeRetFromStack`
只弹一层且弹的可能不是真返回地址。BB=0 与否**无关**，采集完全干净（丢包 = 0）。

修复方案：把 CallStack 拆成两个独立字段 `retStack[]` (纯返回地址) + `pc` (当前工作
地址)，`_generate_protobuf_entries_single` 用 `retStack` 深度决定 B|/E| 层数，工作地址
只用于 `symbolFunctionAt()` 命名当前 slice 顶层。BB=0 也能得到干净调用图。

## 1. 观测事实（都是离线可复现的）

### 1.1 采集端完全干净

| 指标 | 值 | 判据 |
|---|---|---|
| FPGA 侧 `stream_lost_cnt` | 0 | clk200→clk125 async FIFO 从未溢出 |
| UDP `seq-gap` | 0 | stream_recv 每包 4B BE seq 逐包核对 |
| 2 MB 抓样字节完整性 | 干净 | opencsd `deframed 413224 ETM / 104410 fsync` 全数解出 |
| opencsd 输出 EXCEPTION 元素（关 SysTick） | 0 | 无其他中断 |

**故障与 FPGA/RTL/网络/位错无关。** 下述现象是 PC 侧解码器行为。

### 1.2 问题现象（CoreMark 300M / BB=0 / cache 使能 / 4-bit / 关 SysTick）

- 2 MB 抓样、20.94 ms 时间轴、73188 ftrace 事件
- `coremark_main` 应只被 `main()` 调 1 次（源码 `cm_uart.c:45` 的
  `void coremark_main(void) { ... for(;;) cm_benchmark_main(); }`），
  但 Perfetto 里显示 **64 次进入、嵌套深度 0.5..16.5**
- 内层 `core_state_transition/crc16/cmp_complex/crcu32` 等真实 hot function 数量准确
  且深度稳定 —— 假嵌套只发生在**深调用**（UART 打印那段 3-4 层的路径）
- **`verify_calls.py` 独立验证 opencsd 输出 `b+link` 目标 100% 对 ELF**（proposal 39
  已确立的诚实判据）：500KB 段 678/678 全对，0 mismatch。
- 也就是说：**调用边数据本身正确，是 Mortrall 把它渲染成"深嵌套"的过程出错。**

### 1.3 ETM 配置实测

`openocd` 直读 CoreSight 寄存器：
```
TRCIDR0     = 0x080006e1   (RETSTACK bit9=1 → 硬件返回栈已实现)
TRCCONFIGR  = 0x00000001   (RS bit12=0 → 返回栈优化未启用；BB=0, CCI=0, TS=0)
```
所以我们当前的 trace 流里 **ETM 对每个 indirect branch 都发 Address 元素**，无返回栈
优化省略。

### 1.4 opencsd 侧的证据 —— iBR 有完整地址锚点

500KB 段 lister 统计（`decode/opencsd_etm4_run.py --dump-lister`）：
- I_ATOM 系列 78 K 个（P0 元素密集）
- **I_ADDR_MATCH 11885** + **I_ADDR_S_IS1 1904** —— 每条 iBR 都紧跟一个 short/EAM 地址包
- opencsd 输出 `iBR V7:impl ret` × 8671 —— 全部带正确目标地址

单个 iBR 现场（Idx:323）：
```
Idx:323; ID:0; [0xda ]; I_ATOM_F2 : Atom format 2.; NE
Idx:323; ID:2; INSTR_RANGE(range=0x8008e2e:[0x8008e3a] N cond BR)
Idx:323; ID:2; INSTR_RANGE(range=0x8008e3a:[0x8008e48] E iBR V7:impl ret)
Idx:324; ID:0; [0x96 0x92 0x8f ]; I_ADDR_S_IS1: Addr=0x0000000008008F24    ← 目标地址
Idx:327; ID:2; INSTR_RANGE(range=0x8008f24:[...])                          ← 继续在返回目标
```
opencsd 拿到完整信息、恢复完美。**Mortrall 拿到的是同一份数据流**（都消费 orbuculum
`traceDecoder_etm4.c` 的输出），所以信息层面没有丢东西。

### 1.5 Mortrall 端诊断（关 SysTick 后仍有 64 次假嵌套）

在 `_addRetToStack` / `_removeRetFromStack` / CALL / IJMP / IRET 加环形 buffer 打点
（离线日志 `captures/mortrall_stack_diag.log`，316 KB）。第一次假嵌套现场：

```
CALL a=0x8009772(ee_printf) → b=0x800a2d4    ; bl uart_send_char
PUSH a=0x8009776    (return addr, depth 2→3)
IJMP a=0x800a2e0(cm_uart_send_char) b=0x800a2f0   ; cbz r4, 800a2f0（跳过 HAL 调用）
CALL a=0x800a2fe(coremark_main) → b=0x800a284     ; ??? workingAddr 突然跳 0xE 字节
PUSH a=0x800a302    (return addr, depth 3→4)
```

从 `cm_uart_send_char` 结尾 (`0x800a2f0 add sp, #8` → `0x800a2f2 pop {r4, pc}`)
返回到 `coremark_main` 之间**没有任何 IRET 或 POP 事件**，workingAddr 直接跳到
`0x800a2fe`（coremark_main 里下一个 `bl cm_uart_puts`）。**这一次真正的返回操作
被 Mortrall 无声吞掉了**。

之后 CoreMark 继续正常调用 → 栈里累积了 3 份 `0x800a302`（同一 BL 站点的返回地址，
本该 pop 掉的没 pop）。

## 2. 根因：Mortrall 栈模型混淆

### 2.1 现有 CallStack 数据结构（`mortrall.hpp:75-80`）

```cpp
struct CallStack {
    symbolMemaddr stack[MAX_CALL_STACK];    /* Stack of calls */
    int stackDepth{-1};                     /* Current stack depth */
    int perfettoStackDepth{-1};             /* Stack depth at which perfetto is currently */
};
```

**只有一个 `stack[]`** 承担两个语义：
- `stack[0..stackDepth-1]` = 各层调用的**返回地址**（`_addRetToStack` push 的
  `workingAddr + 2/4`）
- `stack[stackDepth]` = **当前正在执行的地址（游标）**（`_addTopToStack` 每条指令
  更新）

### 2.2 关键操作（`mortrall.hpp:1378-1435`）

```cpp
// 1378  BL: push return addr, depth++
static void _addRetToStack(RunTime *r, symbolMemaddr p) { stack[depth] = p; depth++; }

// 1424  每条指令：更新栈顶为当前 PC （不改 depth）
static void _addTopToStack(RunTime *r, symbolMemaddr p) { stack[depth] = p; }

// 1413  return: depth--
static void _removeRetFromStack(RunTime *r) { depth--; }
```

调用者：
- BL 分支 (`ic & LE_IC_CALL`, line 633-644)：`_addRetToStack(workingAddr+2/4)` +
  `_addTopToStack(newaddr)`
- 每收到 EV_CH_ADDRESS (line 488-492) 或每条指令走完 (line 533-538)：
  `_addTopToStack(workingAddr)` + `_generate_protobuf_entries_single(...)`
- 非 immediate JUMP (line 654-679)：从 `stack[depth-1]` 拿返回地址给 workingAddr，
  `_removeRetFromStack`

### 2.3 混淆导致的失效路径

假设正常序列 `A --bl--> B --bl--> C --pop pc-->` 返回到 B 里下一条指令：

理论行为：
```
初始              stack=[]                  depth=-1
A 里 bl B         stack=[retA_after_bl]     depth=0     ← _addRetToStack
进入 B            stack=[retA, B_pc]        depth=0     ← _addTopToStack 覆盖 stack[0]... 
                                                          等一下：depth 是 0，stack[0]=retA 被 B_pc 覆盖！
```

**这里就出问题了**。`_addTopToStack` 用 `stack[stackDepth]` 作为写入位置，`stackDepth`
在 push 之后是 0，所以立刻把 `stack[0]`（应该是 retA）**覆盖成 B 的当前 PC**。

看代码流程更精确：

`_addRetToStack(p)`：先写 `stack[depth] = p` 再 `depth++`，所以 push 后
`stack[oldDepth] = retA` 且 `depth = oldDepth+1`。之后 `_addTopToStack(newaddr)` 写
`stack[depth] = newaddr` = `stack[oldDepth+1] = newaddr` —— **写在 `retA` 上面一格**，
不覆盖返回地址。✓

那为什么会失效？细看下一步：在 B 里走每条 P0，`_addTopToStack(workingAddr)` 更新
`stack[depth] = workingAddr in B`。所以：
```
stack[0] = retA
stack[1] = B_currentPC  (逐指令更新)
depth = 1
```
✓ 依然正确。

问题出在 **iBR/return 分支** (line 654-679)：
```cpp
else  // non-immediate JUMP
{
    if (!_handleExceptionExit(func))
    {
        if (Mortrall::r->callStack->stackDepth)  // depth >= 1
        {
            Mortrall::r->op.workingAddr =
                Mortrall::r->callStack->stack[stackDepth - 1];  // ← 读 stack[depth-1]
        }
        _removeRetFromStack(Mortrall::r);        // depth--
    }
}
```

它读 `stack[depth-1]` 作为返回地址。此时 `stack[depth-1]` 是什么？**看下面**：

**上一步（B 里正常执行）**结束时：`stack[0] = retA, stack[1] = someB_pc, depth = 1`。
现在 B 里做 iBR (`pop {pc}`) 返回：

- 触发 iBR 分支
- 读 `stack[depth-1] = stack[0] = retA` ✓ 正确
- `_removeRetFromStack` → `depth = 0`
- workingAddr = retA

**这也是对的**。所以简单情况没问题。**问题**出现在**嵌套更深**时。

### 2.4 3 层嵌套的失效点（精确到指令）

`A --bl--> B --bl--> C --pop pc--> B --pop pc--> A`：

```
状态                         stack[]              depth
A 中 bl B                    [retA_after_bl]      depth=1  (_addRetToStack 后 depth++)
  _addTopToStack(B)           stack[1]=B          depth=1
B 中 bl C                    [retA, retB_after_bl] depth=2
  _addTopToStack(C)           stack[2]=C          depth=2
C 中 pop pc （iBR）           读 stack[1]=retB ✓  workingAddr=retB, _removeRet, depth=1
  _addTopToStack(retB)        stack[1]=retB       depth=1     ← 覆盖了 stack[1] 的原值 retB_after_bl
                                                                本来就是它，看似 OK
B 里从 retB 继续执行           _addTopToStack(...)  stack[1]=cur_pc_in_B  depth=1
B 中 pop pc （iBR）           读 stack[0]=retA ✓  workingAddr=retA, depth=0
```

**这条路径看起来也是对的**。那假嵌套究竟从哪来？

### 2.5 真正的失效：`_addTopToStack` 与 `_generate_protobuf_entries_single` 的耦合

诊断日志观察到的现象：**iBR 没触发 IRET 事件**，workingAddr 直接跳到不相关的地址。
排查 `_pumpAction` 逻辑（`mortrall.hpp:600-680`），iBR 分支只在特定条件下进入：

```cpp
if (ic & LE_IC_CALL) { ... }          // BL/BLX
else if (ic & LE_IC_JUMP)              // JUMP 或 return
{
    if (insExecuted)
    {
        if (ic & LE_IC_IMMEDIATE) workingAddr = newaddr;
        else                       走 iBR/return 分支
    }
    else
        workingAddr += 2/4;            // 未 taken，顺序
}
```

**触发条件**：`ic & LE_IC_JUMP` 且 `insExecuted`。`ic` 由 `loadelf.c:1141-1163` 根据
指令类型 + 操作数计算：
- `POP + strstr("pc")` → `LE_IC_JUMP`（非 IMMEDIATE，因为 POP 无 IMM 操作数）
- `LDR + pc` → `LE_IC_JUMP`（同上）
- `BX/BXJ/CBZ/CBNZ` → `LE_IC_JUMP`
- 带 IMM 操作数的分支 → 追加 `LE_IC_IMMEDIATE`

诊断日志里 `cm_uart_send_char` 的 `pop {r4, pc}` (0x800a2f2) **确实分类为 JUMP**（因为
`op_str = "{r4, pc}"` 命中 `strstr("pc")`），且无 IMM → 应走 iBR/return 分支。**为什么
没触发 IRET？**

线索：日志里 `IJMP a=0x800a2e0 b=0x800a2f0` 之后**直接就是** `CALL a=0x800a2fe`。中间
两条指令 `add sp, #8` (0x800a2f0)、`pop {r4, pc}` (0x800a2f2) **没有独立事件**。

看 opencsd 侧 lister 那条 range：`0x800a2e0:[0x800a2f2]` — end 停在 pop 之前？不，看
最相近的 range：

```
INSTR_RANGE(range=0x800a2e0:[0x800a2f0] N ...)   ← cbz not-taken
INSTR_RANGE(range=0x800a2f0:[0x800a2f2] E ...)   ← 走 pop
```
（推断 —— 需要在下一次抓样时精确 grep）

**如果 pop 那条 range 的 `ic` 里没设 LE_IC_JUMP**（因为 loadelf 对 pop 的分类依赖
op_str 里出现 "pc"，但 Capstone 对 `pop {r4, pc}` 的 op_str **可能是 `"{r4, pc}"` 或
`"r4, pc"` 或其他格式**），iBR 分支就不会走 —— workingAddr 顺序推进到 0x800a2f4，
超越函数边界，进入 `.word 0x20000288` 数据区，然后**顺序执行数据**直到某个"看起来
像 bl 的字节序列"（0x800a2fe 附近确实是下一个函数 coremark_main 的 bl 指令）。

这个猜测**必须验证** —— 见 §5 实验清单。但**不论具体触发点**，根本症结是同一个：
`stack[]` 一个数组承担两种语义，`_addTopToStack` 每条指令覆盖栈顶导致 iBR/return
时读到的"返回地址"可能已经是本函数内的中间 PC，而不是真正的 caller 返回点。

### 2.6 为什么"其他函数看着好、只 UART 深调用垮"

CoreMark 主循环 hot function（`core_state_transition/crc16/cmp_complex`）深度只 1-2
层，pop 一次就到根，任何一次错误 pop 都能被下一个 BL 覆盖修复。

UART 打印路径 `coremark_main → cm_uart_puts → cm_uart_send_char → HAL_UART_Transmit`
是 4 层深，任何一次弹错就级联到所有更外层，栈里残留调用者的返回地址（各种
`0x800a302 / 0x800a3a0` 都在 coremark_main 内），加上 orbetto 的
"最近符号命名"，Perfetto 里就是**同一函数出现 N 次的假嵌套**。

## 3. 重构方案

### 3.1 拆分栈模型

```cpp
struct CallStack {
    symbolMemaddr retStack[MAX_CALL_STACK];   // 纯返回地址栈（BL 时 push，iBR 时 pop）
    int           retDepth{-1};                // retStack 有效元素数 - 1
    symbolMemaddr pc;                          // 当前工作 PC（游标）
    int           perfettoDepth{-1};           // Perfetto 已输出的 B| 层数 - 1
};
```

**关键不变式**：`retStack[0..retDepth]` 永远是**返回地址**，`pc` 永远是**游标**。
两者不共用存储、不互相覆盖。

### 3.2 操作重写

```cpp
// BL/BLX (line 633-644 附近)
if (ic & LE_IC_CALL && insExecuted) {
    retStack[++retDepth] = workingAddr + (ic & LE_IC_4BYTE ? 4 : 2);
    pc = newaddr;
}

// 立即跳 (line 656-660)
else if ((ic & LE_IC_JUMP) && (ic & LE_IC_IMMEDIATE) && insExecuted) {
    pc = newaddr;
}

// iBR / return (line 662-680)
else if ((ic & LE_IC_JUMP) && insExecuted) {
    if (_handleExceptionExit(func)) { /* ... */ }
    else if (retDepth >= 0) {
        pc = retStack[retDepth--];   // 读并弹
    } else {
        // 异常：栈空但走返回路径，等下一个 Address 元素重锚
        pc = ADDRESS_UNKNOWN;
    }
}

// 顺序执行
else {
    pc += (ic & LE_IC_4BYTE ? 4 : 2);
}
```

`_addTopToStack` **完全删除**。工作地址就是 `pc`，直接写它。

### 3.3 EV_CH_ADDRESS 处理（line 488-492）

```cpp
if (EV_CH_ADDRESS) {
    // ETM 明确告诉我们 PC = cpu->addr。如果它跟 retStack 顶匹配，就是隐式返回
    if (retDepth >= 0 && cpu->addr == retStack[retDepth]) {
        retDepth--;  // 消费返回栈项（就是 §14.1.2 返回栈优化的算法）
    }
    pc = cpu->addr;
    _handleExceptionEntry();
    _generate_protobuf_entries_single(cpu->addr);
}
```

**这才是 ETMv4 §14.1.2 规范的正确实现**。RS=1 时 ETM 会省略 iBR 的 Address 元素，
分析器**自动从 retStack 弹目标**；RS=0 时 ETM 依然发 Address 元素，我们的
`retDepth--` 也能正确同步（因为 top 匹配）。**两种模式统一处理**。

### 3.4 Perfetto 事件生成（`_generate_protobuf_entries_single`, line 977-1030）

```cpp
void _generate_protobuf_entries_single(uint32_t addr) {
    if (!_inconsistentFunctionSwitch(addr) && committed) {
        // top slice 的显示函数 = 包含 pc 的函数
        top_thread_func = symbolFunctionAt(s, pc);

        while (perfettoDepth < retDepth) {
            // 补发 B|：新增的每一层都要用 retStack 对应位置的返回地址反推调用者内的
            // 函数名，但我们其实要显示的是那一层 *进入的* 函数 —— 这个信息 push 时
            // 记下来。所以 retStack 需要同时记录 (return_addr, entered_func)。
            ...
            emit_B(entered_func_at_depth[perfettoDepth+1]);
            perfettoDepth++;
        }
        while (perfettoDepth > retDepth) {
            emit_E();
            perfettoDepth--;
        }
        // 同层内可能换了函数（尾调用/内部跳转）
        if (perfettoDepth >= 0 && slice_top_func != top_thread_func) {
            emit_E(); emit_B(top_thread_func);
        }
    }
}
```

**扩展 CallStack**：
```cpp
struct CallStack {
    symbolMemaddr retStack[MAX];
    struct symbolFunctionStore *enteredFunc[MAX];  // 每层进入时的函数
    int retDepth{-1};
    symbolMemaddr pc;
    struct symbolFunctionStore *pcFunc;    // pc 所在函数（缓存）
    int perfettoDepth{-1};
};
```

BL 时：`enteredFunc[retDepth] = symbolFunctionAt(newaddr)`。iBR 时：`retDepth--`
（enteredFunc 也随之消失）。这样 Perfetto 层级永远对应真实调用栈。

### 3.5 兼容旧行为

- `_inconsistentFunctionSwitch`（噪声塌栈）保留，但**只用 `retStack`** 做比对
- 异常处理（`_handleExceptionEntry` / `_handleExceptionExitETM35`）保持独立
  `exceptionCallStack`，切换语义不变
- `_addTopToStack` 删除；所有调用点改为 `pc = <value>`
- 单元测试：跑 `verify_calls.py` 应仍 100% 通过（数据未变，只是重建方式改）

### 3.6 改动范围（保守估计）

| 文件 | 大约行数 | 说明 |
|---|---|---|
| `mortrall.hpp` `CallStack` 结构 | +10 | 加 `retStack`/`retDepth`/`pc`/`enteredFunc` |
| `_addRetToStack` / `_removeRetFromStack` / `_addTopToStack` | 全改 (~50) | 换栈实现，删 _addTopToStack |
| `_pumpAction` BL/JUMP/return 三分支 | 修 (~30) | pc 直接赋值，retStack 独立 push/pop |
| `EV_CH_ADDRESS` 处理 | 修 (~15) | 加返回栈 top-match 检查（§3.3） |
| `_generate_protobuf_entries_single` | 重写 (~40) | 用 retDepth 做层数、enteredFunc 做名 |
| `_inconsistentFunctionSwitch` | 微调 (~10) | 引用改 `retStack` |
| 单元测试 | +新 | 3 层调用序列、iBR、异常 entry/exit |

**估计总改动 ~150 行，可控**。风险主要在 `_generate_protobuf_entries_single` 的
buffering（`_appendTOProtoBuffer` 里 perfettoStackDepth 的 stepping 逻辑）需要跟着改。

## 4. 修复的可验证效果

修复后**必然满足**以下不变式（可自动化断言）：

1. **CoreMark 主循环里 `coremark_main` 出现次数 = 1**（不是 64、不是 257）
2. **顶层深度 ≤ 3**（`main → coremark_main → cm_benchmark_main` / `cm_uart_puts`）
3. **`verify_calls.py` 依然 100%**（数据未变、只是渲染改）
4. **CoreMark hot function `core_state_transition` 计数与旧版一致**（内层数据不受影响）
5. **`_addRetToStack` 累计次数 == 观察到的 BL/BLX Atom 数**（可 assert 打点）
6. **`_removeRetFromStack` 累计次数 ≥ retDepth 次的重置次数 × (BL 深度)**（不再"pop
   远多于 push"—— 之前观察到 pop 16909 vs push 10546 就是这个 bug 的证据）

## 5. 实施前的实验清单（必做）

在动手改前，先用离线抓样把**假设**逐条验证，避免像 proposal 38 那样在错误诊断上
反复补丁。

### 5.1 精确定位"pop {..., pc}"是否被 loadelf 分类为 JUMP

```bash
# 在 orbuculum loadelf.c 加临时 fprintf，看 0x800a2f2 (cm_uart_send_char 的 pop)
# 处的 ic 值是什么。预期结果：LE_IC_JUMP=set, LE_IC_IMMEDIATE=clear
```

### 5.2 精确定位真返回路径

在 `_pumpAction` 的 iBR 分支入口 (line 663) 加计数器，统计**每个函数**的 iBR 触发次
数。对照 `verify_calls.py` 的调用边数：`iBR_hits` 应约等于 `BL_hits`。

### 5.3 iBR 触发时读到的返回地址精度

在 iBR 分支加打点，输出 `(workingAddr_now, stack[depth-1], expected_return_from_
verify_calls)`。看**stack[depth-1] 和真实返回地址的一致率**。假设不足 50% 就证明栈
项被覆盖污染了。

### 5.4 顺序推进 vs 跳跃的边界

以 `0x800a2f2 pop {r4, pc}` 为例，写一段单元级 replay：喂进指定的 atom/address 序列
（从离线 lister 抽出来），看 Mortrall workingAddr 走到哪。这一步能把 §2.5 的猜测
从"可能"变成"确定"。

## 6. 已排除的其他假设（不再回头）

| 假设 | 证据 | 结论 |
|---|---|---|
| FPGA 采样丢字节 | `stream_lost_cnt=0`, verify_calls 500KB 段 678/678 | 排除 |
| UDP 丢包 | `seq-gap=0` in 2MB 抓样 | 排除 |
| 位错 / SI | 4-bit @300M 现场稳定跑数百 MB 数据 clean | 排除 |
| ETMv4 返回栈优化未处理 | RS=0，Address 元素完整发出（Idx:324 现场） | 排除（现场是 RS 关，不是问题源） |
| SysTick 中断入口/出口特殊路径 | 关 SysTick 后 opencsd EXCEPTION=0，仍 64 次假嵌套 | 部分成立（SysTick 放大问题）但**不是根因** |
| opencsd 大 range 归约到 NACC | verify_calls 依然 100%，说明**opencsd 输出正确**（问题在 Mortrall 消费） | 排除 |

## 7. 与 proposal 39 的关系

proposal 39 结论"BB=0 函数级 trace 可用、调用边逐条对 ELF、100% 匹配"**仍然成立**
—— verify_calls 走 opencsd 的 INSTR_RANGE 判据，不依赖 Mortrall/Perfetto。本 proposal
修的是 **Perfetto 可视化层**的显示问题，不影响诚实的调用图正确性。

修完之后，Perfetto UI 也**首次能被日常使用**（现在打开会被深嵌套刷屏，只能看内层
局部）。

## 8. 时序估算

| 阶段 | 工时 | 备注 |
|---|---|---|
| §5 实验清单跑完 | 半天 | 每条实验都能离线复现，抓样已有 |
| §3 重构实施 | 1 天 | 重点是 `_generate_protobuf_entries_single` |
| 回归测试（verify_calls + CoreMark hot function 计数） | 半天 | 用 2MB 现有抓样 + 一份 BB=1 抓样对照 |
| 上游 PR（可选） | 半天 | 我们的 fork 已经改过 loadelf/etm4/tpiu，加这条不奇怪 |

## 9. 备份数据

- **诊断日志**：`captures/mortrall_stack_diag.log`（316 KB，第一次假嵌套现场 + 事件
  流环形 buffer）
- **抓样**：
  - `captures/coremark_stream_2mb.bin`（关 SysTick、300M、BB=0、4-bit，2 MB, 20.94 ms）
  - `captures/coremark_stream_nosystick_td.perf`（现有的 5MB 版 perf，供对比）
- **opencsd lister**：`/tmp/cm500k.lst`（重跑 `opencsd_etm4_run.py --dump-lister`
  即可再生）
- **ELF/反汇编**：`captures/rt300.elf` / `rt300.dis`

---

## 附：红方评审提示词（打对话框贴给对手）

> 你是 orbetto/Mortrall 领域的独立红方评审。**目标是把这份 proposal 42 打散**：找出
> 论证链条里最薄弱的一环，或直接证伪核心结论。可以拒绝一切"我猜""可能"的推断，
> 只接受能被离线数据或原文引用支持的判断。
>
> 请优先质疑以下几点，任何一点被证伪就撤回或大改本方案：
>
> 1. **§2.5 中"pop {..., pc} 分类失败导致顺序推进"的猜测**：这是本 proposal 假设
>    的失效路径，但 §2.3-2.4 的推理只能证明 `stack[]` 混语义**不必然**触发 bug，
>    真正的直接触发点还没有硬证据。你怎么设计一个能证明 / 证伪它的实验？如果
>    §5.1、5.2 的实验结果**不支持**这个猜测（比如 pop 分类正确、iBR hit 数不足以
>    解释所有假嵌套），本 proposal 的重构价值大打折扣 —— 因为它可能修的不是真正
>    的病灶。
>
> 2. **§3.1 拆栈方案的时序假设**：Mortrall 现在每收到 EV_CH_ATOMS 会**批量重放** N
>    个 disposition bit（Atom Format 5 里最多 5 个），每 bit 都跑一次
>    `_addTopToStack + _generate_protobuf_entries_single`。你确定新的 `pc` 顺序
>    推进能保持所有 Perfetto B|/E| 事件的**时间戳单调性**？特别是当同一个 Atom
>    batch 里既有 BL 又有 iBR 时，新旧方案的时序差别会不会引入新 bug？
>
> 3. **§3.3 EV_CH_ADDRESS 的 top-match 检查**：ETMv4 §14.1.2 说返回栈匹配的判据是
>    "target address **and** IS (instruction set)"完全相同，你在方案里只匹配地
>    址，忽略了 Thumb bit。会不会因此在 ARM/Thumb 切换代码里误弹栈？CoreMark 全
>    Thumb 无 ARM，可能不显现，但方案要向后兼容其他场景。
>
> 4. **§2.6"其他函数看着好因为只 1-2 层深"的说法有循环论证嫌疑**：为什么 UART 那
>    条路径的**每次** iBR 都失效，而 core_state_transition 里的 iBR **一次都不
>    失效**？如果 bug 是通用的，应该是概率性的丢层，不是路径依赖的。你怎么解释
>    这个"path selectivity"？
>
> 5. **§6 排除表里"opencsd 大 range 归约到 NACC 已排除"**：verify_calls 100% 只能
>    证明**存在** BL/BLX 边的目标对 ELF，不能证明 opencsd 没漏发 iBR 事件。如果
>    某段 SysTick 或异常前的 pending atom 归约里**漏了一次 iBR**，Mortrall 就永
>    远少 pop 一次，栈就爆。这跟 §2.5 是不同的失效机理，值得单独验证。
>
> 6. **§5.4 replay 单测的可行性**：你需要拿到 orbetto 里 Mortrall 消费的 `ic` /
>    atom disposition 序列，但那不是 opencsd 的输出，是 orbuculum ETMv4 的输出。
>    抽取难度多大？如果做不到，§5.4 不能作为验证方案的最后一步 —— 会退化成"看起
>    来对就发布"。
>
> 7. **风险量化**：`_generate_protobuf_entries_single` 是 Mortrall 里最复杂的一
>    段，涉及 `_appendTOProtoBuffer` 的 cycle-count buffer 和 FPGA-time path。
>    重写它意味着**要重新验证时基准确性**（proposal 41 §21 的 10ns 步进不变式）。
>    这个回归成本没估进 §8 的 1 天里，是不是低估了？
>
> 8. **备选方案对比缺失**：本 proposal 直接跳到"重构栈模型"。有没有更小的 fix
>    —— 比如**在现有 stack[] 上加 tag 位区分 "return_addr" 和 "cursor"**？或者
>    **每次 `_addTopToStack` 前把 `stack[depth]` 保存到 shadow[depth]`，iBR 从
>    shadow 弹**？两周内能落地的 patch 比彻底重构更符合项目"小步快跑 + 每步验证"
>    的方法论。
>
> 拒绝礼节性认可。给我具体反驳点，不给"整体方向 OK"这种评价。

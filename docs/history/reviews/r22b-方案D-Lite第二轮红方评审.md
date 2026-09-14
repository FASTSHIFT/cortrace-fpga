# R22b · 方案 D-Lite 第二轮红方评审

> **评审对象**：`proposals/24-方案D-OpenCSD调用栈重建设计.md`（D-Lite 版）
> **评审立场**：红方（苛刻挑刺，只挑刺）
> **评审日期**：2026-06-28
> **评审方法**：逐行比对 D-Lite 文档、第一轮评审报告、`etm_reconstruct.py` 源码、
>   `test_etm_reconstruct.py` 源码、`orbetto.perf` 实际数据

---

## 总体结论：**存疑（有条件通过）**

D-Lite 相比原方案 D 有**实质性改进**：放弃了编造的 OpenCSD ctypes 绑定，改用已验证的
`etm_reconstruct.py`，消除了第一轮否决的核心理由（§1.1 C API 编造、§1.2 ctypes 结构体
全错、§5.1 过度工程）。方向正确。

但 D-Lite **不能直接进入实施**，存在 3 个阻塞问题：

1. **callstack_test 固件有活跃中断**（78 个 IRQ 事件），但 D-Lite §4.3 声称"中断部分处理
   （region 边界自然清栈）"——实际 orbetto 数据显示中断在函数执行中途打断，不是在 region
   边界。D-Lite 的中断处理策略基于错误假设。

2. **§4.2.2 和 §4.2.5 给出两段矛盾的算法实现**，且 §4.2.5 的递归修复引入了 `is_call`
   字段但未反映到 §4.2.2 的代码和 §4.5 的测试用例中。最终算法不明确。

3. **D2（修改 etm_reconstruct.py）0.5h 严重低估**：改返回值格式会破坏 `test_etm_reconstruct.py`
   的 10+ 个断言（全部直接比较 `addrs == [0x...]`），需要同步修改所有测试。增加 `is_call`
   字段需要修改 `_classify_insn` + `Insn.__slots__` + `load_image` + `reconstruct_region`
   的 atom walk 循环。0.5h 不够，实际 2-3h。

**实施前必须先确认的前置条件**（见 §8）。

---

## 1. 第一轮否决理由是否真正修复

### 1a) §6.1 C API 编造 → 放弃 OpenCSD — **通过（但有过泛化风险）**

D-Lite 放弃了 OpenCSD ctypes 绑定，改用 `etm_reconstruct.py`。第一轮的 §1.1（C API 编造）
和 §1.2（ctypes 结构体全错）**已规避**。

但"已验证 ground-truth 正确"这个说法**被过度泛化**：

- doc 14 §6 的验证只覆盖了 `proj_add`（`while(1){loop_sum(5);}`），这是一个**极简单的
  紧循环**：只有 2 个函数（`loop_sum` + `add`），只有直接调用（`bl add`），没有递归、
  没有间接调用、没有中断、没有深调用链。
- `test_etm_reconstruct.py` 的集成测试（`test_proj_add_reconstruction_matches_source`）
  只检查 `add()` 的指令三元组 `[0x08000F8C, 0x08000F8E, 0x08000F90]` 出现在重建结果中，
  以及 `bl add`（0x08000FB2）和 `blt.n`（0x08000FBC）地址存在——**不检查完整的调用流
  正确性**。
- callstack_test 有递归（`factorial`）、间接调用（`callback_test`、`indirect_caller`）、
  深调用链（`deep1→deep6`）、互相调用（`pingpong`）——**这些场景 `etm_reconstruct.py`
  从未验证过**。

**具体风险**：
- `factorial` 递归：`etm_reconstruct.py` 的 `reconstruct_region` 在遇到 `bl factorial`
  （direct branch, E atom）时会跳到 `factorial` 入口，继续 walk——**理论上正确**，但从未
  在真实 trace 上验证。如果 `factorial` 的 `bl` 指令的 target 提取有误（`_imm_target`
  的正则匹配问题），PC 会走飞。
- `callback_test` 间接调用：通过函数指针调用 `cb_handler_a/b`，ETM 会发 branch address
  packet。`etm_reconstruct.py` 的 `_next_branch()` 函数需要正确解析这个 packet。如果
  packet 编码格式与 `_next_branch` 的解析逻辑不匹配（比如 2-byte vs 3-byte branch packet），
  region 会在间接调用处中断。
- `pingpong` 互相调用：A 调 B，B 调 A，A 调 B……如果递归深度大，`reconstruct_region`
  的 `max_insns=20000` 限制可能触发。

**建议**：在 D3 阶段，先用 `etm_reconstruct.py` 跑 callstack_test trace，**检查 region
长度分布和 stop_reason 分布**。如果大量 region 因 "indirect w/o branch pkt" 或 "pc not
in image" 中断，说明 `etm_reconstruct.py` 在这些场景下有问题。

### 1b) §6.2 ctypes 结构体全错 → 已规避 — **通过**

D-Lite 不使用 ctypes。第一轮的 §1.2 **已规避**。

### 1c) §5.1 过度工程 → 改为复用 etm_reconstruct.py — **存疑（改动量低估）**

D-Lite §4.1.2 说需要修改 `etm_reconstruct.py` 的返回值格式，改动量 ~30 行。但实际影响
范围远超 30 行：

**`reconstruct_region()` 返回值变更的连锁影响**：

当前 `reconstruct_region()` 返回 `(insn_addrs, consumed_bytes, stop_reason)`，其中
`insn_addrs` 是 `list[int]`。D-Lite 要改为 `list[(addr, kind, byte_offset)]`。

受影响的代码：

| 位置 | 当前代码 | 需要改为 | 行数 |
|------|---------|---------|------|
| `reconstruct_region` L155 | `out.append(pc)` | `out.append((pc, ins.kind, i))` | 1 |
| `reconstruct_region` L155 | 需要记录 `ins.kind`，但 `ins` 在 `img.get(pc)` 之后可能为 None（L153-154 的 `if ins is None: return`）——需要在 None 检查之前记录 kind，或者在 None 时用 `('unknown')` | 增加逻辑 | 3-5 |
| `reconstruct_all` L232 | `addrs, _, reason = reconstruct_region(...)` | `records, _, reason = ...` | 1 |
| `reconstruct_all` L233 | `regions.append((s.addr, [s.addr] + addrs, reason))` | 需要把 `s.addr` 也包装成 `(s.addr, 'other', s.offset)` | 1 |
| `main` L250 | `total = sum(len(r[1]) for r in regions)` | 不变（len 不受影响） | 0 |
| `main` L255 | `lens = sorted((len(r[1]) ...))` | 不变 | 0 |
| `main` L260 | `longest = max(regions, key=lambda r: len(r[1]))` | 不变 | 0 |
| `main` L272 | `hist.update(r[1])` | `hist.update([x[0] for x in r[1]])` | 1 |
| `main` L285 | `_control_flow_check(regions, img, a.elf)` | 需要修改 `_control_flow_check` | — |
| `_control_flow_check` L309 | `addrs = r[1]` | `addrs = [x[0] for x in r[1]]` | 1 |
| `test_etm_reconstruct.py` L70 | `assert addrs[:3] == [0x08001000, ...]` | `assert [x[0] for x in addrs[:3]] == [...]` | 1 |
| `test_etm_reconstruct.py` L87-88 | `assert addrs == [0x08002000, ...]` | 同上 | 1 |
| `test_etm_reconstruct.py` L92-93 | `assert addrs2 == [0x08002000]` | 同上 | 1 |
| `test_etm_reconstruct.py` L99-100 | `assert addrs == [0x08003000]` | 同上 | 1 |
| `test_etm_reconstruct.py` L115-117 | `flat = [a for _, addrs, _ in regions for a in addrs]` | `flat = [x[0] for _, recs, _ in regions for x in recs]` | 1 |
| `test_etm_reconstruct.py` L151-152 | `assert addrs == [0x08000F8C, ...]` | 同上 | 1 |
| `test_etm_reconstruct.py` L160-161 | `assert addrs == [0x08001000]` | 同上 | 1 |
| `test_etm_reconstruct.py` L175-176 | `assert addrs == [target]` | 同上 | 1 |
| `test_etm_reconstruct.py` L184-185 | `assert 0x08003000 in addrs` | `assert 0x08003000 in [x[0] for x in addrs]` | 1 |

**总计**：`etm_reconstruct.py` 改 ~8 行，`test_etm_reconstruct.py` 改 ~10 行，
`_control_flow_check` 改 ~3 行 = **~21 行**。加上 `is_call` 字段的修改（见 §1e），
实际 ~35-40 行。30 行的估计**基本准确**（如果只算 `etm_reconstruct.py` 本身），但
**没有算 `test_etm_reconstruct.py` 的同步修改**。

**关键问题**：文档说"改动量~30行"但没提到 `test_etm_reconstruct.py` 也需要改。如果
只改 `etm_reconstruct.py` 不改测试，**所有 10+ 个现有测试都会失败**（类型不匹配：
`assert [int] == [(int, str, int)]`）。

### 1d) §2.4 递归测试与算法矛盾 → §4.2.5 提出修复但引入新问题 — **存疑**

D-Lite §4.2.5 提出了递归修复方案：用 `is_entry = (addr == func_addr)` + `kind == 'direct'`
判断递归调用。但这个修复**只是把一个 bug 换成了另一个 bug**：

**问题 1**：§4.2.5 自己承认"如何区分递归调用和循环回到函数入口"未解决。`while(1)` 里的
`continue` 跳回函数开头，`is_entry=True`，`kind='direct'`（`b` 指令）——与递归调用的
`bl` 指令特征相同（都是 direct branch 到函数入口）。§4.2.5 提出 `is_call` 字段方案
（`bl`/`blx` 为 True），但承认需要进一步区分 `bl` 和 `b`。

**问题 2**：§4.2.5 的代码用 `kind == 'direct'` 判断递归，但 `etm_reconstruct.py` 的
`_classify_insn` 把 `bl` 和 `b` **都归类为 `'direct'`**（L82-84：`_COND` 集合包含 `b`
和 `bl`）。所以 §4.2.5 的代码在当前 `etm_reconstruct.py` 上**无法区分递归和循环**——
需要先修改 `_classify_insn` 增加 `is_call` 字段。

**问题 3**：§4.2.5 的 `is_entry = (addr == func_addr)` 判断有边界问题。`func_addr` 是
`nm -nSC` 输出的函数起始地址。但：
- 如果编译器在函数入口前插入了对齐填充（padding），`func_addr` 指向 padding 而不是
  第一条指令——`is_entry` 永远为 False，递归不被检测。
- 如果函数有 prologue（`push {r4-r7, lr}`），ETM trace 中函数入口的第一条指令是 `push`，
  不是 `bl` 的目标——`addr == func_addr` 为 True，但 `kind` 是 `'other'`（push 不是
  branch），不触发递归检测。**实际递归检测发生在 `bl factorial` 之后的下一条指令**，
  即 `factorial` 入口的 `push`——此时 `addr == func_addr`，`kind == 'other'`，§4.2.5
  的 `if is_entry and kind == 'direct'` 条件不满足，**递归不被检测**。

**问题 4**：§4.2.5 的代码和 §4.2.2 的代码是**两段不同的 `on_instruction` 实现**。§4.2.2
没有 `is_entry` 判断，§4.2.5 有。最终实现应该用哪个？文档没有给出统一的最终算法。

**建议**：放弃在纯地址层面区分递归和循环。改用 `is_call` 字段（`bl`/`blx imm` 为 True），
在 `on_instruction` 中用 `is_call` 判断是否为 call：
- `is_call=True` 且目标函数 == 当前栈顶函数 → 递归 push
- `is_call=True` 且目标函数不在栈中 → 普通 call push
- `is_call=False` 且目标函数在栈中 → return pop
- `is_call=False` 且目标函数不在栈中 → region 边界或噪声，push（保守）

但这需要 `etm_reconstruct.py` 在 `reconstruct_region` 的 atom walk 中，对 `direct` 分支
进一步区分 `bl`（call）和 `b`（branch），并传递到 `insn_records` 中。

### 1e) §4.2 工程量低估 → D-Lite 改为 5.5-7.5h — **存疑（仍低估）**

D-Lite 的 5.5-7.5h 比 D 的 9.5h 更保守，但仍可能低估。逐项分析见 §6。

关键遗漏：D2（修改 `etm_reconstruct.py`）0.5h **不包含**：
1. 增加 `is_call` 字段到 `Insn.__slots__` 和 `Insn.__init__`
2. 修改 `_classify_insn` 区分 `bl`（call）和 `b`（branch）——当前代码 L82-84 把两者都
   归为 `'direct'`，需要拆分
3. 修改 `load_image` 传递 `is_call`
4. 修改 `reconstruct_region` 的 atom walk 循环，在 `direct` 分支中传递 `is_call`
5. 修改 `test_etm_reconstruct.py` 的 `test_classify_insn` 参数化测试（当前 `bl` 和 `b.n`
   都断言为 `'direct'`，需要改为不同 kind 或增加 `is_call` 断言）
6. 修改 `test_etm_reconstruct.py` 的 `_mk()` 辅助函数（增加 `is_call` 参数）

这些修改涉及 `etm_reconstruct.py` 的**核心逻辑**（`_classify_insn` 和 atom walk），不是
简单的返回值格式变更。0.5h **不够**，实际 2-3h。

---

## 2. etm_reconstruct.py 作为解码层的健壮性 — **存疑**

### 2a) Region 碎片化风险

`reconstruct_region()` 在以下情况停止：
- `pc not in image`（PC 走飞到非代码区）
- `indirect w/o branch pkt`（间接跳转的 branch address packet 丢失）
- 遇到非 P-header、非 branch、非 I-sync 的字节 → `break`

在 2-bit DDR 噪声流上，这些停止条件会频繁触发。D-Lite §4.5 的
`test_region_boundary_clears_stack` 测试了 region 边界清栈，但文档没有评估
**region 碎片化程度**。

**关键问题**：如果 callstack_test 的 8MB trace 被碎片化成几千个短 region（每个只有几条
指令），callstack tracker 还能重建出有意义的调用栈吗？

- 每个 region 边界都会清栈（§4.5 test_region_boundary）——如果 region 平均长度 < 函数
  执行长度，**大部分函数的 B 事件会在下一个 region 边界被 E 事件关闭**，调用栈永远
  深度=1。
- doc 14 §16.3 的数据（LA 干净流）：mean 45 条指令/region，median 24。但 callstack_test
  的函数（`level_a` → `level_b` → `level_c`）可能每个函数只有 5-10 条指令——如果 region
  长度 < 5 条指令，**深调用链永远无法重建**。

**建议**：在 D3 阶段，先跑 `etm_reconstruct.py` 统计 callstack_test trace 的 region
长度分布。如果 median region 长度 < 10 条指令，D-Lite 的调用栈重建**不可行**——需要
先改善 `etm_reconstruct.py` 的 region 连续性（比如容忍个别坏字节而不是立即停止）。

### 2b) 间接调用场景的 region 中断

`_next_branch()` 函数在找间接跳转目标时，如果 branch address packet 丢失（噪声），
返回 None，导致 region 停止（`stop_reason = "indirect w/o branch pkt"`）。

callstack_test 有 `callback_test`（间接调用 `cb_handler_a/b`）和 `indirect_caller`——
这些函数的间接调用在 ETM trace 中会产生 branch address packet。如果 packet 丢失，
region 会在间接调用处中断。

**文档没有评估间接调用场景下的 region 中断率**。从 orbetto.perf 数据看，
`callback_test` 和 `indirect_caller` 确实出现在 trace 中（函数名列表中有
`cb_handler_a`、`cb_handler_b`、`indirect_caller`），但 orbetto 的输出显示这些函数
的调用流**已经混乱**（orphan E、错误的栈深度），说明间接调用场景确实是问题区域。

### 2c) load_image() 的非代码段风险

`load_image()` 用 `objdump -d` 反汇编 ELF。`objdump -d` 只反汇编代码段（.text），但
如果 ELF 有 `.data` 或 `.bss` 段包含可执行地址（比如函数指针表），`objdump -d` 不会
反汇编它们——所以这个风险**不存在**。

但 `objdump -d` 会反汇编 `.init`、`.fini`、`.glue_7` 等辅助段，这些段的指令可能被
`load_image` 加入 `img` 字典。如果噪声 PC 落在这些段的地址范围内，`img.get(pc)` 会
返回垃圾 Insn，导致 PC 走飞。**文档没有讨论这个风险**。

实际影响较小（这些段通常很小），但应该在 D3 阶段检查 `img` 字典的大小和地址范围分布。

---

## 3. callstack_tracker 算法的边界条件 — **存疑**

### 3a) Region 边界后的函数入口时间偏移

§4.2.2 的算法在 Case 3（push）中，`entry_time_ns = time_ns`（当前指令的时间）。但如果
return 的目标函数不在栈中（因为 region 边界清栈了），`func_addr` 不在 `addrs_in_stack`
中，走 Case 3 push——这会把"region 边界后的第一条指令"当 call 压栈。

如果这条指令不是函数入口（比如是函数中间的某条指令），B 事件的 `name` 是正确的
（`func_for_pc` 返回的函数名），但 `entry_time` 是当前指令的时间，**不是真正的函数入口
时间**。这会导致函数持续时间偏短。

**文档没有讨论这个情况**。影响程度取决于 region 碎片化程度——如果 region 边界恰好
出现在函数入口附近（I-sync 锚点），影响小；如果 region 边界出现在函数中间，影响大。

### 3b) is_entry 判断的 padding 边界

§4.2.5 的 `is_entry = (addr == func_addr)` 判断有边界问题：

`nm -nSC` 输出的函数地址是符号表地址，通常是函数第一条指令的地址。但 ARM 编译器可能
在函数前插入对齐填充（`.align 2` 或 `.p2align 2`），这些 padding 字节不是函数的一部分，
`nm` 不会列出它们。所以 `func_addr` 通常指向真正的第一条指令，**padding 问题不存在**。

但如果函数有 prologue（`push {r4-r7, lr}`），ETM trace 中函数入口的第一条指令是 `push`，
`addr == func_addr` 为 True，但 `kind` 是 `'other'`（push 不是 branch）——§4.2.5 的
`if is_entry and kind == 'direct'` 条件不满足。**递归检测发生在 `bl factorial` 之后的
下一条指令**，即 `factorial` 入口的 `push`——此时 `is_entry=True` 但 `kind='other'`，
递归**不被检测**。

这是 §1d 问题 3 的重复，但值得强调：**§4.2.5 的递归检测逻辑在真实 ETM trace 上不工作**，
因为函数入口的第一条指令通常是 `push`（kind='other'），不是 `bl`（kind='direct'）。

### 3c) test_recursion 过于简化

§4.5 的 `test_recursion` 输入是三个相同地址 `(0x08002000, 'direct', ...)`。但在真实
ETM trace 中，递归调用的 ETM 序列不是"连续三个相同地址"——中间会有 factorial 函数体内
的其他指令（比较 n、减 1、`bl factorial`）。

真实递归的 ETM 序列（简化）：
```
0x08002000  push {r4, lr}        # factorial entry (1st)
0x08002002  cmp r0, #0
0x08002004  ble 0x08002010       # if n<=0, return
0x08002006  sub r0, r0, #1
0x08002008  bl 0x08002000        # recursive call
0x08002000  push {r4, lr}        # factorial entry (2nd) ← is_entry=True, kind='other'
...
```

第二条 `0x08002000` 的 `kind` 是 `'other'`（push），不是 `'direct'`——§4.2.5 的递归
检测条件 `is_entry and kind == 'direct'` **不满足**。测试用例用 `kind='direct'` 是
**不符合真实 ETM 序列的**。

**建议**：测试用例应该模拟真实 ETM 序列，包括函数体内的非 branch 指令。至少应该测试
`is_entry=True, kind='other'`（push）的情况，验证递归检测是否正确。

---

## 4. callstack_test ground truth 的完整性 — **否决**

### 4a) 地址范围未给出

§5.1 的调用树列出了 11 个子调用，但**没有给出每个函数的地址范围**。§5.3 列了 4 个
"在实施前需要确认"项，包括 `nm -nSC` 输出——**这些是验证的前提条件**。

如果在设计阶段不反汇编 `callstack_test.elf` 确认地址范围，D3 阶段的"与 §5.1 调用树
对照"就**无法执行**——不知道哪个地址对应哪个函数，无法判断 B/E 事件是否正确。

**但**：从 orbetto.perf 的函数名列表可以提取 demangled 函数名（`level_a`、`level_b`、
`level_c`、`deep1`-`deep6`、`factorial`、`callback_test`、`cb_handler_a/b`、
`mixed_test`、`op_add/mul/sub`、`leaf_add/mul`、`conditional`、`frame_func`、`pingpong`、
`indirect_caller`、`mydelay`、`repeat_test`），与 §5.1 的调用树**基本一致**。所以
ground truth 的函数列表是可信的，只是缺少地址范围。

### 4b) 调用树未标注调用类型

§5.1 的调用树没有标注哪些函数是间接调用（`callback_test`、`indirect_caller`）。间接
调用在 ETM trace 中的表现与直接调用不同（branch address packet vs direct branch
target），这会影响 `etm_reconstruct.py` 的 region 连续性。

从 orbetto.perf 的函数名列表可以确认 `callback_test` 和 `indirect_caller` 确实存在，
但调用树没有标注它们是间接调用——**这会影响 D3 阶段的验证**，因为间接调用的 region
更容易中断。

### 4c) factorial 递归深度未知

§5.1 没有给出 `factorial` 的递归深度 `n`。§6.2 的验收标准说"最大栈深度 = 5
（deep1→deep6）"，但如果 `factorial` 的递归深度 > 4，最大栈深度应该是 `factorial` 的
深度 + caller，不是 5。

从 orbetto.perf 数据看，`factorial` 出现在事件列表中（idx 9-10），但 orbetto 的栈
深度只有 1（`factorial` 单独出现，没有嵌套）——这可能是 orbetto 的 bug（递归未被
正确跟踪），也可能是 `factorial` 的递归深度确实很浅。

**建议**：在 D3 阶段前，反汇编 `callstack_test.elf`，找到 `factorial` 的调用点，
确认递归深度 `n`。

### 4d) ★★★ callstack_test 有活跃中断——D-Lite 的中断处理策略基于错误假设

**这是最大的阻塞问题。**

D-Lite §4.3 声称中断"部分处理"：ETM exception packet 在 `etm_reconstruct.py` 中会
中断 region（stop_reason），tracker 在 region 边界自然清栈。

但 orbetto.perf 的实际数据显示：**callstack_test trace 中有 78 个中断相关事件**，包括：

```
USART3_IRQHandler
USART_GetITStatus
USART_ClearITPendingBit
HardwareSerial::IRQHandler
Stream
```

这些事件**散布在整个 trace 中**（t=5652703, 6707281, 15142524, 16196983, 18188234,
18905209, ...），不是集中在 region 边界。说明中断在函数执行中途打断，不是在 region
边界。

**D-Lite 的中断处理策略（"region 边界自然清栈"）基于错误假设**。实际行为是：

1. 中断打断函数 A → IRQ handler 执行 → 返回函数 A
2. `etm_reconstruct.py` 的 `reconstruct_region` 在遇到 ETM exception packet 时
   **不一定会停止**——`_next_branch()` 的 `L._classify(c)` 把 `exc_entry` 和 `exc_exit`
   归类为可容忍的 interleaved packet（L283-284：`if k in ("trigger", "vmid", "ignore",
   "contextid", "exc_exit", "exc_entry"): j += 1; continue`）。所以 exception packet
   会被跳过，region **不会在中断处中断**。
3. 但中断 handler 的指令地址（`USART3_IRQHandler` 等）会出现在 atom walk 中——如果
   IRQ handler 的地址在 `img` 字典中（它在 ELF 符号表中），`reconstruct_region` 会
   walk 进 IRQ handler 的代码，**把 IRQ handler 的指令混入当前函数的指令序列**。
4. callstack tracker 会看到 PC 从函数 A 跳到 `USART3_IRQHandler`——如果
   `USART3_IRQHandler` 不在栈中，tracker 当 call 压栈。IRQ 返回时 PC 跳回函数 A，
   tracker pop `USART3_IRQHandler`——**看起来正确**。但 IRQ handler 内部的函数调用
   （`USART_GetITStatus`、`HardwareSerial::IRQHandler`）也会被当 call 压栈，增加
   栈深度。

**实际影响**：中断不会导致 orphan E（因为 tracker 的 push/pop 逻辑能处理地址跳转），
但会导致**调用栈中混入中断 handler 的函数**，使调用流与 §5.1 的调用树不一致。

**建议**：
1. 在 D3 阶段，检查 callstack_test trace 中中断事件的频率和分布
2. 如果中断频繁（78 个事件 / 1781 总事件 ≈ 4.4%），需要在 callstack tracker 中增加
  中断过滤逻辑（识别 IRQ handler 地址范围，不压栈）
3. 或者在 `etm_reconstruct.py` 中识别 exception packet，在 region 中断处分割

---

## 5. 与 orbetto 对比的公平性 — **存疑**

### 5a) orbetto 的"最大栈深度=2"来自哪条 trace？

§6.2 验收标准把 orbetto 的"最大栈深度=2（错误）"作为对比基线。经核查
`diag_orbetto_stack.py` 的输出：

- `orbetto.perf`（callstack_test trace）：`Final stack depth: 2`，`Unclosed functions:
  ['deep6', '__scatterload_rt2']`
- `lvgl_orbetto.perf`（LVGL trace）：`Final stack depth: 3`，`Unclosed functions:
  ['loop', 'lv_timer_handler', 'lv_timer_handler']`

所以"最大栈深度=2"来自 callstack_test trace。但 orbetto 在 callstack_test 上的实际
最大栈深度**不是 2**——从事件列表看，idx 41 的栈深度达到 4
（`level_a > level_b > deep6 > mydelay`）。`Final stack depth: 2` 是**最终未关闭的栈
深度**，不是最大栈深度。§6.2 的对比基线**描述不准确**。

### 5b) orphan E = 0 的目标在不同 trace 上的可行性

§6.2 的"orphan E = 0"目标——orbetto 的 61 orphan E 来自 callstack_test trace
（`orbetto.perf`），LVGL trace 有 635 orphan E（`lvgl_orbetto.perf`）。

D-Lite 在 callstack_test 上实现 orphan E = 0 是可能的（如果 region 碎片化不严重），
但在 LVGL trace 上**不能假设为 0**——LVGL 有更复杂的调用流、更多的间接调用、更多的
中断。§6.2 的验收标准应该区分 callstack_test 和 LVGL 两个阶段。

---

## 6. 工程量估计的合理性 — **存疑**

### 6a) D1（callstack_tracker + 单元测试）2-3h

§4.5 有 6 个测试用例，每个需要构造合成 ELF（`nm` 输出）和 `time_ns` 数组。构造测试
fixture 的时间**未明确包含**在 2-3h 内。

`load_symbols(elf_path)` 用 `nm -nSC` 解析 ELF——单元测试需要 mock 这个函数，或者
构造一个真实的测试 ELF。如果 mock，需要额外写 mock 代码；如果构造真实 ELF，需要
写汇编 + 编译。**2-3h 可能不够**。

### 6b) D2（修改 etm_reconstruct.py）0.5h — **严重低估**

如 §1c 和 §1e 分析，D2 需要：
1. 修改返回值格式（~8 行 `etm_reconstruct.py` + ~10 行 `test_etm_reconstruct.py`）
2. 增加 `is_call` 字段（`Insn.__slots__` + `Insn.__init__` + `_classify_insn` +
   `load_image` + `reconstruct_region` atom walk + `test_etm_reconstruct.py` 的
   `test_classify_insn` 和 `_mk()`）

第 2 项涉及 `_classify_insn` 的核心逻辑修改：当前 `bl` 和 `b` 都归为 `'direct'`，
需要拆分为 `'direct_call'`（bl/blx imm）和 `'direct_branch'`（b/cbz/cbnz/b<cond>），
或者保持 `'direct'` 但增加 `is_call` 布尔字段。

`_classify_insn` 的修改需要重新解析 `mnem` 字段：当前用 `root = base.split('.')[0]`
提取助记符，`bl` 的 root 是 `'bl'`，`b` 的 root 是 `'b'`，`b.n` 的 root 是 `'b'`，
`blt.n` 的 root 是 `'blt'`。区分 `bl` 和 `b` 需要：`root == 'bl'` → `is_call=True`；
`root == 'b'` 或 `root in _COND` → `is_call=False`。这个逻辑不复杂，但需要修改
`_classify_insn` 的返回值（从 `(kind, target)` 改为 `(kind, target, is_call)`），
进而修改所有调用 `_classify_insn` 的地方（`load_image` L122、`_mk()` in test）。

**实际 D2 = 2-3h**，不是 0.5h。

### 6c) D3（端到端验证）1-2h

需要反汇编 `callstack_test.elf` 确认地址范围、运行 `etm_reconstruct`、运行
`callstack_tracker`、人工检查 B/E 嵌套。如果第一次运行发现 bug（很可能），调试时间
**未包含**。

从 orbetto 三轮调试的历史看，这类工具的首次运行**几乎必然有 bug**。1-2h 只够跑通
管线，不够调试。

### 6d) 总计 5.5-7.5h

修正后的估计：

| 阶段 | D-Lite 估计 | 修正估计 | 理由 |
|------|------------|---------|------|
| D1 | 2-3h | 3-4h | 测试 fixture 构造 + mock `load_symbols` |
| D2 | 0.5h | 2-3h | 返回值格式 + `is_call` 字段 + 测试同步修改 |
| D3 | 1-2h | 2-4h | 首次运行调试 + 中断处理 + region 碎片化排查 |
| D4 | 1h | 1-2h | LVGL trace 更复杂 |
| D5 | 0.5h | 0.5-1h | 对比分析 |
| D6 | 0.5h | 0.5h | 清理 |
| **总计** | **5.5-7.5h** | **9-14.5h** | — |

9-14.5h 仍然比 D 的 30-50h 好得多，但**是 D-Lite 估计的 1.6-2.6 倍**。

---

## 7. 文档自身的一致性 — **否决**

### 7a) §4.2.2 和 §4.2.5 两段矛盾的算法

§4.2.2 的 `on_instruction` 没有 `is_entry` 判断，§4.2.5 的有。两段代码的 Case 2
（RETURN）逻辑不同：

- §4.2.2：`if func_addr in addrs_in_stack: if stack[-1][1] != func_addr: pop`
- §4.2.5：`if is_entry and kind == 'direct': push (recursive call); return`

如果 PC 在函数入口且 `kind='direct'`：
- §4.2.2：`func_addr in addrs_in_stack`（如果函数在栈中）→ `stack[-1][1] != func_addr`
  → 如果是直接递归（栈顶就是该函数），`stack[-1][1] == func_addr`，不 pop，走到 Case 3
  → `stack[-1][1] == func_addr`，不 push → **什么都不做**（递归丢失）
- §4.2.5：`is_entry and kind == 'direct'` → push（递归正确处理）

**两段代码对递归的处理完全不同**。文档应该给出一个统一的最终算法，而不是两段矛盾的
代码。§4.2.5 说"最终方案：增加 is_call 字段"，但 §4.2.2 的代码没有 `is_call`。

### 7b) 测试用例的 kind 字段与最终算法不一致

§4.5 的 `test_recursion` 输入用 `kind='direct'`，但 §4.2.5 说需要 `is_call`（区分
`bl` 和 `b`）。测试用例的 `kind` 字段和最终算法的 `is_call` 字段**不一致**——测试
用例需要更新为包含 `is_call` 字段的格式。

此外，§4.5 的测试用例输入格式是 `(addr, kind, byte_offset, time_ns)` 四元组，但
§4.2.1 的接口签名是 `(addr, kind, byte_offset)` 三元组（`time_ns` 从 `fpga_timebase`
获取）。**输入格式不一致**。

### 7c) 数据流图的输入格式矛盾

§3.1 数据流图写 `bare ETM + .fpga_ns`，但 §1.2 的已有基础表写
`mmcm_stream_orbetto.py` 生成 `.tpiu + .fpga_ns`。

- `.tpiu` 是 TPIU framed（`etm_to_tpiu.reframe` 重新包装的），给 orbetto 用
- bare ETM 是 `mmcm_decode.py` deframe 后的，给 `etm_reconstruct.py` 用

D-Lite 的输入到底是 bare ETM 还是 `.tpiu`？

- `etm_reconstruct.py` 的输入是 bare ETM（从 `dsl_parse` 或 `mmcm_decode` 来）
- `mmcm_stream_orbetto.py` 生成的是 `.tpiu`（给 orbetto 用，不给 `etm_reconstruct` 用）

所以 D-Lite 管线的输入应该是 **bare ETM**（§3.1 正确），但 §1.2 的已有基础表列了
`mmcm_stream_orbetto.py` 生成 `.tpiu`——**这个组件不是 D-Lite 管线的一部分**，列在
已有基础表中会引起混淆。

---

## 8. 实施前必须先确认的前置条件

1. **反汇编 callstack_test.elf**：获取 `nm -nSC` 输出（函数地址范围），确认 §5.1 调用
   树中每个函数的地址。确认 `factorial` 的递归深度 `n`。确认 `callback_test` 和
   `indirect_caller` 的调用类型（间接调用）。

2. **跑 etm_reconstruct.py 统计 region 长度分布**：在 callstack_test trace 上跑
   `reconstruct_all()`，统计 region 长度分布和 `stop_reason` 分布。如果 median region
   长度 < 10 条指令，D-Lite 的调用栈重建不可行——需要先改善 region 连续性。

3. **评估中断影响**：callstack_test trace 有 78 个中断事件。需要确认中断 handler
   （`USART3_IRQHandler` 等）的地址范围，决定是否在 callstack tracker 中过滤中断。
   如果不过滤，D3 阶段的"与 §5.1 调用树对照"会因为调用栈中混入中断 handler 而失败。

4. **统一算法实现**：合并 §4.2.2 和 §4.2.5 为一个最终的 `on_instruction` 实现，
   使用 `is_call` 字段（不是 `kind == 'direct'`）判断递归。更新 §4.5 的测试用例
   以匹配最终算法。

5. **修正 D2 工时估计**：从 0.5h 修正为 2-3h，包含 `is_call` 字段修改和
   `test_etm_reconstruct.py` 同步修改。

---

## 评审维度汇总

| 维度 | 评级 | 关键问题 |
|------|------|---------|
| 1. 第一轮否决理由修复 | **存疑** | 1a 过泛化（proj_add 验证不等于 callstack_test 验证）；1c 改动量低估（未算测试同步修改）；1d 递归修复引入新 bug（is_entry+kind='direct' 在真实 trace 上不工作）；1e D2 工时仍低估 |
| 2. etm_reconstruct.py 健壮性 | **存疑** | Region 碎片化风险未评估；间接调用 region 中断率未知；中断 handler 指令会混入 region |
| 3. callstack_tracker 边界条件 | **存疑** | Region 边界后函数入口时间偏移；is_entry 的 padding/prologue 边界；test_recursion 过于简化 |
| 4. callstack_test ground truth | **否决** | 地址范围未给出；调用类型未标注；递归深度未知；★★★有 78 个活跃中断事件但 D-Lite 声称"中断部分处理"基于错误假设 |
| 5. 与 orbetto 对比公平性 | **存疑** | "最大栈深度=2"描述不准确（是 final depth 不是 max depth）；LVGL trace orphan E 不能假设为 0 |
| 6. 工程量估计 | **存疑** | D2 0.5h→实际 2-3h；总计 5.5-7.5h→实际 9-14.5h |
| 7. 文档一致性 | **否决** | §4.2.2 和 §4.2.5 两段矛盾算法；测试用例 kind/is_call 不一致；输入格式三元组/四元组不一致；数据流图 bare ETM/.tpiu 矛盾 |

---

## 最终结论

### D-Lite 相比原方案 D 是否有实质性改进？

**是**。放弃了编造的 OpenCSD ctypes 绑定，改用已验证的 `etm_reconstruct.py`，消除了
第一轮否决的核心理由。方向正确，架构合理（三段解耦），复用已有代码而非重新发明。

### D-Lite 是否可以直接进入实施？

**不能**。有 3 个阻塞问题：

1. **callstack_test 有活跃中断**（78 个 IRQ 事件），D-Lite 的中断处理策略基于错误假设
   （"region 边界自然清栈"——实际中断在函数中途打断，不在 region 边界）
2. **§4.2.2 和 §4.2.5 两段矛盾算法**，最终实现不明确
3. **D2 工时严重低估**（0.5h → 实际 2-3h），且未包含 `test_etm_reconstruct.py` 同步修改

### 最大的阻塞问题是什么？

**中断处理**。callstack_test trace 中有 78 个中断事件（`USART3_IRQHandler`、
`HardwareSerial::IRQHandler` 等），散布在整个 trace 中。D-Lite §4.3 声称"中断部分处理
（region 边界自然清栈）"，但 `etm_reconstruct.py` 的 `_next_branch()` 实际上把
`exc_entry`/`exc_exit` 当作可容忍的 interleaved packet 跳过——**region 不会在中断处
中断**，中断 handler 的指令会混入当前函数的指令序列。callstack tracker 会把中断
handler 当 call 压栈，导致调用栈与 §5.1 调用树不一致。

### 实施前必须先确认的前置条件是什么？

见 §8 的 5 个前置条件。最关键的是：
1. 反汇编 `callstack_test.elf` 获取地址范围
2. 跑 `etm_reconstruct.py` 统计 region 长度分布
3. 评估中断影响并决定过滤策略
4. 统一 §4.2.2 和 §4.2.5 为一个最终算法
5. 修正 D2 工时估计

**如果这 5 个前置条件满足，D-Lite 可以进入实施**——但实施工时应修正为 9-14.5h
（不是 5.5-7.5h），且 D3 阶段需要预期 1-2 轮调试迭代。

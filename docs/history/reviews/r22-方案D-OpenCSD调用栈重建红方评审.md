# R22 · 方案 D（OpenCSD 调用栈重建）红方对抗评审

> **评审对象**：`proposals/24-方案D-OpenCSD调用栈重建设计.md`
> **评审立场**：红方（苛刻挑刺，不总结优点）
> **评审日期**：2026-06-28
> **评审方法**：逐行比对 OpenCSD 1.4.1 头文件、现有代码库、doc 14 ground-truth 数据

---

## 总体结论：**否决（需重大修改后重审）**

方案 D 的核心思路（OpenCSD 解码 + 自研 callstack tracker + FPGA 墙钟）方向正确，
但**设计文档存在严重的"纸面工程"问题**：§6 的 C API 代码示例中** 5 个 API 调用有 4 个
不存在**，ctypes 结构体定义与实际头文件**字段顺序和类型均不匹配**。这不是"细节待完善"，
而是**文档作者没有查阅过 `/usr/include/opencsd/` 下的头文件**就写了 API 集成方案。

此外，callstack tracker 的启发式算法对尾调用、中断、RTOS 线程切换等场景**全部会误判**，
且文档未给出任何缓解措施。工程量估计（9.5h）**不包含调试时间**，而 orbetto 三轮调试
的历史表明这类工具的实际调试时间远超编码时间。

**最大风险**：D4（ctypes 绑定）3h 的估计基于一个不存在的 API surface，实际可能需要
1-2 天来逆向 OpenCSD 的 C API 细节（回调签名、内存生命周期、错误传播、decoder 创建
流程），且 ctypes segfault 调试极其耗时。

**推荐替代路径**：见 §8——不引入 OpenCSD ctypes 绑定，直接升级 `etm_reconstruct.py`
（已有 327 行逐指令重建，已验证 ground-truth 正确）+ `etm_to_perfetto.py`（已有 180 行
I-sync 级 Perfetto），把 callstack tracker 作为独立模块插入两者之间。

---

## 1. 技术可行性 — **否决**

### 1.1 ★★★ 文档 §6.1 的 C API 调用大部分是编造的

文档 §6.1 列出的 7 个关键 API 调用，经逐行核查 `/usr/include/opencsd/c_api/opencsd_c_api.h`
和 `/usr/include/opencsd/ocsd_if_types.h`，**只有 2 个存在且正确，5 个不存在或名字错误**：

| 文档 §6.1 写的 | 实际 API（头文件核实） | 状态 |
|---|---|---|
| `ocsd_create_dcd_tree(OCSD_TRC_SRC_FRAME_FORMATTED, OCSD_OP_FLG_PACKET_DECODER)` | `ocsd_create_dcd_tree(ocsd_dcd_tree_src_t, uint32_t deformatterCfgFlags)` | ⚠️ 函数存在，但第二个参数是 **deformatter 配置标志**（`OCSD_DFRMTR_HAS_FSYNCS` 等），不是 `OCSD_OP_FLG_PACKET_DECODER`——**这个常量不存在**。实际创建标志是 `OCSD_CREATE_FLG_FULL_DECODER` (0x02)，且它用于 `ocsd_dt_create_decoder`，不用于 `ocsd_create_dcd_tree` |
| `ocsd_configure_tp(h, &tp_cfg)` | **不存在**。TPIU 配置通过 `ocsd_create_dcd_tree` 的 `deformatterCfgFlags` 参数 + `ocsd_dt_create_decoder` 完成 | ❌ **编造** |
| `ocsd_dt_set_config(h, &etm_cfg)` | **不存在**。ETMv3 配置通过 `ocsd_dt_create_decoder(handle, "ETMV3", create_flags, &etm_cfg, &csid)` 传入 | ❌ **编造** |
| `ocsd_dt_add_callback_memacc(h, 0x08000000, 0x0807FFFF, OCSD_MEM_TYPE_RAM, elf_data)` | `ocsd_dt_add_buffer_mem_acc(h, addr, mem_space, buf, len)` 或 `ocsd_dt_add_binfile_mem_acc(h, addr, mem_space, filepath)` 或 `ocsd_dt_add_callback_mem_acc(h, st, en, mem_space, Fn_MemAcc_CB, ctx)` | ⚠️ 函数名近似但参数完全不同：实际有 3 个不同函数，参数是 `ocsd_mem_space_acc_t`（不是 `OCSD_MEM_TYPE_RAM`），且 buffer 模式需要 `(buf, len)` 不是 `(end_addr, data)` |
| `ocsd_dt_add_callback_trcblob_cb(h, &instr_range_cb)` | `ocsd_dt_set_gen_elem_outfn(h, FnTraceElemIn pFn, const void *p_context)` | ❌ **编造**。实际函数名、参数签名完全不同 |
| `ocsd_dt_process_data(h, 0, data_len, data, &bytes_consumed)` | `ocsd_dt_process_data(h, op, index, dataBlockSize, pDataBlock, numBytesProcessed)` | ⚠️ 函数存在，但缺少 `op` 参数（`ocsd_datapath_op_t`，必须传 `OCSD_OP_DATA`），且 `index` 是 `ocsd_trc_index_t`（uint64/uint32 取决于平台）不是 `0` |
| `instr_range_cb(const ocsd_trc_gen_elem_t *elem)` | `FnTraceElemIn(const void *p_context, ocsd_trc_index_t index_sop, uint8_t trc_chan_id, const ocsd_generic_trace_elem *elem)` 返回 `ocsd_datapath_resp_t` | ❌ 回调签名完全不同：实际有 4 个参数（含 context、trace index、channel ID），返回 `ocsd_datapath_resp_t`（不是 void），且结构体名是 `ocsd_generic_trace_elem` 不是 `ocsd_trc_gen_elem_t` |

**结论**：文档 §6.1 的代码示例**不是"伪代码"级别的简化，而是基于错误猜测的编造**。
D4（ctypes 绑定）3h 的估计建立在这些不存在的 API 上，实际需要从零研究 OpenCSD 的
decoder 创建流程（`ocsd_create_dcd_tree` → `ocsd_dt_create_decoder` →
`ocsd_dt_add_binfile_mem_acc` → `ocsd_dt_set_gen_elem_outfn` → `ocsd_dt_process_data`），
每一步都有 ABI 细节（配置结构体、内存空间枚举、回调返回值语义）。

### 1.2 ★★★ 文档 §6.2 的 ctypes 结构体定义与实际头文件不匹配

文档 §6.2 定义的 `ocsd_trc_gen_elem_t` 结构体：

```python
# 文档写的（错误）
_fields_ = [
    ("elem_type", c_uint32), ("sub_type", c_uint32),     # ← sub_type 不存在
    ("st_addr", c_uint64), ("en_addr", c_uint64),         # ← 顺序错
    ("timestamp", c_uint64), ("cpu_freq", c_uint32),      # ← cpu_freq 不存在
    ("trace_on", c_uint8), ("isa", c_uint8),              # ← isa 位置错
    ("context", c_uint32), ("last_i_addr", c_uint64),     # ← last_i_addr 不存在
]
```

实际 `ocsd_generic_trace_elem`（`trc_gen_elem_types.h:113`）字段顺序：

```c
// 实际头文件（正确）
typedef struct _ocsd_generic_trace_elem {
    ocsd_gen_trc_elem_t  elem_type;       // enum (uint32)
    ocsd_isa             isa;             // enum (uint32) ← 在 st_addr 之前
    ocsd_vaddr_t         st_addr;         // uint32 (32-bit platform) ← 不是 uint64
    ocsd_vaddr_t         en_addr;         // uint32
    ocsd_pe_context      context;         // struct { enum, enum, uint32, uint32, bitfield }
    uint64_t             timestamp;
    uint32_t             cycle_count;     // ← 文档写成 cpu_freq
    ocsd_instr_type      last_i_type;     // enum ← 文档写成 last_i_addr
    ocsd_instr_subtype   last_i_subtype;  // enum
    union { struct { ...bitfields... }; uint32_t flag_bits; };
    union { uint32_t exception_number; ... };  // 多种 payload
    const void *ptr_extended_data;
} ocsd_generic_trace_elem;
```

**关键差异**：
1. `ocsd_vaddr_t` 在 32 位平台是 `uint32_t`（`ocsd_if_types.h:306`），文档写成 `c_uint64`——**偏移量全错**
2. `isa` 在 `st_addr` **之前**，文档放在 `trace_on` 旁边——**偏移量错**
3. `context` 是一个**嵌套结构体** `ocsd_pe_context`（含 2 个 enum + 2 个 uint32 + bitfield），文档简化成 `c_uint32`——**大小计算错**
4. 文档的 `sub_type`、`cpu_freq`、`trace_on`、`last_i_addr` 字段**均不存在**
5. 实际有 `flag_bits` union 和 `payload` union 和 `ptr_extended_data` 指针，文档全部遗漏

ctypes 结构体定义错一个偏移量就是 segfault 或读到垃圾数据。**这个结构体定义不可能工作**。

### 1.3 ★★ TPIU deframing 自相矛盾

文档 §3.1 数据流图写 `.tpiu → TPIU deframe → OpenCSD`，暗示 OpenCSD 做 TPIU deframing。
但：

- `mmcm_decode.py` **已经做了 TPIU deframing**（使用 `etm35lib.tpiu_deframe_hsync` / `tpiu_deframe_walk`），输出的是 **bare ETM bytes**
- `make_opencsd_snapshot.py` 默认 `format=source_data`（bare ETM），只有加 `--coresight` 才是 TPIU framed
- 文档 §4.1.1 接口签名写 `tpiu_bytes: bytes  # TPIU framed ETM data (.tpiu file)`，但 `.tpiu` 文件实际是 bare ETM（`mmcm_stream_orbetto.py` 用 `etm_to_tpiu.reframe` 把 bare ETM **重新包装**成 TPIU 才给 orbetto 用）

如果喂 bare ETM 给 `OCSD_TRC_SRC_FRAME_FORMATTED`（TPIU framed 模式），OpenCSD 会尝试做
TPIU deframing，对已经是 bare ETM 的数据做二次 deframe → 解码失败。如果喂 TPIU framed
数据，需要跳过已有的 `mmcm_decode.py` deframe 步骤。**文档没有澄清这个矛盾**，§4.1.1
的接口签名和 §3.1 的数据流图互相矛盾。

### 1.4 ★★ "210 条指令"是 max 不是 mean，且来自干净 LA 捕获

文档 §7 风险表写"per-anchor 解码可达 210 条指令"，暗示这是典型值。但 doc 14 §16.3
的实际数据是：

| 指标 | 值 |
|---|---|
| mean | **45** 条指令 |
| median | **24** 条指令 |
| max | 210 条指令 |

且这是在**逻辑分析仪捕获**（干净流，0.0018% unknown）上的结果。FPGA 2-bit DDR 采集
的噪声更严重（doc 15 记录 8% 误码率问题，后经修复但仍非零）。OpenCSD 是
correct-or-abort——遇到一个坏字节就中断当前 region。在非零噪声率下，mean 45 条指令的
region 里可能有坏字节，大量 region 会在中途 abort。

**文档没有给出 FPGA 采集流上的 OpenCSD 解码统计**，只有 LA 干净流的数据。风险表把
"中概率"标得太乐观。

### 1.5 ★ OpenCSD 回调的线程模型和内存生命周期未讨论

`FnTraceElemIn` 回调在 `ocsd_dt_process_data` 调用栈内**同步触发**（头文件无线程模型
文档，但 OpenCSD 是单线程同步库）。这意味着：

- 回调中不能调用 `ocsd_dt_process_data`（重入）
- 回调中收到的 `elem` 指针在回调返回后**失效**——必须在此期间拷贝数据
- 回调返回 `OCSD_RESP_WAIT` 会暂停解码，需要后续 `OCSD_OP_FLUSH` 恢复

文档 §6 完全没有讨论这些 ABI 细节。ctypes 中如果回调函数被 GC 回收（Python 侧的
`CFUNCTYPE` 对象未保持引用），会直接 segfault。

### 1.6 ★ 错误传播模型未讨论

`ocsd_dt_process_data` 返回 `ocsd_datapath_resp_t`，有 CONT/WARN/ERR/WAIT/FATAL 五级。
文档 §6 没有讨论：

- 收到 `OCSD_RESP_ERR_CONT` 时是否继续喂数据？
- 收到 `OCSD_RESP_FATAL_INVALID_DATA` 时如何恢复？（需要 reset decoder）
- `OCSD_GEN_TRC_ELEM_NO_SYNC` 元素（解码失步）如何处理？

这些直接影响"噪声流"场景下的解码连续性。

---

## 2. 调用栈算法正确性 — **否决**

### 2.1 ★★★ 尾调用（tail call）会完全破坏栈跟踪

文档 §4.2.2 的算法：`函数切换 = call；回到祖先 = return`。

**尾调用场景**：A 调用 B，B 尾调用 C（`bx C` 而非 `bl C`），C 返回时直接回到 A 的调用者。

```
实际执行：  A → B → C → (return to A's caller)
tracker 看到：A → B → C → A's caller
```

tracker 的栈是 `[A, B, C]`。C 返回到 A 的调用者（比如 `main`），tracker 看到 PC 跳到
`main`，`main` 在栈底，于是 pop C、pop B、pop A——**看起来正确**。但 B 的 E 事件时间
是错的（B 实际在尾调用 C 时就结束了，不是在 C 返回时结束），且如果 A 的调用者不在栈中
（因为 A 本身就是被尾调用的），tracker 会把 `main` 当新 call 压栈，栈深度错误。

Cortex-M4 的 GCC `-O2` 会频繁产生尾调用（`b` 代替 `bl` + `bx lr`）。LVGL 这种复杂代码
几乎必然有尾调用。**文档没有提及尾调用**。

### 2.2 ★★★ 中断处理会污染调用栈

**中断场景**：函数 A 执行中被 IRQ 打断，IRQ handler 执行完返回 A。

```
实际执行：  A → IRQ_Handler → A (继续)
tracker 看到：A → IRQ_Handler → A
```

tracker 的栈是 `[A, IRQ_Handler]`。IRQ 返回时 PC 跳回 A，tracker 看到 A 在栈中
（`[A, IRQ_Handler]`，A 在底部），于是 pop IRQ_Handler（正确），继续在 A 中。

**看起来正确？不。** 问题在于：

1. ETM trace 中 IRQ entry/exit 有 exception packet（`OCSD_GEN_TRC_ELEM_EXCEPTION` 和
   `OCSD_GEN_TRC_ELEM_EXCEPTION_RET`），但文档 §4.2.2 的算法**完全忽略这些元素**，
   只看 `INSTR_RANGE` 的地址。如果 IRQ handler 的地址恰好不在 ELF 符号表中（比如
   `0x08000000` 向量表区域），tracker 会把它当噪声跳过——但 IRQ handler 的指令仍然
   会被执行，只是不产生 B/E 事件，导致 A 的 B 事件持续时间包含了 IRQ handler 的执行
   时间（**时间轴错误**）。

2. 如果 IRQ handler **在符号表中**（比如 `HardFault_Handler`），tracker 会把它当 call
   压栈。IRQ 返回时 PC 跳回 A，tracker pop IRQ_Handler——**这次正确**。但如果 IRQ
   handler 本身调用了其他函数（如 `HAL_GPIO_EXTI_Callback`），且 IRQ 嵌套（高优先级
   IRQ 打断低优先级 IRQ），tracker 的栈会正确嵌套——**但前提是 ETM trace 完整记录了
   所有 exception entry/exit**。F429 的 ETM 在 1024 字节 I-sync 周期之间，如果 IRQ
   entry/exit 的 branch address packet 丢失（噪声），tracker 会完全失控。

3. Cortex-M4 的 exception return 通过 `BX LR`（LR=0xFFFFFFF9 等 EXC_RETURN 值），
   这是间接跳转。ETM 会发 branch address packet，但如果 packet 丢失，tracker 看到的
   PC 会从 IRQ handler 跳到一个看似随机的地址（实际是 A 中断时的位置），tracker 可能
   误判为 call 或 return。

**文档完全没有讨论中断处理**。对于嵌入式系统，这是致命遗漏。

### 2.3 ★★★ RTOS 线程切换会完全摧毁调用栈

如果固件运行 RTOS（FreeRTOS/Zephyr/ThreadX），线程切换时：

```
实际执行：  Thread1::funcA → Scheduler → Thread2::funcB
tracker 看到：funcA → funcB (PC 突然跳到完全不同的函数)
```

tracker 的栈是 `[funcA]`。PC 跳到 funcB，funcB 不在栈中，tracker 当 call 压栈：
`[funcA, funcB]`。但 funcA 和 funcB 属于不同线程，栈深度无意义。

更糟的是，Thread2 执行完后切回 Thread1，PC 跳回 funcA，tracker pop funcB——**看起来
像 funcB 返回到 funcA**，但实际是线程切换。

Mortrall 有 `pending_thread_switch` 和 `sched_note_resume` 的特殊处理（`mortrall.hpp:1046`），
说明 orbetto 已经遇到过这个问题。**方案 D 文档完全没有提及 RTOS 线程切换**。

### 2.4 ★★ 递归 vs 循环回到自身的区分不正确

文档 §4.2.3 说"同函数在栈中出现多次 → push 新帧"。但算法 §4.2.2 的代码是：

```python
if func.addr in [f.addr for f in self.stack]:
    # RETURN: pop until we're back at this function
    while self.stack and self.stack[-1].addr != func.addr:
        popped = self.stack.pop()
        emit_event("E", ...)
```

**问题**：如果当前栈是 `[A, B, A]`（A 递归调用 B，B 递归调用 A），新 PC 在 A 中，
`func.addr in [f.addr for f in stack]` 为 True，算法会 pop 直到 `stack[-1].addr == A`。
但栈中有两个 A，`while stack[-1].addr != func.addr` 会立即停止（因为栈顶就是 A），
**不会 pop 任何东西**。然后呢？算法没有 push 新帧（因为走的是 return 分支），也没有
继续执行（因为 return 分支结束后没有后续逻辑）。

**实际结果**：递归调用 A→B→A 时，tracker 的栈停留在 `[A, B, A]`，第三个 A 的 B 事件
**永远不会发出**。文档 §4.3 的 `test_recursion` 测试用例的输入是：

```python
(0x08002000, 1000),  # factorial entry
(0x08002000, 1100),  # factorial again (recursive call)
```

两个指令地址相同（都是 `0x08002000`），算法 §4.2.2 的第一个检查是
`if self.stack and self.stack[-1].name == func.name: return`——**直接返回，不 push**。
所以 `test_recursion` 的断言 `b_count == 3` **不可能通过**，因为第二个和第三个
`0x08002000` 都会被"same function"检查拦截。

**文档的单元测试与算法实现自相矛盾**。

### 2.5 ★★ 地址范围匹配的 bisect_right 边界条件

文档 §4.2.3 说用 `bisect_right` 做地址范围匹配。`etm_to_perfetto.py` 已有实现：

```python
def func_for_pc(pc, starts, funcs):
    k = bisect_right(starts, pc) - 1
    if 0 <= k < len(funcs):
        s, e, name = funcs[k]
        if s <= pc < e:
            return name
    return f"0x{pc & ~1:08x}"
```

**边界问题**：如果函数 A 的 end == 函数 B 的 start（`A: [0x1000, 0x2000)`,
`B: [0x2000, 0x3000)`），PC=0x2000 时 `bisect_right(starts, 0x2000)` 返回指向 B 的
索引，`k-1` 指向 A，`A.start <= 0x2000 < A.end` → `0x1000 <= 0x2000 < 0x2000` → False。
然后检查 B：`B.start <= 0x2000 < B.end` → True。**这个边界是正确的**。

但 `nm -nSC` 的输出中，**函数 size 可能为 0**（汇编符号、weak 符号），`etm_to_perfetto.py`
已经过滤了 `size == 0` 的情况。如果两个函数地址完全相同（`A: [0x1000, 0x2000)`,
`B: [0x1000, 0x1000)` size=0），`bisect_right` 会把 B 排在 A 前面（相同 key 的稳定排序），
`k-1` 指向 B，`B.start <= pc < B.end` → `0x1000 <= 0x1000 < 0x1000` → False，然后
检查 A → True。**这个边界也是正确的**。

**但文档没有讨论这个边界条件**，只是说"地址范围二分查找"。

### 2.6 ★★ 噪声地址落在真实函数范围内时不会被过滤

文档 §4.2.3 说"不在 ELF 符号范围内的地址 → 跳过"。但如果噪声地址恰好落在某个真实
函数的地址范围内（比如 `0x08003ffe` 落在 `0x08003000-0x08004000` 的函数里），过滤器
**不会过滤它**。

后果：tracker 会看到 PC 从当前函数跳到 `0x08003ffe`（同一个函数内），触发"same function"
检查——如果 `0x08003ffe` 和当前 PC 在同一函数内，`return`（无操作）。**这次碰巧正确**。

但如果噪声地址落在**另一个函数**的范围内（比如 `0x08003ffe` 落在函数 B 中，而当前
在函数 A），tracker 会误判为 call/return，产生**虚假的 B/E 事件**。

文档 §4.3 的 `test_noise_address_filtered` 测试用例只测了噪声地址**不在**任何函数内
的情况（`0x08003ffe` 不在 `test.elf` 的符号范围内），没有测噪声地址**在**某个函数内
的情况。**这是一个被回避的难题**。

### 2.7 ★ 内联函数没有 call/return，但符号表里有函数边界

编译器内联后，被内联函数的代码物理上嵌入调用者，没有 call/return 指令。但 `nm -nSC`
仍然会列出被内联函数的符号（如果它也在别处被非内联调用）。如果 ETM trace 中的 PC
连续穿过内联函数的地址范围，tracker 会看到"函数切换"（从调用者地址范围进入内联函数
地址范围），误判为 call——但实际没有 call 指令执行。

**文档没有讨论内联函数**。

---

## 3. 时间戳精度 — **存疑**

### 3.1 ★★ I-sync 间隔 1024 字节，函数持续时间可能无意义

doc 14 §9.2 实测：F429 的 ETMSYNCFR 硬件锁死 1024 字节，不可调。1024 字节 ETM 流
在 branch broadcast ON 时约对应 ~200 条指令（doc 14 §16.3：mean 45 条指令/anchor，
但那是 LA 干净流；branch broadcast ON 后代码区占比 18.8%，1024 字节中约 192 字节是
代码区，约 100-200 条指令）。

文档 §4.1.3 说"I-sync 之间的指令按均匀分布插值"。但如果两个 I-sync 之间执行了
100-200 条指令，**所有指令的时间戳都取锚点时间**（因为 F429 cycleCount=0，没有
per-instruction 时间），函数持续时间精度 = I-sync 间隔 ≈ 1024 字节 / ETM 码率。

在 84MHz TRACECLK、2-bit DDR 下，1024 字节 ≈ 1024 × 8 / 2 = 4096 个 TRACECLK 周期
≈ 48.8 μs。如果函数执行时间 < 48.8 μs（很多 LVGL 函数都在这个量级），**函数持续时间
可能为 0 或等于 I-sync 间隔**，失去意义。

文档 §7 风险表把"时间戳精度不够"标为"低概率"——**过于乐观**。

### 3.2 ★★ OpenCSD INSTR_RANGE 回调的 index_sop 是字节偏移，但文档没说对

文档 §4.1.3 假设"OpenCSD 的 INSTR_RANGE 回调按 ETM 字节偏移顺序触发"。

实际：`FnTraceElemIn` 回调的第二个参数 `index_sop`（`ocsd_trc_index_t`）是**trace
source 的字节索引**，即 `ocsd_dt_process_data` 调用时传入的 `index` 参数。如果每次
调用 `ocsd_dt_process_data` 时正确传入累积字节偏移，回调中的 `index_sop` 就是该元素
对应的 trace 字节偏移。

**但文档 §6.1 的代码示例写的是 `ocsd_dt_process_data(h, 0, data_len, data, &bytes_consumed)`——
index 恒为 0**。如果每次都传 0，所有回调的 `index_sop` 都是 0，**无法关联 FPGA 时基**。

正确做法是维护一个累积 offset：

```python
offset = 0
while offset < len(data):
    consumed = c_uint32(0)
    resp = lib.ocsd_dt_process_data(
        handle, OCSD_OP_DATA, offset,
        len(data) - offset, data[offset:], byref(consumed))
    offset += consumed.value
```

文档没有给出这个关键细节。

### 3.3 ★ 回调顺序与输入字节顺序的一致性

OpenCSD 的回调是**同步**的——在 `ocsd_dt_process_data` 调用栈内触发，按 trace 字节
顺序输出元素。但 `OCSD_GEN_TRC_ELEM_NO_SYNC`（失步）和 `OCSD_GEN_TRC_ELEM_TRACE_ON`
（trace 重启）元素会打断指令序列，文档没有讨论这些元素对时间戳关联的影响。

---

## 4. 工程量估计 — **否决**

### 4.1 ★★★ D1（callstack_tracker + 单元测试）2h — 测试覆盖严重不足

文档 §4.3 的 4 个单元测试只覆盖了：
- 简单 call/return
- 递归（且测试与算法矛盾，见 §2.4）
- 噪声地址过滤（只测不在函数内的情况，见 §2.6）
- 深调用链

**未覆盖的关键场景**：
- 尾调用（§2.1）
- 中断处理（§2.2）
- RTOS 线程切换（§2.3）
- 噪声地址落在函数内（§2.6）
- 内联函数（§2.7）
- 间接返回（`pop {pc}`）目标丢失
- I-sync 间大间隔导致函数切换不可见

"合成数据怎么构造中断、尾调用、递归"——文档没有回答。构造中断测试数据需要模拟
ETM exception packet，构造尾调用测试数据需要知道编译器是否生成了尾调用——**这些
都需要反汇编真实固件来验证**，不是 2h 能完成的。

### 4.2 ★★★ D4（ctypes 绑定）3h — 严重低估

D4 的 3h 估计基于 §6.1-6.2 的 API 代码，而这些代码**大部分是编造的**（见 §1.1-1.2）。
实际需要：

1. **研究 OpenCSD C API**（2-4h）：阅读 `opencsd_c_api.h`、`ocsd_c_api_types.h`、
   `ocsd_if_types.h`、`trc_gen_elem_types.h`，理解 decoder 创建流程、回调签名、
   内存管理、错误传播
2. **定义 ctypes 结构体**（1-2h）：`ocsd_generic_trace_elem`（含嵌套 `ocsd_pe_context`
   和两个 union）、`ocsd_etmv3_cfg`、`ocsd_mem_space_acc_t` 等
3. **实现 decoder 创建和配置**（1-2h）：`ocsd_create_dcd_tree` →
   `ocsd_dt_create_decoder`（需要正确的 decoder name "ETMV3"）→
   `ocsd_dt_add_binfile_mem_acc` → `ocsd_dt_set_gen_elem_outfn`
4. **实现数据喂入循环**（1h）：维护累积 offset，处理 `OCSD_RESP_WAIT`/FATAL
5. **调试 ctypes segfault**（2-8h）：结构体偏移量错误、回调签名错误、GC 回收回调对象、
   string 编码问题……

**保守估计 D4 = 8-16h**，不是 3h。

### 4.3 ★★ 整体 9.5h 不包含调试时间

orbetto/Mortrall 三轮调试未收敛（61 orphan E、2 unclosed），说明这类工具的调试时间
远超编码时间。方案 D 的 9.5h 是**纯编码估计**，不包含：

- OpenCSD 解码结果与 `etm_reconstruct.py` 对照验证的时间
- callstack tracker 在真实 LVGL trace 上的调试时间
- ctypes segfault 调试时间
- 时间戳对齐调试时间

**实际估计应 ×3-5 = 30-50h**。

---

## 5. 替代方案是否被充分排除 — **否决**

### 5.1 ★★★ 为什么不直接升级 etm_reconstruct.py + etm_to_perfetto.py？

`etm_reconstruct.py`（327 行）**已经实现了逐指令级 PC 重建**：
- `reconstruct_region()`：从 I-sync 锚点出发，walk P-header atoms + branch address packets，
  逐指令推进 PC
- `reconstruct_all()`：对所有 I-sync 锚点重建
- doc 14 §6 验证：proj_add 的 `loop_sum → add` 循环被逐指令重建，ground-truth 对上

`etm_to_perfetto.py`（180 行）**已经实现了 I-sync 级 Perfetto 输出**：
- `build_stack_events()`：callstack 启发式（与方案 D §4.2.2 算法**完全相同**）
- `func_for_pc()`：地址范围匹配（与方案 D §4.2.3 **完全相同**）
- 已有 `test_etm_to_perfetto.py` 测试

**方案 D 的 callstack tracker 算法与 `etm_to_perfetto.py` 的 `build_stack_events()`
逻辑完全相同**，唯一区别是输入从 I-sync anchor 级升级为逐指令级。

**最小改动路径**：
1. 把 `etm_reconstruct.py` 的 `reconstruct_all()` 输出（逐指令地址列表）作为
   `etm_to_perfetto.py` 的 `build_stack_events()` 输入
2. 把 `build_stack_events()` 的输入从 `(time_ns, function_name)` 改为
   `(addr, time_ns)`，内部做 `func_for_pc()` 映射
3. 时间戳用 `fpga_timebase.py`（已有）

这只需要修改 ~50 行代码，不需要 OpenCSD ctypes 绑定，不需要 9.5h。

**文档 §8 说"etm_reconstruct.py 是 fallback"但没有解释为什么不直接升级它**。
方案 D 引入 OpenCSD ctypes 绑定是**过度工程**——`etm_reconstruct.py` 已经能做逐指令
重建，且已验证 ground-truth 正确。

### 5.2 ★★ 为什么不用 orbuculum 的 traceDecoder_etm35.c？

`orbuculum/Src/traceDecoder_etm35.c`（894 行）是**完整的 ETMv3.5 解码器**，已编译为
`liborb.so`（`orbuculum/build/liborb.so.2.2.0`）。它是 C 代码，可以直接 ctypes 调用，
不需要 OpenCSD 依赖。

文档没有提到这个替代方案。orbuculum 的解码器已经在这个项目中使用（orbetto 就是基于
它的），且 `etm35lib.py` 已经用 Python 重新实现了它的包解析逻辑。

**但**：orbuculum 的解码器不做指令路径重建（不 walk code image），只输出 ETM 包。
`etm_reconstruct.py` 才是做指令路径重建的。所以这个替代方案实际上是"用 orbuculum
做包解析 + etm_reconstruct.py 做指令重建"——**这正是现有方案**。

### 5.3 ★★ orbetto/Mortrall 修复是否真的走投无路？

文档说 Mortrall 的问题是 C++ mangled 名导致 strcmp 栈折叠误判。文档自己也承认
（§4.2.4）方案 D 的修复就是"用地址匹配代替名字匹配"。

**那为什么不直接修 Mortrall？** Mortrall 的 `_catchInconsistencies()`
（`mortrall.hpp:1008`）用 `strcmp(current_func->funcname, new_func->funcname)` 做栈
折叠——把它改成 `current_func->addr == new_func->addr` 就行了。

文档 §2.3 说"Mortrall 的设计思路值得记录"但没说为什么不直接修。可能的原因：
- Mortrall 是 C++ 代码，编译/调试不如 Python 方便
- Mortrall 依赖 ETM cycleCount 做时间轴（F429 上为 0）
- Mortrall 与 orbuculum 解码器紧耦合

但这些都是**可修复的工程问题**，不是"走投无路"。文档没有给出 Mortrall 不可修复的
技术理由。

---

## 6. 删除 embedded-debug-tools 的风险 — **存疑**

### 6.1 ★★ 删了 orbetto 后没有 ground truth

文档 §2.2 说"验证通过后再删"。但 orbetto/Mortrall 是唯一能做对照的参考实现——
即使它有 bug（61 orphan E），它的输出仍然可以用来**对比**方案 D 的输出差异。

如果删了 orbetto 后发现方案 D 也有 bug（比如尾调用误判），拿什么做 ground truth？
`etm_reconstruct.py` 可以做逐指令级对照，但它不做 callstack 重建——无法验证
callstack tracker 的正确性。

**建议**：保留 orbetto 直到方案 D 在 LVGL trace 上稳定运行至少 3 次无 orphan E。

### 6.2 ★ embedded-debug-tools 里的其他依赖

`mmcm_stream_orbetto.py` 生成 orbetto 输入文件（`.tpiu` + `.fpga_ns`），`orbetto_perf_to_json.py`
转换 orbetto 输出为 JSON，`diag_orbetto_stack.py` 诊断 orbetto 栈——这些脚本都依赖
orbetto 二进制。如果删除 `embedded-debug-tools/`，这些脚本也需要删除或重写。

文档没有列出这些依赖脚本。

---

## 7. 文档遗漏 — **否决**

### 7.1 ★★★ 没有给出 OpenCSD INSTR_RANGE 回调的实际输出格式

文档 §4.1.3 假设回调提供 `(st_addr, en_addr, trace_byte_offset)`，但没有给出实际
`ocsd_generic_trace_elem` 结构体在 `elem_type == OCSD_GEN_TRC_ELEM_INSTR_RANGE` 时的
字段含义：

- `st_addr`：指令范围起始地址
- `en_addr`：指令范围结束地址（**exclusive**）
- `last_i_type`：范围内最后一条指令的类型（`OCSD_INSTR_BR` / `OCSD_INSTR_BR_INDIRECT` 等）
- `last_instr_exec`：最后一条指令是否被执行
- `last_instr_sz`：最后一条指令的大小（2/4 字节）
- `num_instr_range`：范围内的指令数（union payload 字段）
- `has_cc`：是否有 cycle count（F429 上为 0）

**关键遗漏**：`last_i_type` 可以区分直接分支/间接分支/其他，这对 callstack tracker
判断 call/return 至关重要。文档 §4.2.2 的算法完全基于地址切换，**忽略了 OpenCSD 已经
提供的指令类型信息**——这是浪费。

### 7.2 ★★ TPIU deframing 位置矛盾（见 §1.3）

文档同时提到两种输入格式：
- §3.1 数据流图：`.tpiu → TPIU deframe → OpenCSD`（OpenCSD 做 deframing）
- §4.1.1 接口签名：`tpiu_bytes: bytes  # TPIU framed ETM data`（输入是 TPIU framed）
- §1.2 已有基础：`mmcm_decode.py` 做 TPIU deframe（输出 bare ETM）

**没有澄清 OpenCSD 的输入应该是 bare ETM 还是 TPIU framed**。

实际答案：如果用 `OCSD_TRC_SRC_SINGLE`（单源），输入是 bare ETM，不需要 TPIU deframing。
如果用 `OCSD_TRC_SRC_FRAME_FORMATTED`，输入是 TPIU framed，OpenCSD 内部做 deframing。
文档应该选一个并说清楚。

### 7.3 ★ 没有考虑多核场景

虽然 F429 是单核，但 OpenCSD API 设计为多核（`ocsd_dt_create_decoder` 需要 trace ID，
`FnTraceElemIn` 回调有 `trc_chan_id` 参数）。文档没有说明单核使用时的简化方式。

### 7.4 ★★ 没有给出 callstack_test 固件的已知调用流

文档 §5.1 D3 说"callstack_test 对照"，但没有给出 callstack_test 固件的已知调用流
（ground truth）。doc 14 有 `proj_add` 的 ground truth（`loop_sum → add` 5x），但
`callstack_test` 是文档新提出的固件，没有反汇编、没有已知调用流。

**没有 ground truth 的验证是空话**。

### 7.5 ★ 没有讨论 OCSD_GEN_TRC_ELEM_EXCEPTION 的处理

OpenCSD 对中断/异常会输出 `OCSD_GEN_TRC_ELEM_EXCEPTION` 和
`OCSD_GEN_TRC_ELEM_EXCEPTION_RET` 元素。文档 §4.2.2 的算法只处理地址切换，
**完全忽略 exception 元素**。这会导致中断处理被误判为普通 call/return（见 §2.2）。

### 7.6 ★ 没有讨论 OCSD_GEN_TRC_ELEM_NO_SYNC 的处理

OpenCSD 遇到坏包或溢出时会输出 `OCSD_GEN_TRC_ELEM_NO_SYNC`（失步），之后需要等
下一个 I-sync 重新同步。文档没有讨论 tracker 在失步期间的行为——应该清空栈？
保留栈？标记为"不确定"？

---

## 8. 替代方案推荐

### 8.1 推荐：升级 etm_reconstruct.py + etm_to_perfetto.py（方案 D-Lite）

**不引入 OpenCSD ctypes 绑定**，直接利用已有的、已验证的代码：

```
etm_reconstruct.py (逐指令重建, 已验证 ground-truth)
       ↓ (addr, byte_offset) 列表
callstack_tracker.py (新, ~200行, 纯 Python)
       ↓ (B/E events)
etm_to_perfetto.py (Perfetto JSON 输出, 已有)
```

**改动量**：
1. 新建 `callstack_tracker.py`（~200 行）：输入 `(addr, byte_offset)` 列表，
   输出 B/E 事件。算法与文档 §4.2.2 相同，但增加 `last_i_type` 判断（从
   `etm_reconstruct.py` 的 `Insn.kind` 获取，区分 direct/indirect/other）
2. 修改 `etm_to_perfetto.py`（~30 行）：把输入从 I-sync anchor 级改为逐指令级
3. 时间戳：用 `fpga_timebase.py` 的 byte_offset → ns 映射

**优点**：
- 不需要 ctypes 绑定（避免 §1.1-1.2 的所有问题）
- `etm_reconstruct.py` 已验证 ground-truth 正确（doc 14 §6）
- 工程量 ~4-6h（不是 9.5h）
- 可测试性相同（callstack_tracker 仍是纯 Python）

**缺点**：
- `etm_reconstruct.py` 的指令重建不如 OpenCSD 健壮（自研 vs ARM 官方）
- 但 doc 14 §16.3 已验证 `etm_reconstruct.py` 在干净流上正确

### 8.2 如果坚持用 OpenCSD：先用 trc_pkt_lister CLI（方案 B），不做 ctypes

文档 §4.1.2 方案 B（trc_pkt_lister CLI + 解析输出）是可行的，且 `opencsd_region_probe.py`
已经实现了这个流程。建议：

1. **D2 先做**：用 `make_opencsd_snapshot.py` + `trc_pkt_lister -decode` 生成解码输出
2. 解析 `trc_pkt_lister` 的 `Generic Element` 输出行（不是 INSTR_RANGE，实际格式见
   `trc_pkt_lister.ppl`）
3. **D4 ctypes 绑定推迟到方案 D-Lite 验证通过后**：如果 CLI 模式性能不够再考虑 ctypes

### 8.3 如果坚持修 Mortrall

把 `mortrall.hpp:1008` 的 `strcmp(current_func->funcname, new_func->funcname)` 改为
`current_func->addr == new_func->addr`，时间轴改用 FPGA 墙钟（已有 `.fpga_ns` 文件）。
工程量 ~2-4h。但 Mortrall 是 C++ 代码，调试不如 Python 方便。

---

## 评审维度汇总

| 维度 | 评级 | 关键问题 |
|------|------|---------|
| 1. 技术可行性 | **否决** | §6 的 C API 代码 5/7 不存在；ctypes 结构体定义全错；TPIU deframing 矛盾 |
| 2. 调用栈算法正确性 | **否决** | 尾调用/中断/RTOS 全部误判；递归测试与算法矛盾；噪声地址过滤有漏洞 |
| 3. 时间戳精度 | **存疑** | I-sync 间隔 1024B ≈ 48μs，短函数持续时间无意义；index_sop 传 0 导致时间关联失败 |
| 4. 工程量估计 | **否决** | D4 3h→实际 8-16h；整体 9.5h 不含调试；实际 30-50h |
| 5. 替代方案排除 | **否决** | 未解释为什么不升级 etm_reconstruct.py（已有逐指令重建）；未考虑 orbuculum 解码器 |
| 6. 删除 embedded-debug-tools | **存疑** | 删后无 ground truth；依赖脚本未列出 |
| 7. 文档遗漏 | **否决** | 无 INSTR_RANGE 输出格式；无 callstack_test ground truth；未讨论 exception/no-sync 处理 |

---

## 最终结论

**方案 D 不可行（当前文档版本）**。核心问题不是思路错误，而是**文档作者没有做足够的
技术验证就写了详细设计**——§6 的 C API 代码是编造的，§4.2 的算法与测试矛盾，§5 的
工程量估计不包含调试时间。

**推荐路径**：方案 D-Lite（§8.1）——不引入 OpenCSD ctypes 绑定，直接升级已有的
`etm_reconstruct.py`（逐指令重建，已验证）+ `etm_to_perfetto.py`（Perfetto 输出，已有），
把 callstack tracker 作为独立 Python 模块插入两者之间。工程量 ~4-6h，风险可控。

**如果坚持用 OpenCSD**：先用 trc_pkt_lister CLI（方案 B）验证端到端流程，ctypes 绑定
推迟到性能成为瓶颈后再做。D4 估计修正为 8-16h。

**最大风险**（无论哪条路径）：callstack tracker 的启发式算法对尾调用、中断、RTOS 线程
切换全部误判，且文档没有给出缓解措施。在 LVGL 这种复杂代码上，这些问题**必然出现**。
建议在 callstack tracker 中增加 `last_i_type` 判断（区分 direct/indirect branch）和
exception 元素处理，而不是纯靠地址切换启发式。

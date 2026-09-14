# 方案 D-Lite：自研指令重建 + 调用栈跟踪 → Perfetto

> **背景**：orbetto/Mortrall 经三轮调试未收敛（61 orphan E、2 unclosed、C++ mangled
> 名不一致）。Mortrall 是 ~1200 行 C++ 指令级调用栈重建器，与 orbuculum 的 ETM 解码器
> 紧耦合，且依赖 ETM cycleCount 做时间轴（F429 上 cycleCount=0）。
>
> **方案 D-Lite**：不引入 OpenCSD ctypes 绑定，直接升级已有的 `etm_reconstruct.py`
> （逐指令 PC 重建，已验证 ground-truth 正确）+ `etm_to_perfetto.py`（Perfetto 输出，
> 已有），把 `callstack_tracker.py` 作为独立 Python 模块插入两者之间。
>
> **核心思路**：`etm_reconstruct.py` 已经能做逐指令级 PC 重建且已验证正确——不需要
> OpenCSD。只需要把它的输出喂给一个新的 callstack tracker，用地址匹配（非名字匹配）
> 重建函数调用栈，输出 Perfetto JSON。

---

## 0. 演进历史

| 版本 | 方案 | 状态 | 结论 |
|------|------|------|------|
| **D** (原版) | OpenCSD ctypes + callstack tracker | ❌ 红方否决 | §6 的 C API 代码 5/7 不存在；ctypes 结构体全错；过度工程 |
| **D-Lite** (本文) | etm_reconstruct + callstack tracker | ✅ 采纳 | 复用已验证代码，~5.5-7.5h，风险可控 |

### 红方否决 D 的关键理由（已验证属实）

1. **§6.1 C API 编造**：`ocsd_configure_tp`、`ocsd_dt_set_config`、
   `ocsd_dt_add_callback_trcblob_cb` 在 `/usr/include/opencsd/c_api/opencsd_c_api.h`
   中不存在。实际流程是 `ocsd_create_dcd_tree` → `ocsd_dt_create_decoder` →
   `ocsd_dt_add_binfile_mem_acc` → `ocsd_dt_set_gen_elem_outfn` → `ocsd_dt_process_data`
2. **§6.2 ctypes 结构体全错**：实际 `ocsd_generic_trace_elem` 字段顺序是
   `elem_type→isa→st_addr→en_addr→context→timestamp→cycle_count→last_i_type`，
   且 `ocsd_vaddr_t` 在 32 位平台是 `uint32_t`（不是 `uint64`），`context` 是嵌套结构体
3. **§5.1 过度工程**：`etm_reconstruct.py`（327 行）已实现逐指令级 PC 重建且已验证
   ground-truth 正确（doc 14 §6）；`etm_to_perfetto.py`（180 行）已实现 Perfetto 输出。
   方案 D 的 callstack tracker 算法与 `etm_to_perfetto.py:build_stack_events()` 完全相同，
   唯一区别是输入粒度——不需要 OpenCSD
4. **§2.4 递归测试与算法矛盾**：相同地址会被 "same function" 检查拦截，不可能产生 3 个 B 事件
5. **§4.2 工程量严重低估**：D4 ctypes 绑定 3h → 实际 8-16h；整体 9.5h 不含调试

---

## 1. 为什么放 `orbtrace/syn/artix7/bringup/decode/`

### 1.1 三个候选目录对比

| 目录 | 定位 | 适合放 D-Lite？ | 理由 |
|------|------|----------------|------|
| `orbuculum/` | 上游通用 C 解码工具链 | ❌ | 上游项目，不应塞 Artix-7 专用逻辑 |
| `embedded-debug-tools/` | Auterion 的 PX4 调试 Python 库 | ❌ | 第三方项目（ext/orbetto 是其子模块），D-Lite 要替代 orbetto |
| `orbtrace/syn/artix7/bringup/decode/` | Artix-7 移植的 PC 端解码工具集 | ✅ | **已有全部基础设施**：`etm35lib.py`、`etm_reconstruct.py`、`etm_to_perfetto.py`、`fpga_timebase.py` |

### 1.2 已有基础（直接复用，不重新发明）

| 已有组件 | 行数 | D-Lite 中的角色 | 验证状态 |
|---------|------|----------------|---------|
| `etm35lib.py` | 1064 | ETM3.5 包解析（I-sync、P-header、Branch Address） | ✅ 有 test_etm35lib.py |
| `etm_reconstruct.py` | 327 | **逐指令 PC 重建**——D-Lite 的解码层 | ✅ 有 test_etm_reconstruct.py，doc 14 §6 ground-truth 验证 |
| `etm_to_perfetto.py` | 180 | I-sync 级 Perfetto 输出——D-Lite 的输出层基础 | ✅ 有 test_etm_to_perfetto.py |
| `fpga_timebase.py` | — | FPGA 墙钟时基 | ✅ 有 test_fpga_timebase.py |
| `mmcm_decode.py` | — | 2-bit DDR nibble 恢复 + TPIU deframe | ✅ 已跑通 |
| `make_opencsd_snapshot.py` | 148 | OpenCSD snapshot 打包——保留作离线对照工具 | ✅ 已跑通 |

### 1.3 新增文件

```
orbtrace/syn/artix7/bringup/decode/
├── etm35lib.py              # 已有 - ETM3.5 包解析
├── etm_reconstruct.py       # 已有 - 逐指令 PC 重建（D-Lite 解码层）
├── etm_to_perfetto.py       # 已有 - I-sync 级 Perfetto（保留作快速预览）
├── fpga_timebase.py         # 已有 - FPGA 墙钟时基
├── mmcm_decode.py           # 已有 - 2-bit 恢复 + TPIU deframe
├── make_opencsd_snapshot.py # 已有 - OpenCSD snapshot（保留作离线对照）
│
├── callstack_tracker.py     # 【新】指令地址序列 → 函数调用栈 → Perfetto JSON (~200行)
└── test_callstack_tracker.py # 【新】单元测试
```

**只新增 2 个文件**，不引入任何新依赖。

---

## 2. 是否可以干掉 `embedded-debug-tools/`？

### 2.1 当前依赖分析

| 组件 | 当前用途 | D-Lite 后是否还需要 |
|------|---------|-------------------|
| `ext/orbetto/` | ETM→Perfetto（Mortrall 调用栈） | ❌ D-Lite 完全替代 |
| `src/emdbg/` | PX4 调试库 | ❌ 不用 PX4 |

### 2.2 结论

**暂不删除。** 红方评审 §6.1 指出：orbetto/Mortrall 是唯一能做对照的参考实现——即使它
有 bug（61 orphan E），它的输出仍可用来对比 D-Lite 的输出差异。

**删除条件**：D-Lite 在 LVGL trace 上稳定运行至少 3 次无 orphan E 后，再 `git rm`。

### 2.3 依赖脚本清单（删除前需一并处理）

| 脚本 | 依赖 orbetto 的方式 |
|------|-------------------|
| `mmcm_stream_orbetto.py` | 生成 orbetto 输入文件（`.tpiu` + `.fpga_ns`） |
| `orbetto_perf_to_json.py` | 转换 orbetto 输出为 JSON |
| `diag_orbetto_stack.py` | 诊断 orbetto 栈平衡 |

删除 `embedded-debug-tools/` 时，这些脚本也需要删除或重写为 D-Lite 管线。

### 2.4 orbetto/Mortrall 中值得保留的知识

- Mortrall 的 `_catchInconsistencies`：栈折叠启发式（用函数名匹配 → D-Lite 改用地址）
- Mortrall 的 indirect return 延迟弹出：间接返回需等下一个 PC 才知道返回目标
- orbetto 的 Perfetto protobuf 输出格式：B/E 事件 + thread metadata

---

## 3. 架构设计

### 3.1 数据流

```
┌─────────────────────────────────────────────────────────────────┐
│  FPGA (Artix-7)                                                  │
│  trace pins → MMCM DDR → TPIU framing → UDP :5555               │
└──────────────────────────┬──────────────────────────────────────┘
                           │ UDP stream
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  PC 端 - 现有（不改动）                                           │
│  trace_stream_rx.py → mmcm_decode.py → bare ETM + .fpga_ns       │
│  (2-bit nibble 恢复 + TPIU deframe + FPGA 墙钟时基)              │
└──────────────────────────┬──────────────────────────────────────┘
                           │ bare ETM bytes + fpga_ns (wall-clock ns per byte)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  PC 端 - D-Lite                                                   │
│                                                                   │
│  ┌─────────────────────────┐    ┌────────────────────────────┐  │
│  │ etm_reconstruct.py      │    │ callstack_tracker.py       │  │
│  │ (已有, 327行, 已验证)    │    │ (新, ~200行)               │  │
│  │                         │    │                            │  │
│  │ bare ETM → I-sync 锚点  │    │ (addr, kind, time_ns) 序列 │  │
│  │  → P-header atom walk   │───▶│  → ELF 符号映射 (nm -nSC)  │  │
│  │  → 逐指令 PC + kind     │    │  → call/return 跟踪        │  │
│  │  + byte_offset → time_ns│    │  → Perfetto JSON (B/E)     │  │
│  │                         │    │                            │  │
│  │ Insn.kind:              │    │ 纯 Python, 可单元测试      │  │
│  │  'direct'|'indirect'    │    │                            │  │
│  │  |'other'               │    │                            │  │
│  └─────────────────────────┘    └────────────────────────────┘  │
│                                                                   │
│  输出: trace_perfetto.json → https://ui.perfetto.dev             │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 三段解耦设计

| 段 | 职责 | 实现 | 可测试性 |
|----|------|------|---------|
| **① 解码** | ETM 包 → 逐指令 PC + kind | `etm_reconstruct.py` (已有, 已验证) | test_etm_reconstruct.py + doc 14 ground-truth |
| **② 跟踪** | 指令序列 → 函数调用栈 | `callstack_tracker.py` (新, ~200行) | 纯 Python，合成数据单元测试 |
| **③ 时基** | byte_offset → 墙钟 ns | `fpga_timebase.py` (已有) | test_fpga_timebase.py |

### 3.3 与原方案 D 的关键差异

| 维度 | 方案 D (否决) | 方案 D-Lite (本文) |
|------|--------------|-------------------|
| ETM 解码 | OpenCSD ctypes (API 编造, 不可用) | `etm_reconstruct.py` (已有, 已验证) |
| 新增文件 | 2 个 (opencsd_decode + callstack_tracker) | 1 个 (callstack_tracker) |
| 新增依赖 | libopencsd_c_api + ctypes 绑定 | 无 |
| 工程量 | 9.5h (不含调试, 实际 30-50h) | 5.5-7.5h (含调试) |
| 解码正确性 | OpenCSD (ARM 官方) | etm_reconstruct (自研, doc 14 已验证) |
| 指令类型 | OpenCSD last_i_type | etm_reconstruct Insn.kind (direct/indirect/other) |

### 3.4 为什么比 Mortrall 好

| 问题 | Mortrall | D-Lite |
|------|---------|--------|
| ETM 解码 | orbuculum 自研解码器，有 corner case | `etm_reconstruct.py`（doc 14 已验证 ground-truth） |
| 调用栈重建 | ~1200 行 C++，与解码器紧耦合 | ~200 行 Python，输入是地址序列，纯逻辑 |
| 时间轴 | ETM cycleCount（F429=0，不可用） | FPGA 墙钟 ns（已有，精确） |
| 函数名 | DWARF DW_AT_name（C++ mangled 不一致） | `arm-none-eabi-nm -nSC`（demangled，一致） |
| 栈折叠 | strcmp(funcname)（C++ 名不匹配会误判） | 地址范围匹配（无歧义） |
| 可测试性 | 需要 ETM 硬件流才能测 | 可用合成指令序列做单元测试 |

---

## 4. 详细设计

### 4.1 `etm_reconstruct.py` — 解码层（已有，需小幅修改）

#### 4.1.1 现有能力

`etm_reconstruct.py` 已实现：
- `reconstruct_region()`：从 I-sync 锚点出发，walk P-header atoms + branch address packets，
  逐指令推进 PC
- `reconstruct_all()`：对所有 I-sync 锚点重建，返回 `(anchor_pc, [insn_addrs], stop_reason)`
- `Insn` 类有 `kind` 字段：`'direct'` | `'indirect'` | `'other'`
- doc 14 §6 验证：proj_add 的 `loop_sum → add` 循环被逐指令重建，ground-truth 对上

#### 4.1.2 需要的修改

当前 `reconstruct_all()` 返回 `(anchor_pc, [insn_addrs], stop_reason)`，需要扩展为
返回每条指令的 `(addr, kind, byte_offset)`：

```python
# 修改 reconstruct_region() 返回值：
#   原: (insn_addrs, consumed_bytes, stop_reason)
#   新: (insn_records, consumed_bytes, stop_reason)
#       insn_records = [(addr, kind, byte_offset), ...]

# 修改 reconstruct_all() 返回值：
#   原: [(anchor_pc, [insn_addrs], stop_reason)]
#   新: [(anchor_pc, [insn_records], stop_reason)]
```

改动量：~30 行（在 `reconstruct_region` 的 atom walk 循环中，把 `out.append(pc)` 改为
`out.append((pc, ins.kind, i))`，`i` 是当前 ETM 字节偏移）。

### 4.2 `callstack_tracker.py` — 调用栈跟踪层（新）

#### 4.2.1 接口

```python
def build_callstack_events(
    insn_stream: list[tuple[int, str, int]],  # (addr, kind, byte_offset)
    elf_path: str,          # for symbol resolution (nm -nSC)
    time_ns: list[int],     # wall-clock ns per byte (from fpga_timebase)
    pid: int = 1,
    tid: int = 1,
) -> list[dict]:
    """
    Returns Chrome/Perfetto trace events (B/E + metadata).
    """
```

#### 4.2.2 核心算法

```python
class CallStackTracker:
    def __init__(self, elf_path):
        self.starts, self.funcs = load_symbols(elf_path)  # nm -nSC, sorted
        self.stack = []  # [(func_name, func_addr, entry_time_ns)]

    def on_instruction(self, addr, kind, time_ns):
        func = self._func_for_pc(addr)

        if func is None:
            return  # unknown address (noise), skip

        func_name, func_addr = func

        # Case 1: same function as stack top → no change
        if self.stack and self.stack[-1][1] == func_addr:
            return

        # Case 2: function already in stack → RETURN
        # Pop frames above it. But for recursion (same func_addr == stack top),
        # we need special handling (see §4.2.5).
        addrs_in_stack = [f[1] for f in self.stack]
        if func_addr in addrs_in_stack:
            # Check if this is recursion (func_addr == stack top)
            # → NOT a return, it's a recursive call (see §4.2.5)
            if self.stack[-1][1] != func_addr:
                # Normal return: pop until we reach this function
                while self.stack and self.stack[-1][1] != func_addr:
                    popped = self.stack.pop()
                    emit_event("E", popped[2], time_ns)
            # else: recursion handled in Case 3

        # Case 3: new function (call or recursive call)
        # If we didn't return in Case 2, or func_addr not in stack, push.
        if not self.stack or self.stack[-1][1] != func_addr:
            self.stack.append((func_name, func_addr, time_ns))
            emit_event("B", time_ns, name=func_name)
```

#### 4.2.3 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 函数识别 | 地址范围二分查找 (`nm -nSC`) | 无 C++ mangled 名歧义 |
| call/return 判定 | 函数切换 = call；回到祖先 = return | 纯地址逻辑，不依赖 BL/BX 指令识别 |
| 栈折叠 | 地址匹配（不是名字匹配） | Mortrall 的 bug 根源就是名字匹配 |
| 噪声地址 | 不在 ELF 符号范围内的地址 → 跳过 | 2-bit 噪声产生的 0x08003ffe 等自动过滤 |
| 递归 | 同函数在栈中出现多次 → push 新帧 | 正确处理 factorial 等递归（见 §4.2.5） |
| 时间轴 | FPGA 墙钟 ns | 不依赖 ETM cycleCount |
| 指令类型 | `Insn.kind` (direct/indirect/other) | 从 `etm_reconstruct.py` 获取，辅助判断 |

#### 4.2.4 与 Mortrall 的关键差异

```python
# Mortrall (有 bug):
if strcmp(current_func->funcname, new_func->funcname) == 0:
    # 名字相同就折叠 → C++ mangled 名不一致时误判
    fold_stack_to(new_func)

# D-Lite (修复):
if new_func_addr in [f.addr for f in stack]:
    # 地址相同才折叠 → 无歧义
    pop_until(new_func_addr)
```

#### 4.2.5 递归处理（修复红方 §2.4 指出的矛盾）

红方 §2.4 指出：原方案的递归测试与算法矛盾——相同地址会被 "same function" 检查拦截，
不可能产生 3 个 B 事件。

**修复**：递归调用的特征是 PC 进入一个**已经在栈中**的函数，但不是栈顶（栈顶是调用者）。
但如果是直接递归（A 调用 A），栈顶就是 A，"same function" 检查会拦截。

**关键洞察**：递归调用在 ETM trace 中表现为 PC 从函数 A 的某条指令跳到函数 A 的入口
（第一条指令）。这不是 "same function running"（PC 在函数内连续推进），而是 "re-entered
function A at its entry point"。

```python
# 递归检测：PC 跳到当前函数的入口地址，且上一条指令不是入口
def on_instruction(self, addr, kind, time_ns):
    func = self._func_for_pc(addr)
    if func is None:
        return

    func_name, func_addr = func
    is_entry = (addr == func_addr)  # PC at function entry point

    if self.stack and self.stack[-1][1] == func_addr:
        if is_entry and kind == 'direct':
            # Recursive call: same function, but entered at entry via a branch
            self.stack.append((func_name, func_addr, time_ns))
            emit_event("B", time_ns, name=func_name)
            return
        else:
            # Same function, normal execution
            return

    # ... rest of algorithm (return / call as before)
```

**但这引入新问题**：如何区分"递归调用"和"循环回到函数入口"（比如 `while(1)` 里的
`continue` 跳回函数开头）？`Insn.kind == 'direct'` 不够——`b` 指令既可以是递归调用
也可以是循环回边。

**务实方案**：对于 callstack_test 固件（O0 编译，无优化），递归调用一定是 `bl` 指令
（function call），循环回边是 `b` 指令。`etm_reconstruct.py` 的 `_classify_insn` 把
`bl` 归类为 `'direct'`，`b` 也归类为 `'direct'`——需要进一步区分。

**最终方案**：在 `etm_reconstruct.py` 的 `Insn` 类中增加 `is_call` 字段（`bl`/`blx` 为
True，`b`/`cbz`/`cbnz` 为 False），callstack tracker 用 `is_call` 判断递归。

### 4.3 已知局限性与缓解（回应红方 §2）

红方评审指出了 5 个算法误判场景。D-Lite 的立场是：**承认局限，记录边界，不假装解决**。

| 场景 | 影响 | D-Lite 处理 | 缓解 |
|------|------|------------|------|
| **尾调用** (tail call) | B 的 E 事件时间偏晚 | ❌ 不处理 | callstack_test 用 O0 编译，无尾调用；LVGL 主要是 C 函数，尾调用少 |
| **中断** | IRQ handler 被当 call 压栈 | ⚠️ 部分处理 | ETM exception packet 在 `etm_reconstruct.py` 中会中断 region（stop_reason），tracker 在 region 边界自然清栈；但 IRQ handler 内的函数调用会被当普通 call |
| **RTOS 线程切换** | 栈深度无意义 | ❌ 不处理 | callstack_test 和 LVGL 都是裸机（无 RTOS）；如果未来需要 RTOS 支持，再增加 thread ID 跟踪 |
| **噪声地址落在函数内** | 可能产生虚假 B/E | ⚠️ 部分处理 | 2-bit 噪声地址通常是 0x0800Xffe（偶数对齐边界），落在函数间隙的概率高于落在函数内；如果落在函数内，"same function" 检查会吸收它 |
| **内联函数** | 误判为 call | ❌ 不处理 | callstack_test 用 O0 编译（无内联）；LVGL 用 -O2 但内联函数的符号通常不单独列出 |

**设计原则**：D-Lite 面向 callstack_test（O0，确定性调用流）和 LVGL（裸机，无 RTOS），
不试图覆盖所有理论场景。如果未来需要 RTOS/中断/尾调用支持，再迭代。

### 4.4 时间戳精度（回应红方 §3）

红方 §3.1 指出：F429 的 I-sync 间隔 1024 字节 ≈ 48μs，短函数持续时间可能无意义。

**D-Lite 的时间戳策略**：

1. `etm_reconstruct.py` 的 `reconstruct_region()` 在每个 I-sync 锚点重新对齐 PC，
   同时记录该锚点的 byte_offset
2. `fpga_timebase.py` 提供 byte_offset → wall-clock ns 映射
3. **每条指令的时间戳 = 该指令所在 region 的 I-sync 锚点时间**（不是插值）
4. 函数持续时间 = 函数内最后一条指令的锚点时间 - 函数入口的锚点时间

**精度限制**：
- I-sync 间隔 1024 字节 ≈ 48μs（84MHz TRACECLK, 2-bit DDR）
- 如果函数执行时间 < 48μs，持续时间可能为 0（入口和出口在同一 I-sync region 内）
- **但这不影响调用栈正确性**——B/E 事件的嵌套关系由 PC 序列决定，不由时间戳决定
- 时间戳只影响 Perfetto timeline 的横向宽度，不影响纵向嵌套

**结论**：时间戳精度对函数级调用栈可视化**够用**。如果未来需要指令级时间精度，
需要启用 ETM cycle count（F429 的 ETMCCR bit[12] 是否支持需确认）。

### 4.5 单元测试设计（修复红方 §2.4, §4.1）

```python
# test_callstack_tracker.py

def test_simple_call_return():
    """A calls B, B returns to A"""
    insns = [
        (0x08001000, 'other', 0, 1000),  # func_a entry
        (0x08001100, 'direct', 10, 1100), # func_b entry (bl)
        (0x08001004, 'indirect', 20, 1200), # back in func_a (bx lr)
    ]
    events = build_callstack_events(insns, "test.elf", time_ns)
    assert len(events) == 4  # B(a), B(b), E(b), E(a)

def test_recursion():
    """factorial(n) calls factorial(n-1)"""
    # factorial at 0x08002000, calls itself via bl
    insns = [
        (0x08002000, 'other', 0, 1000),   # factorial entry (1st)
        (0x08002000, 'direct', 10, 1100),  # factorial entry (2nd, bl = recursive call)
        (0x08002000, 'direct', 20, 1200),  # factorial entry (3rd, bl = recursive call)
        (0x08002004, 'indirect', 30, 1300), # return from 3rd (bx lr)
        (0x08002004, 'indirect', 40, 1400), # return from 2nd (bx lr)
        (0x08002004, 'indirect', 50, 1500), # return from 1st (bx lr)
    ]
    events = build_callstack_events(insns, "test.elf", time_ns)
    b_count = sum(1 for e in events if e["ph"] == "B")
    e_count = sum(1 for e in events if e["ph"] == "E")
    assert b_count == 3 and e_count == 3

def test_noise_address_not_in_any_function():
    """0x08003ffe (noise) not in any function → filtered"""
    insns = [
        (0x08001000, 'other', 0, 1000),  # func_a
        (0x08003ffe, 'other', 10, 1100),  # noise - not in any function
        (0x08001004, 'other', 20, 1200),  # still func_a
    ]
    events = build_callstack_events(insns, "test.elf", time_ns)
    names = [e.get("name", "") for e in events if e["ph"] == "B"]
    assert "0x08003ffe" not in names

def test_noise_address_inside_function():
    """Noise address falls inside a real function → absorbed by 'same function'"""
    # func_a: 0x08001000-0x08002000, noise at 0x08001800 (inside func_a)
    insns = [
        (0x08001000, 'other', 0, 1000),   # func_a entry
        (0x08001800, 'other', 10, 1100),   # noise but inside func_a
        (0x08001004, 'other', 20, 1200),   # still func_a
    ]
    events = build_callstack_events(insns, "test.elf", time_ns)
    # Should NOT create spurious B/E for the noise address
    b_count = sum(1 for e in events if e["ph"] == "B")
    assert b_count == 1  # only func_a

def test_deep_call_chain():
    """deep1 → deep2 → deep3 → deep5 → deep6"""
    insns = [
        (0x08003000, 'other', 0, 1000),    # deep1
        (0x08003100, 'direct', 10, 1100),   # deep2 (bl)
        (0x08003200, 'direct', 20, 1200),   # deep3 (bl)
        (0x08003300, 'direct', 30, 1300),   # deep5 (bl)
        (0x08003400, 'direct', 40, 1400),   # deep6 (bl)
        (0x08003204, 'indirect', 50, 1500), # return to deep3 (bx lr)
        (0x08003104, 'indirect', 60, 1600), # return to deep2 (bx lr)
        (0x08003004, 'indirect', 70, 1700), # return to deep1 (bx lr)
    ]
    events = build_callstack_events(insns, "test.elf", time_ns)
    max_depth = 0
    depth = 0
    for e in events:
        if e["ph"] == "B": depth += 1
        elif e["ph"] == "E": depth -= 1
        max_depth = max(max_depth, depth)
    assert max_depth == 5

def test_region_boundary_clears_stack():
    """When etm_reconstruct stops a region (noise/loss), stack should be cleared"""
    # Region 1: func_a calls func_b
    # Region 2: starts fresh at func_c (previous stack lost)
    insns = [
        (0x08001000, 'other', 0, 1000),    # func_a
        (0x08001100, 'direct', 10, 1100),   # func_b
        # --- region boundary (stop_reason="pc not in image") ---
        (0x08001200, 'other', 100, 2000),   # func_c (new region, fresh start)
    ]
    events = build_callstack_events(insns, "test.elf", time_ns)
    # func_a and func_b should be closed (E events) at region boundary
    # func_c should be a new B event
    # Stack at end: [func_c], not [func_a, func_b, func_c]
```

---

## 5. callstack_test 固件 ground truth（回应红方 §7.4）

红方 §7.4 指出：没有 ground truth 的验证是空话。

### 5.1 callstack_test 已知调用流

callstack_test 固件（O0 编译，确定性调用流）的已知函数调用关系：

```
main()
├── repeat_test()
│   ├── level_a() → level_b() → level_c()        # 3 层深调用链
│   ├── deep1() → deep2() → deep3() → deep5() → deep6()  # 5 层深调用链
│   ├── factorial(n)                               # 递归
│   │   └── factorial(n-1) → ...
│   ├── callback_test()                            # 间接调用
│   │   ├── cb_handler_a()
│   │   └── cb_handler_b()
│   ├── mixed_test()                               # 混合调用
│   │   ├── op_add() → leaf_add()
│   │   ├── op_mul() → leaf_mul()
│   │   └── op_sub()
│   ├── conditional(n)                             # 条件分支
│   ├── frame_func()                               # 栈帧测试
│   ├── pingpong(n)                                # 互相调用
│   │   └── pingpong(n-1)
│   ├── indirect_caller()                          # 间接调用
│   └── mydelay()                                  # 延时
```

### 5.2 验证方法

1. 反汇编 callstack_test.elf，确认每个函数的地址范围
2. 用 `etm_reconstruct.py` 重建逐指令 PC 序列
3. 用 `callstack_tracker.py` 生成 B/E 事件
4. **人工检查**：B/E 事件的嵌套关系是否与 §5.1 的调用树一致
5. **自动检查**：最大栈深度 = 5（deep1→deep6），递归深度 = factorial 的参数 n

### 5.3 需要补充的固件信息

在实施前需要确认：
- [ ] callstack_test.elf 的 `nm -nSC` 输出（函数地址范围）
- [ ] factorial 的递归深度参数 n
- [ ] repeat_test 的循环次数
- [ ] 是否有中断（USART3_IRQHandler 等）会打断调用流

---

## 6. 实施计划

### 6.1 分阶段交付

| 阶段 | 内容 | 预计工时 | 交付物 |
|------|------|---------|--------|
| **D1** | `callstack_tracker.py` + 单元测试 | 2-3h | 纯 Python，合成数据测试通过 |
| **D2** | 修改 `etm_reconstruct.py` 输出格式 | 0.5h | 返回 (addr, kind, byte_offset) |
| **D3** | 端到端：callstack_test.bin → Perfetto JSON | 1-2h | B/E 事件与 §5.1 调用树对照 |
| **D4** | LVGL trace 验证 | 1h | 替代 orbetto 跑 LVGL trace |
| **D5** | 与 orbetto 输出对比 | 0.5h | 确认 D-Lite 无 orphan E |
| **D6** | 清理（验证通过后） | 0.5h | git rm embedded-debug-tools |

**总计：5.5-7.5h**（含调试，红方 §4.3 建议的 ×3-5 系数已部分纳入）

### 6.2 验收标准

| 指标 | orbetto/Mortrall 现状 | D-Lite 目标 |
|------|----------------------|------------|
| orphan E events | 61 | 0 |
| unclosed functions | 2 | 0 |
| 函数名 | mangled 不一致 | 全部 demangled (nm -nSC) |
| 噪声地址 | 0x08003ffe 等出现在输出 | 自动过滤 |
| callstack_test 验证 | 脱轨 | 与 §5.1 调用树一致 |
| 时间轴 | cycleCount=0（不可用） | FPGA 墙钟 ns |
| 最大栈深度 | 2（错误） | 5（deep1→deep6） |

### 6.3 D1 优先：先写 callstack_tracker

**为什么先写 tracker**：
1. 不依赖 ETM 硬件流，可用合成指令序列立即测试
2. 核心逻辑（地址→函数→B/E）是 D-Lite 的关键创新
3. 可以先用合成数据验证算法正确性（包括递归、噪声、深调用链）
4. D2 只需小幅修改 `etm_reconstruct.py`，D3 是对接

---

## 7. OpenCSD 的角色（降级为离线对照工具）

### 7.1 不再用作解码引擎

原方案 D 把 OpenCSD 作为核心解码引擎。D-Lite 改用 `etm_reconstruct.py`。

### 7.2 保留为离线对照

`make_opencsd_snapshot.py` + `trc_pkt_lister` 保留为**离线对照工具**：
- 当 `etm_reconstruct.py` 的解码结果有疑问时，用 OpenCSD 做交叉验证
- `opencsd_region_probe.py` 保留用于测量 capture clean-ness
- 不需要 ctypes 绑定，只用 CLI 模式

### 7.3 如果未来需要 OpenCSD

如果 `etm_reconstruct.py` 在某些 corner case 上不正确，且 OpenCSD 能正确处理，
再考虑 ctypes 绑定。但必须：
1. 先用 `trc_pkt_lister` CLI 验证 OpenCSD 确实能正确解码
2. 逐行对照 `/usr/include/opencsd/c_api/opencsd_c_api.h` 写 ctypes 绑定
3. 估计工时 8-16h（不是 3h）

---

## 8. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| `etm_reconstruct.py` 在噪声流上 region 频繁中断 | 中 | 大量短 region，调用栈碎片化 | region 边界清栈（§4.5 test_region_boundary）；碎片化不影响正确性，只影响连续性 |
| 噪声地址落在真实函数内 | 低 | 可能产生虚假 B/E | "same function" 检查吸收（§4.3）；callstack_test 的噪声地址主要是 0x0800Xffe（函数间隙） |
| I-sync 间隔 48μs 导致短函数持续时间=0 | 中 | Perfetto timeline 横向精度低 | 不影响嵌套正确性（§4.4）；函数级 trace 够用 |
| 尾调用/中断/RTOS 误判 | 低（callstack_test）/ 中（LVGL） | 栈深度错误 | callstack_test 用 O0 无尾调用；LVGL 裸机无 RTOS（§4.3） |
| `etm_reconstruct.py` 的 Insn.kind 分类有误 | 低 | call/return 判定错误 | doc 14 已验证 `_classify_insn` 对 proj_add 正确；callstack_test 可做交叉验证 |

---

## 9. 与现有工具的关系

```
D-Lite 完成后的工具链：

  FPGA capture → mmcm_decode.py → bare ETM + .fpga_ns
                                    │
                     ┌──────────────┼──────────────┐
                     ▼              ▼               ▼
              etm_reconstruct   etm_to_perfetto   make_opencsd_snapshot
              (D-Lite 解码层)   (I-sync级预览)    + trc_pkt_lister
                     │              │               (离线对照)
                     ▼              ▼
              callstack_tracker  (直接输出)
              (D-Lite 跟踪层)         │
                     │               │
                     ▼               ▼
              Perfetto JSON → ui.perfetto.dev

  orbetto/Mortrall → 保留作对照，验证通过后删除
  orbuculum → 保留（上游项目，不改动）
  OpenCSD → 降级为离线对照工具（CLI 模式，不做 ctypes 绑定）
```

---

## 10. 总结

| 维度 | 方案 D (否决) | 方案 D-Lite (本文) |
|------|--------------|-------------------|
| **位置** | `orbtrace/syn/artix7/bringup/decode/` | 同左 |
| **新增代码** | 2 文件 (opencsd_decode + callstack_tracker) | 1 文件 (callstack_tracker, ~200行) |
| **新增依赖** | libopencsd_c_api + ctypes | 无 |
| **ETM 解码** | OpenCSD (API 编造, 不可用) | `etm_reconstruct.py` (已有, 已验证) |
| **调用栈** | 地址匹配 Python | 同左 |
| **时间轴** | FPGA 墙钟 ns | 同左 |
| **工程量** | 9.5h (实际 30-50h) | 5.5-7.5h (含调试) |
| **OpenCSD** | 核心解码引擎 | 离线对照工具 (CLI) |
| **第一步** | D1: callstack_tracker + 测试 | 同左 |

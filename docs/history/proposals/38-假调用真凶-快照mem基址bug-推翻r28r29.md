# Proposal 38：假调用真凶 = 快照 mem.bin 基址 bug（推翻 r28/r29 的 ETM/cache 归因）

> 日期：2026-07-20
> 状态：根因锁定 + bugfix + 重解验证完成
> 触发：用户追问"BB=0 漂移到底发生在哪、解析器为何对付不了高主频"——要求单步研究解析器
>       消除猜测，而非继续推断。
> 结论：**假调用（`core_list_init→HAL_UART_Init`、`ee_printf` 等）的真凶不是 ETM 压缩模型、
>       不是 cache、不是采集丢字节、不是 BB-OFF 盲推漂移——是 `make_opencsd_snapshot.py`
>       把 mem.bin 基址硬编码成 0x08000000，而 objcopy 剥掉 .isr_vector 后 binary 实际从
>       .text 的 LMA 0x080002a0 起，全局错位 0x2a0 字节。** 之前 r28 阶段3、r29 的所有
>       "盲推漂移/稀疏锚点/cache 拉稀"归因**全部推翻**。

---

## 0. 怎么找到的（单步研究解析器，不再猜）

用户否决了继续推断，要求单步看解析器。执行路径：

1. **先排除溢出（可能性 A）**：抓脏配置 BB-OFF+cache+150M，解码数溢出——
   `OVERFLOW=0, TRACE_ON=0, ADDR_NACC=0`，流完整无损。排除"高吞吐溢出丢字节"。
2. **定位第一个可疑跳变**：解码流里 `range=0x8008254:[0x800825e]` 被标为 `E BR b+link`
   （BL/函数调用），但真实反汇编 `0x825e` 是 `bne.n`（条件分支），**根本不是 BL**。
   解析器看到的指令 ≠ 真实指令。
3. **核对解析器读的内存镜像**：mem.bin 偏移 0 = `0348 044b 8342...` = ELF **.text 首字节
   （LMA 0x080002a0）**，但 cpu.ini 声明 `address=0x08000000`。
4. **验证错位**：真实 0x8008254 的字节（`012d 4ff0 0104`）在 mem.bin 的偏移
   `0x8254-0x2a0=0x7fb4` 处找到，与 ELF 一致——**证实全局错位 0x2a0**。

---

## 1. Bug 本体

`make_opencsd_snapshot.py` 生成解码用内存镜像：
```python
objcopy -O binary --only-section=.text --only-section=.rodata ... elf mem.bin
mem_base = 0x08000000   # ← 硬编码
```
- ELF 段布局：`.isr_vector @0x08000000 (0x298B)` → `.text @0x080002a0` → `.rodata @0x0800a4f8`。
- `--only-section` **不含 .isr_vector**，objcopy `-O binary` 从**最低included section 的 LMA**
  （=.text 的 0x080002a0）开始输出，且**不在前面填充**。
- 但 cpu.ini 告诉解码器 mem.bin 基址 = 0x08000000。
- ⇒ 解码器取 PC=X 的指令时读 mem.bin[X-0x08000000]，实际得到的是地址 X+0x2a0 的字节。
  **每一次取指都错位 0x2a0。**

---

## 2. 为什么 BB=1 "看起来干净"、BB=0 "脏"——错位被地址包掩盖

这解释了困扰 r28/r29 好几轮的"BB=1 干净、BB-OFF 脏"现象，且与之前的所有解释都不同：

- **BB=1**：每个 taken 分支都发**绝对地址包**。解码器每步都被真实地址**强制重锚**，即使
  mem 镜像错位导致中间反汇编错，下一个地址包立刻纠正 PC。错位影响被**每分支的显式地址掩盖**，
  调用图看起来正确（其实中间指令流仍是错的，只是锚点频繁到看不出）。
- **BB=0**：直接分支不发地址，解码器**靠 mem 镜像反汇编盲推**目标。镜像错位 0x2a0 ⇒ 盲推读到
  的是错误指令 ⇒ 把 `bne` 当 `BL`、算错目标、走进错误函数。间接分支的稀疏地址包只能偶尔重锚，
  中间大段全错 ⇒ 大量假调用。

**所以"BB=1 干净 vs BB-OFF 脏"根本不是 ETM 机制或锚点密度差异，是"BB=1 的显式地址掩盖了 mem
错位、BB=0 暴露了它"。** r28/r29 把这个现象误归因成了 ETM 压缩模型的固有局限。

---

## 3. Bugfix + 重解验证（决定性）

**Fix**：`make_opencsd_snapshot.py`
1. objcopy 加回 `--only-section=.isr_vector`（让 binary 真正从 0x08000000 起）；
2. `mem_base` 不再硬编码，改从 ELF 最低可加载段 LMA 推导（`_lowest_load_lma()`，防御性）。

**重解同一份脏数据**（BB-OFF + cache + 150M，`/tmp/dirty_bb0cache150.bin`）：

| | 修复前（错位 0x2a0）| 修复后（基址正确）|
|---|---|---|
| INSTR_RANGE | 2986 | **17882**（6× 正确解出）|
| unique PC | 940 | 1458（全落 flash）|
| top 函数 | `core_list_init`(假)、`HAL_UART_Init`、`ee_printf` | `matrix_test 354`、`core_state_transition 188`、`core_bench_state 113`、`core_bench_list 56`、`core_bench_matrix 10`、`core_list_mergesort`、`matrix_*`、`crcu16`、`cmp_complex` |
| HAL_UART/ee_printf 假调用 | 有 | **零** |

**修复后 BB-OFF+cache+150M 调用图全部是 CoreMark 真实核心函数，零假调用。**

---

## 4. 被推翻的结论（诚实记账）

| 文档 | 原结论 | 现状 |
|------|--------|------|
| proposal 36 阶段3 | "BB-OFF+cache 假调用 = cache 拉稀锚点 → 盲推越界" | **推翻**：真凶是 mem 基址 bug |
| r28 评审 R1 分叉 | 纠结"盲推 vs 采集丢锚点" | **两者皆非**：是解码镜像错位 |
| r29-response | "假调用 = atom 盲推漂移，返回栈不参与" | **推翻**：不是 atom 漂移，是喂错镜像 |
| r28-response-ETMv4机理 | C1-C8 关于 BB/atom/返回栈的推理 | 手册引用仍属实，但**用它们解释假调用是错的**（假调用与这些机制无关）|
| proposal 37 | cache 改/不改 trace（已被 r30 证伪）| 不受影响（那是另一条线）|

**唯一站得住的前序结论**：R2 的带宽/生成率真值（G50=17.9、G75=22.5 MB/s、峰值簇溢出）——那是
采集侧字节率测量，不依赖 mem 镜像，**不受本 bug 影响**，仍有效。

---

## 5. 方法论教训（本项目老毛病的又一次，且最深）

1. **连续多轮（r28/r29）在错误的解码基座上做"根因分析"**，把一个**工具 bug** 反复归因成
   **ETM 架构固有局限**（"压缩模型盲推""稀疏锚点""cache 拉稀"）——越猜越"高级"，离真相越远。
   红方 r30 早就点过"甩锅式解释：把可查的工程 bug 归给听起来高级的架构限制"，**这次坐实了**。
2. **用户的坚持是对的**："单步研究解析器消除猜测"——一旦真去看解析器读到的字节，10 分钟就
   定位了几轮都没找到的真凶。**推断链再自洽，也不如单步看一眼实际数据。**
3. **验收判据的盲区**：之前一直用"字节错 0.000%（RESERVED/BAD_SEQ）"判采集质量——它确实说明
   采集没问题，但**完全测不出解码镜像错位**（错位不产生非法包）。红方 r28 R1 说的"字节错是
   盲区"是对的，只是连红方也没想到盲区后面藏的是 mem 基址 bug。
4. **改正**：① 任何 BB-OFF 解码结论必须先验证 mem.bin 基址 = ELF 最低 LMA；② 单变量实验
   校验镜像逐字节；③ 引用架构机制解释现象前，先排除工具链 bug。

---

## 6. 对主线的重大影响

**"满血 + BB-OFF 精确函数级 trace"的可达性需要重新评估**——之前判"不可达"的核心理由之一
（cache 全速下 BB-OFF 必然盲推漂移出假调用）**已被推翻**。现在已知：BB-OFF+cache+150M 在
**修复解码器后调用图是干净的**。

**仍需独立验证的**（不受本 bug 影响、仍成立的约束）：
- R2 的带宽峰值簇溢出：满血高主频下 BB=1 峰值 >端口，BB-OFF 削峰但高主频峰值仍需实测；
- 修复后需**重抓 BB-OFF 各频点**，用正确解码器重新评估调用图在 200M/满血下是否仍干净、
  是否溢出。

**下一步**：用修复后的解码器，重跑 proposal 36 的阶段2/3 关键抓样（BB-OFF 无cache、BB-OFF+cache
各频点），重新评估"函数级 trace 追平"的真实可达性——之前的悲观结论建立在错误解码上，需要
在正确基座上重做。

---

## 7. 端到端重跑验证（bugfix 后，2026-07-20）

用修复后的解码器重抓 BB-OFF+cache+150M，导出 Perfetto perf，做**三重独立交叉验证**：

### 验证 1：opencsd 调用边逐条对照 ELF（`verify_calls.py`）
提取解码流所有 `b+link`（函数调用）边，检查末尾指令确为 bl/blx 且目标 == ELF 静态目标：
```
b+link call edges: 373
  end insn really is bl/blx: 373
  target matches ELF static (or indirect blx): 373
  MISMATCH/suspect: 0
```
**373/373 调用边全部对上 ELF 真实调用关系，零不匹配。**

### 验证 2：orbetto 独立解码路径（自读 ELF、自 deframe）
`orbetto -C 150000 -t 1 -e stm32h743_*.elf -F timed.time.bin`：
- **PC bitmap cardinality = 962**（bug 修复前是 0——"cardinality=0"也是基址 bug 的连锁后果）。
- Overflows: 0。导出 `coremark_bb0_cache_150m_memfix.perf`（工程根目录）。

### 验证 3：perf 调用栈函数名（Perfetto slice）
perf 里的调用栈 slice 全是 CoreMark 真实函数，**零 HAL/ee_printf 假调用**：
```
core_state_transition 142, crc16 114, cmp_complex 95, crcu32 16, crcu16 5,
core_bench_state 3, core_bench_list 2, cm_benchmark_main 1
```
嵌套关系合 ELF：cmp_complex 真实调用 core_bench_matrix/crcu16/core_bench_state（objdump 证实），
与 perf 共现一致。

**三重独立路径（opencsd 逐指令 + orbetto 独立解码 + Perfetto 栈）全部干净、互相印证。**
BB-OFF + cache + 150M 的函数级 trace 在修复解码器后**逐条对上真实调用链**——彻底坐实
proposal 38 的根因结论，也彻底推翻 r28/r29"cache 下 BB-OFF 必然漂移"的悲观判断。

**产物**：`~/workpath/orbcode/coremark_bb0_cache_150m_memfix.perf`；验证脚本
`syn/artix7/bringup/decode/verify_calls.py`。

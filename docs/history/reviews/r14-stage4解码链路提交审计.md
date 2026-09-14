# r14 · Stage-4 解码链路最近提交审计（漏洞核查）

> 审计范围：`57dec9f / 7a859d2 / 982489c / feae9ed / 2971fdc / 3adc344` 六个提交，核心是 `etm35lib.py` + 三份 stage4 文档（10/11/12）。
> 立场：红方。独立读了 `etm35lib.py` 全文、`etm_decode_cli.py` 的 addr2line 逻辑、三份文档。
> 目的：找这批"已走通"提交里的逻辑漏洞、被夸大的结论、和被默认成立的前提。

---

## 总评

这批提交的**方向修对了**——从"硬过 tpiu_demux 毁数据"消去法收敛到"FPGA 出 traceIF 流 + PC 端锚 I-sync"，且诚实标注了"锚点是真值、realign 流是指示性"。文档 10 的 §8 A/B 实测、文档 12 的"不做 formatter"判断都是有数据支撑的好结论。

但审计发现 **3 个真漏洞（其中 1 个会让 I-sync 锚点这个"铁证"本身漏报错误锚点）+ 4 个被夸大/未验证的前提**。最严重的是：**蓝方把"I-sync 落 flash 段"当成"锚点是 ground truth"，但 flash-range 检查根本挡不住误锚——而整个 V3/V4 的可信度全压在这个"铁证"上。**

---

## 🟥 真漏洞

### BUG-1〔致命〕I-sync 锚点"铁证"被高估——flash-range 不足以排除误锚，0x08 假头会混入

**位置**：`etm35lib.find_isyncs` + `parse_isync_at`。

**逻辑**：`find_isyncs` 扫全流，凡遇到字节 `0x08`（ISYNC_HEADER）、其后 info 字节 bit7=0、且后 4 字节组成的地址落在 `0x08000000..0x08100000`，就判定为一个 I-sync 锚点，并把它当 **ground truth 绝对 PC**（文档 11 "I-sync 锚点 = 铁证"）。

**漏洞**：这个判据**挡不住假阳性**，原因有三：
1. **`0x08` 是极常见字节**。在一条**未对齐/含噪声**的字节流里（蓝方自己反复强调 4-bit 口子字节错位），任意位置出现 `0x08` 的概率很高。
2. **flash-range 太宽**：`0x08000000..0x08100000` 是 1MB 区间 = 地址低 4 字节里高字节 `0x08`、其余 3 字节任意。一个随机 4 字节序列落进该区间的概率 ≈ 1/256（只要 addr[31:24]==0x08，外加 addr 在范围内）——**每 256 个随机 `0x08` 候选就有约 1 个"看起来像合法 flash I-sync"的纯噪声**。
3. **info 字节只查了 bit7**：`if info & 0x80: return None`。bits[6:5] reason、bit4 Jazelle、bit3 NS、bit2 AltISA **全没校验**。Cortex-M 不可能有 Jazelle（bit4 必 0）、AltISA（bit2 必 0）——**这两位是免费的强校验，蓝方没用**，等于放宽了假阳性。

**后果**：`recover_pcs` / `aligned_pcs` 返回的"distinct flash PCs"里**可能混入纯噪声地址**。这些噪声地址喂给 `addr2line`，**会解析出一个看似合理的函数名**（addr2line 对任意 flash 地址都会给最近符号），于是**一个误锚会冒充成"程序执行过这个函数"**。而文档把这套结果当"铁证/真函数集"——**铁证的地基有洞**。

**可证伪验证**：
- 拿一段**已知全是 idle / 已知 PC 范围很窄**的抓取跑 `recover_pcs`，看是否冒出该程序根本不会执行的地址（如中断向量区、未链接区段的函数）。
- 或把 `find_isyncs` 加上 **Jazelle=0 (bit4)、AltISA=0 (bit2) 必须为 0** 的校验，重跑，看锚点数是否下降——**若下降，说明之前有假阳性混入**。

**修法**：
1. info 字节加 `(info & 0x14) == 0`（bit4 Jazelle + bit2 AltISA 必 0）；
2. flash 上界用**实际 .text 段范围**（从 ELF 读，而非固定 1MB）；
3. 更强：对每个候选 I-sync 地址，要求 `addr2line` 能解析到**有源码行**（非 `??:0`），把"落在某函数"升级成"落在有调试信息的指令"。
4. 最强（蓝方文档 11 自己提到但没做）：**用后续 region 解码的自洽性反向确认锚点**——真锚点后面应能续出一段合法包流，纯噪声 `0x08` 后面大概率立刻 derail。

**判定**：🟥 致命。这不是"realign 流不可信"那个已知边界，而是**连"铁证锚点"本身都可能含假阳性**，且 V3 的全部可交付结论建立在它之上。

---

### BUG-2〔严重〕`traceif_assemble` 参考模型与 `traceIF.v` RTL 的同步语义不一致

**位置**：`etm35lib.traceif_assemble`，注释声称"mirrors the RTL closely enough"。

**对照 RTL**（`verilog/traceIF.v`，width==3 路径）：
- RTL 的 sync 检测是 `REsyncPacket = (construct[35 -: 32]==32'h7fff_ffff)` 与一个 `FEsyncPacket`（不同 bit 窗口），且有 `isREsync` 状态区分 RE/FE 对齐——**两种 sync 相位**。
- RTL 用 `remainingClocks`/`packetClocks`（width=3 时 =1）和 `elemCount` 管理 16-bit packet 的提取节奏，并把 8 个 16-bit packet 组装成 128-bit `cFrame`，`FrAvail` toggle。

**Python 模型**：
- 只检测 `top32 == 0x7FFFFFFF` **一种** sync（漏了 `FEsyncPacket` / `isREsync` 的 FE 相位）；
- 用 `rem` 在 0/1 间翻转近似 `remainingClocks`，但**没有 `elemCount`、没有 128-bit 帧组装**，直接逐 16-bit packet 输出字节。

**后果**：这是个**简化模型，不是 RTL 的忠实镜像**。注释"mirrors the RTL"**夸大**。它作为"回归交叉校验 RTL 字节序"的价值有限——因为它和 RTL 在 sync 相位、帧边界上行为不同。**若用它去"验证 RTL 正确"，可能两边都错却对上，或 RTL 对而模型报错。**

**判定**：🟥 严重（误导性，不是功能 bug——但它被列为"cross-check RTL"的依据，名不副实）。要么把模型补成真镜像（含 FEsync + elemCount + 128bit 帧），要么把注释改成"简化近似，不用于 RTL 正确性背书"。

---

### BUG-3〔严重〕`decode_region` 的 branch-packet 长度解析跨过了流尾/误吞

**位置**：`etm35lib.decode_region`（和 `decode_region_realign` 同款逻辑）branch 分支：
```python
if c & 1:
    n = 1
    while i + n < end and (data[i + n - 1] & 0x80) and n < 5:
        n += 1
    events.append(FlowEvent("branch", base_addr))
    i += n
```

**问题 1（continuation 判据起点错位）**：ETM branch packet 的 continuation 规则是"**当前字节 bit7=1 表示还有下一字节**"。这里循环条件查的是 `data[i+n-1] & 0x80`——当 n=1 时查 `data[i]`（branch 头字节本身），**对**；但它用 `data[i+n-1]`（前一字节）决定要不要**再读一字节**，语义是"前一字节 bit7 set 就继续"。branch 头字节 `c & 1 == 1`（bit0=1 是 branch 地址字节标志），其 bit7 是地址 continuation——**逻辑大体对**，但：

**问题 2（base_addr 永远不更新）**：branch packet **携带跳转目标地址**，但这里 `FlowEvent("branch", base_addr)` 用的是**旧的 base_addr**，且解析完 branch **没有从 packet 里解出新地址去更新 base_addr**。也就是说——**branch packet 的地址载荷被整个丢弃了**，只记了"这里有个 branch"。

**后果**：`decode_region` 声称"walk P-headers + branch packets to extend the recovered trace"，但 branch 的**目标地址根本没被解码**。文档 11 说 realign 后 "branches 19→39"——这些 branch 事件**不含目标 PC**，对"还原函数执行链路"的价值远低于文档暗示的。FlowEvent 里 branch 的 `addr` 字段是误导的（它是 base_addr 不是 branch target）。

**判定**：🟥 严重。branch 解码是半成品——只数了个数，没解地址。这直接削弱"还原执行流"的真实成色（真正能定位的还是 I-sync 锚点，branch/atoms 只是计数）。文档应明说"branch 仅计数、未解目标地址"。

---

## 🟡 被夸大 / 未验证的前提

### GAP-1〔文档 12 vs 文档 10 自相矛盾，未交代清楚〕
- **文档 10 §6**（提交 3adc344）：老抓取 `trace_etm.bin` 喂 TPIUSync **装出 820 帧**，据此一度判"formatter 帧确实存在、推翻裸 ETM 判断"。
- **文档 12 §2**（提交 57dec9f）：新干净抓取，TPIUSync 装出 **0 帧**，结论"STM32 没在发 formatter 帧，820 帧是巧合"。

两个提交结论**完全相反**，文档 12 §2.3 用一句"820 是巧合"带过。**但 820 个 16 字节对齐帧"恰好巧合"的概率极低**——更可能是两份抓取的**采样配置/nibble 序/数据内容不同**导致。蓝方没有解释清楚"为什么老数据能装 820 帧、新数据 0 帧"的真正机制，就跳到"不做 formatter"。**这个矛盾没闭合，"不做 formatter"的结论地基不稳。** 建议：用同一份新抓取，明确给出"820 帧那次的 7FFFFFFF 来源"（是真 sync 还是 idle 残影的统计），别用"巧合"二字收尾。

### GAP-2〔realign 评分函数可被噪声满足，文档已诚实但程度可能更糟〕
`_classifiable_run` 用"连续 ≥3 个可分类包头"给 bit-shift 打分。但 `_classify` 把**大量常见字节**都判成合法包头（bit0=1 全是 branch、`(c&0x81)==0x80` 全是 pheader）——**随机字节落进"可分类"的概率很高**。所以"realign 选最长合法序列"很容易**被噪声相位满足**，选出一个看似合法实则错误的对齐。文档 11 已诚实标注"realign 流仅指示性"，**但可能比文档说的还不可信**——`min_run=3` 太低，3 个随机字节里 2 个落进 branch/pheader 类的概率不小。建议把 min_run 提到 ≥6 并要求 run 内**至少出现一个 flash I-sync** 才采信该 realign。

### GAP-3〔addr2line 解析成功 ≠ PC 真实执行过〕
`etm_decode_cli.resolve` 对每个 PC 跑 addr2line。**addr2line 对任意 flash 地址都会返回最近符号**（除非完全越界）。所以"解出函数名"**不证明该地址是真 PC**——它和 BUG-1 叠加：噪声锚点 → addr2line 给个函数名 → 看起来"程序执行了这个函数"。**没有任何一步在验证"这个 PC 是被测程序的合法指令边界"。** 至少应校验 PC 是 2 字节对齐（Thumb）、且 addr2line 返回非 `??`。

### GAP-4〔V4 "3x 提升"的指标具有误导性〕
文档 11 报"capDIV16 plain 78 → realign 254 events"。但其中**新增的 176 个 events 是 atoms/branch（BUG-3 证明 branch 连地址都没解），不是新 I-sync 锚点**（文档自己说"extra in-region I-sync = 0"）。所以"3.3×"是**计数膨胀，不是可信信息量增长**——而且分母里的 atoms 计数依赖 realign 相位正确（GAP-2 存疑）。"延长流 3 倍"这个数字对外汇报时极易被误读成"解码能力提升 3 倍"。建议：对外只报 **I-sync 锚点数**（真值指标），realign events 作为内部诊断量，不作能力指标。

---

## 值得肯定（不是只挑刺）

- **消去法收敛是对的**：文档 10 §8 用 A/B 上板实测坐实"过 tpiu_demux 就归零、不过 demux 稳定 14-15 锚点"，这是扎实的实验，且把路线从"硬串 orbtrace 管线"修正到"traceIF 流 + PC 锚 I-sync"，方向正确。
- **bypass 语义对标**：认识到这对应 orbtrace core.py 的 `input_bypass`（SWO 路径），是正确的架构理解。
- **诚实标注信任边界**：文档 11/12 主动区分"锚点真值 vs realign 指示性"、CLI 默认不开 --realign——这种自我设限的诚实在前几轮就该有，这轮做到了。
- **TPIUSync 导出 + 单测**：补上 reuse 缺口的方向对。

---

## 行动项（按优先级）

1. **BUG-1（致命）**：`parse_isync_at` 加 `(info & 0x14)==0`（Jazelle/AltISA 必 0）+ flash 上界用 ELF .text 实际范围 + 候选锚点要求 addr2line 命中有源码行。重跑所有 fixture，看锚点数变化——**下降即说明之前有假阳性**。这一步做完才能说"锚点是铁证"。
2. **BUG-3（严重）**：branch packet 要么真解目标地址（更新 base_addr），要么文档/字段明说"branch 仅计数、addr 字段无效"。别让 FlowEvent.addr 在 branch 上是误导值。
3. **GAP-1**：闭合"820 帧 vs 0 帧"矛盾，给出机制解释，别用"巧合"。这关系到"不做 formatter"结论是否站得住。
4. **BUG-2**：`traceif_assemble` 注释降级为"简化近似"，或补成 RTL 忠实镜像。
5. **GAP-4**：对外能力指标只用 I-sync 锚点数，realign 计数标为诊断量。

---

## 一句话结论

**这批提交方向对（消去法把路线收敛到"traceIF 流 + PC 锚 I-sync"，且诚实标注了 realign 流只是指示性），但审计出 3 个真漏洞：最致命的是 I-sync 锚点这个"铁证"其实挡不住假阳性——`0x08` 头只查了 bit7、flash-range 宽达 1MB、Jazelle/AltISA 这两位免费强校验没用，纯噪声每 ~256 个 `0x08` 就可能伪造一个"合法 flash 锚点"，再被 addr2line 自动配上函数名，冒充成"执行过该函数"；其次 branch packet 只数个数、目标地址根本没解（FlowEvent.addr 是误导值），所以"还原执行链路 3×提升"是计数膨胀不是信息量增长；另外 traceif_assemble 自称镜像 RTL 实为简化模型、且文档 10/12 对"820 帧 vs 0 帧"用'巧合'草草收尾未闭合矛盾。先把 I-sync 校验收紧（加 Jazelle/AltISA=0 + ELF 实际段范围 + addr2line 命中源码行），重跑看锚点数掉不掉——掉了就证明之前的'铁证'里混着噪声。**

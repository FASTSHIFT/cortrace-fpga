# Stage-4 · FPGA formatter 决策:先定性 STM32 是否在发 formatter 帧

> 用户问:UDP 问题大吗?不大就搞 FPGA formatter。本文先回答 UDP,再用**新抓的干净原始 nibble 流**定性 STM32 到底发不发 TPIU formatter 帧——这是决定要不要/怎么做 FPGA formatter 的前提。

---

## 1. UDP 问题:不大(可以放心做别的)

当前是 **one-shot BRAM + 请求-应答分页读**,不是连续推流:
- 数据先冻结在 BRAM,PC 主动发请求读每页,**幂等**:丢包重读同一 base 即可拿到一样数据。
- 实测十几次 60KB 抓取 `full=1`、字节数对、解出真函数,本地千兆 UDP 丢包率近乎 0。
- **UDP seq/重传只有"连续推流"模式才需要**(数据流过不可重读),当前架构用不上。

→ UDP 不是瓶颈,可以投精力到 formatter 方向。

## 2. 定性实验:STM32 发不发 formatter 帧?(新抓干净数据)

给 `trace_stream_top` 加了 `CAP_RAW` 参数,综合出**抓原始 nibble**(`{trace_b,trace_a}` per trace_clk,traceIF 之前)的 bitstream,上板抓 60KB 新数据,用**已单测验证的 orbtrace TPIUSync 模型**判定:

| 判据 | as-captured | nibble-swap |
|------|------------|-------------|
| TPIUSync 装出的帧 | **0** | **0** |
| TPIU 全同步 `7FFFFFFF` | ~0 | ~0 |
| ETM A-sync `000000000080` | 0 | **6** |
| 0xff 占比 | 4% | 4% |

**结论(定性,新数据):**
1. **STM32 没在发 TPIU formatter 帧** —— 两种 nibble 序都装不出 formatter 帧(0),也没有周期性 `7FFFFFFF` 全同步、没有 formatter 半同步 idle 洪流(0xff 仅 4%)。
2. **流是裸 ETM** —— swap 序有 6 个 ETM A-sync,as-captured 序 0 个 → **swap 序(`{a<<4|b}`)才是正确的 ETM 字节序**(和之前 reuse 审计的发现一致)。
3. 之前老抓取 `trace_etm.bin` 那 820 帧是**那份特定数据的巧合**(`7FFFFFFF` 在 idle/数据里偶然出现),不是真 formatter sync——新干净抓取证伪了"formatter 在发帧"。

## 3. 这对 FPGA formatter 意味着什么(修正方向)

**不能"照搬"一个 formatter 去解已有帧——因为根本没有 formatter 帧。** 两条真实可选路:

### 路 A(推荐):FPGA 侧自己加 TPIU formatter,把裸 ETM 重新封装成对齐帧
- 像 ORBTrace 那样,FPGA 把裸 ETM 字节流**主动**打成 16 字节 TPIU formatter 帧(插 `7FFFFFFF` 全同步 + channel ID),输出天然字节对齐。
- 但**关键认知**:FPGA 收到的裸 ETM 本身若有 4-bit DDR 子字节错位,FPGA formatter 也只是把错位的字节重新打包——**formatter 解决的是"多源复用 + 帧同步",不解决"采样字节错位"**。所以单加 formatter 不够。
- 真正要对齐的是**采样字节边界**:确认 trace_a/trace_b nibble 序(已知 swap 对)+ DDR 相位(IDELAY tap)正确,让 FPGA 吐出的字节就是 ETM 包的正确字节序。这个对了,PC 端 `etm35lib` 直接锚 I-sync 就够,根本不需要 formatter。

### 路 B(已走通):FPGA 出 traceIF 字节流 + PC 端锚 I-sync
- 这是 V3 已验证的路:trace_stream(traceIF 帧)→ PC `etm35lib` 锚 I-sync → 真函数。
- 它**绕过了 formatter 和 demux 的需求**,因为 I-sync 自带绝对 PC,不需要 channel 解复用。

## 4. 修正后的结论

- **不做 FPGA formatter**(对单源裸 ETM 是多余的——没有多路要复用,加 formatter 只增复杂度不解决字节对齐)。
- **真正该做的是把 FPGA 采样字节序固定对**:`trace_stream_top` 加了 `SWAP_NIBBLES`/正确 nibble 序后,FPGA 吐的 traceIF 帧字节序就对,PC 端 `etm35lib` 锚 I-sync(已 100% 测试)。
- 下一步实验:用 **swap 序固定**的 trace_stream 抓繁忙负载,看 I-sync 锚点数 + region 连续度是否比之前(nibble 序未固定)显著提升。这是把"指示性流"往"可信连续流"推的正路,且不需要 formatter。

> 一句话:UDP 不是问题;但新干净数据证伪了"STM32 在发 formatter 帧",所以 FPGA formatter 对我们这单源裸 ETM 是多余的——该做的是**固定正确的采样字节序(nibble swap)**,让 FPGA 出的字节就是对齐的 ETM,PC 端锚 I-sync 即可。


---

## 5. GAP-1 闭合(回应 r14):820 帧 vs 0 帧的机制(非"巧合")

r14 指出 §2.3 用"巧合"草草收尾。查清机制:**TPIUSync 是"锁一次 `0x7FFFFFFF` 后自由跑、每 16 字节吐一帧"**。
- 老 `trace_etm.bin`:有 30 个 `0xFFFFFF7F`(idle/数据里偶发),第一个一锁,后续 16KB 全程每 16 字节硬吐 → **820 帧,内容多为 idle/垃圾,非有效 formatter 帧**。
- 新 `raw_nibbles.bin`:**0 个 `0xFFFFFF7F` → 从不锁 → 0 帧**。

**所以"帧数"不是 formatter 存在与否的指标**(单次偶发锁 + 自由跑就能造几百帧)。正确指标是**周期性 `0xFFFFFF7F` 全同步**——两份都没有。"不发 formatter 帧"的结论不变,但地基从"巧合"换成"TPIUSync free-run 语义 + 无周期全同步"。

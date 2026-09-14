# Stage-4 · V4:子字节重对齐延长 region(连续流增强)

> 目标:把 V3 的"I-sync 锚点 + 短 region"往"连续指令流"推进。依据 IHI0014Q §7.10.4——4-bit 口非字节对齐,一次采样错位后续字节整体偏移,解码器需在错位处重对齐。

---

## 做法

`etm35lib.decode_region_realign`:从 I-sync 锚点正常走 P-header/branch/已知包;**一旦撞到无法分类的字节(misalignment 征兆),试 1..7 bit 移位,选能续出最长合法包序列的相位**继续解。`decode_all_realign` 对每个锚点这样跑。

新增完整覆盖的辅助:`_classify`(ETM3.5 IDLE 全部包头分类)、`_classifiable_run`(给候选 bit 移位打分)。

## 实测效果(真实抓取)

| 抓取 | plain events | realign events | realigns |
|------|-------------|----------------|----------|
| capGND | 19 | 47 (+28) | 6 |
| capDIV16 | 78 | **254 (+176)** | 31 |
| trace_e2e | 42 | 74 (+32) | 11 |

`etm_decode_cli.py --realign`:trace_e2e 的 exec-atoms 109→186、branches 19→39。

## 诚实的信任边界(重要)

- **I-sync 锚点 = 铁证**:每个携带 4 字节绝对 PC(规范 Fig 7-42),落 flash 段,可 addr2line 验证 → 这是 ground truth。
- **重对齐后的 atoms/branch = 仅供参考**:它们通过了"在重对齐相位下是合法 ETM3.5 包头"的检验,但**没有独立佐证**。实测重对齐 region 里**没有再surface 出额外的 flash 地址 I-sync 锚点**(extra in-region I-sync = 0),所以无法证明这些延长出来的事件逐个正确——可能含错位噪声。
- 因此 `decode_all_realign` 文档串明确标注:**锚点当真值,重对齐流当指示性**。CLI 默认不开 `--realign`,需要显式开启。

## 结论

- V3 的可交付物(锚点 → 真实函数集)不变,仍是可信结果。
- V4 重对齐**显著增加了解码事件量**(最多 3.3×),作为"指示性执行流"有价值,但**不声称逐指令正确**。
- 要让延长流也变铁证,需要的不是更多软件 trick,而是**消除 4-bit 口的字节错位本身**——即 FPGA 侧做真正的 TPIU formatter 重封装(像 ORBTrace),让流天然字节对齐。那是后续硬件方向,不是 PC 端能根治的。

> 一句话:V4 用子字节重对齐把短 region 延长了 3 倍上下(纯 PC、有测试),但诚实标注"锚点是真值、延长流是指示性"。根治字节对齐要回到 FPGA 侧上 formatter。
